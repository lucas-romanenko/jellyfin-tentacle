using System.Linq;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Tasks;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Catches items deleted through Jellyfin's native web UI and notifies
/// the Tentacle backend so it can clean up DB records and playlists.
/// </summary>
public class LibraryDeleteHandler : IHostedService, IDisposable
{
    private readonly ILibraryManager _libraryManager;
    private readonly ITaskManager _taskManager;
    private readonly ILogger<LibraryDeleteHandler> _logger;
    private readonly HttpClient _httpClient;
    private Timer? _debounceTimer;
    private readonly object _lock = new();
    private readonly List<(string mediaType, string tmdbId)> _pendingDeletes = new();

    /// <summary>Upper bound on in-flight DELETE notifications to the backend.</summary>
    private const int MaxConcurrentNotifications = 4;

    // One gate for the handler, not one per batch: a second batch starting while
    // the first is still draining (a slow backend, a long bulk delete) must share
    // the same four slots rather than bring four more.
    private readonly SemaphoreSlim _gate = new SemaphoreSlim(MaxConcurrentNotifications, MaxConcurrentNotifications);

    // Batches still running, so StopAsync can wait for them before Dispose()
    // takes the HttpClient away from under them.
    private readonly HashSet<Task> _runningBatches = new();

    public LibraryDeleteHandler(
        ILibraryManager libraryManager,
        ITaskManager taskManager,
        ILogger<LibraryDeleteHandler> logger)
    {
        _libraryManager = libraryManager;
        _taskManager = taskManager;
        _logger = logger;
        _httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
    }

    public Task StartAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemRemoved += OnItemRemoved;
        _logger.LogInformation("[Tentacle] LibraryDeleteHandler started — listening for item deletions");
        return Task.CompletedTask;
    }

    public async Task StopAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemRemoved -= OnItemRemoved;
        _debounceTimer?.Change(Timeout.Infinite, 0);
        ProcessPendingDeletes();

        Task[] running;
        lock (_lock)
        {
            running = _runningBatches.ToArray();
        }

        try
        {
            // Bounded by the host's shutdown token; each request is bounded by the
            // client's own timeout.
            await Task.WhenAll(running).WaitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            _logger.LogWarning("[Tentacle] Shutdown did not wait for {Count} deletion batch(es) to finish", running.Length);
        }

        _logger.LogInformation("[Tentacle] LibraryDeleteHandler stopped");
    }

    private void OnItemRemoved(object? sender, ItemChangeEventArgs e)
    {
        var item = e.Item;
        if (item == null) return;

        // Only handle Movie and Series types
        string? mediaType = null;
        if (item is Movie) mediaType = "movie";
        else if (item is MediaBrowser.Controller.Entities.TV.Series) mediaType = "series";
        else return;

        // Ignore virtual/placeholder items — these are never real on-disk deletions.
        if (item.IsVirtualItem)
        {
            return;
        }

        // Only act on genuine user deletions. During a library scan/refresh Jellyfin
        // removes and re-resolves items, firing ItemRemoved while the underlying files
        // still exist on disk. A real delete removes the files, so if the path still
        // exists we treat it as scan churn and skip it to avoid wiping DB records and
        // playlists for content that is still present.
        var path = item.Path;
        if (!string.IsNullOrEmpty(path) && (File.Exists(path) || Directory.Exists(path)))
        {
            _logger.LogDebug("[Tentacle] {Type} '{Name}' removed but path still exists ({Path}) — treating as scan churn, skipping",
                mediaType, item.Name, path);
            return;
        }

        // A library scan removes every item whose file it cannot see — a provider
        // sync that rewrote .strm files, an unreadable mount, a pool branch that
        // dropped out. Those are not user deletions, and the path check above
        // cannot tell them apart, because the file really is gone. While a scan
        // is running, leave the catalogue alone; a deletion made in the UI during
        // a scan is reconciled by the backend's nightly orphan sweep (downloaded
        // rows; a VOD row simply comes back through the next sync's .strm repair).
        if (IsLibraryScanRunning())
        {
            _logger.LogInformation("[Tentacle] {Type} '{Name}' removed during a library scan — not forwarding to the backend",
                mediaType, item.Name);
            return;
        }

        // Extract TMDB provider ID
        if (!item.ProviderIds.TryGetValue("Tmdb", out var tmdbId) || string.IsNullOrEmpty(tmdbId))
        {
            _logger.LogDebug("[Tentacle] Deleted {Type} '{Name}' has no TMDB ID — skipping", mediaType, item.Name);
            return;
        }

        // Debug, not Info: a bulk removal produced one Info line per item.
        _logger.LogDebug("[Tentacle] Detected deletion: {Type} '{Name}' (TMDB:{TmdbId})", mediaType, item.Name, tmdbId);

        lock (_lock)
        {
            _pendingDeletes.Add((mediaType, tmdbId));

            // Debounce 2 seconds to batch rapid deletions
            _debounceTimer?.Dispose();
            _debounceTimer = new Timer(_ => ProcessPendingDeletes(), null, TimeSpan.FromSeconds(2), Timeout.InfiniteTimeSpan);
        }
    }

    /// <summary>
    /// True while Jellyfin's library scan / refresh task is running.
    /// </summary>
    private bool IsLibraryScanRunning()
    {
        try
        {
            foreach (var task in _taskManager.ScheduledTasks)
            {
                if (task.State != TaskState.Running)
                {
                    continue;
                }

                var key = task.ScheduledTask?.Key;
                if (string.Equals(key, "RefreshLibrary", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "[Tentacle] Could not read scheduled task state");
        }

        return false;
    }

    private void ProcessPendingDeletes()
    {
        List<(string mediaType, string tmdbId)> batch;
        lock (_lock)
        {
            if (_pendingDeletes.Count == 0) return;
            batch = new List<(string, string)>(_pendingDeletes);
            _pendingDeletes.Clear();
        }

        var tentacleUrl = Plugin.Instance?.Configuration?.TentacleUrl;
        if (string.IsNullOrEmpty(tentacleUrl))
        {
            _logger.LogWarning("[Tentacle] TentacleUrl not configured — cannot notify backend of deletions");
            return;
        }

        // One task per deleted item used to be launched at once. A bulk removal
        // (or a library scan that re-resolves thousands of items) fired thousands of
        // simultaneous DELETEs at the backend; they all queued behind the connection
        // limit, all hit the 10 s client timeout, and each logged an ERROR with a full
        // stack trace — tens of thousands of log lines from a single event.
        var run = Task.Run(() => ProcessBatchAsync(batch, tentacleUrl));
        lock (_lock)
        {
            _runningBatches.Add(run);
        }

        _ = run.ContinueWith(
            t =>
            {
                lock (_lock)
                {
                    _runningBatches.Remove(t);
                }
            },
            TaskScheduler.Default);
    }

    private async Task ProcessBatchAsync(List<(string mediaType, string tmdbId)> batch, string tentacleUrl)
    {
        var gate = _gate;
        var failures = 0;
        Exception? firstFailure = null;
        var succeeded = 0;

        var tasks = batch.Select(async entry =>
        {
            await gate.WaitAsync().ConfigureAwait(false);
            try
            {
                var url = $"{tentacleUrl.TrimEnd('/')}/api/library/item/{entry.mediaType}/{entry.tmdbId}";
                var response = await _httpClient.DeleteAsync(url).ConfigureAwait(false);

                if (response.IsSuccessStatusCode)
                {
                    Interlocked.Increment(ref succeeded);
                    _logger.LogDebug("[Tentacle] Notified backend of deletion: {Type} TMDB:{TmdbId}", entry.mediaType, entry.tmdbId);
                }
                else
                {
                    Interlocked.Increment(ref failures);
                    _logger.LogDebug("[Tentacle] Backend returned {Status} for deletion: {Type} TMDB:{TmdbId}",
                        (int)response.StatusCode, entry.mediaType, entry.tmdbId);
                }
            }
            catch (Exception ex)
            {
                Interlocked.Increment(ref failures);
                Interlocked.CompareExchange(ref firstFailure, ex, null);
                _logger.LogDebug(ex, "[Tentacle] Failed to notify backend of deletion: {Type} TMDB:{TmdbId}", entry.mediaType, entry.tmdbId);
            }
            finally
            {
                gate.Release();
            }
        });

        await Task.WhenAll(tasks).ConfigureAwait(false);

        // One summary line per batch instead of one (or two) per item.
        if (failures > 0)
        {
            _logger.LogWarning(
                firstFailure,
                "[Tentacle] Notified backend of {Succeeded}/{Total} deletions; {Failed} failed (first failure shown)",
                succeeded, batch.Count, failures);
        }
        else if (succeeded > 0)
        {
            _logger.LogInformation("[Tentacle] Notified backend of {Succeeded} deletion(s)", succeeded);
        }
    }

    public void Dispose()
    {
        _debounceTimer?.Dispose();
        _httpClient.Dispose();
        _gate.Dispose();
    }
}

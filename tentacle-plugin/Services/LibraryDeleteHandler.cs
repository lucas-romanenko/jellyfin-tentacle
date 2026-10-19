using System.Linq;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Providers;
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
    private readonly IProviderManager _providerManager;
    private readonly ILogger<LibraryDeleteHandler> _logger;
    private readonly HttpClient _httpClient;
    private Timer? _debounceTimer;
    private readonly object _lock = new();
    // The deleted item's own id and path go with its TMDB id: the same title can
    // be in Jellyfin twice (a VOD .strm and a download), and the backend must act
    // on the copy that was deleted, never on "the first item with this TMDB id".
    private readonly List<(string mediaType, string tmdbId, string itemId, string path)> _pendingDeletes = new();

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
        IProviderManager providerManager,
        ILogger<LibraryDeleteHandler> logger)
    {
        _libraryManager = libraryManager;
        _taskManager = taskManager;
        _providerManager = providerManager;
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

        // Extract TMDB provider ID
        if (!item.ProviderIds.TryGetValue("Tmdb", out var tmdbId) || string.IsNullOrEmpty(tmdbId))
        {
            _logger.LogDebug("[Tentacle] Deleted {Type} '{Name}' has no TMDB ID — skipping", mediaType, item.Name);
            return;
        }

        // A library re-read removes every item whose file it cannot see — a provider
        // sync that rewrote .strm files, an unreadable mount, a pool branch that
        // dropped out. Those are not user deletions, and the path check above
        // cannot tell them apart, because the file really is gone. While a re-read
        // is running, leave the catalogue alone; a deletion made in the UI during
        // one is reconciled by the backend's nightly orphan sweep (downloaded
        // rows; a VOD row simply comes back through the next sync's .strm repair).
        if (IsLibraryScanRunning() || IsParentBeingValidated(e.Parent))
        {
            _logger.LogInformation("[Tentacle] {Type} '{Name}' removed during a library scan — not forwarding to the backend",
                mediaType, item.Name);
            return;
        }

        // Debug, not Info: a bulk removal produced one Info line per item.
        _logger.LogDebug("[Tentacle] Detected deletion: {Type} '{Name}' (TMDB:{TmdbId})", mediaType, item.Name, tmdbId);

        // The backend asks this server to confirm the deletion before it acts on
        // the notification (#139); see RecentDeletions.
        RecentDeletions.Record(mediaType, tmdbId);

        lock (_lock)
        {
            _pendingDeletes.Add((mediaType, tmdbId, item.Id.ToString("N"), path ?? string.Empty));

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

    /// <summary>
    /// True while Jellyfin is validating the folder the item was removed from.
    /// The Scan Media Library task is only one way in: the library monitor
    /// (real-time monitoring, /Library/Media/Updated from Radarr/Sonarr) and a
    /// one-library "Scan library" (/Items/{id}/Refresh) validate folders with the
    /// task idle. Folder.ValidateChildrenInternal registers the folder with
    /// OnRefreshStart for as long as it runs and passes it as the removal's
    /// parent. A delete from the UI passes the item's own parent too, which is
    /// held back only if a re-read of that folder runs at that moment (#448).
    /// </summary>
    private bool IsParentBeingValidated(BaseItem? parent)
    {
        if (parent == null)
        {
            return false;
        }

        try
        {
            return _providerManager.GetRefreshProgress(parent.Id).HasValue;
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "[Tentacle] Could not read refresh state of {Parent}", parent.Id);
            return false;
        }
    }

    private void ProcessPendingDeletes()
    {
        List<(string mediaType, string tmdbId, string itemId, string path)> batch;
        lock (_lock)
        {
            if (_pendingDeletes.Count == 0) return;
            batch = new List<(string, string, string, string)>(_pendingDeletes);
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

    private async Task ProcessBatchAsync(List<(string mediaType, string tmdbId, string itemId, string path)> batch, string tentacleUrl)
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
                var url = $"{tentacleUrl.TrimEnd('/')}/api/library/item/{entry.mediaType}/{entry.tmdbId}"
                    + $"?item_id={Uri.EscapeDataString(entry.itemId)}&path={Uri.EscapeDataString(entry.path)}";
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

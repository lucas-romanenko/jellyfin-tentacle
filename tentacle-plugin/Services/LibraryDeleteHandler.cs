using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities.TV;
using MediaBrowser.Controller.Library;
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
    private readonly ILogger<LibraryDeleteHandler> _logger;
    private readonly HttpClient _httpClient;
    private Timer? _debounceTimer;
    private readonly object _lock = new();
    private readonly List<(string mediaType, string tmdbId)> _pendingDeletes = new();

    public LibraryDeleteHandler(
        ILibraryManager libraryManager,
        ILogger<LibraryDeleteHandler> logger)
    {
        _libraryManager = libraryManager;
        _logger = logger;
        _httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
    }

    public Task StartAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemRemoved += OnItemRemoved;
        _logger.LogInformation("[Tentacle] LibraryDeleteHandler started — listening for item deletions");
        return Task.CompletedTask;
    }

    public Task StopAsync(CancellationToken cancellationToken)
    {
        _libraryManager.ItemRemoved -= OnItemRemoved;
        _debounceTimer?.Change(Timeout.Infinite, 0);
        ProcessPendingDeletes();
        _logger.LogInformation("[Tentacle] LibraryDeleteHandler stopped");
        return Task.CompletedTask;
    }

    private void OnItemRemoved(object? sender, ItemChangeEventArgs e)
    {
        var item = e.Item;

        // Only handle Movie and Series types
        string? mediaType = null;
        if (item is Movie) mediaType = "movie";
        else if (item is MediaBrowser.Controller.Entities.TV.Series) mediaType = "series";
        else return;

        // Extract TMDB provider ID
        if (!item.ProviderIds.TryGetValue("Tmdb", out var tmdbId) || string.IsNullOrEmpty(tmdbId))
        {
            _logger.LogDebug("[Tentacle] Deleted {Type} '{Name}' has no TMDB ID — skipping", mediaType, item.Name);
            return;
        }

        _logger.LogInformation("[Tentacle] Detected deletion: {Type} '{Name}' (TMDB:{TmdbId})", mediaType, item.Name, tmdbId);

        lock (_lock)
        {
            _pendingDeletes.Add((mediaType, tmdbId));

            // Debounce 2 seconds to batch rapid deletions
            _debounceTimer?.Dispose();
            _debounceTimer = new Timer(_ => ProcessPendingDeletes(), null, TimeSpan.FromSeconds(2), Timeout.InfiniteTimeSpan);
        }
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

        foreach (var (mediaType, tmdbId) in batch)
        {
            _ = Task.Run(async () =>
            {
                try
                {
                    var url = $"{tentacleUrl.TrimEnd('/')}/api/library/item/{mediaType}/{tmdbId}";
                    var response = await _httpClient.DeleteAsync(url).ConfigureAwait(false);

                    if (response.IsSuccessStatusCode)
                    {
                        _logger.LogInformation("[Tentacle] Notified backend of deletion: {Type} TMDB:{TmdbId}", mediaType, tmdbId);
                    }
                    else
                    {
                        _logger.LogWarning("[Tentacle] Backend returned {Status} for deletion: {Type} TMDB:{TmdbId}",
                            (int)response.StatusCode, mediaType, tmdbId);
                    }
                }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "[Tentacle] Failed to notify backend of deletion: {Type} TMDB:{TmdbId}", mediaType, tmdbId);
                }
            });
        }
    }

    public void Dispose()
    {
        _debounceTimer?.Dispose();
        _httpClient.Dispose();
    }
}

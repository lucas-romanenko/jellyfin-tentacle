using Jellyfin.Plugin.Tentacle.Playlists;
using MediaBrowser.Model.Tasks;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Tasks;

/// <summary>
/// Scheduled task that refreshes all SmartList playlists.
/// Runs on a configurable interval and on library changes.
/// </summary>
public class PlaylistRefreshTask : IScheduledTask
{
    private readonly PlaylistManager _playlistManager;
    private readonly ILogger<PlaylistRefreshTask> _logger;

    public PlaylistRefreshTask(
        PlaylistManager playlistManager,
        ILogger<PlaylistRefreshTask> logger)
    {
        _playlistManager = playlistManager;
        _logger = logger;
    }

    public string Name => "Tentacle Playlist Refresh";

    public string Key => "TentaclePlaylistRefresh";

    public string Description => "Refreshes all Tentacle SmartList playlists by querying the Jellyfin library and updating playlist contents.";

    public string Category => "Tentacle";

    public async Task ExecuteAsync(IProgress<double> progress, CancellationToken cancellationToken)
    {
        _logger.LogInformation("Tentacle playlist refresh task started");
        progress.Report(0);

        try
        {
            var count = await _playlistManager.RefreshAllPlaylists(cancellationToken);
            _logger.LogInformation("Tentacle playlist refresh completed: {Count} playlists processed", count);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Tentacle playlist refresh task failed");
        }

        progress.Report(100);
    }

    public IEnumerable<TaskTriggerInfo> GetDefaultTriggers()
    {
        return new[]
        {
            // Run every 6 hours
            new TaskTriggerInfo
            {
                Type = TaskTriggerInfoType.IntervalTrigger,
                IntervalTicks = TimeSpan.FromHours(6).Ticks,
            },
        };
    }
}

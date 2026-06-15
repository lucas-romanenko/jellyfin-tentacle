using Jellyfin.Plugin.Tentacle.Playlists;
using MediaBrowser.Model.Tasks;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Tasks;

/// <summary>
/// Legacy scheduled task that rebuilt SmartList playlists from on-disk config files.
/// Disabled: playlists are now managed per-user by the Tentacle backend via the Jellyfin
/// API (IsPublic=false). This task created admin-owned playlists that conflicted with
/// the backend-driven ones, so it has no default triggers and is a no-op when run
/// manually. Kept (with the manual entry point) only so existing scheduled-task state
/// degrades gracefully.
/// </summary>
public class PlaylistRefreshTask : IScheduledTask
{
    private readonly ILogger<PlaylistRefreshTask> _logger;

    public PlaylistRefreshTask(ILogger<PlaylistRefreshTask> logger)
    {
        _logger = logger;
    }

    public string Name => "Tentacle Playlist Refresh (disabled)";

    public string Key => "TentaclePlaylistRefresh";

    public string Description => "Legacy task — playlists are managed by the Tentacle backend. This task does nothing.";

    public string Category => "Tentacle";

    public Task ExecuteAsync(IProgress<double> progress, CancellationToken cancellationToken)
    {
        _logger.LogInformation("Tentacle playlist refresh task is disabled — playlists are managed by the Tentacle backend. No action taken.");
        progress.Report(100);
        return Task.CompletedTask;
    }

    public IEnumerable<TaskTriggerInfo> GetDefaultTriggers()
    {
        // No automatic triggers — the legacy disk-based rebuild conflicts with the
        // backend-driven playlist management.
        return Array.Empty<TaskTriggerInfo>();
    }
}

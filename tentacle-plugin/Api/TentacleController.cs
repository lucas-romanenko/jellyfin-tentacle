using Jellyfin.Plugin.Tentacle.HomeScreen;
using Jellyfin.Plugin.Tentacle.Playlists;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// API controller for Tentacle plugin.
/// POST /Tentacle/Refresh is the main webhook — triggers the full pipeline:
///   1. Refresh all SmartList playlists
///   2. Clear home config cache
///   3. Clear item cache
/// </summary>
[ApiController]
[Route("[controller]")]
public class TentacleController : ControllerBase
{
    private readonly HomeScreenManager _homeScreenManager;
    private readonly PlaylistManager _playlistManager;
    private readonly ILogger<TentacleController> _logger;

    public TentacleController(
        HomeScreenManager homeScreenManager,
        PlaylistManager playlistManager,
        ILogger<TentacleController> logger)
    {
        _homeScreenManager = homeScreenManager;
        _playlistManager = playlistManager;
        _logger = logger;
    }

    /// <summary>
    /// Full refresh: refreshes playlists, clears caches.
    /// Called by Tentacle server after every sync.
    /// Requires Jellyfin API key auth (X-Emby-Token header).
    /// </summary>
    [HttpPost("Refresh")]
    [Authorize]
    public async Task<ActionResult> Refresh()
    {
        _logger.LogInformation("Tentacle refresh triggered — full pipeline starting");

        // Step 1: Refresh all SmartList playlists
        int playlistCount = 0;
        try
        {
            playlistCount = await _playlistManager.RefreshAllPlaylists();
            _logger.LogInformation("Refreshed {Count} playlists", playlistCount);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Playlist refresh failed");
        }

        // Step 2: Clear home config cache
        _homeScreenManager.ClearCache();
        TentacleResultsHandler.ClearItemCache();

        _logger.LogInformation("Tentacle refresh complete — {Playlists} playlists refreshed, caches cleared", playlistCount);

        return Ok(new
        {
            status = "ok",
            message = "Full refresh complete",
            playlistsRefreshed = playlistCount,
        });
    }

    /// <summary>
    /// Returns the current home config for preview/debugging.
    /// </summary>
    [HttpGet("HomeConfig")]
    [Authorize]
    public ActionResult GetHomeConfig()
    {
        var config = _homeScreenManager.GetHomeConfig();
        if (config == null)
        {
            return Ok(new { enabled = false, message = "No home config loaded" });
        }

        return Ok(new
        {
            enabled = true,
            hero = config.Hero,
            rowCount = config.Rows?.Count ?? 0,
            rows = config.Rows,
        });
    }
}

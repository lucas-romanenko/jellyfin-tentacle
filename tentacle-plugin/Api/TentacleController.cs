using System;
using System.Linq;
using Jellyfin.Plugin.Tentacle.HomeScreen;
using Jellyfin.Plugin.Tentacle.Playlists;
using MediaBrowser.Controller.Session;
using MediaBrowser.Model.Session;
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
///   4. Broadcast WebSocket event to all connected clients
/// </summary>
[ApiController]
[Route("[controller]")]
public class TentacleController : ControllerBase
{
    private readonly HomeScreenManager _homeScreenManager;
    private readonly PlaylistManager _playlistManager;
    private readonly ISessionManager _sessionManager;
    private readonly ILogger<TentacleController> _logger;

    public TentacleController(
        HomeScreenManager homeScreenManager,
        PlaylistManager playlistManager,
        ISessionManager sessionManager,
        ILogger<TentacleController> logger)
    {
        _homeScreenManager = homeScreenManager;
        _playlistManager = playlistManager;
        _sessionManager = sessionManager;
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

        // Step 2: Clear home config + discover + ratings caches
        _homeScreenManager.ClearCache();
        TentacleResultsHandler.ClearItemCache();
        TentacleDiscoverController.ClearCache();
        TentacleMdbListController.ClearSettingsCache();
        TentacleTmdbController.ClearCache();
        TentacleConfigController.ClearCache();

        // Step 3: Broadcast LibraryChanged to all connected WebSocket clients
        // This triggers instant home screen refresh on Android TV and Jellyfin web
        int broadcastCount = 0;
        try
        {
            var userIds = _sessionManager.Sessions
                .Where(s => s.UserId != Guid.Empty)
                .Select(s => s.UserId)
                .Distinct()
                .ToList();

            if (userIds.Count > 0)
            {
                await _sessionManager.SendMessageToUserSessions(
                    userIds,
                    SessionMessageType.LibraryChanged,
                    () => new
                    {
                        ItemsAdded = Array.Empty<string>(),
                        ItemsUpdated = Array.Empty<string>(),
                        ItemsRemoved = Array.Empty<string>(),
                        CollectionFolders = Array.Empty<string>()
                    },
                    CancellationToken.None);
                broadcastCount = userIds.Count;
                _logger.LogInformation("Broadcast WebSocket refresh to {Count} connected user(s)", broadcastCount);
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to broadcast WebSocket update");
        }

        _logger.LogInformation("Tentacle refresh complete — {Playlists} playlists refreshed, caches cleared, {Broadcast} users notified", playlistCount, broadcastCount);

        return Ok(new
        {
            status = "ok",
            message = "Full refresh complete",
            playlistsRefreshed = playlistCount,
            broadcastedTo = broadcastCount,
        });
    }

    /// <summary>
    /// Returns the current home config for preview/debugging.
    /// </summary>
    [HttpGet("HomeConfig")]
    [Authorize]
    public ActionResult GetHomeConfig([FromQuery] Guid userId)
    {
        var config = _homeScreenManager.GetHomeConfig(userId);
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

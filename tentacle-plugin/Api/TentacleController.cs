using System;
using System.Linq;
using Jellyfin.Plugin.Tentacle.HomeScreen;
using MediaBrowser.Controller.Session;
using MediaBrowser.Model.Session;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Api;

/// <summary>
/// API controller for Tentacle plugin.
/// POST /Tentacle/Refresh is the main webhook — triggers:
///   1. Clear home config cache
///   2. Clear item / discover / ratings caches
///   3. Broadcast WebSocket event to all connected clients
/// (Playlists are managed by the Tentacle backend via the Jellyfin API, not here.)
/// </summary>
[ApiController]
[Route("[controller]")]
public class TentacleController : ControllerBase
{
    private readonly HomeScreenManager _homeScreenManager;
    private readonly ISessionManager _sessionManager;
    private readonly ILogger<TentacleController> _logger;

    public TentacleController(
        HomeScreenManager homeScreenManager,
        ISessionManager sessionManager,
        ILogger<TentacleController> logger)
    {
        _homeScreenManager = homeScreenManager;
        _sessionManager = sessionManager;
        _logger = logger;
    }

    /// <summary>
    /// Full refresh: clears caches and broadcasts a library-changed event.
    /// Called by Tentacle server after every sync.
    /// Requires Jellyfin API key auth (X-Emby-Token header).
    /// </summary>
    [HttpPost("Refresh")]
    [Authorize]
    public async Task<ActionResult> Refresh()
    {
        _logger.LogInformation("Tentacle refresh triggered — full pipeline starting");

        // Step 1: (Removed) Legacy disk-based playlist rebuild.
        // Playlists are now managed per-user by the Tentacle backend via the Jellyfin API
        // (IsPublic=false, owned by each user). The plugin's old PlaylistManager built
        // admin-owned playlists from on-disk SmartList configs, which conflicted with the
        // backend-driven playlists. This endpoint only clears caches and broadcasts now.
        const int playlistCount = 0;

        // Step 2: Clear home config + discover + ratings caches
        _homeScreenManager.ClearCache();
        TentacleResultsHandler.ClearItemCache();
        TentacleDiscoverController.ClearCache();
        TentacleMdbListController.ClearSettingsCache();
        Services.MdbListCacheService.Clear();
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
                        CollectionFolders = Array.Empty<string>(),
                        FoldersAddedTo = Array.Empty<string>(),
                        FoldersRemovedFrom = Array.Empty<string>(),
                        IsEmpty = true
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
        var req = HttpContext.Request;
        var apiKey = req.Query["api_key"].FirstOrDefault()
                     ?? req.Headers["X-Emby-Token"].FirstOrDefault()
                     ?? "";
        var config = _homeScreenManager.GetHomeConfig(userId, apiKey);
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

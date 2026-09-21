using System;
using System.Linq;
using System.Globalization;
using Jellyfin.Plugin.Tentacle.HomeScreen;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Net;
using MediaBrowser.Controller.Playlists;
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
    private readonly ILibraryManager _libraryManager;
    private readonly IPlaylistManager _playlistManager;
    private readonly IAuthorizationContext _authContext;
    private readonly ILogger<TentacleController> _logger;

    public TentacleController(
        HomeScreenManager homeScreenManager,
        ISessionManager sessionManager,
        ILibraryManager libraryManager,
        IPlaylistManager playlistManager,
        IAuthorizationContext authContext,
        ILogger<TentacleController> logger)
    {
        _homeScreenManager = homeScreenManager;
        _sessionManager = sessionManager;
        _libraryManager = libraryManager;
        _playlistManager = playlistManager;
        _authContext = authContext;
        _logger = logger;
    }

    /// <summary>
    /// Full refresh: clears caches and broadcasts a library-changed event.
    /// Called by Tentacle server after every sync.
    /// Requires Jellyfin API key auth (X-Emby-Token header).
    /// </summary>
    [HttpPost("Refresh")]
    // Admin only: the Tentacle server calls this with the server API key, which
    // satisfies the policy. A bare [Authorize] let any signed-in account, a child
    // profile included, wipe every cache and reload every client in a loop (#79).
    [Authorize(Policy = "RequiresElevation")]
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
    /// Moves one entry of a user's playlist to a new position, in-process.
    /// Called by the Tentacle server (API key) to put a just-downloaded title at the
    /// front of its "recently added" playlists. Jellyfin 10.11's own
    /// POST /Playlists/{id}/Items/{entry}/Move/{index} takes the user from the
    /// caller's token and ignores ?UserId=, so an API key (no user) always gets
    /// 400 "Guid can't be empty"; IPlaylistManager takes the user explicitly.
    /// An API key may name any user; a user token only itself — and either way only
    /// the playlist's OWNER may reorder it (being able to see it is not enough).
    /// </summary>
    [HttpPost("Playlists/{playlistId}/Items/{entryId}/Move/{newIndex}")]
    [Authorize]
    public async Task<ActionResult> MovePlaylistItem(string playlistId, string entryId, int newIndex, [FromQuery] Guid userId)
    {
        if (!Guid.TryParse(playlistId, out var playlistGuid) || !Guid.TryParse(entryId, out var entryGuid) || newIndex < 0)
        {
            return BadRequest("Invalid playlist id, entry id or index");
        }

        var caller = await CallerIdentity.ResolveAsync(_authContext, HttpContext, userId).ConfigureAwait(false);
        if (!caller.Allowed)
        {
            return Forbid();
        }

        userId = caller.UserId;
        if (userId.Equals(default))
        {
            return BadRequest("userId is required");
        }

        if (_libraryManager.GetItemById(playlistGuid) is not Playlist playlist)
        {
            return NotFound("Playlist not found");
        }

        if (!playlist.OwnerUserId.Equals(userId))
        {
            return Forbid();
        }

        // PlaylistItemId is the linked child's ItemId in "N" format. Look it up the way
        // Jellyfin's own MoveItemAsync does, through GetManageableItems(): a just-added
        // LinkedChild has no ItemId (LinkedChild.Create sets only Path) until something
        // resolves it on THIS cached playlist instance, and the listing Tentacle read the
        // id from resolves a different instance. Reading LinkedChildren raw answered 404
        // for an entry that was really there whenever the move followed an add closely.
        var entry = entryGuid.ToString("N", CultureInfo.InvariantCulture);
        if (!playlist.GetManageableItems().Any(c => c.Item1.ItemId.HasValue && c.Item1.ItemId.Value.Equals(entryGuid)))
        {
            return NotFound("Playlist entry not found");
        }

        try
        {
            await _playlistManager.MoveItemAsync(playlistGuid.ToString("N", CultureInfo.InvariantCulture), entry, newIndex, userId).ConfigureAwait(false);
        }
        catch (ArgumentException ex)
        {
            _logger.LogWarning(ex, "Move of entry {Entry} in playlist {Playlist} refused by Jellyfin", entry, playlistGuid);
            return BadRequest(ex.Message);
        }

        return NoContent();
    }

    /// <summary>
    /// Boot stamp of the injected client assets — changes on every plugin update
    /// or Jellyfin restart. The injected JS compares this against the ?v= stamp
    /// its own script tag was served with and reloads the page on mismatch, so
    /// tabs left open across a restart never keep running stale assets.
    /// Anonymous (like the asset endpoints): it must work from any page state.
    /// </summary>
    [HttpGet("Boot")]
    public ActionResult GetBoot()
    {
        return Ok(new { boot = Patching.IndexHtmlPatch.CacheBust });
    }

    /// <summary>
    /// Returns the current home config for preview/debugging.
    /// </summary>
    [HttpGet("HomeConfig")]
    // Admin only: it returns any user's home config for the userId it is given,
    // and nothing in the web UI or the app calls it (#72).
    [Authorize(Policy = "RequiresElevation")]
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

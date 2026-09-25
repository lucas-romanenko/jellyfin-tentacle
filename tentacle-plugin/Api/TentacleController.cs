using System;
using System.Collections.Generic;
using System.Linq;
using System.Globalization;
using Jellyfin.Data.Enums;
using Jellyfin.Plugin.Tentacle.HomeScreen;
using MediaBrowser.Controller.Entities;
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
        TentacleHomeController.ClearSectionCache();
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

    /// <summary>Body of POST /Tentacle/Playlists/PruneDead.</summary>
    public class PruneDeadRequest
    {
        /// <summary>Gets or sets the playlists to check. Empty = every playlist on the server.</summary>
        public List<string>? Ids { get; set; }

        /// <summary>Gets or sets a value indicating whether to skip the mass-removal guards.</summary>
        public bool Force { get; set; }
    }

    // A playlist this big with not ONE entry resolving looks like storage that is
    // offline, not like every title being deleted. Same idea across the whole run.
    internal const int WholePlaylistGuardMin = 10;
    internal const int RunGuardMinDead = 500;

    /// <summary>
    /// Drops playlist entries whose item no longer exists in Jellyfin (#120).
    /// When YouTube retention deletes a video or Radarr replaces a file, the old
    /// entry stays in every playlist that held it. Jellyfin hides such entries
    /// from /Playlists/{id}/Items, so Tentacle's refresh — which only sees what
    /// that endpoint returns — can never remove them, and every read of the
    /// playlist logs "Unable to find linked item at path".
    /// The removal itself is Jellyfin's own RemoveItemFromPlaylistAsync with no
    /// ids: it rebuilds the list from the entries that still resolve and saves
    /// playlist.xml exactly as a normal removal does.
    /// Called by the Tentacle server (API key) after playlist refreshes.
    /// </summary>
    [HttpPost("Playlists/PruneDead")]
    [Authorize(Policy = "RequiresElevation")]
    public async Task<ActionResult> PruneDeadPlaylistEntries([FromBody] PruneDeadRequest? body)
    {
        List<Playlist> playlists;
        if (body?.Ids is { Count: > 0 } ids)
        {
            playlists = ids
                .Select(id => Guid.TryParse(id, out var g) ? _libraryManager.GetItemById(g) as Playlist : null)
                .OfType<Playlist>()
                .DistinctBy(p => p.Id)
                .ToList();
        }
        else
        {
            playlists = _libraryManager.GetItemList(new InternalItemsQuery
            {
                IncludeItemTypes = new[] { BaseItemKind.Playlist },
                Recursive = true,
            }).OfType<Playlist>().ToList();
        }

        // Pass 1: count, touching nothing.
        var found = new List<(Playlist Playlist, int Total, int Dead)>();
        int totalEntries = 0, totalDead = 0;
        foreach (var playlist in playlists)
        {
            var total = playlist.LinkedChildren.Length;
            if (total == 0)
            {
                continue;
            }

            var dead = total - playlist.GetLinkedChildrenInfos().Count;
            totalEntries += total;
            totalDead += dead;
            if (dead > 0)
            {
                found.Add((playlist, total, dead));
            }
        }

        var force = body?.Force ?? false;
        var skipped = new List<object>();
        if (!force && totalDead >= RunGuardMinDead && totalDead * 2 > totalEntries)
        {
            _logger.LogWarning(
                "Tentacle prune: REFUSING — {Dead} of {Total} playlist entries across {Count} playlist(s) do not resolve. "
                + "That many at once looks like media storage being unavailable, not deleted items. Nothing removed.",
                totalDead, totalEntries, playlists.Count);
            return Ok(new { checkedPlaylists = playlists.Count, prunedPlaylists = 0, removed = 0, refused = true, dead = totalDead });
        }

        // Pass 2: remove.
        int pruned = 0, removed = 0;
        foreach (var (playlist, total, dead) in found)
        {
            if (!force && dead == total && total >= WholePlaylistGuardMin)
            {
                _logger.LogWarning(
                    "Tentacle prune: skipping playlist {Name} ({Id}) — none of its {Total} entries resolve, which looks like storage being offline",
                    playlist.Name, playlist.Id, total);
                skipped.Add(new { id = playlist.Id.ToString("N", CultureInfo.InvariantCulture), name = playlist.Name, total });
                continue;
            }

            try
            {
                await _playlistManager.RemoveItemFromPlaylistAsync(
                    playlist.Id.ToString("N", CultureInfo.InvariantCulture), Array.Empty<string>()).ConfigureAwait(false);
                pruned++;
                removed += dead;
                _logger.LogInformation(
                    "Tentacle prune: removed {Dead} dead entries from playlist {Name} ({Id}); {Left} left",
                    dead, playlist.Name, playlist.Id, total - dead);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Tentacle prune: could not update playlist {Name} ({Id})", playlist.Name, playlist.Id);
            }
        }

        return Ok(new
        {
            checkedPlaylists = playlists.Count,
            prunedPlaylists = pruned,
            removed,
            refused = false,
            skipped,
        });
    }

    /// <summary>
    /// Lists playlists with no owner (OwnerUserId empty), with their entry counts (#120).
    /// Jellyfin's own API never says who owns a playlist, and an ownerless one is
    /// hidden from — or, with open access, shown to — every user alike, so the
    /// Tentacle server cannot tell it apart from a shared playlist and its
    /// duplicate clean-up leaves it alone. The server decides what to delete.
    /// </summary>
    [HttpGet("Playlists/Ownerless")]
    [Authorize(Policy = "RequiresElevation")]
    public ActionResult GetOwnerlessPlaylists()
    {
        var result = _libraryManager.GetItemList(new InternalItemsQuery
            {
                IncludeItemTypes = new[] { BaseItemKind.Playlist },
                Recursive = true,
            })
            .OfType<Playlist>()
            .Where(p => p.OwnerUserId.Equals(Guid.Empty))
            .Select(p => new
            {
                id = p.Id.ToString("N", CultureInfo.InvariantCulture),
                name = p.Name,
                entries = p.LinkedChildren.Length,
                openAccess = p.OpenAccess,
                shares = p.Shares?.Count ?? 0,
                created = p.DateCreated,
            })
            .ToList();
        return Ok(result);
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

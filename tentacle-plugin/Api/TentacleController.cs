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
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<TentacleController> _logger;

    public TentacleController(
        HomeScreenManager homeScreenManager,
        ISessionManager sessionManager,
        ILibraryManager libraryManager,
        IPlaylistManager playlistManager,
        IAuthorizationContext authContext,
        IHttpClientFactory httpClientFactory,
        ILogger<TentacleController> logger)
    {
        _homeScreenManager = homeScreenManager;
        _sessionManager = sessionManager;
        _libraryManager = libraryManager;
        _playlistManager = playlistManager;
        _authContext = authContext;
        _httpClientFactory = httpClientFactory;
        _logger = logger;
    }

    /// <summary>
    /// Confirms that this server deleted a title and told the backend so. The backend
    /// acts on a delete notification only when this answers 200 (#139). Each forwarded
    /// deletion confirms once, within 15 minutes (see RecentDeletions).
    /// </summary>
    /// <param name="mediaType">"movie" or "series".</param>
    /// <param name="tmdbId">The deleted title's TMDB id.</param>
    [HttpPost("Deletions/{mediaType}/{tmdbId}/Confirm")]
    [Authorize(Policy = "RequiresElevation")]
    public ActionResult ConfirmDeletion([FromRoute] string mediaType, [FromRoute] string tmdbId)
    {
        return Services.RecentDeletions.TryConsume(mediaType, tmdbId) ? Ok() : NotFound();
    }

    /// <summary>
    /// Full refresh: clears caches and broadcasts a library-changed event.
    /// Called by Tentacle server after every sync.
    /// Requires Jellyfin API key auth (X-Emby-Token header).
    /// With <paramref name="userId"/> (a save of that user's home or toolbar) it clears
    /// only the home caches, for that user, and tells only that user's clients; ratings,
    /// TMDB and Discover stay cached and nobody else reloads.
    /// <paramref name="homeVersion"/> goes into the message (see <see cref="HomeVersionMarker"/>).
    /// With <paramref name="scope"/> "livetv" (Tentacle set Live TV up: channels came or
    /// went, someone's access changed) no cache is cleared and every connected user gets a
    /// LibraryChanged carrying "tentacle-livetv:&lt;version&gt;", so open guides read their
    /// channel list again. An older plugin ignores the scope and does the full refresh.
    /// </summary>
    /// <param name="userId">The user whose home was saved; absent means everything.</param>
    /// <param name="homeVersion">The home version the save made (Tentacle sends it; 0 = none).</param>
    /// <param name="scope">"livetv" for a Live TV lineup change; absent otherwise.</param>
    [HttpPost("Refresh")]
    // Admin only: the Tentacle server calls this with the server API key, which
    // satisfies the policy. A bare [Authorize] let any signed-in account, a child
    // profile included, wipe every cache and reload every client in a loop (#79).
    [Authorize(Policy = "RequiresElevation")]
    public async Task<ActionResult> Refresh([FromQuery] Guid? userId = null, [FromQuery] long homeVersion = 0, [FromQuery] string? scope = null)
    {
        if (string.Equals(scope, "livetv", StringComparison.OrdinalIgnoreCase))
        {
            var told = await BroadcastLibraryChanged(null, new[] { LiveTvVersionMarker(homeVersion) }).ConfigureAwait(false);
            _logger.LogInformation("Tentacle Live TV changed — told {Count} connected user(s)", told);
            return Ok(new { status = "ok", message = "Live TV change sent", broadcastedTo = told, homeOnly = false });
        }

        var homeOnly = userId is { } one && one != Guid.Empty;
        _logger.LogInformation(
            "Tentacle refresh triggered — {Scope}",
            homeOnly ? "home of one user" : "full pipeline starting");

        // Step 1: (Removed) Legacy disk-based playlist rebuild.
        // Playlists are now managed per-user by the Tentacle backend via the Jellyfin API
        // (IsPublic=false, owned by each user). The plugin's old PlaylistManager built
        // admin-owned playlists from on-disk SmartList configs, which conflicted with the
        // backend-driven playlists. This endpoint only clears caches and broadcasts now.
        const int playlistCount = 0;

        // Step 2: Clear caches. A home save only needs the user's home config (and their
        // cached row contents); the 7-day ratings, TMDB and Discover caches stay.
        if (homeOnly)
        {
            _homeScreenManager.ClearCache(userId);
            TentacleHomeController.ClearSectionCache(userId!.Value);
        }
        else
        {
            _homeScreenManager.ClearCache();
            TentacleHomeController.ClearSectionCache();
            TentacleDiscoverController.ClearCache();
            TentacleMdbListController.ClearSettingsCache();
            Services.MdbListCacheService.Clear();
            TentacleTmdbController.ClearCache();
            TentacleConfigController.ClearCache();
        }

        // Step 3: Send LibraryChanged to the connected WebSocket clients (the user's own,
        // or everyone's). This triggers instant home screen refresh on Android TV and
        // Jellyfin web; the marker lets the TV app remember the version it was told of.
        var marker = homeVersion > 0
            ? new[] { HomeVersionMarker(homeOnly ? userId : null, homeVersion) }
            : Array.Empty<string>();
        int broadcastCount = await BroadcastLibraryChanged(homeOnly ? userId : null, marker).ConfigureAwait(false);
        if (broadcastCount > 0)
        {
            _logger.LogInformation("Broadcast WebSocket refresh to {Count} connected user(s)", broadcastCount);
        }

        _logger.LogInformation("Tentacle refresh complete — {Playlists} playlists refreshed, caches cleared, {Broadcast} users notified", playlistCount, broadcastCount);

        return Ok(new
        {
            status = "ok",
            message = homeOnly ? "Home refresh complete" : "Full refresh complete",
            playlistsRefreshed = playlistCount,
            broadcastedTo = broadcastCount,
            homeOnly,
        });
    }

    /// <summary>
    /// The home version in a LibraryChanged message: one CollectionFolders entry
    /// "tentacle-home:&lt;user id, no dashes, or *&gt;:&lt;version&gt;". It rides in a field
    /// every client already parses as a list of strings (the Jellyfin SDK drops unknown
    /// fields), and clients that don't know it ignore it, as they did the empty list.
    /// </summary>
    internal static string HomeVersionMarker(Guid? userId, long homeVersion) =>
        $"tentacle-home:{(userId is { } id ? id.ToString("N") : "*")}:{homeVersion}";

    /// <summary>
    /// The Live TV marker in a LibraryChanged message: "tentacle-livetv:&lt;version&gt;"
    /// (Tentacle's number, or the time in ms when it sent none).
    /// </summary>
    internal static string LiveTvVersionMarker(long version) =>
        $"tentacle-livetv:{(version > 0 ? version : DateTimeOffset.UtcNow.ToUnixTimeMilliseconds())}";

    /// <summary>
    /// Sends an empty LibraryChanged (with <paramref name="markers"/> in CollectionFolders)
    /// to one user's sessions, or to every connected user's. Returns how many users.
    /// </summary>
    private async Task<int> BroadcastLibraryChanged(Guid? userId, string[] markers)
    {
        try
        {
            var userIds = userId is { } one
                ? new List<Guid> { one }
                : _sessionManager.Sessions
                    .Where(s => s.UserId != Guid.Empty)
                    .Select(s => s.UserId)
                    .Distinct()
                    .ToList();
            if (userIds.Count == 0)
            {
                return 0;
            }

            await _sessionManager.SendMessageToUserSessions(
                userIds,
                SessionMessageType.LibraryChanged,
                () => new
                {
                    ItemsAdded = Array.Empty<string>(),
                    ItemsUpdated = Array.Empty<string>(),
                    ItemsRemoved = Array.Empty<string>(),
                    CollectionFolders = markers,
                    FoldersAddedTo = Array.Empty<string>(),
                    FoldersRemovedFrom = Array.Empty<string>(),
                    IsEmpty = true
                },
                CancellationToken.None).ConfigureAwait(false);
            return userIds.Count;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to broadcast WebSocket update");
            return 0;
        }
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

        /// <summary>
        /// Gets or sets a value indicating whether to answer at once (202 with a run id to
        /// poll at GET Playlists/PruneDead/{runId}) instead of when the prune is done.
        /// </summary>
        public bool Async { get; set; }
    }

    // One prune at a time: a second request (a slow run overlapping the next hour's)
    // waits instead of rewriting the same playlists alongside it.
    private static readonly SemaphoreSlim PruneGate = new(1, 1);
    private static readonly object PruneRunsLock = new();
    // Runs started with Async, by id: the one running and the last few finished ones.
    private static readonly Dictionary<string, PruneRun> PruneRuns = new();
    private static PruneRun? _currentPruneRun;

    private sealed class PruneRun
    {
        public string Id { get; } = Guid.NewGuid().ToString("N", CultureInfo.InvariantCulture);

        public DateTime Started { get; } = DateTime.UtcNow;

        public Task<object>? Work { get; set; }
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
        if (body?.Async == true)
        {
            // The server polls the run (#181 follow-up): on a large library while
            // Jellyfin is busy the prune outlasted any fixed timeout, and the server
            // released its playlist lock while this was still rewriting playlists.
            // A run already going is joined, not doubled.
            PruneRun run;
            lock (PruneRunsLock)
            {
                if (_currentPruneRun is { Work.IsCompleted: false } current)
                {
                    run = current;
                }
                else
                {
                    run = new PruneRun();
                    var request = body;
                    run.Work = Task.Run(() => RunPruneAsync(request));
                    _currentPruneRun = run;
                    PruneRuns[run.Id] = run;
                    foreach (var old in PruneRuns.Values.Where(r => r.Work!.IsCompleted).OrderByDescending(r => r.Started).Skip(5).ToList())
                    {
                        PruneRuns.Remove(old.Id);
                    }
                }
            }

            return StatusCode(202, new { runId = run.Id, state = "running" });
        }

        return Ok(await RunPruneAsync(body).ConfigureAwait(false));
    }

    /// <summary>
    /// State of a prune started with Async: running, done (with the summary the
    /// synchronous call answers) or failed. 404 when this Jellyfin doesn't know the
    /// run (it restarted meanwhile).
    /// </summary>
    /// <param name="runId">The id POST Playlists/PruneDead answered.</param>
    [HttpGet("Playlists/PruneDead/{runId}")]
    [Authorize(Policy = "RequiresElevation")]
    public ActionResult GetPruneRun([FromRoute] string runId)
    {
        PruneRun? run;
        lock (PruneRunsLock)
        {
            PruneRuns.TryGetValue(runId, out run);
        }

        if (run?.Work == null)
        {
            return NotFound();
        }

        if (!run.Work.IsCompleted)
        {
            return Ok(new { runId, state = "running" });
        }

        if (run.Work.IsCompletedSuccessfully)
        {
            return Ok(new { runId, state = "done", result = run.Work.Result });
        }

        return Ok(new { runId, state = "failed", error = run.Work.Exception?.GetBaseException().Message ?? "cancelled" });
    }

    private async Task<object> RunPruneAsync(PruneDeadRequest? body)
    {
        await PruneGate.WaitAsync().ConfigureAwait(false);
        try
        {
            return await PruneAsync(body).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Tentacle prune: failed");
            throw;
        }
        finally
        {
            PruneGate.Release();
        }
    }

    private async Task<object> PruneAsync(PruneDeadRequest? body)
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
            return new { checkedPlaylists = playlists.Count, prunedPlaylists = 0, removed = 0, refused = true, dead = totalDead };
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

        return new
        {
            checkedPlaylists = playlists.Count,
            prunedPlaylists = pruned,
            removed,
            refused = false,
            skipped,
        };
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
    /// The settings page's "Test Connection": asks the Tentacle server's status from
    /// Jellyfin's side, where the plugin makes its calls (#290). The page used to ask
    /// from the admin's browser, which often can't resolve the server-side address
    /// (http://tentacle:8888), is refused by Tentacle's CORS allowlist from any other
    /// Jellyfin origin, or blocked as mixed content: "Connection failed" for a URL the
    /// plugin uses fine. Admin only; it only ever asks {url}/api/widget/status and
    /// answers the status fields, never the target's body.
    /// </summary>
    /// <param name="url">The Tentacle URL to test (the saved one when empty).</param>
    [HttpGet("TestConnection")]
    [Authorize(Policy = "RequiresElevation")]
    public async Task<ActionResult> TestConnection([FromQuery] string? url)
    {
        var target = (string.IsNullOrWhiteSpace(url) ? Plugin.Instance?.Configuration?.TentacleUrl : url)?.Trim().TrimEnd('/');
        if (string.IsNullOrEmpty(target)
            || !Uri.TryCreate(target, UriKind.Absolute, out var baseUri)
            || (baseUri.Scheme != Uri.UriSchemeHttp && baseUri.Scheme != Uri.UriSchemeHttps))
        {
            return BadRequest(new { error = "Enter an http:// or https:// URL" });
        }

        var client = _httpClientFactory.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(5);
        try
        {
            using var response = await client.GetAsync(target + "/api/widget/status", HttpContext.RequestAborted).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                return StatusCode(502, new { error = $"Tentacle answered HTTP {(int)response.StatusCode}" });
            }

            var text = await response.Content.ReadAsStringAsync(HttpContext.RequestAborted).ConfigureAwait(false);
            using var doc = System.Text.Json.JsonDocument.Parse(text);
            var root = doc.RootElement;
            long Count(string name) =>
                root.TryGetProperty(name, out var v) && v.ValueKind == System.Text.Json.JsonValueKind.Number && v.TryGetInt64(out var n) ? n : 0;
            if (root.ValueKind != System.Text.Json.JsonValueKind.Object || !root.TryGetProperty("status", out _))
            {
                throw new System.Text.Json.JsonException("no status field");
            }

            string? lastSync = root.TryGetProperty("last_sync", out var ls) && ls.ValueKind == System.Text.Json.JsonValueKind.String ? ls.GetString() : null;
            return Ok(new { movies = Count("movies"), series = Count("series"), last_sync = lastSync });
        }
        catch (System.Text.Json.JsonException)
        {
            return StatusCode(502, new { error = "Not a Tentacle server (the answer isn't Tentacle's status)" });
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            if (HttpContext.RequestAborted.IsCancellationRequested)
            {
                throw;
            }

            var reason = ex is TaskCanceledException ? "Timed out after 5 s" : ex.Message;
            return StatusCode(502, new { error = "Jellyfin can't reach it: " + reason });
        }
    }

    /// <summary>
    /// Returns the current home config for preview/debugging.
    /// </summary>
    [HttpGet("HomeConfig")]
    // Admin only: it returns any user's home config for the userId it is given,
    // and nothing in the web UI or the app calls it (#72).
    [Authorize(Policy = "RequiresElevation")]
    public async Task<ActionResult> GetHomeConfig([FromQuery] Guid userId)
    {
        var req = HttpContext.Request;
        var apiKey = req.Query["api_key"].FirstOrDefault()
                     ?? req.Headers["X-Emby-Token"].FirstOrDefault()
                     ?? "";
        var config = await _homeScreenManager.GetHomeConfigAsync(userId, apiKey, HttpContext.RequestAborted).ConfigureAwait(false);
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

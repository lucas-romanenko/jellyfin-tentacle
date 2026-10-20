using System.Text.RegularExpressions;
using Jellyfin.Data;
using Jellyfin.Database.Implementations.Enums;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Makes Jellyfin re-encode the video of Tentacle's VOD (.strm items whose
/// address is Tentacle's signed /api/vod/ link) whenever it serves them as
/// HLS, instead of copying the video and transcoding only the audio.
///
/// Why: with the video copied (jellyfin/jellyfin#17966), a resumed play
/// (`-ss … -noaccurate_seek -codec:v:0 copy`) gets segments cut at the
/// source's keyframes, about 2.7 s each, while the playlist says 6 s each:
/// fifty segments read as 300 s but hold under 3 minutes of media. Jellyfin
/// Web runs off the end of what was written, decides the episode is over
/// and plays the next one. Re-encoded, the segments are as long as the
/// playlist says.
///
/// How: an MVC filter on the requests that decide it. On
/// <c>POST /Items/{id}/PlaybackInfo</c> it sets the (query) argument
/// <c>allowVideoStreamCopy=false</c> before Jellyfin answers, so Jellyfin
/// itself builds the TranscodingUrl with <c>&amp;allowVideoStreamCopy=false</c>
/// and drops direct stream (remux), while direct play is decided as before
/// (the TV app direct-plays these). The same argument is set on the HLS
/// video requests (master.m3u8, main.m3u8, the segments) as a backstop for
/// a client that builds its own URL; all three get it, so the playlist and
/// the segments agree. Library files and Live TV are never touched, nor is
/// a user who may not transcode video: Jellyfin copies the video for them
/// whatever the flag says, and on PlaybackInfo the flag would take away the
/// remux they play through.
/// </summary>
public sealed class VodNoVideoCopyFilter : IActionFilter
{
    // The token URL services.vod_tokens writes (/api/vod/movie/12.345.<sig>.mkv),
    // under any address and path prefix Tentacle is reached at.
    private static readonly Regex TentacleVodUrl = new(
        @"^https?://[^/?#]+(?:/[^?#]*)?/api/vod/(?:movie|series)/\d+\.\d+\.[0-9a-f]+\.[A-Za-z0-9]+(?:[?#]|$)",
        RegexOptions.Compiled | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    // (controller, action) pairs whose allowVideoStreamCopy argument decides
    // whether a video HLS stream copies the video.
    private static readonly HashSet<(string Controller, string Action)> Actions = new()
    {
        ("MediaInfo", "GetPostedPlaybackInfo"),
        ("DynamicHls", "GetMasterHlsVideoPlaylist"),
        ("DynamicHls", "GetVariantHlsVideoPlaylist"),
        ("DynamicHls", "GetHlsVideoSegment"),
    };

    private const string CopyArgument = "allowVideoStreamCopy";

    // The claim Jellyfin's authentication puts the caller's user id in
    // (Jellyfin.Api's InternalClaimTypes.UserId, not referenced by plugins).
    private const string UserIdClaim = "Jellyfin-UserId";

    private readonly ILibraryManager _libraryManager;
    private readonly IUserManager _userManager;
    private readonly ILogger<VodNoVideoCopyFilter> _logger;

    public VodNoVideoCopyFilter(ILibraryManager libraryManager, IUserManager userManager, ILogger<VodNoVideoCopyFilter> logger)
    {
        _libraryManager = libraryManager;
        _userManager = userManager;
        _logger = logger;
    }

    /// <inheritdoc />
    public void OnActionExecuting(ActionExecutingContext context)
    {
        if (context.ActionDescriptor is not ControllerActionDescriptor action
            || !Actions.Contains((action.ControllerName, action.ActionName)))
        {
            return;
        }

        try
        {
            var args = context.ActionArguments;
            if (args.TryGetValue(CopyArgument, out var copy) && copy is false)
            {
                return;
            }

            if (!args.TryGetValue("itemId", out var id) || id is not Guid itemId)
            {
                return;
            }

            var item = PlayedItem(itemId, MediaSourceId(args));
            if (item is null || !IsTentacleVod(item))
            {
                return;
            }

            if (!MayTranscodeVideo(context))
            {
                return;
            }

            args[CopyArgument] = false;
            if (action.ActionName == "GetPostedPlaybackInfo")
            {
                _logger.LogInformation(
                    "[Tentacle] {Name}: Tentacle VOD, HLS will re-encode the video instead of copying it (jellyfin#17966)",
                    item.Name);
            }
        }
        catch (Exception ex)
        {
            // Never fail a play over this: Jellyfin then decides as it always did.
            _logger.LogWarning(ex, "[Tentacle] Could not check whether {Action} is a Tentacle VOD", action.ActionName);
        }
    }

    /// <inheritdoc />
    public void OnActionExecuted(ActionExecutedContext context)
    {
    }

    /// <summary>
    /// Whether the item plays from Tentacle's /api/vod/ route (a .strm
    /// Tentacle wrote), as opposed to a library file, Live TV or a .strm
    /// pointing elsewhere.
    /// </summary>
    public static bool IsTentacleVod(BaseItem item)
        => item is Video { IsShortcut: true } && IsTentacleVodUrl(item.ShortcutPath);

    /// <summary>Whether the address is Tentacle's signed VOD link.</summary>
    public static bool IsTentacleVodUrl(string? url)
        => !string.IsNullOrEmpty(url) && TentacleVodUrl.IsMatch(url.Trim());

    // Whether the user the stream is for may have its video re-encoded. As
    // Jellyfin decides it: PlaybackInfo is for the userId it names (query or
    // body), else for the caller; an HLS request is for the caller. No user
    // (an API key without one): Jellyfin applies no permission, nor do we.
    private bool MayTranscodeVideo(ActionExecutingContext context)
    {
        var args = context.ActionArguments;
        var userId = args.TryGetValue("userId", out var query) && query is Guid fromQuery ? fromQuery : Guid.Empty;
        if (userId == Guid.Empty && args.TryGetValue("playbackInfoDto", out var body) && body is not null
            && body.GetType().GetProperty("UserId")?.GetValue(body) is Guid fromBody)
        {
            userId = fromBody;
        }

        if (userId == Guid.Empty)
        {
            Guid.TryParse(context.HttpContext.User.FindFirst(UserIdClaim)?.Value, out userId);
        }

        var user = userId == Guid.Empty ? null : _userManager.GetUserById(userId);
        return user is null || user.HasPermission(PermissionKind.EnableVideoPlaybackTranscoding);
    }

    // The version being played: the media source when the request names one
    // (an item with several versions), else the item itself.
    private BaseItem? PlayedItem(Guid itemId, string? mediaSourceId)
    {
        if (Guid.TryParse(mediaSourceId, out var sourceId) && sourceId != itemId)
        {
            var source = _libraryManager.GetItemById(sourceId);
            if (source is not null)
            {
                return source;
            }
        }

        return _libraryManager.GetItemById(itemId);
    }

    // mediaSourceId from the query, or from PlaybackInfo's body (PlaybackInfoDto
    // lives in Jellyfin.Api, which a plugin does not reference: read by name).
    private static string? MediaSourceId(IDictionary<string, object?> args)
    {
        if (args.TryGetValue("mediaSourceId", out var query) && query is string fromQuery && fromQuery.Length > 0)
        {
            return fromQuery;
        }

        return args.TryGetValue("playbackInfoDto", out var body) && body is not null
            ? body.GetType().GetProperty("MediaSourceId")?.GetValue(body) as string
            : null;
    }
}

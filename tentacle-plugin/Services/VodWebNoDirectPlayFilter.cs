using MediaBrowser.Model.Dto;
using MediaBrowser.Model.MediaInfo;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Makes Jellyfin Web play Tentacle's VOD through Jellyfin instead of
/// straight from Tentacle.
///
/// Why: a Tentacle VOD source the browser can play as it is (an MKV or MP4
/// of H.264 and AAC) comes back from PlaybackInfo as direct play, with
/// <c>Path</c> = Tentacle's own address (<c>http://&lt;lan ip&gt;:8888/api/vod/…</c>).
/// Jellyfin Web direct-plays a remote HTTP source by loading that
/// <c>Path</c> in the browser: on an https Jellyfin the browser blocks it as
/// mixed content, and away from home the address doesn't answer at all.
/// The player sits on a black screen; Tentacle never even sees a request.
/// Titles that need converting (AC3 audio) were fine: they go through
/// Jellyfin's HLS.
///
/// How: an MVC filter on <c>/Items/{id}/PlaybackInfo</c>. After Jellyfin
/// answered, for a client that plays with Jellyfin Web's player, each
/// Tentacle VOD source offered as direct play gets
/// <c>SupportsDirectPlay = false</c>. Jellyfin offers direct stream exactly
/// when it chose direct play (and builds no TranscodingUrl then), so the
/// player takes its direct-stream path: <c>/Videos/{id}/stream.mkv?Static=true</c>,
/// which Jellyfin serves by passing Tentacle's stream through (seeks
/// included: it forwards the Range header). Same file, no conversion,
/// reached at Jellyfin's own address. If that fails, Jellyfin Web asks again
/// without direct play and gets HLS (re-encoded, VodNoVideoCopyFilter).
/// Only apps playing with Jellyfin Web's player (and the Android phone app)
/// are changed: the Tentacle TV app direct-plays
/// Tentacle's address on purpose, and native Jellyfin apps already play a
/// direct-play source through Jellyfin's /Videos/{id}/stream (the Android
/// TV one would find no stream at all with direct play off and no
/// TranscodingUrl). Library files, Live TV and .strm files pointing
/// straight at a provider are untouched.
/// </summary>
public sealed class VodWebNoDirectPlayFilter : IActionFilter
{
    private static readonly HashSet<(string Controller, string Action)> Actions = new()
    {
        ("MediaInfo", "GetPostedPlaybackInfo"),
        ("MediaInfo", "GetPlaybackInfo"),
    };

    // The client names of the apps that play with Jellyfin Web's player
    // (playbackmanager.js direct-plays a remote HTTP source from its Path):
    // the browser, the desktop apps (old and new name), the iOS app
    // ("Jellyfin " + the OS) and the Android phone app, whose ExoPlayer
    // also loads Path on direct play and Jellyfin's /Videos/{id}/stream on
    // direct stream. Not the Android TV apps.
    private static readonly HashSet<string> WebPlayerClients = new(StringComparer.Ordinal)
    {
        "Jellyfin Web",
        "Jellyfin Media Player",
        "Jellyfin Desktop",
        "Jellyfin iOS",
        "Jellyfin iPadOS",
        "Jellyfin for Android",
    };

    // The claim Jellyfin's authentication puts the caller's client name in
    // (Jellyfin.Api's InternalClaimTypes.Client, not referenced by plugins).
    private const string ClientClaim = "Jellyfin-Client";

    private readonly ILogger<VodWebNoDirectPlayFilter> _logger;

    public VodWebNoDirectPlayFilter(ILogger<VodWebNoDirectPlayFilter> logger)
    {
        _logger = logger;
    }

    /// <inheritdoc />
    public void OnActionExecuting(ActionExecutingContext context)
    {
    }

    /// <inheritdoc />
    public void OnActionExecuted(ActionExecutedContext context)
    {
        if (context.ActionDescriptor is not ControllerActionDescriptor action
            || !Actions.Contains((action.ControllerName, action.ActionName))
            || !IsWebPlayer(context.HttpContext.User.FindFirst(ClientClaim)?.Value))
        {
            return;
        }

        try
        {
            if (context.Result is not ObjectResult { Value: PlaybackInfoResponse info })
            {
                return;
            }

            foreach (var source in info.MediaSources ?? Array.Empty<MediaSourceInfo>())
            {
                if (!NeedsJellyfinStream(source))
                {
                    continue;
                }

                source.SupportsDirectPlay = false;
                _logger.LogInformation(
                    "[Tentacle] {Name}: Tentacle VOD, the browser plays it through Jellyfin (direct stream) instead of Tentacle's address",
                    source.Name);
            }
        }
        catch (Exception ex)
        {
            // Never fail a play over this: Jellyfin's answer goes out as it was.
            _logger.LogWarning(ex, "[Tentacle] Could not check PlaybackInfo for Tentacle VOD");
        }
    }

    /// <summary>Whether the client plays with Jellyfin Web's player.</summary>
    public static bool IsWebPlayer(string? client)
        => client is not null && WebPlayerClients.Contains(client);

    /// <summary>
    /// Whether a media source is Tentacle VOD offered as direct play, with
    /// Jellyfin's direct stream to fall back on.
    /// </summary>
    public static bool NeedsJellyfinStream(MediaSourceInfo source)
        => source.SupportsDirectPlay
           && source.SupportsDirectStream
           && source.Protocol == MediaProtocol.Http
           && VodNoVideoCopyFilter.IsTentacleVodUrl(source.Path);
}

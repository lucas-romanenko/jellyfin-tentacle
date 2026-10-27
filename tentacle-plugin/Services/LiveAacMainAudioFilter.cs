using Jellyfin.Data;
using Jellyfin.Database.Implementations.Enums;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.MediaInfo;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// Makes Jellyfin re-encode the audio of a Live TV channel whose audio is
/// AAC Main when a browser plays it, from the first request, instead of
/// copying it.
///
/// Why: some providers' channels carry AAC Main audio (MPEG-TS profile 0,
/// "aac (Main)" in ffmpeg). Jellyfin copies it into the HLS stream, and
/// browsers cannot decode it: Chrome's MediaSource refuses
/// <c>audio/mp4;codecs=mp4a.40.1</c> outright. Jellyfin Web then tries
/// hls.js's recovery twice (the picture goes on and off), reports an error,
/// and asks again with the video re-encoded (no help), then with the audio
/// re-encoded too: three ffmpeg starts and about 10 seconds of flashing
/// before the channel plays, and it ends up re-encoding 1080p60 video it
/// could have copied. AAC LC channels never did this.
///
/// How: an MVC filter on <c>POST /Items/{id}/PlaybackInfo</c> and
/// <c>/LiveStreams/Open</c>. After Jellyfin answered (the stream is open
/// and its streams known), for each infinite (live) media source with an
/// AAC Main audio stream, it adds <c>AllowAudioStreamCopy=false</c> to the
/// TranscodingUrl. Jellyfin then copies the video and encodes the audio to
/// AAC LC: one ffmpeg, a clean start. Only Jellyfin Web (the browser
/// player) is changed: the TV app plays AAC Main fine and keeps its stream
/// as it is. A user who may not transcode audio is left alone, and so is a
/// URL that already says whether to copy the audio.
/// </summary>
public sealed class LiveAacMainAudioFilter : IAsyncActionFilter
{
    private static readonly HashSet<(string Controller, string Action)> Actions = new()
    {
        ("MediaInfo", "GetPostedPlaybackInfo"),
        ("MediaInfo", "OpenLiveStream"),
    };

    private const string NoAudioCopy = "AllowAudioStreamCopy=false";

    // The client name Jellyfin Web signs in with (browsers, whatever the OS).
    private const string WebClient = "Jellyfin Web";

    // The claims Jellyfin's authentication puts the caller's client name and
    // user id in (Jellyfin.Api's InternalClaimTypes, not referenced by plugins).
    private const string ClientClaim = "Jellyfin-Client";
    private const string UserIdClaim = "Jellyfin-UserId";

    private readonly IUserManager _userManager;
    private readonly ILogger<LiveAacMainAudioFilter> _logger;

    public LiveAacMainAudioFilter(IUserManager userManager, ILogger<LiveAacMainAudioFilter> logger)
    {
        _userManager = userManager;
        _logger = logger;
    }

    /// <inheritdoc />
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        if (context.ActionDescriptor is not ControllerActionDescriptor action
            || !Actions.Contains((action.ControllerName, action.ActionName))
            || !string.Equals(context.HttpContext.User.FindFirst(ClientClaim)?.Value, WebClient, StringComparison.Ordinal))
        {
            await next().ConfigureAwait(false);
            return;
        }

        var executed = await next().ConfigureAwait(false);
        try
        {
            if (executed.Result is not ObjectResult { Value: { } value })
            {
                return;
            }

            var sources = value switch
            {
                PlaybackInfoResponse info => info.MediaSources ?? Array.Empty<MediaSourceInfo>(),
                LiveStreamResponse { MediaSource: { } live } => new[] { live },
                _ => Array.Empty<MediaSourceInfo>(),
            };

            foreach (var source in sources)
            {
                if (!NeedsAudioReencode(source) || !MayTranscodeAudio(context))
                {
                    continue;
                }

                source.TranscodingUrl = WithoutAudioCopy(source.TranscodingUrl!);
                _logger.LogInformation(
                    "[Tentacle] Live stream {Id}: AAC Main audio, the browser gets it re-encoded (video still copied)",
                    source.LiveStreamId ?? source.Id);
            }
        }
        catch (Exception ex)
        {
            // Never fail a play over this: Jellyfin's answer goes out as it was.
            _logger.LogWarning(ex, "[Tentacle] Could not check a live stream's audio for {Action}", action.ActionName);
        }
    }

    /// <summary>
    /// Whether a media source is a live stream served as a transcode (HLS)
    /// whose audio is AAC Main and whose URL does not yet say whether to copy
    /// the audio.
    /// </summary>
    public static bool NeedsAudioReencode(MediaSourceInfo source)
        => source.IsInfiniteStream
           && !string.IsNullOrEmpty(source.TranscodingUrl)
           && source.TranscodingUrl.IndexOf("allowaudiostreamcopy=", StringComparison.OrdinalIgnoreCase) < 0
           && source.MediaStreams?.Any(IsAacMain) == true;

    /// <summary>Whether a stream is AAC audio of the Main profile.</summary>
    public static bool IsAacMain(MediaStream stream)
        => stream.Type == MediaStreamType.Audio
           && string.Equals(stream.Codec, "aac", StringComparison.OrdinalIgnoreCase)
           && string.Equals(stream.Profile, "Main", StringComparison.OrdinalIgnoreCase);

    /// <summary>The URL with <c>AllowAudioStreamCopy=false</c> added to its query.</summary>
    public static string WithoutAudioCopy(string url)
    {
        var hash = url.IndexOf('#', StringComparison.Ordinal);
        var path = hash < 0 ? url : url[..hash];
        var fragment = hash < 0 ? string.Empty : url[hash..];
        var separator = path.Contains('?', StringComparison.Ordinal)
            ? (path.EndsWith('?') || path.EndsWith('&') ? string.Empty : "&")
            : "?";
        return path + separator + NoAudioCopy + fragment;
    }

    // Whether the user the stream is for may have its audio re-encoded. As
    // Jellyfin decides it: PlaybackInfo is for the userId it names (query or
    // body), else for the caller. No user (an API key without one): Jellyfin
    // applies no permission, nor do we.
    private bool MayTranscodeAudio(ActionExecutingContext context)
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
        return user is null || user.HasPermission(PermissionKind.EnableAudioPlaybackTranscoding);
    }
}

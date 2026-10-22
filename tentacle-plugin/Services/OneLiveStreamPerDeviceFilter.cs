using System.Collections.Concurrent;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.LiveTv;
using MediaBrowser.Controller.MediaEncoding;
using MediaBrowser.Controller.Session;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.MediaInfo;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Controllers;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.Tentacle.Services;

/// <summary>
/// One device holds at most one Live TV stream: when a device opens a
/// channel, the live streams it opened before are closed first (their
/// transcodes stopped), so a channel switch never holds two provider
/// connections, not even for the seconds until the old player reports that
/// it stopped.
///
/// Why: many IPTV accounts allow one connection at a time. Jellyfin opens
/// the new channel's stream on <c>PlaybackInfo</c> and closes the old one
/// only when the client reports the old play stopped. Jellyfin Web opens
/// first and stops after; a player that is still retrying, or one left in
/// another tab of the same browser, may never report. Meanwhile the account
/// has two streams: the provider refuses or cuts the new one, Jellyfin
/// Web's retries restart it every couple of seconds (the "plays half a
/// second, then loading" loop) and the old one keeps playing unseen.
///
/// How: an MVC filter on <c>POST /Items/{id}/PlaybackInfo</c> (asked to
/// open the stream: AutoOpenLiveStream) and
/// <c>/LiveStreams/Open</c>. Before Jellyfin opens a channel for a device
/// (the device id of the caller's token), it kills that device's
/// transcodes of the streams it remembers for the device and closes those
/// streams; after Jellyfin answered, it remembers the live stream the
/// answer opened. A request that names a live stream it already has (a
/// retry with other settings, a seek) reuses that stream and closes
/// nothing. Devices are separate: two people, or one person on two
/// devices, each keep their own stream. Tabs of one browser share a device
/// id: the newest channel wins.
/// </summary>
public sealed class OneLiveStreamPerDeviceFilter : IAsyncActionFilter
{
    private static readonly HashSet<(string Controller, string Action)> Actions = new()
    {
        ("MediaInfo", "GetPostedPlaybackInfo"),
        ("MediaInfo", "OpenLiveStream"),
    };

    // The claim Jellyfin's authentication puts the caller's device id in
    // (Jellyfin.Api's InternalClaimTypes.DeviceId, not referenced by plugins).
    private const string DeviceIdClaim = "Jellyfin-DeviceId";

    private readonly ILibraryManager _libraryManager;
    private readonly IMediaSourceManager _mediaSourceManager;
    private readonly ISessionManager _sessionManager;
    private readonly ITranscodeManager _transcodeManager;
    private readonly ILogger<OneLiveStreamPerDeviceFilter> _logger;

    // device id → the live streams Jellyfin opened for it (live stream id → play session id)
    private readonly ConcurrentDictionary<string, ConcurrentDictionary<string, string>> _streams =
        new(StringComparer.OrdinalIgnoreCase);

    // One channel open at a time per device (a double click opens two otherwise)
    private readonly ConcurrentDictionary<string, SemaphoreSlim> _locks = new(StringComparer.OrdinalIgnoreCase);

    public OneLiveStreamPerDeviceFilter(
        ILibraryManager libraryManager,
        IMediaSourceManager mediaSourceManager,
        ISessionManager sessionManager,
        ITranscodeManager transcodeManager,
        ILogger<OneLiveStreamPerDeviceFilter> logger)
    {
        _libraryManager = libraryManager;
        _mediaSourceManager = mediaSourceManager;
        _sessionManager = sessionManager;
        _transcodeManager = transcodeManager;
        _logger = logger;
    }

    /// <inheritdoc />
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        string? deviceId = null;
        try
        {
            if (context.ActionDescriptor is ControllerActionDescriptor action
                && Actions.Contains((action.ControllerName, action.ActionName))
                && OpensChannel(context.ActionArguments))
            {
                deviceId = context.HttpContext.User.FindFirst(DeviceIdClaim)?.Value;
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "[Tentacle] Could not check whether a play request opens a Live TV channel");
        }

        if (string.IsNullOrEmpty(deviceId))
        {
            await next().ConfigureAwait(false);
            return;
        }

        var gate = _locks.GetOrAdd(deviceId, _ => new SemaphoreSlim(1, 1));
        await gate.WaitAsync().ConfigureAwait(false);
        try
        {
            await CloseStreamsOf(deviceId).ConfigureAwait(false);
            var executed = await next().ConfigureAwait(false);
            Remember(deviceId, executed);
        }
        finally
        {
            gate.Release();
        }
    }

    // Whether the request opens a Live TV channel's stream: the item is a
    // channel, PlaybackInfo is asked to open it, and the request doesn't name
    // a live stream it already has.
    private bool OpensChannel(IDictionary<string, object?> args)
    {
        var body = args.TryGetValue("playbackInfoDto", out var b) ? b
            : args.TryGetValue("openLiveStreamDto", out var o) ? o : null;

        var liveStreamId = args.TryGetValue("liveStreamId", out var q) ? q as string : null;
        if (string.IsNullOrEmpty(liveStreamId) && body is not null)
        {
            liveStreamId = body.GetType().GetProperty("LiveStreamId")?.GetValue(body) as string;
        }

        if (!string.IsNullOrEmpty(liveStreamId))
        {
            return false;
        }

        // PlaybackInfo opens a stream only when asked to (AutoOpenLiveStream,
        // query or body): one that only asks about the channel closes nothing
        if (args.ContainsKey("playbackInfoDto") || args.ContainsKey("autoOpenLiveStream"))
        {
            var autoOpen = args.TryGetValue("autoOpenLiveStream", out var a) && a is true;
            if (!autoOpen && body is not null && body.GetType().GetProperty("AutoOpenLiveStream")?.GetValue(body) is true)
            {
                autoOpen = true;
            }

            if (!autoOpen)
            {
                return false;
            }
        }

        Guid itemId = Guid.Empty;
        if (args.TryGetValue("itemId", out var id) && id is Guid fromRoute)
        {
            itemId = fromRoute;
        }
        else if (body?.GetType().GetProperty("ItemId")?.GetValue(body) is Guid fromBody)
        {
            itemId = fromBody;
        }

        return itemId != Guid.Empty && _libraryManager.GetItemById(itemId) is LiveTvChannel;
    }

    private async Task CloseStreamsOf(string deviceId)
    {
        if (!_streams.TryGetValue(deviceId, out var open) || open.IsEmpty)
        {
            return;
        }

        foreach (var (liveStreamId, playSessionId) in open.ToArray())
        {
            open.TryRemove(liveStreamId, out _);
            if (_mediaSourceManager.GetLiveStreamInfo(liveStreamId) is null)
            {
                continue; // already closed (the player stopped)
            }

            try
            {
                _logger.LogInformation(
                    "[Tentacle] Device {Device} opens another channel: closing its previous live stream {LiveStream}",
                    deviceId,
                    liveStreamId);
                await _transcodeManager.KillTranscodingJobs(deviceId, playSessionId, _ => true).ConfigureAwait(false);

                // Through the session manager when a player reported this
                // stream (it then won't close it a second time on its stop);
                // else straight away.
                await _sessionManager.CloseLiveStreamIfNeededAsync(liveStreamId, playSessionId).ConfigureAwait(false);
                if (_mediaSourceManager.GetLiveStreamInfo(liveStreamId) is not null)
                {
                    await _mediaSourceManager.CloseLiveStream(liveStreamId).ConfigureAwait(false);
                }
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "[Tentacle] Could not close live stream {LiveStream}", liveStreamId);
            }
        }
    }

    private void Remember(string deviceId, ActionExecutedContext executed)
    {
        if (executed.Result is not ObjectResult { Value: { } value })
        {
            return;
        }

        var opened = new List<(string LiveStreamId, string PlaySessionId)>();
        switch (value)
        {
            case PlaybackInfoResponse info:
                foreach (var source in info.MediaSources ?? Array.Empty<MediaSourceInfo>())
                {
                    if (!string.IsNullOrEmpty(source.LiveStreamId))
                    {
                        opened.Add((source.LiveStreamId, info.PlaySessionId ?? string.Empty));
                    }
                }

                break;
            case LiveStreamResponse live when !string.IsNullOrEmpty(live.MediaSource?.LiveStreamId):
                opened.Add((live.MediaSource.LiveStreamId, string.Empty));
                break;
        }

        if (opened.Count == 0)
        {
            return;
        }

        var open = _streams.GetOrAdd(deviceId, _ => new ConcurrentDictionary<string, string>(StringComparer.OrdinalIgnoreCase));
        foreach (var (liveStreamId, playSessionId) in opened)
        {
            open[liveStreamId] = playSessionId;
        }
    }
}

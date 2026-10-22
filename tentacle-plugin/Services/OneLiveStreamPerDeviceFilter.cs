using System.Collections.Concurrent;
using MediaBrowser.Controller;
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
/// nothing, and so does a request for the channel the device already has
/// (the TV app asks afresh, without the stream's id, for an audio track or
/// subtitle change). A stream other devices share is only given up by this
/// one (its consumer count goes down once). API-key callers without a
/// device id are left alone. Devices are separate: two people, or one
/// person on two devices, each keep their own stream. Tabs of one browser
/// share a device id: the newest channel wins.
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
    private readonly IServerApplicationHost _appHost;
    private readonly ILogger<OneLiveStreamPerDeviceFilter> _logger;

    // device id → the live streams Jellyfin opened for it, by live stream id
    private readonly ConcurrentDictionary<string, ConcurrentDictionary<string, Opened>> _streams =
        new(StringComparer.OrdinalIgnoreCase);

    // One channel open at a time per device (a double click opens two otherwise)
    private readonly ConcurrentDictionary<string, SemaphoreSlim> _locks = new(StringComparer.OrdinalIgnoreCase);

    public OneLiveStreamPerDeviceFilter(
        ILibraryManager libraryManager,
        IMediaSourceManager mediaSourceManager,
        ISessionManager sessionManager,
        ITranscodeManager transcodeManager,
        IServerApplicationHost appHost,
        ILogger<OneLiveStreamPerDeviceFilter> logger)
    {
        _libraryManager = libraryManager;
        _mediaSourceManager = mediaSourceManager;
        _sessionManager = sessionManager;
        _transcodeManager = transcodeManager;
        _appHost = appHost;
        _logger = logger;
    }

    // A live stream a device opened: its play session and its channel
    private sealed record Opened(string PlaySessionId, Guid ChannelId);

    /// <inheritdoc />
    public async Task OnActionExecutionAsync(ActionExecutingContext context, ActionExecutionDelegate next)
    {
        string? deviceId = null;
        var channelId = Guid.Empty;
        var playSessionId = string.Empty;
        try
        {
            if (context.ActionDescriptor is ControllerActionDescriptor action
                && Actions.Contains((action.ControllerName, action.ActionName)))
            {
                channelId = OpenedChannel(action.ActionName, context.ActionArguments, out playSessionId);
                deviceId = channelId == Guid.Empty ? null : context.HttpContext.User.FindFirst(DeviceIdClaim)?.Value;

                // An API key without a device id gets the server's own id:
                // all such callers would be one "device"
                if (string.Equals(deviceId, _appHost.SystemId, StringComparison.OrdinalIgnoreCase))
                {
                    deviceId = null;
                }
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
            await CloseStreamsOf(deviceId, channelId).ConfigureAwait(false);
            var executed = await next().ConfigureAwait(false);
            Remember(deviceId, channelId, playSessionId, executed);
        }
        finally
        {
            gate.Release();
        }
    }

    // The channel the request opens a stream of, or Guid.Empty: the item is
    // a channel, PlaybackInfo is asked to open it (AutoOpenLiveStream), and
    // the request doesn't name a live stream it already has.
    private Guid OpenedChannel(string actionName, IDictionary<string, object?> args, out string playSessionId)
    {
        playSessionId = string.Empty;
        var body = args.TryGetValue("playbackInfoDto", out var b) ? b
            : args.TryGetValue("openLiveStreamDto", out var o) ? o : null;

        var liveStreamId = args.TryGetValue("liveStreamId", out var q) ? q as string : null;
        if (string.IsNullOrEmpty(liveStreamId) && body is not null)
        {
            liveStreamId = body.GetType().GetProperty("LiveStreamId")?.GetValue(body) as string;
        }

        if (!string.IsNullOrEmpty(liveStreamId))
        {
            return Guid.Empty;
        }

        if (actionName == "GetPostedPlaybackInfo")
        {
            // only asked about the channel: nothing opens
            var autoOpen = (args.TryGetValue("autoOpenLiveStream", out var a) && a is true)
                || body?.GetType().GetProperty("AutoOpenLiveStream")?.GetValue(body) is true;
            if (!autoOpen)
            {
                return Guid.Empty;
            }
        }
        else
        {
            // LiveStreams/Open names its play session (PlaybackInfo's comes with the answer)
            playSessionId = (args.TryGetValue("playSessionId", out var ps) ? ps as string : null)
                ?? body?.GetType().GetProperty("PlaySessionId")?.GetValue(body) as string
                ?? string.Empty;
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

        return itemId != Guid.Empty && _libraryManager.GetItemById(itemId) is LiveTvChannel ? itemId : Guid.Empty;
    }

    private async Task CloseStreamsOf(string deviceId, Guid channelId)
    {
        if (!_streams.TryGetValue(deviceId, out var open) || open.IsEmpty)
        {
            return;
        }

        foreach (var (liveStreamId, opened) in open.ToArray())
        {
            // The same channel again (the TV app asks afresh for an audio
            // track or subtitle change): Jellyfin shares the open stream
            if (opened.ChannelId == channelId)
            {
                continue;
            }

            open.TryRemove(liveStreamId, out _);
            var stream = _mediaSourceManager.GetLiveStreamInfo(liveStreamId);
            if (stream is null)
            {
                continue; // already closed (the player stopped)
            }

            try
            {
                _logger.LogInformation(
                    "[Tentacle] Device {Device} opens another channel: closing its previous live stream {LiveStream}",
                    deviceId,
                    liveStreamId);
                if (!string.IsNullOrEmpty(opened.PlaySessionId))
                {
                    await _transcodeManager.KillTranscodingJobs(deviceId, opened.PlaySessionId, _ => true).ConfigureAwait(false);
                }

                // This device's share of the stream, once: through the session
                // manager when a player reported it (it then won't close it a
                // second time on its stop), else straight away. A stream
                // others share stays open for them.
                var consumers = stream.ConsumerCount;
                if (!string.IsNullOrEmpty(opened.PlaySessionId))
                {
                    await _sessionManager.CloseLiveStreamIfNeededAsync(liveStreamId, opened.PlaySessionId).ConfigureAwait(false);
                }

                if (_mediaSourceManager.GetLiveStreamInfo(liveStreamId) is not null && stream.ConsumerCount == consumers)
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

    private void Remember(string deviceId, Guid channelId, string playSessionId, ActionExecutedContext executed)
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
                opened.Add((live.MediaSource.LiveStreamId, playSessionId));
                break;
        }

        if (opened.Count == 0)
        {
            return;
        }

        var open = _streams.GetOrAdd(deviceId, _ => new ConcurrentDictionary<string, Opened>(StringComparer.OrdinalIgnoreCase));
        foreach (var (liveStreamId, session) in opened)
        {
            // a stream the device already had (the same channel again) keeps its first play session
            open.TryAdd(liveStreamId, new Opened(session, channelId));
        }
    }
}

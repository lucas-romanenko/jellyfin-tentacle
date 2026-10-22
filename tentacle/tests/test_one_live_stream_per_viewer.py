"""One person watching holds one provider connection (Lucas, 22 Oct 2026).

Seen on Jellyfin Web: a channel opened from the Tentacle guide started
playing in another Jellyfin tab of the same browser (sound with no picture),
a remote Stop reached the wrong tab, and on a channel switch the old stream
stayed open until the old player reported that it stopped, so a
one-connection account had two streams: the provider cut the new one and
Jellyfin Web's retries restarted it every couple of seconds.

- The guide plays in its own tab: it hands Jellyfin Web's own message
  handler a local Play message instead of asking the server to play
  (sendPlayCommand goes to the browser's newest websocket, any tab).
- The plugin's OneLiveStreamPerDeviceFilter closes a device's previous live
  stream before that device opens another channel.
- The guide stops a live video still playing after the player page closed.

There is no C# or browser test host in this repo: this reads the sources the
way test_vod_hls_no_video_copy.py does.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

GUIDE = Path("../tentacle-plugin/Inject/tentacle-livetv.js")
FILTER = Path("../tentacle-plugin/Services/OneLiveStreamPerDeviceFilter.cs")
REGISTRATOR = Path("../tentacle-plugin/PluginServiceRegistrator.cs")


class GuidePlaysHere(unittest.TestCase):
    def setUp(self):
        self.src = GUIDE.read_text(encoding="utf-8")

    def test_never_asks_the_server_to_play(self):
        code = "\n".join(l for l in self.src.splitlines() if not l.strip().startswith("//"))
        self.assertNotIn("sendPlayCommand", code)
        self.assertNotIn("/Playing?", code)

    def test_plays_through_jellyfin_webs_own_message_handler(self):
        self.assertRegex(self.src, r"ev\.trigger\(a, 'message', \[\{ MessageType: 'Play'")
        self.assertIn("PlayCommand: 'PlayNow'", self.src)

    def test_other_pages_get_the_same_local_player(self):
        # tentacle-details.js / tentacle-search.js look for a global playbackManager
        self.assertIn("window.playbackManager = {", self.src)


class LeftoverPlayerStopped(unittest.TestCase):
    def setUp(self):
        self.src = GUIDE.read_text(encoding="utf-8")

    def test_only_off_the_player_page_and_not_picture_in_picture(self):
        self.assertIn("function onPlayerPage() { return /^#\\/video\\b/.test(location.hash); }", self.src)
        self.assertIn("v === document.pictureInPictureElement", self.src)

    def test_only_live_tv(self):
        # a theme video or other video is left alone: hls.js (blob:) after a live playlist
        self.assertIn("live\\.m3u8|[?&]LiveStreamId=", self.src)
        self.assertIn("!/^blob:/.test(v.currentSrc || '')", self.src)

    def test_stopped_like_a_remote_stop_then_taken_off(self):
        self.assertIn("MessageType: 'Playstate', Data: { Command: 'Stop' }", self.src)
        self.assertIn("v.pause(); v.removeAttribute('src'); v.load();", self.src)

    def test_watches_media_events_in_the_capture_phase(self):
        self.assertIn("document.addEventListener('playing', watchPlayerPage, true);", self.src)
        # the player page opens with pushState: no hashchange, so the route is watched
        self.assertIn("setInterval(watchPlayerPage, 500);", self.src)

    def test_a_starting_play_is_left_alone(self):
        # only after the player page was left, and only a video that runs on off it
        self.assertIn("if (!leftPlayerAt || onPlayerPage()) return out;", self.src)
        self.assertIn("now - v.__tgOff.at >= STRAY_MS && v.currentTime - v.__tgOff.t >= 0.5", self.src)


class OneStreamPerDevice(unittest.TestCase):
    def setUp(self):
        self.src = FILTER.read_text(encoding="utf-8")

    def test_registered_as_an_mvc_filter(self):
        reg = REGISTRATOR.read_text(encoding="utf-8")
        self.assertIn("AddSingleton<OneLiveStreamPerDeviceFilter>()", reg)
        self.assertIn("options.Filters.AddService<OneLiveStreamPerDeviceFilter>()", reg)

    def test_on_the_requests_that_open_a_channel(self):
        for action in ('("MediaInfo", "GetPostedPlaybackInfo")', '("MediaInfo", "OpenLiveStream")'):
            self.assertIn(action, self.src)
        self.assertIn("is LiveTvChannel", self.src)

    def test_only_when_playback_info_opens_the_stream(self):
        # GET PlaybackInfo and a POST without AutoOpenLiveStream open nothing
        self.assertNotIn('("MediaInfo", "GetPlaybackInfo")', self.src)
        body = self.src[self.src.index("private Guid OpenedChannel"):]
        self.assertIn('if (actionName == "GetPostedPlaybackInfo")', body)
        self.assertIn('GetProperty("AutoOpenLiveStream")', body)
        self.assertRegex(body, r"if \(!autoOpen\)\s*\{\s*return Guid\.Empty;")

    def test_by_the_callers_device(self):
        self.assertIn('DeviceIdClaim = "Jellyfin-DeviceId"', self.src)

    def test_a_retry_on_its_own_stream_closes_nothing(self):
        body = self.src[self.src.index("private Guid OpenedChannel"):]
        self.assertRegex(body, r"if \(!string\.IsNullOrEmpty\(liveStreamId\)\)\s*\{\s*return Guid\.Empty;")

    def test_the_same_channel_again_closes_nothing(self):
        # the TV app asks afresh (no liveStreamId) for an audio or subtitle change
        body = self.src[self.src.index("private async Task CloseStreamsOf"):]
        self.assertRegex(body, r"if \(opened\.ChannelId == channelId\)\s*\{[^}]*continue;")

    def test_api_key_callers_are_not_one_device(self):
        self.assertIn("string.Equals(deviceId, _appHost.SystemId, StringComparison.OrdinalIgnoreCase)", self.src)

    def test_closes_before_jellyfin_opens_and_remembers_after(self):
        body = self.src[self.src.index("public async Task OnActionExecutionAsync"):]
        close, run, keep = (body.index(s) for s in ("await CloseStreamsOf(deviceId, channelId)",
                                                     "var executed = await next()", "Remember(deviceId"))
        self.assertLess(close, run)
        self.assertLess(run, keep)

    def test_stops_its_own_transcode_and_gives_up_its_share_once(self):
        body = self.src[self.src.index("private async Task CloseStreamsOf"):]
        # by play session only: an empty one would match every transcode of the device
        self.assertRegex(body, r"if \(!string\.IsNullOrEmpty\(opened\.PlaySessionId\)\)\s*\{\s*"
                               r"await _transcodeManager\.KillTranscodingJobs\(deviceId, opened\.PlaySessionId")
        self.assertIn("_sessionManager.CloseLiveStreamIfNeededAsync(liveStreamId, opened.PlaySessionId)", body)
        # straight away only if the session manager didn't take this device's share
        self.assertIn("var consumers = stream.ConsumerCount;", body)
        self.assertIn("stream.ConsumerCount == consumers", body)

    def test_live_streams_open_keeps_its_play_session(self):
        self.assertIn('args.TryGetValue("playSessionId", out var ps)', self.src)
        self.assertIn("opened.Add((live.MediaSource.LiveStreamId, playSessionId));", self.src)

    def test_never_fails_a_play(self):
        self.assertIn('"[Tentacle] Could not close live stream {LiveStream}"', self.src)
        self.assertTrue(re.search(r"catch \(Exception ex\)\s*\{\s*_logger\.LogWarning\(ex, \"\[Tentacle\] Could not check",
                                  self.src))


if __name__ == "__main__":
    unittest.main()

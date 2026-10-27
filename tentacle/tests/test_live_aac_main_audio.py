"""A browser gets a live channel's AAC Main audio re-encoded from the start.

Some channels carry AAC Main audio. Jellyfin copied it into the HLS stream
it served Jellyfin Web, Chrome on a Mac could not decode it, and Jellyfin
Web's error handling (hls.js recovery twice, then PlaybackInfo again with
allowVideoStreamCopy=false, then with allowAudioStreamCopy=false as well)
started three ffmpegs: the picture flashed on and off for about 10 s before
the channel played, and played with its 1080p60 video re-encoded. Every
flickering open in Jellyfin's log of 26-27 Oct was AAC Main; no AAC LC
channel ever did it.

The plugin's `LiveAacMainAudioFilter` adds AllowAudioStreamCopy=false to the
TranscodingUrl of a live media source with AAC Main audio, for Jellyfin Web
only. There is no C# test host in this repo, so this reads the filter's
source the way test_vod_hls_no_video_copy.py does. Checked end to end on a
local Jellyfin 10.11.8 with a fake AAC Main channel: one ffmpeg, video
copied, audio encoded; an AAC LC channel and a TV-app client unchanged.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

FILTER = Path("../tentacle-plugin/Services/LiveAacMainAudioFilter.cs")
REGISTRATOR = Path("../tentacle-plugin/PluginServiceRegistrator.cs")


def member(src: str, signature: str) -> str:
    start = src.index(signature)
    rest = src[start:]
    end = re.search(r"\n    (?:private|public|protected|internal|/// )", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestLiveAacMainAudioFilter(unittest.TestCase):
    def setUp(self):
        self.src = re.sub(r"//[^\n]*", "", FILTER.read_text(encoding="utf-8"))
        self.execution = member(self.src, "public async Task OnActionExecutionAsync(")

    def test_it_covers_the_requests_that_open_a_live_stream(self):
        actions = set(re.findall(r'\("(\w+)",\s*"(\w+)"\)', self.src))
        self.assertEqual(actions, {
            ("MediaInfo", "GetPostedPlaybackInfo"),
            ("MediaInfo", "OpenLiveStream"),
        })

    def test_only_jellyfin_web_is_changed(self):
        # The TV app plays AAC Main itself: its stream stays as it was.
        self.assertRegex(self.src, r'WebClient\s*=\s*"Jellyfin Web"')
        self.assertRegex(self.src, r'ClientClaim\s*=\s*"Jellyfin-Client"')
        guard = self.execution.index("WebClient")
        self.assertLess(guard, self.execution.index("var executed = await next()"))

    def test_it_changes_the_answer_after_jellyfin_opened_the_stream(self):
        # Only then are the stream's codecs and profile known.
        after = self.execution.index("var executed = await next()")
        self.assertLess(after, self.execution.index("source.TranscodingUrl = WithoutAudioCopy("))
        self.assertIn("PlaybackInfoResponse info => info.MediaSources", self.execution)
        self.assertIn("LiveStreamResponse { MediaSource: { } live }", self.execution)

    def test_only_live_aac_main_with_audio_copy_still_open_is_changed(self):
        check = member(self.src, "public static bool NeedsAudioReencode(")
        self.assertIn("source.IsInfiniteStream", check, "Live TV and recordings in progress only")
        self.assertIn("!string.IsNullOrEmpty(source.TranscodingUrl)", check, "direct play is left alone")
        self.assertIn('"allowaudiostreamcopy="', check, "a URL that already decides audio copy is kept")
        self.assertIn("StringComparison.OrdinalIgnoreCase", check)
        self.assertIn("MediaStreams?.Any(IsAacMain)", check)
        aac = member(self.src, "public static bool IsAacMain(")
        self.assertIn("MediaStreamType.Audio", aac)
        self.assertIn('"aac"', aac)
        self.assertIn('"Main"', aac, "AAC LC, HE-AAC and the rest keep being copied")

    def test_it_only_turns_audio_copy_off(self):
        self.assertRegex(self.src, r'NoAudioCopy\s*=\s*"AllowAudioStreamCopy=false"')
        for untouched in ("AllowVideoStreamCopy", "EnableDirectPlay", "EnableDirectStream", "EnableTranscoding"):
            self.assertNotIn(untouched, self.src, "the video stays copied")

    def test_the_flag_goes_into_the_query(self):
        build = member(self.src, "public static string WithoutAudioCopy(")
        self.assertIn("'#'", build, "before a fragment, if any")
        self.assertIn('"&"', build)
        self.assertIn('"?"', build)

    def test_a_user_who_may_not_transcode_audio_is_left_alone(self):
        guard = self.execution.index("MayTranscodeAudio(context)")
        self.assertLess(guard, self.execution.index("source.TranscodingUrl = WithoutAudioCopy("))
        check = member(self.src, "private bool MayTranscodeAudio(")
        self.assertIn("PermissionKind.EnableAudioPlaybackTranscoding", check)
        self.assertIn('GetProperty("UserId")', check, "PlaybackInfo's body names the user")
        self.assertIn("UserIdClaim", check)

    def test_a_failure_never_breaks_the_play(self):
        self.assertRegex(self.execution, r"(?s)var executed = await next\(\).*try\s*\{.*\}\s*catch \(Exception ex\)")
        self.assertNotRegex(self.execution, r"\bthrow\b")
        self.assertNotRegex(self.src, r"context\.Result\s*=|executed\.Result\s*=", "the answer is Jellyfin's, only its URL changes")
        self.assertEqual(len(re.findall(r"await next\(\)", self.execution)), 2, "next() runs exactly once on each path")

    def test_no_blocking_on_the_request_path(self):
        self.assertNotRegex(self.src, r"\.GetAwaiter\(\)\.GetResult\(\)|\.Wait\(\)")

    def test_it_is_registered_as_a_global_mvc_filter(self):
        reg = REGISTRATOR.read_text(encoding="utf-8")
        self.assertRegex(reg, r"AddSingleton<LiveAacMainAudioFilter>\(\)")
        self.assertRegex(reg, r"Configure<MvcOptions>\(\s*options\s*=>\s*options\.Filters\.AddService<LiveAacMainAudioFilter>\(\)\s*\)")


if __name__ == "__main__":
    unittest.main()

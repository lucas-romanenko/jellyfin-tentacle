"""Tentacle VOD played as HLS re-encodes its video instead of copying it.

Jellyfin Web resumed a provider episode at 15:00 and, a few minutes later,
played the next episode (jellyfin/jellyfin#17966). The web player got an HLS
stream with the video copied and only the audio transcoded; ffmpeg then cut
the segments at the source's keyframes (about 2.7 s) while the playlist said
6 s each, so 50 segments read as 300 s but held under 3 minutes of media. The
player ran off the end of what was written and decided the episode was over.
With `allowVideoStreamCopy=false` the same resume writes as much media as the
playlist says.

The plugin's `VodNoVideoCopyFilter` (an MVC action filter) sets that argument
on PlaybackInfo and on the HLS video requests, for .strm items whose address
is Tentacle's signed /api/vod/ link only. There is no C# test host in this
repo, so this reads the filter's source the way test_delete_handler_rereads.py
does, and runs its URL pattern against links the server really writes.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

from services import vod_tokens

FILTER = Path("../tentacle-plugin/Services/VodNoVideoCopyFilter.cs")
REGISTRATOR = Path("../tentacle-plugin/PluginServiceRegistrator.cs")


def method(src: str, signature: str) -> str:
    start = src.index(signature)
    rest = src[start:]
    end = re.search(r"\n    (?:private|public|protected|internal|/// )", rest[1:])
    return rest[: end.start() + 1] if end else rest


def csharp_pattern(src: str) -> re.Pattern:
    """The filter's TentacleVodUrl regex, compiled as Python (the syntax it
    uses means the same in both) with the same IgnoreCase option."""
    m = re.search(r'TentacleVodUrl\s*=\s*new\(\s*@"((?:[^"]|"")*)"', src)
    assert m, "TentacleVodUrl is not a verbatim-string Regex"
    return re.compile(m.group(1).replace('""', '"'), re.IGNORECASE)


class TestTentacleVodUrl(unittest.TestCase):
    def setUp(self):
        self.src = FILTER.read_text(encoding="utf-8")
        self.pattern = csharp_pattern(self.src)
        self.secret = "0" * 64

    def assertVod(self, url):
        self.assertTrue(self.pattern.match(url), f"not taken for Tentacle VOD: {url}")

    def assertNotVod(self, url):
        self.assertIsNone(self.pattern.match(url), f"taken for Tentacle VOD: {url}")

    def test_links_the_sync_writes_match(self):
        for base in ("http://192.0.2.10:8888", "https://tentacle.example.org",
                     "http://tentacle:8888/", "https://example.org/tentacle"):
            links = vod_tokens.Links(base, self.secret, 3)
            self.assertVod(links.movie(12345, "mkv"))
            self.assertVod(links.episode(987654, "mp4"))
            self.assertVod(vod_tokens.url(base, self.secret, 7, "series", 42, "ts"))

    def test_the_servers_own_pattern_agrees(self):
        link = vod_tokens.url("http://192.0.2.10:8888", self.secret, 2, "movie", 55, "mp4")
        self.assertIsNotNone(vod_tokens._TOKEN_URL.search(link))
        self.assertVod(link)

    def test_other_addresses_do_not_match(self):
        for url in (
            # a .strm straight at the provider (VOD through Tentacle turned off)
            "http://provider.example:8080/movie/user/pass/12345.mkv",
            "http://provider.example:8080/series/user/pass/987.mp4",
            # Live TV and recordings through Tentacle's tuner
            "http://192.0.2.10:8888/api/livetv/stream/1/2.ts",
            "http://192.0.2.10:8888/auto/v101",
            # a library file, and a local path that happens to contain the route
            "/media/Movies/Film (2020)/Film (2020).mkv",
            "/data/api/vod/movie/1.2.abcdef.mkv",
            # not a signed token
            "http://192.0.2.10:8888/api/vod/movie/12345.mkv",
            "http://192.0.2.10:8888/api/vod/music/1.2.abcdef.mp3",
            "http://192.0.2.10:8888/api/vod/movie/1.2.xyz.mkv",
            "",
        ):
            self.assertNotVod(url)

    def test_the_check_needs_a_strm_item(self):
        check = re.search(r"public static bool IsTentacleVod\(BaseItem item\)\s*=>([^;]+);", self.src)
        self.assertIsNotNone(check)
        self.assertIn("IsShortcut: true", check.group(1), "a library file is not a .strm")
        self.assertIn("IsTentacleVodUrl(item.ShortcutPath)", check.group(1))


class TestVodNoVideoCopyFilter(unittest.TestCase):
    def setUp(self):
        self.src = re.sub(r"//[^\n]*", "", FILTER.read_text(encoding="utf-8"))
        self.executing = method(self.src, "public void OnActionExecuting(")

    def test_it_covers_playback_info_and_every_hls_video_request(self):
        actions = set(re.findall(r'\("(\w+)",\s*"(\w+)"\)', self.src))
        self.assertEqual(actions, {
            ("MediaInfo", "GetPostedPlaybackInfo"),
            ("DynamicHls", "GetMasterHlsVideoPlaylist"),
            ("DynamicHls", "GetVariantHlsVideoPlaylist"),
            ("DynamicHls", "GetHlsVideoSegment"),
        }, "the playlist and its segments must agree: master, main and segments all get the flag")

    def test_it_only_turns_video_copy_off(self):
        self.assertRegex(self.src, r'CopyArgument\s*=\s*"allowVideoStreamCopy"')
        writes = re.findall(r"args\[([^\]]+)\]\s*=\s*([^;]+);", self.executing)
        self.assertEqual(writes, [("CopyArgument", "false")],
                         "only allowVideoStreamCopy changes; direct play, audio copy and the rest stay Jellyfin's")
        for untouched in ("enableDirectPlay", "enableDirectStream", "allowAudioStreamCopy", "enableTranscoding"):
            self.assertNotIn(untouched, self.src)

    def test_only_tentacle_vod_is_changed(self):
        guard = self.executing.index("IsTentacleVod(item)")
        self.assertLess(guard, self.executing.index("args[CopyArgument] = false"))
        self.assertRegex(self.executing, r"if \(item is null \|\| !IsTentacleVod\(item\)\)\s*\{\s*return;")

    def test_the_version_played_is_the_one_checked(self):
        self.assertIn("PlayedItem(itemId, MediaSourceId(args))", self.executing)
        source_id = method(self.src, "private static string? MediaSourceId(")
        self.assertIn('"mediaSourceId"', source_id)
        self.assertIn('"playbackInfoDto"', source_id, "PlaybackInfo sends the media source in its body")

    def test_a_failure_never_breaks_the_play(self):
        self.assertRegex(self.executing, r"(?s)try\s*\{.*\}\s*catch \(Exception ex\)")
        self.assertNotRegex(self.executing, r"\bthrow\b")
        self.assertNotRegex(self.src, r"context\.Result\s*=", "the filter must never answer the request itself")

    def test_no_blocking_on_the_request_path(self):
        self.assertNotRegex(self.src, r"\.GetAwaiter\(\)\.GetResult\(\)|\.Result\b|\.Wait\(\)")

    def test_it_is_registered_as_a_global_mvc_filter(self):
        reg = REGISTRATOR.read_text(encoding="utf-8")
        self.assertRegex(reg, r"AddSingleton<VodNoVideoCopyFilter>\(\)")
        self.assertRegex(reg, r"Configure<MvcOptions>\(\s*options\s*=>\s*options\.Filters\.AddService<VodNoVideoCopyFilter>\(\)\s*\)")


if __name__ == "__main__":
    unittest.main()

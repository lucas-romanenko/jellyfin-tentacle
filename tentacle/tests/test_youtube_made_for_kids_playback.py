"""A YouTube video "made for kids" never plays: its master.m3u8 answers 502.

YouTube refuses such a video to the visionos client (UNPLAYABLE). yt-dlp then
asks web_embedded on its own, the only client that plays it. With the image as
built that gives nothing the resolver can serve, so every press of play is
"web_embedded returned no usable HLS" and a 502, for good:

- web_embedded needs YouTube's JavaScript challenge solved. The image has
  Deno, but yt-dlp also needs its solver scripts, the yt-dlp-ejs package, and
  requirements.txt does not install it. yt-dlp says "n challenge solving
  failed" and returns no formats at all.
- With the solver, yt-dlp 2026.8.19 gets web_embedded DASH and one 360p
  progressive file only, never HLS. 2026.10.21 sends web_embedded with a Safari
  user agent (yt-dlp #17684), and YouTube then answers with an HLS master:
  itags 91-96, H.264 up to 1080p with the audio muxed in, MPEG-TS segments,
  #EXT-X-ENDLIST. That is the shape the resolver already serves.

Both are needed: either one alone still ends in the 502 (checked against real
YouTube with a made-for-kids video).

Run from tentacle/:  python tests/hermetic.py discover -s tests -p "test_youtube_made_for_kids_playback.py"
The last test asks real YouTube: the same with TENTACLE_LIVE_TESTS=1 (and Deno on PATH).
"""
import os
import unittest
from unittest import mock

import yt_dlp
from yt_dlp.utils import version_tuple

from hermetic import live
from services.youtube import resolver, traffic
from tmp_dirs import temp_dir

# The first yt-dlp whose web_embedded client gets HLS.
WEB_EMBEDDED_HLS_SINCE = "2026.10.21"

# A well-known video that is made for kids.
KIDS_VIDEO = "XqZsoesa55w"


def _hls(itag, height, codecs):
    return {"format_id": str(itag), "protocol": "m3u8_native", "ext": "mp4", "height": height,
            "vcodec": codecs, "acodec": "mp4a.40.2",
            "url": f"https://manifest.googlevideo.com/api/manifest/hls_playlist/itag/{itag}/index.m3u8",
            "manifest_url": "https://manifest.googlevideo.com/api/manifest/hls_variant/c/WEB_EMBEDDED_PLAYER/file/index.m3u8",
            "http_headers": {"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605.1.15"}}


def _https(itag, height, vcodec, acodec="none"):
    return {"format_id": str(itag), "protocol": "https", "ext": "mp4", "height": height,
            "vcodec": vcodec, "acodec": acodec,
            "url": f"https://rr1.googlevideo.com/videoplayback?itag={itag}&c=WEB_EMBEDDED_PLAYER",
            "http_headers": {}}


# What yt-dlp returns for a made-for-kids video, by version (formats trimmed).
DASH_ONLY = {"id": KIDS_VIDEO, "duration": 136, "formats": [
    _https(140, None, "none", "mp4a.40.2"), _https(18, 360, "avc1.42001E", "mp4a.40.2"),
    _https(136, 720, "avc1.4d401f"), _https(137, 1080, "avc1.640028")]}
WITH_HLS = {"id": KIDS_VIDEO, "duration": 136, "formats": DASH_ONLY["formats"] + [
    _hls(94, 480, "avc1.4D401F"), _hls(95, 720, "avc1.4D401F"), _hls(96, 1080, "avc1.640028")]}


class TheImage(unittest.TestCase):
    """What requirements.txt installs (scripts/check and CI run the suite with it)."""

    def test_the_js_challenge_solver_is_installed(self):
        from yt_dlp.dependencies import yt_dlp_ejs
        self.assertIsNotNone(
            yt_dlp_ejs, "yt-dlp-ejs is not installed: Deno is there, but yt-dlp cannot solve "
                        "YouTube's JS challenge, so web_embedded returns no formats")

    def test_the_solver_is_the_version_this_yt_dlp_runs(self):
        # yt-dlp skips a solver script of another major.minor version.
        import yt_dlp_ejs
        from yt_dlp.extractor.youtube.jsc._builtin.vendor import VERSION
        self.assertEqual(version_tuple(yt_dlp_ejs.version)[:2], version_tuple(VERSION)[:2])

    def test_yt_dlp_gets_web_embedded_hls(self):
        self.assertGreaterEqual(
            version_tuple(yt_dlp.version.__version__), version_tuple(WEB_EMBEDDED_HLS_SINCE),
            f"yt-dlp {yt_dlp.version.__version__} gets web_embedded DASH only, which the "
            "resolver cannot serve: made-for-kids videos answer 502")


class TheResolver(unittest.TestCase):
    """The resolver serves HLS only, so the formats yt-dlp returns decide it."""

    def test_dash_only_is_not_playable(self):
        self.assertEqual(resolver._pick_hls_master(DASH_ONLY, 1080), (None, None))

    def test_web_embedded_hls_is_served(self):
        master, headers = resolver._pick_hls_master(WITH_HLS, 1080)
        self.assertIn("WEB_EMBEDDED_PLAYER", master)
        self.assertIn("Safari", headers["User-Agent"])

    def test_a_kids_video_resolves_through_the_chain(self):
        # visionos is asked first; for a made-for-kids video yt-dlp itself adds
        # web_embedded and returns its formats under that request.
        with mock.patch("services.youtube.client.extract", return_value=WITH_HLS) as extract:
            resolved = resolver._extract(KIDS_VIDEO, 1080)
        self.assertEqual(extract.call_count, 1)
        self.assertIn("WEB_EMBEDDED_PLAYER", resolved.master_url)
        self.assertEqual(resolved.duration, 136)


class RealYouTube(unittest.TestCase):
    @live
    def test_a_made_for_kids_video_resolves(self):
        data = temp_dir(self)
        env = mock.patch.dict(os.environ, {"DATA_DIR": data})
        env.start()
        self.addCleanup(env.stop)
        traffic.reset_for_tests()
        self.addCleanup(traffic.reset_for_tests)
        resolved = resolver._extract(KIDS_VIDEO, 1080)
        self.assertTrue(resolved.master_url.startswith("https://"))


if __name__ == "__main__":
    unittest.main()

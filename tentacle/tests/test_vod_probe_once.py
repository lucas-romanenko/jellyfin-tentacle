"""A Tentacle VOD title is probed by Jellyfin once, not at every play.

Jellyfin 10.11 (MediaSourceManager.GetPlaybackMediaSources) runs a full
metadata refresh with a remote ffprobe on every PlaybackInfo of any .strm,
even one whose streams it saved the first time. For a provider film that was
about 3.4 s of range requests through the relay before PlaybackInfo answered
(TV app, direct play, Oct 2026). The plugin's `VodProbeOncePatch` (a Harmony
prefix) turns `allowMediaProbe` off for a Tentacle VOD item that already has
a video stream. There is no C# test host in this repo, so this reads the
source the way test_vod_subtitle_extraction.py does.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

PATCH = Path("../tentacle-plugin/Services/VodProbeOncePatch.cs")
REGISTRATOR = Path("../tentacle-plugin/PluginServiceRegistrator.cs")


def method(src: str, signature: str) -> str:
    start = src.index(signature)
    rest = src[start:]
    end = re.search(r"\n    (?:private|public|protected|internal|/// )", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestProbeOncePatch(unittest.TestCase):
    def setUp(self):
        self.src = PATCH.read_text(encoding="utf-8")

    def test_it_is_registered_and_patches_jellyfins_playback_media_sources(self):
        self.assertIn("AddSingleton<IHostedService, VodProbeOncePatch>()", REGISTRATOR.read_text(encoding="utf-8"))
        self.assertIn('"Emby.Server.Implementations.Library.MediaSourceManager"', self.src)
        self.assertIn('"GetPlaybackMediaSources"', self.src)
        self.assertIn('== "allowMediaProbe"', self.src, "patched only while the argument is what we think it is")
        self.assertIn("UnpatchAll(HarmonyInstance.Id)", self.src, "an in-process restart must not patch twice")

    def test_only_a_tentacle_vod_item_with_known_streams_skips_the_probe(self):
        prefix = method(self.src, "private static void Prefix(")
        self.assertIn("ref bool allowMediaProbe", prefix)
        self.assertIn("VodNoVideoCopyFilter.IsTentacleVod(item)", prefix)
        self.assertIn("item.GetMediaStreams()", prefix)
        self.assertEqual(1, prefix.count("allowMediaProbe = false"))
        skip = method(self.src, "public static bool SkipProbe(")
        self.assertIn("MediaStreamType.Video", skip, "a title never probed (no video stream yet) is probed")


if __name__ == "__main__":
    unittest.main()

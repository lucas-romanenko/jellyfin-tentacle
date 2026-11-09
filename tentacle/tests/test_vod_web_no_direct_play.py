"""Jellyfin Web plays Tentacle VOD through Jellyfin, never from Tentacle's address.

A provider film the browser could play as it is (MKV, H.264 + AAC) came back
from PlaybackInfo as direct play with Path = Tentacle's LAN http address.
Jellyfin Web direct-plays a remote Http source by loading that Path in the
browser: on an https Jellyfin it is blocked as mixed content (and away from
home it doesn't answer), so the player sat on a black screen and Tentacle
never saw a request. Titles that need converting went through HLS and played.

The plugin's `VodWebNoDirectPlayFilter` turns direct play off for those
sources, for Jellyfin Web's player only, so it takes Jellyfin's direct
stream (/Videos/{id}/stream.mkv?Static=true). There is no C# test host in
this repo, so this reads the filter's source the way
test_vod_hls_no_video_copy.py does.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

FILTER = Path("../tentacle-plugin/Services/VodWebNoDirectPlayFilter.cs")
REGISTRATOR = Path("../tentacle-plugin/PluginServiceRegistrator.cs")


def method(src: str, signature: str) -> str:
    start = src.index(signature)
    rest = src[start:]
    end = re.search(r"\n    (?:private|public|protected|internal|/// )", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestVodWebNoDirectPlayFilter(unittest.TestCase):
    def setUp(self):
        self.src = re.sub(r"//[^\n]*", "", FILTER.read_text(encoding="utf-8"))
        self.executed = method(self.src, "public void OnActionExecuted(")
        self.needs = method(self.src, "public static bool NeedsJellyfinStream(")

    def test_it_changes_playback_info_answers_only(self):
        actions = set(re.findall(r'\("(\w+)",\s*"(\w+)"\)', self.src))
        self.assertEqual(actions, {("MediaInfo", "GetPostedPlaybackInfo"), ("MediaInfo", "GetPlaybackInfo")})
        self.assertIn("ObjectResult { Value: PlaybackInfoResponse info }", self.executed)
        self.assertEqual(method(self.src, "public void OnActionExecuting(").count(";"), 0,
                         "nothing changes before Jellyfin decides")

    def test_only_jellyfin_webs_player_is_changed(self):
        clients = re.search(r"WebPlayerClients = new\(StringComparer\.Ordinal\)\s*\{([^}]*)\}", self.src)
        self.assertIsNotNone(clients)
        names = set(re.findall(r'"([^"]+)"', clients.group(1)))
        self.assertEqual(names, {"Jellyfin Web", "Jellyfin Media Player", "Jellyfin Desktop",
                                 "Jellyfin iOS", "Jellyfin iPadOS", "Jellyfin for Android"})
        for native in ("Tentacle", "Android TV"):
            self.assertFalse(any(native in n for n in names),
                             f"{native}: the Tentacle TV app direct-plays Tentacle's address; Jellyfin's Android TV app"
                             " finds no stream without direct play and a TranscodingUrl")
        self.assertRegex(self.src, r'ClientClaim\s*=\s*"Jellyfin-Client"')
        guard = self.executed.index("IsWebPlayer(")
        self.assertLess(guard, self.executed.index("SupportsDirectPlay = false"))

    def test_only_tentacle_vod_offered_as_direct_play_with_direct_stream(self):
        for part in ("source.SupportsDirectPlay", "source.SupportsDirectStream",
                     "source.Protocol == MediaProtocol.Http",
                     "VodNoVideoCopyFilter.IsTentacleVodUrl(source.Path)"):
            self.assertIn(part, self.needs)
        self.assertNotIn("!source.SupportsDirectStream", self.needs,
                         "without direct stream to fall back on, direct play is all the browser has")
        self.assertRegex(self.executed, r"if \(!NeedsJellyfinStream\(source\)\)\s*\{\s*continue;")

    def test_it_only_turns_direct_play_off(self):
        writes = re.findall(r"source\.(\w+)\s*=\s*([^;=]+);", self.executed)
        self.assertEqual(writes, [("SupportsDirectPlay", "false")],
                         "direct stream, transcoding and the TranscodingUrl stay Jellyfin's")

    def test_a_failure_never_breaks_the_play(self):
        self.assertRegex(self.executed, r"(?s)try\s*\{.*\}\s*catch \(Exception ex\)")
        self.assertNotRegex(self.executed, r"\bthrow\b")
        self.assertNotRegex(self.src, r"context\.Result\s*=", "the filter never replaces Jellyfin's answer")

    def test_it_is_registered_as_a_global_mvc_filter(self):
        reg = REGISTRATOR.read_text(encoding="utf-8")
        self.assertRegex(reg, r"AddSingleton<VodWebNoDirectPlayFilter>\(\)")
        self.assertRegex(reg, r"Configure<MvcOptions>\(\s*options\s*=>\s*options\.Filters\.AddService<VodWebNoDirectPlayFilter>\(\)\s*\)")


if __name__ == "__main__":
    unittest.main()

"""A Tentacle VOD title whose .strm was re-pointed is probed again (#747 follow-up).

VodProbeOncePatch skips Jellyfin's probe at PlaybackInfo for a Tentacle VOD
item that already has a video stream. But Tentacle rewrites a .strm in place
when the title's stream moves (services/sync.py: "Rewrote <name>: stream
address changed": another provider, another encode), so it stays the same
Jellyfin item with the old file's streams. A library scan re-reads the
address (ProbeProvider.FetchShortcutInfo sets ShortcutPath) but never probes
a shortcut (FFProbeVideoInfo.ProbeVideo: `!item.IsShortcut ||
EnableRemoteContentProbe`), and PlaybackInfo was the only probe left. So the
new file plays with the old file's codec, resolution, audio tracks and
runtime for ever.

The skip must depend on the address the streams were probed at. There is no
C# test host in this repo, so this reads the source the way
test_vod_probe_once.py does; the hermetic reproduction (a console program
calling the real Prefix on a Jellyfin 10.11 Movie) is in the issue.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p test_vod_probe_once_repointed.py
"""
import unittest
from pathlib import Path

from test_vod_probe_once import method

PATCH = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Services" / "VodProbeOncePatch.cs"


class RepointedStrmIsProbedAgain(unittest.TestCase):
    def setUp(self):
        self.src = PATCH.read_text(encoding="utf-8")

    def test_the_skip_compares_the_address_the_strm_holds_now(self):
        prefix = method(self.src, "private static void Prefix(")
        self.assertIn("item.ShortcutPath", prefix, "a re-pointed .strm keeps the old file's streams")

    def test_skip_probe_needs_the_address_it_was_probed_at(self):
        skip = method(self.src, "public static bool SkipProbe(")
        self.assertIn("shortcutPath", skip.split("=>")[0], "SkipProbe can't tell a re-pointed .strm")


if __name__ == "__main__":
    unittest.main()

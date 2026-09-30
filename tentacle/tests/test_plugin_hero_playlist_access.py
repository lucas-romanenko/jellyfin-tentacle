"""GET /TentacleHome/Hero must apply the same playlist access rule as a row.

GetSectionItems checks CallerIdentity.CanReadPlaylist (owner, shared, or open
access) before reading a playlist's items; GetHeroItems did not. The hero's
playlist id comes from the caller's own home config, and POST
/TentacleHome/Hero stores whatever GUID it is given, so any signed-in user
could point their hero at someone else's private playlist and read its items.

There is no C# test host here, so this reads the controller source.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

API = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api"


def _method_body(src: str, name: str) -> str:
    start = re.search(r"public [^\n]*\b" + re.escape(name) + r"\(", src).start()
    nxt = re.search(r"\n    \[Http", src[start:])
    return src[start:start + nxt.start()] if nxt else src[start:]


class TestPlaylistReadsAreChecked(unittest.TestCase):
    def test_hero_checks_playlist_access_before_reading_items(self):
        body = _method_body((API / "HomeScreenController.cs").read_text(), "GetHeroItems")
        check = body.find("CallerIdentity.CanReadPlaylist(playlist, user)")
        read = min(i for i in (body.find("GetManageableItems()"), body.find("VisibleEntries(")) if i >= 0)
        self.assertGreater(check, 0, "GetHeroItems reads a playlist without CanReadPlaylist")
        self.assertLess(check, read)

    def test_every_controller_playlist_read_is_checked(self):
        # Any action that lists a playlist's items must check read access,
        # except the reorder endpoint, which demands ownership instead.
        for cs in sorted(API.glob("*.cs")):
            src = cs.read_text()
            for m in re.finditer(r"public [^\n]*\b(\w+)\(", src):
                name = m.group(1)
                body = _method_body(src, name)
                reads = "GetManageableItems()" in body or "VisibleEntries(" in body
                if not reads or name == "MovePlaylistItem":
                    continue
                with self.subTest(action=f"{cs.name}:{name}"):
                    self.assertIn("CanReadPlaylist", body)


if __name__ == "__main__":
    unittest.main()

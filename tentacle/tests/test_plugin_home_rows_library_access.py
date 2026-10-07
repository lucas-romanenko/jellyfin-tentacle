"""Home rows and the hero must only show titles the user can open.

GET /TentacleHome/Section/{id} and GET /TentacleHome/Hero read the playlist
with GetManageableItems() and filtered with BaseItem.IsVisible(user). In
Jellyfin 10.11 IsVisible checks parental rating and tags only, not library
access, so a user without access to a library still got its titles in a row
(a shared or public playlist, or a list holding titles from several
libraries), while /Items showed them nothing. Jellyfin's own
/Items?ParentId={playlist} reads a playlist with GetLinkedChildren(user) and
then IsVisible(user); the plugin has to do the same, and the cached ids have
to be re-checked with IsVisibleStandalone(user) (IsVisible + library access).

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


class HomeRowsLibraryAccess(unittest.TestCase):
    def setUp(self):
        self.src = (API / "HomeScreenController.cs").read_text(encoding="utf-8")

    def test_rows_and_hero_do_not_read_entries_without_the_user(self):
        for name in ("GetSectionItems", "GetHeroItems"):
            with self.subTest(action=name):
                body = _method_body(self.src, name)
                self.assertNotIn("GetManageableItems()", body)
                self.assertIn("VisibleEntries(playlist, user)", body)

    def test_entries_are_filtered_like_jellyfins_own_playlist_query(self):
        helper = re.search(r"IEnumerable<BaseItem> VisibleEntries\([^)]*\)\s*=>\s*([^;]+);", self.src)
        self.assertIsNotNone(helper, "VisibleEntries helper not found")
        self.assertIn("playlist.GetLinkedChildren(user)", helper.group(1))
        self.assertIn("IsVisible(user)", helper.group(1))

    def test_cached_ids_are_rechecked_with_library_access(self):
        # Every place that turns kept ids back into items re-checks them for the user.
        rechecks = re.findall(r"GetItemById\(id\)\)\s*\.OfType<BaseItem>\(\)\s*\.Where\(i => i\.(\w+)\(user\)\)", self.src)
        self.assertTrue(rechecks, "no re-check of kept ids found")
        self.assertEqual(set(rechecks), {"IsVisibleStandalone"})


if __name__ == "__main__":
    unittest.main()

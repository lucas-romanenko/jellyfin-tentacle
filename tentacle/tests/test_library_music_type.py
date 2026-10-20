"""Music is a type in Library → Browse, beside Movies and Shows, not a tab of its own (Lucas, 2026-10-20)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PAGES = (ROOT / "static" / "js" / "pages.js").read_text(encoding="utf-8")
APP = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


class LibraryMusicType(unittest.TestCase):
    def test_no_music_tab(self):
        self.assertNotIn('data-libtab="music"', HTML)

    def test_music_sits_with_movies_and_shows(self):
        group = re.search(r'<div class="filter-group" id="lib-type-group">.*?</div>', HTML, re.S).group(0)
        types = re.findall(r'data-libtype="(\w+)"', group)
        self.assertEqual(types, ["all", "movie", "series", "music"])
        self.assertIn("data-music-only", re.search(r'<button[^>]*data-libtype="music"[^>]*>', group).group(0))

    def test_music_panel_lives_in_browse(self):
        browse = re.search(r'<div id="lib-tab-browse">.*?<!-- /lib-tab-browse -->', HTML, re.S).group(0)
        self.assertIn('id="lib-music-panel"', browse)
        self.assertIn('id="music-library"', browse)

    def test_picking_music_swaps_the_grid_for_the_music_list(self):
        self.assertIn("_showLibMusic(type === 'music')", PAGES)
        self.assertIn("['lib-grid', 'lib-search', 'lib-source', 'lib-sort', 'lib-stats']", PAGES)

    def test_old_music_links_land_on_the_music_type(self):
        self.assertIn("const wantMusic = tab === 'music';", APP)


if __name__ == "__main__":
    unittest.main()

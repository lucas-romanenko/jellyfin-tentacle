"""Home rows must not re-read a whole playlist on every request.

Jellyfin walks every entry of a playlist whenever it is read. GET
/TentacleHome/Section/{id} did that on every call: a 16.7k-entry TV playlist
took 5.6 s alone and 20-26 s when the Android app loads all rows at once (its
call timeout is 30 s), and a 56k-entry one 13-14 s per request. The chosen ids
are cached; the key has to carry the playlist's DateLastSaved so a changed
playlist is re-read at once, and POST /Tentacle/Refresh has to clear it.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

API_DIR = Path("../tentacle-plugin/Api")


def _section_endpoint(src: str) -> str:
    start = src.index('[HttpGet("Section/')
    end = src.index("[Http", start + 10)
    return src[start:end]


class HomeRowCache(unittest.TestCase):
    def setUp(self):
        self.home = (API_DIR / "HomeScreenController.cs").read_text(encoding="utf-8")
        self.refresh = (API_DIR / "TentacleController.cs").read_text(encoding="utf-8")

    def test_the_section_endpoint_uses_a_cache(self):
        body = _section_endpoint(self.home)
        self.assertIn("_sectionCache.TryGetValue", body)
        self.assertIn("_sectionCache[", body)

    def test_the_cache_key_changes_when_the_playlist_is_saved(self):
        key = re.search(r"sectionCacheKey\s*=\s*\$\"([^\"]+)\"", self.home)
        self.assertIsNotNone(key, "no cache key found")
        self.assertIn("DateLastSaved", key.group(1))
        self.assertIn("userId", key.group(1))

    def test_ids_not_dtos_are_cached(self):
        # DTOs carry per-user played/progress state, which must stay fresh.
        self.assertRegex(self.home, r"_sectionCache\s*=\s*new\(\)")
        self.assertIn("List<Guid> Ids", self.home)

    def test_refresh_clears_the_row_cache(self):
        start = self.refresh.index('[HttpPost("Refresh")]')
        self.assertIn("TentacleHomeController.ClearSectionCache()", self.refresh[start:start + 2500])


if __name__ == "__main__":
    unittest.main()

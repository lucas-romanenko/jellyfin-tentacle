"""Plugin side of the home version (androidtv audit H2, H3, M4).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Source-level, in the style of test_plugin_home_config_single_flight.py: the
repository has no C# test project.
- The version Tentacle sends with /home-config goes out with Sections, Toolbar
  and HeroConfig, so a client can remember which home it shows.
- POST /Tentacle/Refresh?userId=&homeVersion= (a save of one user's home)
  clears only that user's home caches and tells only that user; the 7-day
  ratings, TMDB and Discover caches stay. Without userId it is the full
  refresh it always was (an older server never sends it).
- The LibraryChanged message carries "tentacle-home:<user|*>:<version>" in
  CollectionFolders, a list of strings every client already parses.
- A home-config fetch that started before a clear can't cache what it read (M4).
"""
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"


def _code(rel: str) -> str:
    return re.sub(r"//[^\n]*", "", (PLUGIN / rel).read_text(encoding="utf-8"))


def _block(src: str, signature: str) -> str:
    start = src.index(signature)
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[start:i]


MANAGER = _code("HomeScreen/HomeScreenManager.cs")
HOME = _code("Api/HomeScreenController.cs")
TENTACLE = _code("Api/TentacleController.cs")


class TheVersionReachesClients(unittest.TestCase):
    def test_the_backend_answer_is_read_into_the_result(self):
        self.assertRegex(MANAGER, r'\[JsonPropertyName\("home_version"\)\]\s*public long HomeVersion')
        self.assertIn("wrapper.HomeVersion", MANAGER)

    def test_sections_toolbar_and_hero_config_answer_with_it(self):
        for signature in ("public async Task<ActionResult> GetSections(",
                          "public async Task<ActionResult> GetToolbar(",
                          "public async Task<ActionResult> GetHeroConfig("):
            body = _block(HOME, signature)
            returns = re.findall(r"return Ok\(new\b.*?\);", body, re.S)
            self.assertTrue(returns, signature)
            for ret in returns:
                self.assertIn("homeVersion = home.Version", ret, f"{signature} answers without the version")


class RefreshIsScopedToTheUser(unittest.TestCase):
    REFRESH = _block(TENTACLE, "public async Task<ActionResult> Refresh(")

    def test_it_takes_the_user_and_the_version_from_the_query(self):
        self.assertRegex(self.REFRESH, r"\[FromQuery\] Guid\? userId")
        self.assertRegex(self.REFRESH, r"\[FromQuery\] long homeVersion")

    def test_a_home_save_keeps_ratings_tmdb_and_discover(self):
        scoped = _block(self.REFRESH, "if (homeOnly)")
        self.assertIn("_homeScreenManager.ClearCache(userId)", scoped)
        for kept in ("MdbListCacheService.Clear", "TentacleDiscoverController.ClearCache",
                     "TentacleTmdbController.ClearCache", "ClearSettingsCache"):
            self.assertNotIn(kept, scoped)

    def test_without_a_user_everything_is_cleared_as_before(self):
        full = self.REFRESH[self.REFRESH.index("else", self.REFRESH.index("if (homeOnly)")):]
        for cleared in ("_homeScreenManager.ClearCache()", "TentacleHomeController.ClearSectionCache()",
                        "TentacleDiscoverController.ClearCache()", "MdbListCacheService.Clear()",
                        "TentacleTmdbController.ClearCache()", "TentacleConfigController.ClearCache()"):
            self.assertIn(cleared, full)

    def test_a_home_save_is_pushed_only_to_its_user(self):
        self.assertRegex(self.REFRESH, r"homeOnly\s*\?\s*new List<Guid> \{ userId!\.Value \}")

    def test_the_message_carries_the_marker(self):
        self.assertIn("CollectionFolders = marker", self.REFRESH)
        self.assertIn('$"tentacle-home:{(userId is { } id ? id.ToString("N") : "*")}:{homeVersion}"', TENTACLE)


class AFetchBeforeAClearIsNotCached(unittest.TestCase):
    def test_the_fetch_remembers_the_generation_it_started_in(self):
        self.assertRegex(MANAGER, r"FetchAndCacheAsync\([^;]*_generation\)")

    def test_it_stores_only_when_no_clear_came_since(self):
        fetch = _block(MANAGER, "private async Task<HomeConfigResult> FetchAndCacheAsync(")
        guard = fetch.index("generation != _generation")
        self.assertLess(guard, fetch.index("_userCache[cacheKey] ="))

    def test_a_clear_moves_the_generation_and_drops_running_fetches(self):
        clear = _block(MANAGER, "public void ClearCache(")
        self.assertIn("_generation++", clear)
        self.assertIn("_inflight.Clear()", clear)
        self.assertIn("_inflight.Remove(key)", clear)

    def test_an_old_fetch_does_not_remove_a_newer_ones_entry(self):
        fetch = _block(MANAGER, "private async Task<HomeConfigResult> FetchAndCacheAsync(")
        self.assertRegex(fetch, r"entry\.Generation == generation\)\s*\{\s*_inflight\.Remove\(flightKey\)")


if __name__ == "__main__":
    unittest.main()

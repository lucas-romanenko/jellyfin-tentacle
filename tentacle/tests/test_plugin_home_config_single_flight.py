"""HomeScreenManager: one backend fetch per user at a time, awaited, never
blocking a Jellyfin thread (#102 item 2, #256), and a cache that does not grow
for ever.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Source-level, in the style of tests/test_frontend_state.py: the repository has
no C# test project. A home page load asks for the same user's config from
several plugin endpoints at once (Sections, every row, Hero, HeroConfig,
Toolbar). #102 serialised the fetch per user with a `lock`, but the fetch was
sync-over-async (`GetAwaiter().GetResult()`): the holder blocked a thread-pool
thread on the HTTP call and every other request blocked on the lock. A burst
of home loads after a cache clear starved Jellyfin's thread pool, and unrelated
routes (`/System/Info/Public`) took 5-40 s (#256), even with a healthy backend.
"""
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"
SRC = (PLUGIN / "HomeScreen" / "HomeScreenManager.cs").read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


def _method(src: str, signature: str) -> str:
    start = src.index(signature)
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[start:i]


CODE = _strip_comments(SRC)


class NeverBlocksAThread(unittest.TestCase):
    def test_no_sync_over_async_in_the_manager(self):
        self.assertNotRegex(CODE, r"\.GetAwaiter\(\)\s*\.GetResult\(\)",
                            "the home-config fetch blocks a thread-pool thread on the HTTP call")
        self.assertNotRegex(CODE, r"\.Result\b(?!\s*[=,)])",
                            "the home-config fetch blocks on a Task's .Result")

    def test_no_monitor_lock_around_the_fetch(self):
        self.assertNotIn("_fetchLocks", CODE,
                         "waiting requests queue on a lock (a blocked thread each) instead of "
                         "awaiting the fetch that is already running")

    def test_the_http_call_and_the_body_read_are_awaited(self):
        self.assertRegex(CODE, r"await\s+client\.GetAsync\(")
        self.assertRegex(CODE, r"await\s+response\.Content\.ReadAsStringAsync\(")

    def test_every_caller_awaits_the_config(self):
        for path in (PLUGIN / "Api").glob("*.cs"):
            src = _strip_comments(path.read_text(encoding="utf-8"))
            for m in re.finditer(r"_homeScreenManager\.GetHomeConfig\w*\(", src):
                before = src[max(0, m.start() - 12):m.start()]
                self.assertIn("await", before,
                              f"{path.name} calls {m.group(0)} without awaiting it")
                self.assertIn("Async(", m.group(0), f"{path.name} uses a blocking {m.group(0)}")

    def test_the_dead_sync_home_section_handlers_are_gone(self):
        # Nothing registered them; their sync call was the reason the fetch was sync.
        self.assertFalse((PLUGIN / "HomeScreen" / "TentacleHomeSection.cs").exists())
        self.assertFalse((PLUGIN / "HomeScreen" / "TentacleHeroSection.cs").exists())


class SingleFlight(unittest.TestCase):
    def setUp(self):
        self.get = _method(CODE, "public Task<HomeConfigResult> GetHomeConfigResultAsync(")

    def test_concurrent_callers_share_one_in_flight_task(self):
        self.assertRegex(self.get, r"_inflight\.TryGetValue\(",
                         "each caller starts its own fetch")
        self.assertRegex(self.get, r"_inflight\[\s*flightKey\s*\]\s*=",
                         "the running fetch is not registered for other callers to join")

    def test_the_shared_fetch_is_keyed_by_the_callers_token_too(self):
        """A 401/403 is about the caller's token; sharing it would hand one caller's
        refusal to the user's other requests (#95)."""
        self.assertRegex(self.get, r"flightKey\s*=\s*[^;]*apiKey",
                         "the in-flight fetch is shared across different tokens")

    def test_a_caller_can_stop_waiting(self):
        self.assertRegex(self.get, r"\.WaitAsync\(\s*\w+\s*\)",
                         "a client that went away keeps waiting for the backend")

    def test_the_cache_is_checked_before_joining_or_starting_a_fetch(self):
        cache = self.get.index("_userCache.TryGetValue(cacheKey")
        self.assertLess(cache, self.get.index("_inflight.TryGetValue("))

    def test_the_fetch_forgets_itself_when_done(self):
        fetch = _method(CODE, "private async Task<HomeConfigResult> FetchAndCacheAsync(")
        finally_block = fetch[fetch.rindex("finally"):]
        self.assertRegex(finally_block, r"_inflight\.Remove\(\s*flightKey\s*\)",
                         "a finished fetch stays registered, so its result is served for ever")

    def test_the_fetch_never_completes_before_it_is_registered(self):
        fetch = _method(CODE, "private async Task<HomeConfigResult> FetchAndCacheAsync(")
        first = fetch[fetch.index("{"):].lstrip("{ \n")
        self.assertTrue(first.startswith("await Task.Yield()"),
                        "a fetch that completed inline would remove its entry before the "
                        "caller added it, leaving a finished task registered for ever")

    def test_a_refused_fetch_is_still_not_cached(self):
        fetch = _method(CODE, "private async Task<HomeConfigResult> FetchAndCacheAsync(")
        self.assertRegex(fetch, r"if\s*\(\s*!\s*\w*[Rr]efused\s*\)")

    def test_expired_entries_are_pruned(self):
        self.assertRegex(CODE, r"_userCache\.Remove\(",
                         "entries are only ever added to the cache")


if __name__ == "__main__":
    unittest.main()

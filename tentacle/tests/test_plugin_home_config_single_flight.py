"""HomeScreenManager.GetHomeConfig: one backend fetch per user at a time, and a
cache that does not grow for ever.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Source-level, in the style of tests/test_frontend_state.py: the repository has
no C# test project. A home page load asks for the same user's config from
several plugin endpoints at once; each made its own blocking sync-over-async
call (3 s timeout), and entries were only ever added to the cache.
"""
import re
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "tentacle-plugin" / "HomeScreen"
       / "HomeScreenManager.cs").read_text(encoding="utf-8")


def _method(name: str) -> str:
    start = SRC.index("public HomeConfig? %s(" % name)
    depth, i = 0, SRC.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(SRC[i], 0)
        i += 1
        if depth == 0:
            return SRC[start:i]


class SingleFlight(unittest.TestCase):
    def setUp(self):
        self.body = _method("GetHomeConfig")

    def test_the_fetch_happens_under_a_per_user_lock(self):
        fetch = self.body.index("FetchFromApi(")
        locks = [m.start() for m in re.finditer(r"lock\s*\(\s*_fetchLocks\.GetOrAdd\(\s*cacheKey", self.body)]
        self.assertTrue(locks and locks[0] < fetch,
                        "FetchFromApi is not serialised per cache key, so concurrent requests "
                        "for one user each make their own blocking call")

    def test_the_cache_is_rechecked_after_waiting_for_the_lock(self):
        lock = re.search(r"lock\s*\(\s*_fetchLocks", self.body).start()
        fetch = self.body.index("FetchFromApi(")
        self.assertIn("TryGetValue(cacheKey", self.body[lock:fetch],
                      "a caller that waited for the lock must use what the previous holder cached, "
                      "not fetch again")

    def test_the_lock_is_not_one_global_lock(self):
        self.assertNotRegex(self.body[:self.body.index("FetchFromApi(")].split("_fetchLocks")[-1],
                            r"lock\s*\(\s*_cacheLock\s*\)\s*\{[^}]*$",
                            "the blocking fetch must not run under the global cache lock: one slow "
                            "user would stall every other user's home screen")

    def test_a_refused_fetch_is_still_not_cached(self):
        self.assertRegex(self.body, r"if\s*\(\s*!callerRefused\s*\)")

    def test_expired_entries_are_pruned(self):
        self.assertRegex(self.body, r"_userCache\.Remove\(",
                         "entries are only ever added to the cache")


if __name__ == "__main__":
    unittest.main()

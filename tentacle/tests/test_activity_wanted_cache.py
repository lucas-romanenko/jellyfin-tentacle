"""The Activity wanted-lists cache under a slow Radarr/Sonarr.

Run from the tentacle/ directory:  python -m unittest discover -s tests

With Sonarr answering every call in 8 s one /api/activity took 64 s. The cache
was stamped with the time the fetch STARTED, so a fetch slower than the 60 s
TTL was stored already expired, and every concurrent request that found it
empty fetched every list again.
"""
import threading
import time
import unittest
from unittest import mock

import routers.activity as activity


class TestWantedCache(unittest.TestCase):
    def setUp(self):
        activity._unreleased_cache.update(data=None, ts=0, gen=0)
        self.addCleanup(activity._unreleased_cache.update, data=None, ts=0, gen=0)
        self.calls = 0
        self.delay = 0.0
        self.during = None

        def fetch(db):
            self.calls += 1
            if self.during:
                self.during()
            time.sleep(self.delay)
            return {"unreleased": [], "searching": [{"n": self.calls}]}
        p = mock.patch.object(activity, "_fetch_wanted", side_effect=fetch)
        p.start()
        self.addCleanup(p.stop)

    def test_concurrent_requests_fetch_once(self):
        self.delay = 0.5
        out = []
        threads = [threading.Thread(target=lambda: out.append(activity._get_wanted(None))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, self.calls, "the waiting requests use the one fetch")
        self.assertEqual(6, len(out))
        self.assertTrue(all(o["searching"] == [{"n": 1}] for o in out))

    def test_a_fetch_slower_than_the_ttl_is_still_cached(self):
        with mock.patch.object(activity, "UNRELEASED_TTL", 0.3):
            self.delay = 0.4
            activity._get_wanted(None)
            self.delay = 0
            activity._get_wanted(None)
        self.assertEqual(1, self.calls, "the TTL counts from when the lists were read")

    def test_invalidated_during_the_fetch_is_not_stored(self):
        self.during = activity.invalidate_wanted_cache  # e.g. a search started meanwhile
        first = activity._get_wanted(None)
        self.assertEqual([{"n": 1}], first["searching"], "the caller still gets its answer")
        self.during = None
        activity._get_wanted(None)
        self.assertEqual(2, self.calls, "the next request reads the lists again")

    def test_cached_answer_is_reused_and_invalidation_forces_a_fetch(self):
        activity._get_wanted(None)
        activity._get_wanted(None)
        self.assertEqual(1, self.calls)
        activity.invalidate_wanted_cache()
        activity._get_wanted(None)
        self.assertEqual(2, self.calls)


if __name__ == "__main__":
    unittest.main()

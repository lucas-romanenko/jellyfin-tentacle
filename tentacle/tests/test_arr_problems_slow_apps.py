"""A slow Radarr/Sonarr problems pass is cached, and runs once at a time (#619).

Run from the tentacle/ directory:  python tests/hermetic.py tests/test_arr_problems_slow_apps.py

arr_insight.problems() runs on every GET /api/activity (through
searching_problems) and on /api/health/arr-problems. Per app it reads health
(10 s timeout), root folders and disk space (15 s each), one app after the
other. Its 60 s cache was stamped with the time the pass STARTED, so a pass
slower than 60 s was stored already expired and every later request ran the
whole pass again; and nothing made concurrent callers share one pass. #171
fixed both for the wanted lists (_get_wanted); this is the same fix here.
"""
import threading
import time
import unittest
from unittest import mock

from services import arr_insight


class _Clock:
    """arr_insight's view of the time module: time() is a fake clock the
    fake Radarr/Sonarr move forward; everything else is the real module."""

    def __init__(self):
        self.t = 1_000_000.0
        self.lock = threading.Lock()

    def time(self):
        with self.lock:
            return self.t

    def advance(self, s):
        with self.lock:
            self.t += s

    def __getattr__(self, name):
        return getattr(time, name)


ANSWERS = {
    "health": [],
    "rootfolder": [{"path": "/data/media"}],
    "diskspace": [{"path": "/data", "freeSpace": 900 * 1024 ** 3, "totalSpace": 1000 * 1024 ** 3}],
}


class ProblemsPassOnSlowApps(unittest.TestCase):
    # Each answer comes just inside its timeout: health 6 s, the two others 14 s.
    COST = {"health": 6, "rootfolder": 14, "diskspace": 14}

    def setUp(self):
        arr_insight._problems_cache.update(at=0, data=None)
        self.addCleanup(arr_insight._problems_cache.update, at=0, data=None)
        self.clock = _Clock()
        self.calls = []
        self.calls_lock = threading.Lock()
        self.gate = None

        def fake_get(url, key, path, timeout=15, **params):
            with self.calls_lock:
                self.calls.append((url, path))
            if self.gate is not None:
                self.gate.wait(5)
            self.clock.advance(self.COST[path])
            return ANSWERS[path]

        # Only arr_insight sees the fake clock: time.time stays real for
        # everything else in the process.
        for p in (
            mock.patch.object(arr_insight, "_get", side_effect=fake_get),
            mock.patch.object(arr_insight, "_conn", side_effect=lambda db, app: (f"http://{app}.test", "k")),
            mock.patch.object(arr_insight, "time", self.clock),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_the_pass_is_slower_than_the_cache_lifetime(self):
        """The premise: two apps at these speeds take 68 s, the cache keeps 60 s."""
        start = self.clock.time()
        arr_insight.problems(None)
        self.assertGreater(self.clock.time() - start, arr_insight.PROBLEMS_TTL)
        self.assertEqual(6, len(self.calls))

    def test_the_next_request_is_answered_from_the_cache(self):
        first = arr_insight.problems(None)
        n = len(self.calls)
        self.clock.advance(1)  # the next Activity poll, one second later
        second = arr_insight.problems(None)
        self.assertEqual(first, second)
        self.assertEqual(n, len(self.calls),
                         "a pass slower than PROBLEMS_TTL was stored already expired, "
                         "so the next request read health, root folders and disk space again")

    def test_the_cache_still_expires(self):
        arr_insight.problems(None)
        n = len(self.calls)
        self.clock.advance(arr_insight.PROBLEMS_TTL + 1)
        arr_insight.problems(None)
        self.assertEqual(2 * n, len(self.calls), "a minute after the pass finished, it runs again")

    def test_concurrent_callers_share_one_pass(self):
        self.gate = threading.Event()
        results = []
        threads = [threading.Thread(target=lambda: results.append(arr_insight.problems(None)))
                   for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.3)  # all three are in (or waiting for) the pass
        self.gate.set()
        for t in threads:
            t.join(10)
        self.assertEqual(3, len(results))
        self.assertEqual(6, len(self.calls),
                         f"{len(self.calls) // 6} passes ran at once against the slow apps")


if __name__ == "__main__":
    unittest.main()

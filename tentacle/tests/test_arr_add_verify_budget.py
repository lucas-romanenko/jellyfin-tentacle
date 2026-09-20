"""A timed-out add must give its answer before the Jellyfin plugin stops waiting.

Context: #6 / #22, closed by 4d49b86 ("Poll for a timed-out add instead of
sampling once"). 6e50661 gave the plugin a dedicated add client,
`AddClient = new() { Timeout = TimeSpan.FromMinutes(4) }`, so Jellyfin users
now wait up to 240 s for Tentacle's answer.

Bug (0e1805f): after the 90 s POST timeout, `_poll_until_present()` sleeps
2+4+8+15x10 = 164 s *between* probes and does not count the probes' own time
(each probe may take up to READ_TIMEOUT = 30 s). An add that never lands is
therefore reported after >= 254 s with instant probes, and after much longer
when the *arr answers slowly - always after the plugin has already given up.
The plugin then returns 500 {"detail": "The request was canceled due to the
configured HttpClient.Timeout of 240 seconds elapsing."} instead of
Tentacle's reason.

Expected: verification is a wall-clock budget (sleeps AND probes count, a
probe's timeout is clipped to what is left), and POST timeout + verification
ends before the plugin's add timeout. A title that lands late is still
reported as added, and a failing probe is still retried.

The plugin timeout is read from tentacle-plugin/Api/DiscoverController.cs, so
the test follows the plugin if it changes. No real sleeping: time.sleep and
time.monotonic are replaced by a fake clock.
"""
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import requests

from services.arr_add import ADDED, FAILED, add_movie_to_radarr
from services.sonarr import SonarrService


def _plugin_add_timeout_seconds() -> float:
    """AddClient's timeout from the plugin source; 240 s if it can't be read."""
    cs = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api" / "DiscoverController.cs"
    try:
        text = cs.read_text(encoding="utf-8")
    except OSError:
        return 240.0
    m = re.search(r"AddClient\s*=\s*new\(\)\s*\{\s*Timeout\s*=\s*TimeSpan\.From(Seconds|Minutes)\((\d+)\)", text)
    if not m:
        return 240.0
    return float(m.group(2)) * (60 if m.group(1) == "Minutes" else 1)


PLUGIN_ADD_TIMEOUT = _plugin_add_timeout_seconds()
TMDB = 11587
TVDB = 424242
MOVIE = {"id": 7, "tmdbId": TMDB, "title": "The Exorcist III"}
SERIES = {"id": 5, "title": "Lanterns", "tvdbId": TVDB, "tmdbId": 5555, "path": "/tv/Lanterns"}


class FakeClock:
    """Replaces time.monotonic/time.sleep: sleeping advances the clock."""

    def __init__(self):
        self.start = self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)

    @property
    def elapsed(self):
        return self.now - self.start


def _json(status, payload):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload).encode()
    r.headers["Content-Type"] = "application/json"
    return r


def _slow_get(clock, seconds, payload_for_call):
    """A GET that takes `seconds` (bounded by the timeout it was given)."""
    calls = []

    def get(url, params=None, timeout=None, **kw):
        calls.append((url, dict(params or {}), timeout))
        budget = seconds if timeout is None else min(seconds, float(timeout))
        clock.sleep(budget)
        if budget < seconds:
            raise requests.exceptions.ReadTimeout(f"read timeout={timeout}")
        return payload_for_call(len(calls))

    return get, calls


class RadarrVerifyBudget(unittest.TestCase):
    def _add(self, get):
        clock = self.clock

        def post(url, timeout=None, **kw):
            clock.sleep(timeout or 90)
            raise requests.exceptions.ReadTimeout(f"read timeout={timeout}")

        with mock.patch("requests.post", side_effect=post), \
             mock.patch("requests.get", side_effect=get), \
             mock.patch("time.sleep", clock.sleep), \
             mock.patch("time.monotonic", clock.monotonic):
            return add_movie_to_radarr("http://radarr:7878", "k", TMDB, 7, "/movies")

    def setUp(self):
        self.clock = FakeClock()

    def test_never_lands_answers_before_the_plugin_gives_up(self):
        get, calls = _slow_get(self.clock, 0.05, lambda n: _json(200, []))
        outcome, reason = self._add(get)
        self.assertEqual(outcome, FAILED)
        self.assertTrue(reason)
        self.assertGreaterEqual(len(calls), 3, "verification must poll, not sample once")
        self.assertLess(self.clock.elapsed, PLUGIN_ADD_TIMEOUT,
                        f"answered after {self.clock.elapsed:.0f}s; the plugin gives up at {PLUGIN_ADD_TIMEOUT:.0f}s")

    def test_slow_probes_count_against_the_budget(self):
        """Each probe takes 25 s (a busy Radarr): the total must still be bounded."""
        get, calls = _slow_get(self.clock, 25, lambda n: _json(200, []))
        outcome, _ = self._add(get)
        self.assertEqual(outcome, FAILED)
        self.assertLess(self.clock.elapsed, PLUGIN_ADD_TIMEOUT,
                        f"answered after {self.clock.elapsed:.0f}s with {len(calls)} slow probes")

    def test_add_that_lands_late_is_still_added(self):
        """Visible only ~130 s after the POST started (the slowest add measured was 127 s)."""
        post_start = self.clock.now
        get, calls = _slow_get(self.clock, 0.05,
                               lambda n: _json(200, [MOVIE] if self.clock.now - post_start >= 130 else []))
        outcome, reason = self._add(get)
        self.assertEqual(outcome, ADDED, reason)
        for url, params, _ in calls:
            self.assertEqual(params, {"tmdbId": TMDB}, "verification must use the targeted filter")

    def test_failing_probe_is_retried(self):
        def payload(n):
            if n == 1:
                raise requests.exceptions.ConnectionError("reset")
            return _json(200, [MOVIE])
        get, calls = _slow_get(self.clock, 0.05, payload)
        outcome, reason = self._add(get)
        self.assertEqual(outcome, ADDED, reason)
        self.assertGreaterEqual(len(calls), 2)


class SonarrVerifyBudget(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def _add(self, series_probe_seconds, visible, **kwargs):
        clock = self.clock
        sonarr = SonarrService("http://sonarr:8989", "key")
        probes = []

        def get(url, params=None, timeout=None, **kw):
            if url.endswith("/api/v3/series/lookup"):
                return _json(200, [{"title": "Lanterns", "tvdbId": TVDB, "tmdbId": 5555}])
            if url.endswith("/api/v3/series"):
                probes.append(timeout)
                budget = series_probe_seconds if timeout is None else min(series_probe_seconds, float(timeout))
                clock.sleep(budget)
                if budget < series_probe_seconds:
                    raise requests.exceptions.ReadTimeout(f"read timeout={timeout}")
                return _json(200, [dict(SERIES)] if visible(len(probes)) else [])
            if url.endswith("/api/v3/episode"):
                return _json(200, [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}])
            if url.endswith("/api/v3/series/5"):
                return _json(200, dict(SERIES))
            raise AssertionError(url)

        def post(url, json=None, timeout=None, **kw):
            if url.endswith("/api/v3/series"):
                clock.sleep(timeout or 90)
                raise requests.exceptions.ReadTimeout(f"read timeout={timeout}")
            return _json(201, {"id": 1})

        with mock.patch.object(sonarr.session, "get", side_effect=get), \
             mock.patch.object(sonarr.session, "post", side_effect=post), \
             mock.patch.object(sonarr.session, "put", side_effect=lambda url, json=None, **kw: _json(202, json)), \
             mock.patch("time.sleep", clock.sleep), \
             mock.patch("time.monotonic", clock.monotonic):
            result = sonarr.add_series(tvdb_id=TVDB, quality_profile_id=7, root_folder="/tv", **kwargs)
        return sonarr, result, probes

    def test_never_visible_answers_before_the_plugin_gives_up(self):
        sonarr, result, probes = self._add(0.05, lambda n: False)
        self.assertIsNone(result)
        self.assertTrue(sonarr.last_error)
        self.assertGreaterEqual(len(probes), 3)
        self.assertLess(self.clock.elapsed, PLUGIN_ADD_TIMEOUT,
                        f"answered after {self.clock.elapsed:.0f}s; the plugin gives up at {PLUGIN_ADD_TIMEOUT:.0f}s")

    def test_slow_probes_count_against_the_budget(self):
        sonarr, result, probes = self._add(25, lambda n: False)
        self.assertIsNone(result)
        self.assertLess(self.clock.elapsed, PLUGIN_ADD_TIMEOUT,
                        f"answered after {self.clock.elapsed:.0f}s with {len(probes)} slow probes")

    def test_series_visible_on_third_probe_is_returned(self):
        sonarr, result, _ = self._add(0.05, lambda n: n >= 3, monitor="firstSeason")
        self.assertTrue(result, sonarr.last_error)
        self.assertEqual(result.get("id"), 5)


if __name__ == "__main__":
    unittest.main()

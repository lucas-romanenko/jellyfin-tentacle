"""Add to Sonarr must not act before Sonarr has set the new series up (#532).

Sonarr answers POST /api/v3/series before it has done anything with the
series (src/NzbDrone.Core/Tv): SeriesAddedHandler only queues a
RefreshSeriesCommand. The refresh creates the episodes; then
SeriesScannedHandler applies addOptions.monitor (EpisodeMonitoredService,
which writes the whole series row back), queues the post-add
MissingEpisodeSearch and clears addOptions. A season search only accepts
releases while the series is monitored. Sonarr runs three commands at a
time, so on a busy Sonarr all of this starts minutes after the add.

Bugs:
* First Season / Last Season: Tentacle unmonitored the series right after
  the POST. On a busy Sonarr the post-add search then rejected every
  release ("Series is not monitored") and nothing was grabbed. (On an idle
  one the scan's whole-row write undid the unmonitor anyway.)
* Pick Episodes: Tentacle looked for the episodes for 3 s, then logged a
  warning and reported "Added" with nothing monitored or searched. When the
  episodes did show up in time, the scan finishing later applied the add's
  own `monitor: none` and unmonitored the picked episode again.

Expected: presets leave the series to Sonarr's own post-add search; picked
episodes are monitored and searched once Sonarr has set the series up
(addOptions cleared), and if that can't happen before the plugin stops
listening, the add says why (last_error) instead of reading as added.

No network and no real sleeping: a fake clock drives a fake Sonarr.
"""
import unittest
from unittest import mock

from services.sonarr import SonarrService
from test_arr_add_verify_budget import PLUGIN_ADD_TIMEOUT

SERIES_ID = 7


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.history, self.text = data, status, [], ""
        self.ok = status < 400

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


class SlowSonarr:
    """POST /series answers at once. `refresh_after` s later the refresh
    creates the episodes (all unmonitored); `scan_lag` s after that the scan
    applies addOptions.monitor, writes back the series row it read at the
    refresh (a PUT in between is lost), runs the post-add search (monitored
    episodes only, nothing while the series is unmonitored) and clears
    addOptions."""

    def __init__(self, clock, refresh_after, scan_lag=0.0):
        self.clock, self.refresh_after, self.scan_lag = clock, refresh_after, scan_lag
        self.series, self.episodes, self.grabbed, self.searched = None, [], [], []
        self.refreshed_row = None

    def _tick(self):
        if not self.series or "addOptions" not in self.series:
            return
        now = self.clock.t
        if self.refreshed_row is None and now >= self.added_at + self.refresh_after:
            self.episodes = [{"id": 100 * s + e, "seasonNumber": s, "episodeNumber": e, "monitored": False}
                             for s in (1, 2) for e in (1, 2, 3)]
            self.refreshed_row = dict(self.series)
        if self.refreshed_row is not None and now >= self.added_at + self.refresh_after + self.scan_lag:
            opts = self.refreshed_row.pop("addOptions")
            self.series = self.refreshed_row
            last = max(ep["seasonNumber"] for ep in self.episodes)
            for ep in self.episodes:
                ep["monitored"] = (opts["monitor"] == "all"
                                   or (opts["monitor"] == "firstSeason" and ep["seasonNumber"] == 1)
                                   or (opts["monitor"] == "lastSeason" and ep["seasonNumber"] == last))
            if opts["searchForMissingEpisodes"] and self.series["monitored"]:
                self.grabbed += [ep["id"] for ep in self.episodes if ep["monitored"]]

    def get(self, url, params=None, timeout=None, **kw):
        self._tick()
        if url.endswith("/api/v3/episode"):
            return Resp([dict(e) for e in self.episodes])
        if url.endswith(f"/api/v3/series/{SERIES_ID}"):
            return Resp(dict(self.series))
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, json=None, timeout=None, **kw):
        self._tick()
        if url.endswith("/api/v3/series"):
            self.series, self.added_at = dict(json, id=SERIES_ID), self.clock.t
            return Resp(dict(self.series), 201)
        if url.endswith("/api/v3/command") and json.get("name") == "EpisodeSearch":
            # A search for chosen episodes is not limited to monitored ones.
            self.searched += json["episodeIds"]
            self.grabbed += json["episodeIds"]
            return Resp({"id": 1}, 201)
        raise AssertionError(f"unexpected POST {url}")

    def put(self, url, json=None, timeout=None, **kw):
        self._tick()
        if url.endswith("/api/v3/episode/monitor"):
            for ep in self.episodes:
                if ep["id"] in json["episodeIds"]:
                    ep["monitored"] = json["monitored"]
            return Resp([], 202)
        if url.endswith(f"/api/v3/series/{SERIES_ID}"):
            self.series = dict(json)
            return Resp(dict(json), 202)
        raise AssertionError(f"unexpected PUT {url}")


class AddWaitsForSonarrsSetup(unittest.TestCase):
    def add(self, refresh_after, scan_lag=0.0, **kwargs):
        clock = Clock()
        fake = SlowSonarr(clock, refresh_after, scan_lag)
        svc = SonarrService("http://sonarr.test", "key")
        svc.session = fake
        with mock.patch("time.monotonic", clock.monotonic), mock.patch("time.sleep", clock.sleep), \
                mock.patch.object(SonarrService, "lookup_by_tvdb",
                                  return_value={"title": "Show", "tvdbId": 1}):
            result = svc.add_series(tvdb_id=1, quality_profile_id=7, root_folder="/tv", **kwargs)
            answered_after = clock.t
            clock.sleep(600)  # Sonarr's queue catches up
            fake._tick()
        return svc, result, fake, answered_after

    def episode(self, fake, episode_id):
        return next(ep for ep in fake.episodes if ep["id"] == episode_id)

    def test_first_season_on_a_busy_sonarr_grabs_the_season(self):
        svc, result, fake, _ = self.add(60, monitor="firstSeason")
        self.assertTrue(result, svc.last_error)
        self.assertEqual([101, 102, 103], fake.grabbed, "the post-add search grabbed nothing")

    def test_last_season_on_a_busy_sonarr_grabs_the_season(self):
        svc, result, fake, _ = self.add(60, monitor="lastSeason")
        self.assertTrue(result, svc.last_error)
        self.assertEqual([201, 202, 203], fake.grabbed, "the post-add search grabbed nothing")

    def test_picked_episode_on_a_busy_sonarr_is_monitored_and_searched(self):
        svc, result, fake, answered_after = self.add(60, scan_lag=5,
                                                     selected_episodes=[{"season": 1, "episode": 2}])
        self.assertTrue(result)
        self.assertIsNone(svc.last_error)
        self.assertTrue(self.episode(fake, 102)["monitored"], "S01E02 is not monitored")
        self.assertIn(102, fake.searched, "S01E02 was never searched")
        self.assertFalse(fake.series["monitored"],
                         "without Auto-download new episodes the series ends unmonitored")
        self.assertLess(answered_after, PLUGIN_ADD_TIMEOUT)

    def test_picked_episode_is_not_unmonitored_by_a_scan_that_finishes_later(self):
        # The episodes exist within Tentacle's first look, the scan ends later.
        svc, result, fake, _ = self.add(1, scan_lag=10, selected_episodes=[{"season": 1, "episode": 2}])
        self.assertTrue(result)
        self.assertTrue(self.episode(fake, 102)["monitored"],
                        "the scan's monitor: none undid the pick")
        self.assertIn(102, fake.searched)

    def test_picked_episode_sonarr_never_sets_up_is_reported_in_time(self):
        svc, result, fake, answered_after = self.add(100_000,
                                                     selected_episodes=[{"season": 1, "episode": 2}])
        self.assertTrue(result, "the series is in Sonarr, so the add still hands it back")
        self.assertTrue(svc.last_error, "nothing was monitored or searched, yet the add reads as added")
        self.assertLess(answered_after, PLUGIN_ADD_TIMEOUT,
                        f"answered after {answered_after:.0f}s; the plugin gives up at {PLUGIN_ADD_TIMEOUT:.0f}s")


if __name__ == "__main__":
    unittest.main()

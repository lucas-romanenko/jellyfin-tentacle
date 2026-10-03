"""Add to Sonarr: what Tentacle does after POST /series must wait for Sonarr.

Sonarr answers POST /api/v3/series before it has done anything with the new
series (Sonarr v4 source, src/NzbDrone.Core):

- Tv/AddSeriesService stores the row (with addOptions.monitor "none" it is
  stored unmonitored); Tv/SeriesAddedHandler only queues a RefreshSeriesCommand.
- Tv/RefreshSeriesService reads the series, asks Skyhook, writes the whole row
  back, creates the episodes and scans the folder.
- Tv/SeriesScannedHandler then applies addOptions.monitor to the episodes
  (another whole-row write of the same in-memory series), queues the post-add
  MissingEpisodeSearch and clears addOptions.
- That search reads the series again. A season of more than one episode is
  searched with MonitoredEpisodesOnly, so every release is rejected with
  "Series is not monitored" while the series is unmonitored.
- Every command waits for one of Sonarr's three command threads.

Two bugs came from Tentacle acting the moment the POST returned:

1. First Season / Last Season / Pilot unmonitored the series at once. An idle
   Sonarr's refresh had already read it and wrote monitored=true back over it;
   on a busy Sonarr the post-add search then grabbed nothing.
2. "Pick Episodes" looked for the episodes for about 3 s, then gave up with a
   warning and the add still read "Added": nothing was ever monitored or
   searched. When they did appear in time, Sonarr's own monitor "none" could
   unmonitor the picked ones again afterwards.

FakeSonarr replays that pipeline on a simulated clock: nothing sleeps and
nothing leaves the process.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import copy
import heapq
import itertools
import random
import unittest
from unittest import mock

import requests

from services import sonarr as sonarr_module
from services.arr_add import ADD_TIMEOUT, VERIFY_TOTAL_SECONDS
from services.sonarr import SETUP_WAIT_SECONDS, SonarrService
from tmp_dirs import temp_dir

TVDB = 424242
SERIES_ID = 7
CALL = 0.05  # seconds one HTTP round trip takes
LOOKUP = {"title": "Lanterns", "tvdbId": TVDB, "status": "ended",
          "seasons": [{"seasonNumber": 1, "monitored": True}, {"seasonNumber": 2, "monitored": True}]}
PRESETS = ("all", "firstSeason", "lastSeason", "pilot", "none")


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.text = "" if body is None else repr(body)
        self.history = []
        self.headers = {}

    def json(self):
        return copy.deepcopy(self._body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSonarr:
    """Sonarr v4's add pipeline, only as much of it as these bugs need.

    queue_wait: seconds a queued command waits for a free command thread
                (0 = an idle Sonarr: the refresh starts while it answers the POST).
    skyhook:    seconds the refresh spends fetching metadata.
    scan:       seconds from the episodes existing to SeriesScannedHandler.
    sets_up:    False = the refresh never gets that far (Skyhook no longer knows
                the show), so addOptions is never cleared.
    post_times_out: the add lands but its answer does not come within ADD_TIMEOUT.
    fault:      fault(kind) -> None, "error" (HTTP 500) or "raise" (connection
                error) for each of Tentacle's calls after the POST; kind is
                "get", "put" or "command".
    """

    def __init__(self, queue_wait=0.0, skyhook=0.5, scan=0.5, sets_up=True,
                 post_times_out=False, fault=None):
        self.now = 0.0
        self.queue_wait, self.skyhook, self.scan = queue_wait, skyhook, scan
        self.sets_up = sets_up
        self.post_times_out = post_times_out
        self.fault = fault or (lambda kind: None)
        self.posted_at = None
        self._events = []
        self._seq = itertools.count()
        self.series = None          # the stored row
        self.episodes = []
        self.grabbed = set()
        self.rejections = []
        self.searched = set()       # episode ids Tentacle asked Sonarr to search
        self.series_writes = 0      # PUT /series from Tentacle

    # -- clock and command queue -------------------------------------------
    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self._advance(self.now + max(0.0, float(seconds)))

    def drain(self):
        """Sonarr carries on after Tentacle has answered."""
        self._advance(self.now + 3600)

    def _advance(self, until):
        while self._events and self._events[0][0] <= until:
            at, _, fn = heapq.heappop(self._events)
            self.now = max(self.now, at)
            fn()
        self.now = max(self.now, until)

    def _after(self, delay, fn):
        heapq.heappush(self._events, (self.now + delay, next(self._seq), fn))

    def _queue(self, fn):
        self._after(self.queue_wait, fn)

    def _update_series(self, series):
        # SeriesService.UpdateSeries: a whole-row write that keeps the stored addOptions
        series["addOptions"] = self.series["addOptions"]
        self.series = copy.deepcopy(series)

    # -- Sonarr's side -------------------------------------------------------
    def _refresh_series(self):
        series = copy.deepcopy(self.series)            # RefreshSeriesInfo: GetSeries
        if not self.sets_up:
            return

        def written():                                 # ... Skyhook, then UpdateSeries
            self._update_series(series)
            if not self.episodes:                      # RefreshEpisodeInfo
                self.episodes = [{"id": sn * 100 + en, "seasonNumber": sn, "episodeNumber": en,
                                  "monitored": True, "hasFile": False}
                                 for sn in (1, 2) for en in (1, 2, 3)]
            self._after(self.scan, lambda: self._series_scanned(series))
        self._after(self.skyhook, written)

    def _series_scanned(self, series):
        opts = series["addOptions"]
        if opts is None:
            return
        rule = {
            "all": lambda e: True,
            "firstSeason": lambda e: e["seasonNumber"] == 1,
            "lastSeason": lambda e: e["seasonNumber"] == 2,
            "pilot": lambda e: (e["seasonNumber"], e["episodeNumber"]) == (1, 1),
            "none": lambda e: False,
        }[opts["monitor"]]
        for ep in self.episodes:                       # EpisodeMonitoredService
            ep["monitored"] = rule(ep)
        watched = {e["seasonNumber"] for e in self.episodes if e["monitored"]}
        for s in series["seasons"]:
            s["monitored"] = s["seasonNumber"] in watched
        self._update_series(series)
        if opts.get("searchForMissingEpisodes"):
            self._queue(self._missing_episode_search)
        self.series["addOptions"] = None               # RemoveAddOptions

    def _missing_episode_search(self):
        series = self.series                           # read again when it runs
        wanted = [e for e in self.episodes if e["monitored"] and not e["hasFile"]]
        for season in sorted({e["seasonNumber"] for e in wanted}):
            group = [e for e in wanted if e["seasonNumber"] == season]
            if len(group) > 1 and not series["monitored"]:
                self.rejections.append((season, "Series is not monitored"))  # MonitoredEpisodeSpecification
            else:
                self.grabbed.update(e["id"] for e in group)

    def _episode_search(self, ids):
        # EpisodeSearch searches with monitoredOnly=false: no monitored check
        self.grabbed.update(e["id"] for e in self.episodes if e["id"] in ids)

    # -- HTTP ---------------------------------------------------------------
    def _call(self, kind):
        self._advance(self.now + CALL)
        outcome = self.fault(kind)
        if outcome == "raise":
            raise requests.ConnectionError("injected")
        return outcome

    def post(self, url, json=None, **_):
        if url.endswith("/api/v3/series"):
            sent = self.now
            self._advance(self.now + CALL)
            row = copy.deepcopy(json)
            row["id"] = SERIES_ID
            if (row.get("addOptions") or {}).get("monitor") == "none":
                row["monitored"] = False               # AddSeriesService
            self.series = row
            self.posted_at = self.now
            self._queue(self._refresh_series)          # SeriesAddedHandler
            self._advance(self.now)                    # an idle Sonarr starts it at once
            if self.post_times_out:
                self._advance(sent + ADD_TIMEOUT)
                raise requests.exceptions.Timeout("injected")
            return _Resp(201, self.series)
        if self._call("command") == "error":
            return _Resp(500)
        if url.endswith("/api/v3/command") and json.get("name") == "EpisodeSearch":
            ids = set(json["episodeIds"])
            self.searched |= ids
            self._queue(lambda: self._episode_search(ids))
            return _Resp(201, {"id": 1})
        return _Resp(404)

    def get(self, url, params=None, **_):
        if self._call("get") == "error":
            return _Resp(500)
        path = url.split("/api/v3/", 1)[1]
        if path == f"series/{SERIES_ID}" and self.series:
            return _Resp(200, self.series)
        if path == "series":
            return _Resp(200, [self.series] if self.series else [])
        if path == "episode":
            return _Resp(200, self.episodes)
        return _Resp(404)

    def put(self, url, json=None, **_):
        if self._call("put") == "error":
            return _Resp(500)
        path = url.split("/api/v3/", 1)[1]
        if path == "episode/monitor":
            for ep in self.episodes:
                if ep["id"] in json["episodeIds"]:
                    ep["monitored"] = json["monitored"]
            return _Resp(202, [])
        if path == f"series/{SERIES_ID}":
            self.series_writes += 1
            self._update_series(copy.deepcopy(json))
            return _Resp(202, self.series)
        return _Resp(404)

    # -- what the user ends up with -------------------------------------------
    def episode(self, season, number):
        return next((e for e in self.episodes
                     if (e["seasonNumber"], e["episodeNumber"]) == (season, number)), None)


def add(fake, monitor="all", picked=None, monitor_new=False):
    """Run Tentacle's add against the fake; returns (service, result, seconds it took)."""
    sonarr = SonarrService("http://sonarr", "key")
    with mock.patch("time.sleep", fake.sleep), \
         mock.patch("time.monotonic", fake.monotonic), \
         mock.patch.object(sonarr, "lookup_by_tvdb", return_value=copy.deepcopy(LOOKUP)), \
         mock.patch.object(sonarr.session, "post", side_effect=fake.post), \
         mock.patch.object(sonarr.session, "get", side_effect=fake.get), \
         mock.patch.object(sonarr.session, "put", side_effect=fake.put):
        started = fake.now
        result = sonarr.add_series(quality_profile_id=4, root_folder="/tv", tvdb_id=TVDB,
                                   monitor=monitor, selected_episodes=picked, monitor_new=monitor_new)
        took = fake.now - started
    fake.drain()
    return sonarr, result, took


def preset_wants(monitor):
    return {"all": {101, 102, 103, 201, 202, 203}, "firstSeason": {101, 102, 103},
            "lastSeason": {201, 202, 203}, "pilot": {101}, "none": set()}[monitor]


class TestPresets(unittest.TestCase):
    def test_busy_sonarr_still_grabs_the_chosen_season(self):
        # Sonarr's command threads are busy (a long search, an RSS sync), so the
        # post-add refresh is still queued when Tentacle answers.
        for monitor in ("firstSeason", "lastSeason"):
            with self.subTest(monitor=monitor):
                fake = FakeSonarr(queue_wait=4)
                sonarr, result, _ = add(fake, monitor)
                self.assertTrue(result and sonarr.last_error is None, sonarr.last_error)
                self.assertEqual(fake.rejections, [],
                                 "the post-add search rejected every release with 'Series is not monitored'")
                self.assertEqual(fake.grabbed, preset_wants(monitor))

    def test_idle_sonarr_is_left_with_nothing_to_overwrite(self):
        # An idle Sonarr has read the series before the POST returns and writes
        # all of it back after Skyhook: a PUT from Tentacle in between was lost.
        for monitor in ("firstSeason", "lastSeason", "pilot"):
            with self.subTest(monitor=monitor):
                fake = FakeSonarr(queue_wait=0)
                sonarr, result, took = add(fake, monitor)
                self.assertTrue(result and sonarr.last_error is None, sonarr.last_error)
                self.assertEqual(fake.grabbed, preset_wants(monitor))
                self.assertEqual(fake.series_writes, 0, "Tentacle wrote the series while Sonarr was setting it up")
                self.assertLess(took, 1, "a preset add has nothing to wait for")


class TestPickedEpisodes(unittest.TestCase):
    PICKED = [{"season": 1, "episode": 3}]

    def test_slow_refresh_still_monitors_and_searches_the_picked_episode(self):
        # The refresh waits 10 s for a command thread: the episodes exist only
        # after that, well past the 3 s Tentacle used to wait.
        fake = FakeSonarr(queue_wait=10, skyhook=1, scan=2)
        sonarr, result, took = add(fake, "none", self.PICKED)
        self.assertTrue(result and sonarr.last_error is None, sonarr.last_error)
        self.assertTrue(fake.episode(1, 3)["monitored"], "the picked episode was never monitored")
        self.assertIn(103, fake.grabbed, f"the picked episode was never searched ({took:.1f}s)")
        self.assertEqual({e["id"] for e in fake.episodes if e["monitored"]}, {103})

    def test_selection_is_not_undone_by_sonarrs_own_monitor_none(self):
        # The episodes exist after 2 s, but the folder scan ends at 8 s and only
        # then does Sonarr apply addOptions.monitor "none" to every episode.
        fake = FakeSonarr(queue_wait=0, skyhook=2, scan=6)
        sonarr, result, _ = add(fake, "none", self.PICKED)
        self.assertTrue(result and sonarr.last_error is None, sonarr.last_error)
        self.assertTrue(fake.episode(1, 3)["monitored"],
                        "Sonarr's post-add monitor 'none' unmonitored the picked episode again")

    def test_sonarr_never_setting_up_is_not_reported_as_added(self):
        fake = FakeSonarr(sets_up=False)
        sonarr, result, took = add(fake, "none", self.PICKED)
        self.assertEqual(result["id"], SERIES_ID, "the series is in Sonarr: the caller needs it")
        self.assertIn("in Sonarr", sonarr.last_error or "",
                      "the add read as a success with nothing monitored or searched")
        self.assertEqual(fake.searched, set())
        self.assertLessEqual(took, SETUP_WAIT_SECONDS + 1)

    def test_a_timed_out_add_waits_for_sonarr_too(self):
        fake = FakeSonarr(queue_wait=120, post_times_out=True)
        sonarr, result, took = add(fake, "none", self.PICKED)
        self.assertTrue(result and sonarr.last_error is None, sonarr.last_error)
        self.assertIn(103, fake.grabbed)
        self.assertLessEqual(took, SETUP_WAIT_SECONDS + 1)

    def test_picked_episodes_sonarr_does_not_have_are_reported(self):
        fake = FakeSonarr()
        sonarr, result, took = add(fake, "none", [{"season": 4, "episode": 1}])
        self.assertTrue(result)
        self.assertIn("none of the episodes you picked", sonarr.last_error or "")
        self.assertLess(took, 30, "a finished setup with no match need not wait out the budget")

    def test_refused_monitoring_or_search_is_reported(self):
        for kind in ("put", "command"):
            with self.subTest(refused=kind):
                fake = FakeSonarr(fault=lambda k, kind=kind: "error" if k == kind else None)
                sonarr, result, _ = add(fake, "none", self.PICKED)
                self.assertTrue(result)
                self.assertTrue(sonarr.last_error, "a refused episode/monitor or search read as Added")

    def test_series_stays_unmonitored_only_when_not_following(self):
        for monitor_new in (False, True):
            with self.subTest(monitor_new=monitor_new):
                fake = FakeSonarr(queue_wait=3)
                sonarr, result, _ = add(fake, "none", self.PICKED, monitor_new=monitor_new)
                self.assertIsNone(sonarr.last_error)
                self.assertEqual(fake.series_writes, 0 if monitor_new else 1)
                self.assertEqual(fake.series["monitorNewItems"], "all" if monitor_new else "none")


class TestStaleLookupError(unittest.TestCase):
    def test_a_failed_tvdb_lookup_does_not_mark_a_good_add_as_failed(self):
        # lookup_by_tvdb could not reach Sonarr, lookup_by_tmdb then found the show
        sonarr = SonarrService("http://sonarr", "key")
        fake = FakeSonarr()

        def tvdb_down(_):
            sonarr.last_error = "Could not reach Sonarr to look this series up."
            return None

        with mock.patch("time.sleep", fake.sleep), mock.patch("time.monotonic", fake.monotonic), \
             mock.patch.object(sonarr, "lookup_by_tvdb", side_effect=tvdb_down), \
             mock.patch.object(sonarr, "lookup_by_tmdb", return_value=copy.deepcopy(LOOKUP)), \
             mock.patch.object(sonarr.session, "post", side_effect=fake.post), \
             mock.patch.object(sonarr.session, "get", side_effect=fake.get), \
             mock.patch.object(sonarr.session, "put", side_effect=fake.put):
            result = sonarr.add_series(tmdb_id=99, tvdb_id=TVDB, quality_profile_id=4, root_folder="/tv",
                                       monitor="firstSeason")
        self.assertTrue(result)
        self.assertIsNone(sonarr.last_error)


class TestRequestSeries(unittest.TestCase):
    """services.media_requests: a pick Sonarr could not apply reads as failed with
    the reason, while the series Sonarr now holds is still recorded. Without its
    sonarr_path the scan can take a hybrid VOD show for a duplicate once a
    download lands."""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add(mdb.TentacleUser(id=1, jellyfin_user_id="u1", display_name="u"))
        self.db.add(mdb.Series(tmdb_id=77, title="Lanterns", source="provider_1",
                               strm_path="/media/vod/shows/Lanterns (2020)"))
        self.db.commit()
        for key, value in (("sonarr_url", "http://sonarr"), ("sonarr_api_key", "k"),
                           ("sonarr_quality_profile_id", "4"), ("sonarr_root_folder", "/tv"),
                           ("hybrid_series_layout", "shared_library")):
            mdb.set_setting(self.db, key, value)
        for target, value in (("services.media_requests._tmdb_service", None),
                              ("services.media_requests.read_quality_profiles", None),
                              ("services.media_requests._bust_discover_cache", None)):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _request(self, reason):
        from services import media_requests

        def add_series(svc, *a, **kw):
            svc.last_error = reason
            return {"id": SERIES_ID, "path": "/tv/Lanterns (2020)", "tmdbId": 77}

        with mock.patch.object(SonarrService, "add_series", autospec=True, side_effect=add_series):
            return media_requests.request_series(
                self.db, tmdb_ids=[77], user_id=1, via="test", monitor="none",
                selected_episodes=[{"season": 1, "episode": 3}]).as_response()

    def test_picked_episodes_not_applied_is_reported_and_the_series_recorded(self):
        import models.database as mdb
        resp = self._request("Sonarr added the show but ... in Sonarr.")
        self.assertEqual((resp["added"], resp["failed"]), (0, 1), resp)
        self.assertIn("in Sonarr", resp["detail"])
        self.assertEqual(self.db.query(mdb.Series).one().sonarr_path, "/tv/Lanterns (2020)")
        self.assertEqual(self.db.query(mdb.DownloadRequest).count(), 1)

    def test_applied_pick_is_added(self):
        resp = self._request(None)
        self.assertEqual((resp["added"], resp["failed"]), (1, 0), resp)


class TestRandomSchedules(unittest.TestCase):
    """Random Sonarr timings, timeouts and failed calls (1,000 seeds).

    Invariants, checked after Sonarr has finished everything it queued:
    - an add reported as done (a series, no reason) has what the user chose:
      every picked episode monitored and searched, or every episode of the
      preset grabbed, with no "Series is not monitored" rejection;
    - a preset never fails because of anything after the POST;
    - an add Sonarr made but whose picked episodes were not applied still
      returns the series (the caller records it) with a reason;
    - with no failures and Sonarr set up inside the budget, picked episodes
      are applied;
    - the add returns within its budget.
    """

    SEEDS = 1000

    def test_invariants_hold_for_random_schedules(self):
        with mock.patch.object(sonarr_module, "logger"):
            for seed in range(self.SEEDS):
                self._one(seed)

    def _one(self, seed):
        rng = random.Random(seed)
        fault_rate = rng.choice((0, 0, 0.1, 0.3))
        kind = rng.choice(PRESETS + ("pick", "pick", "pick"))
        picked = None
        if kind == "pick":
            picked = [{"season": s, "episode": e}
                      for s, e in rng.sample([(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3), (3, 1)],
                                             rng.randint(1, 3))]
        fake = FakeSonarr(
            queue_wait=rng.choice((0, 0, 0.5, 3, 10, 40, 120, 300)),
            skyhook=rng.choice((0.2, 1, 5)),
            scan=rng.choice((0.1, 1, 8, 30)),
            sets_up=rng.random() > 0.05,
            post_times_out=rng.random() < 0.15,
            fault=lambda _k: (rng.choice(("error", "raise")) if rng.random() < fault_rate else None),
        )
        monitor_new = rng.random() < 0.5
        ctx = (f"seed={seed} kind={kind} picked={picked} queue_wait={fake.queue_wait} "
               f"skyhook={fake.skyhook} scan={fake.scan} sets_up={fake.sets_up} "
               f"timeout={fake.post_times_out} faults={fault_rate}")
        try:
            sonarr, result, took = add(fake, "none" if kind == "pick" else kind, picked, monitor_new)
        except Exception as e:  # pragma: no cover - the message is the point
            self.fail(f"add_series raised {e!r}: {ctx}")
        done = bool(result) and sonarr.last_error is None
        exists = {(p["season"], p["episode"]) for p in picked or []} & {(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)}
        ids = {s * 100 + e for s, e in exists}

        budget = max(SETUP_WAIT_SECONDS, VERIFY_TOTAL_SECONDS) if kind == "pick" or fake.post_times_out else 1
        self.assertLessEqual(took, budget + 1, f"took {took:.1f}s: {ctx}")

        if kind != "pick":
            if not fake.post_times_out:
                self.assertTrue(done, f"a preset add failed after the POST ({sonarr.last_error}): {ctx}")
            if done and fake.sets_up:
                self.assertEqual(fake.rejections, [], ctx)
                self.assertEqual(fake.grabbed, preset_wants(kind), ctx)
            return

        if done:
            self.assertTrue(ids, f"reported done with none of the picked episodes in Sonarr: {ctx}")
            monitored = {e["id"] for e in fake.episodes if e["monitored"]}
            self.assertEqual(ids - monitored, set(), f"picked episodes not monitored after Sonarr finished: {ctx}")
            self.assertEqual(ids - (fake.searched & fake.grabbed), set(), f"picked episodes never searched: {ctx}")
            self.assertEqual(monitored - ids, set(), f"episodes nobody picked are monitored: {ctx}")
        elif fake.series is not None and not (fake.post_times_out and result is None):
            self.assertEqual((result or {}).get("id"), SERIES_ID, f"the series is in Sonarr but was not returned: {ctx}")
            self.assertTrue(sonarr.last_error, ctx)

        setup_done_at = fake.queue_wait + fake.skyhook + fake.scan + 1
        if (fault_rate == 0 and fake.sets_up and not fake.post_times_out and ids
                and setup_done_at < SETUP_WAIT_SECONDS - 20):
            self.assertTrue(done, f"Sonarr was set up in time, yet the pick failed ({sonarr.last_error}): {ctx}")


if __name__ == "__main__":
    unittest.main()

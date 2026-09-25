"""#121: Activity lists titles that are still searching for a release.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import models.database as mdb
import routers.activity as activity


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.utcnow()
PAST = _iso(NOW - timedelta(days=30))
FUTURE = _iso(NOW + timedelta(days=12))


def _movie(tmdb, title, *, monitored=True, has_file=False, available=True,
           digital=PAST, physical=None, cinemas=PAST, added=None):
    return {"tmdbId": tmdb, "title": title, "year": 2026, "monitored": monitored,
            "hasFile": has_file, "isAvailable": available, "digitalRelease": digital,
            "physicalRelease": physical, "inCinemas": cinemas,
            "added": added or _iso(NOW - timedelta(hours=2)),
            "images": [{"coverType": "poster",
                        "remoteUrl": f"https://image.tmdb.org/t/p/original/{tmdb}.jpg"}]}


RADARR_MOVIES = [
    _movie(1, "Searching Movie", added=_iso(NOW - timedelta(hours=3))),
    _movie(2, "Newer Request", added=_iso(NOW - timedelta(minutes=5))),
    _movie(3, "Coming Soon", digital=FUTURE),
    _movie(4, "Already Downloaded", has_file=True),
    _movie(5, "Unmonitored", monitored=False),
    _movie(6, "Not Yet Available", available=False),
]


def _ep(series, season, number, aired, *, has_file=False, monitored=True):
    return {"seriesId": series["id"], "seasonNumber": season, "episodeNumber": number,
            "airDateUtc": aired, "hasFile": has_file, "monitored": monitored,
            "series": series}


SHOW_A = {"id": 10, "title": "Show A", "tmdbId": 100, "tvdbId": 1000, "year": 2020,
          "monitored": True, "added": _iso(NOW - timedelta(days=400)), "images": []}
SHOW_B = {"id": 11, "title": "Show B", "tmdbId": 101, "tvdbId": 1001, "year": 2026,
          "monitored": True, "added": _iso(NOW - timedelta(hours=1)), "images": []}
SONARR_MISSING = {"page": 1, "totalRecords": 5, "records": [
    # Long-followed show, one new episode aired yesterday.
    _ep(SHOW_A, 3, 4, _iso(NOW - timedelta(days=1))),
    # Show just added, backlog of three.
    _ep(SHOW_B, 1, 2, _iso(NOW - timedelta(days=20))),
    _ep(SHOW_B, 1, 1, _iso(NOW - timedelta(days=27))),
    _ep(SHOW_B, 1, 3, _iso(NOW - timedelta(days=13))),
    # Not aired — never "searching".
    _ep(SHOW_B, 1, 4, FUTURE),
]}


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_get(queue_records=None, sonarr_series=None):
    def get(url, headers=None, params=None, timeout=None):
        if ":7878" in url and url.endswith("/api/v3/movie"):
            return _Resp(RADARR_MOVIES)
        if ":7878" in url and url.endswith("/api/v3/queue"):
            return _Resp({"records": queue_records or []})
        if ":8989" in url and url.endswith("/api/v3/wanted/missing"):
            return _Resp(SONARR_MISSING)
        if ":8989" in url and url.endswith("/api/v3/series"):
            return _Resp(sonarr_series or [])
        if ":8989" in url and url.endswith("/api/v3/queue"):
            return _Resp({"records": []})
        raise AssertionError(f"unexpected GET {url}")
    return get


class _Base(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        for k, v in {"radarr_url": "http://arr:7878", "radarr_api_key": "k",
                     "sonarr_url": "http://arr:8989", "sonarr_api_key": "k"}.items():
            mdb.set_setting(self.db, k, v)
        self.admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.kid = mdb.TentacleUser(jellyfin_user_id="b" * 32, display_name="Kid", is_admin=False)
        self.db.add_all([self.admin, self.kid])
        self.db.commit()
        activity.invalidate_wanted_cache()
        activity._last_queue_keys = set()
        self.addCleanup(activity.invalidate_wanted_cache)
        for p in (mock.patch.object(activity.requests, "post"),
                  mock.patch.object(activity, "_fetch_tmdb_poster", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.db.close()

    def activity(self, user=None, queue=None, sonarr_series=None):
        with mock.patch.object(activity.requests, "get", side_effect=_fake_get(queue, sonarr_series)) as g:
            out = activity.get_activity(request=None, db=self.db, user=user or self.admin)
        self.gets = [c.args[0] for c in g.call_args_list]
        return out


class TestRadarrSplit(unittest.TestCase):
    def test_one_movie_read_feeds_both_lists(self):
        with mock.patch.object(activity.requests, "get", side_effect=_fake_get()):
            wanted = activity._fetch_radarr_wanted("http://arr:7878", "k")
        self.assertEqual({1, 2}, {m["tmdb_id"] for m in wanted["searching"]})
        self.assertEqual([3], [m["tmdb_id"] for m in wanted["unreleased"]])
        for m in wanted["searching"]:
            self.assertEqual("searching", m["status"])
            self.assertTrue(m["waiting_since"].endswith("Z"))

    def test_a_radarr_failure_is_two_empty_lists(self):
        with mock.patch.object(activity.requests, "get", side_effect=OSError("down")):
            self.assertEqual({"unreleased": [], "searching": []},
                             activity._fetch_radarr_wanted("http://arr:7878", "k"))


class TestSonarrSearching(unittest.TestCase):
    def fetch(self):
        with mock.patch.object(activity.requests, "get", side_effect=_fake_get()) as g:
            out = activity._fetch_sonarr_searching("http://arr:8989", "k")
        self.params = g.call_args.kwargs["params"]
        return {x["title"]: x for x in out}

    def test_one_entry_per_series_with_the_first_missing_episode(self):
        out = self.fetch()
        self.assertEqual({"Show A", "Show B"}, set(out))
        self.assertEqual("S03E04", out["Show A"]["episode"])
        self.assertEqual(1, out["Show A"]["missing_episodes"])
        self.assertEqual("S01E01 +2", out["Show B"]["episode"])
        self.assertEqual(3, out["Show B"]["missing_episodes"])  # unaired one excluded

    def test_waiting_since_is_when_it_became_wanted(self):
        out = self.fetch()
        # Followed for 400 days, but the missing episode aired yesterday.
        a = datetime.strptime(out["Show A"]["waiting_since"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertLess(abs((a - (NOW - timedelta(days=1))).total_seconds()), 5)
        # Old episodes of a show added an hour ago: waiting since it was added.
        b = datetime.strptime(out["Show B"]["waiting_since"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertLess(abs((b - (NOW - timedelta(hours=1))).total_seconds()), 5)

    def test_asks_sonarr_for_monitored_missing_with_series(self):
        self.fetch()
        self.assertEqual("true", self.params["monitored"])
        self.assertEqual("true", self.params["includeSeries"])

    def test_skips_unmonitored_series_and_episodes(self):
        unmon = dict(SHOW_A, id=12, title="Unmonitored", monitored=False)
        data = {"records": [_ep(unmon, 1, 1, PAST), _ep(SHOW_A, 1, 1, PAST, monitored=False)]}
        with mock.patch.object(activity.requests, "get", return_value=_Resp(data)):
            self.assertEqual([], activity._fetch_sonarr_searching("http://arr:8989", "k"))

    def test_says_how_much_of_the_show_is_on_disk(self):
        with mock.patch.object(activity.requests, "get", side_effect=_fake_get()):
            out = {x["title"]: x for x in activity._fetch_sonarr_searching(
                "http://arr:8989", "k", {10: 23})}
        self.assertEqual(23, out["Show A"]["episodes_on_disk"])
        self.assertEqual(0, out["Show B"]["episodes_on_disk"])
        self.assertEqual(["S01E01", "S01E02", "S01E03"], out["Show B"]["missing_labels"])

    def test_file_counts_come_from_the_series_list(self):
        series = [dict(SHOW_A, statistics={"episodeFileCount": 9}), dict(SHOW_B)]
        with mock.patch.object(activity.requests, "get", side_effect=_fake_get(sonarr_series=series)):
            self.assertEqual({10: 9, 11: 0}, activity._fetch_sonarr_file_counts("http://arr:8989", "k"))
        with mock.patch.object(activity.requests, "get", side_effect=OSError("down")):
            self.assertEqual({}, activity._fetch_sonarr_file_counts("http://arr:8989", "k"))

    def test_a_sonarr_failure_is_an_empty_list(self):
        with mock.patch.object(activity.requests, "get", side_effect=OSError("down")):
            self.assertEqual([], activity._fetch_sonarr_searching("http://arr:8989", "k"))


class TestActivityEndpoint(_Base):
    def test_searching_is_returned_newest_request_first(self):
        out = self.activity()
        titles = [x["title"] for x in out["searching"]]
        self.assertEqual(["Newer Request", "Show B", "Searching Movie", "Show A"], titles)
        self.assertEqual(["Coming Soon"], [u["title"] for u in out["unreleased"]])

    def test_a_grab_moves_it_from_searching_to_downloads(self):
        queue = [{"id": 77, "movie": {"tmdbId": 1, "title": "Searching Movie"},
                  "size": 100, "sizeleft": 50, "status": "downloading",
                  "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading"}]
        out = self.activity(queue=queue)
        self.assertIn(1, [d["tmdb_id"] for d in out["downloads"]])
        self.assertNotIn(1, [x["tmdb_id"] for x in out["searching"]])

    def test_never_in_two_sections_at_once(self):
        queue = [{"id": 77, "movie": {"tmdbId": 2, "title": "Newer Request"},
                  "size": 100, "sizeleft": 50}]
        out = self.activity(queue=queue)
        sections = ("downloads", "searching", "unreleased")
        for tid in {x.get("tmdb_id") for s in sections for x in out[s]}:
            present = [s for s in sections if any(x.get("tmdb_id") == tid for x in out[s])]
            self.assertEqual(1, len(present), (tid, present))

    def test_a_finished_download_is_not_shown_as_searching_again(self):
        queue = [{"id": 77, "movie": {"tmdbId": 1, "title": "Searching Movie"},
                  "size": 100, "sizeleft": 0}]
        self.activity(queue=queue)            # cached while it downloads
        global RADARR_MOVIES
        saved = list(RADARR_MOVIES)
        try:
            RADARR_MOVIES[0] = dict(RADARR_MOVIES[0], hasFile=True)
            out = self.activity(queue=[])     # imported: gone from the queue
        finally:
            RADARR_MOVIES[:] = saved
        self.assertNotIn(1, [x["tmdb_id"] for x in out["searching"]])
        self.assertTrue(any(u.endswith("/api/v3/movie") for u in self.gets),
                        "leaving the queue should re-read Radarr, not serve the cache")

    def test_a_movie_with_a_radarr_file_in_tentacle_is_dropped_even_from_cache(self):
        self.activity()
        self.db.add(mdb.Movie(tmdb_id=2, title="Newer Request", source="radarr"))
        self.db.commit()
        out = self.activity()
        self.assertNotIn(2, [x["tmdb_id"] for x in out["searching"]])

    def test_the_wanted_lists_are_cached_between_polls(self):
        self.activity()
        self.activity()
        self.assertFalse(any("/movie" in u or "wanted/missing" in u for u in self.gets))

    def test_a_series_searching_for_aired_episodes_is_not_also_upcoming(self):
        series = [dict(SHOW_B, statistics={"episodeFileCount": 0}, nextAiring=FUTURE)]
        out = self.activity(sonarr_series=series)
        self.assertIn("Show B", [x["title"] for x in out["searching"]])
        self.assertNotIn("Show B", [u["title"] for u in out["unreleased"]])

    def test_non_admin_sees_only_their_requests(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=101, media_type="series", user_id=self.kid.id))
        self.db.commit()
        out = self.activity(user=self.kid)
        self.assertEqual(["Show B"], [x["title"] for x in out["searching"]])

    # TMDB numbers movies and shows separately: movie 101 and show 101 are
    # different titles. A request is for one media type only.
    def test_a_movie_request_does_not_show_the_series_with_the_same_number(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=101, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        out = self.activity(user=self.kid)
        self.assertEqual([], [x["title"] for x in out["searching"]], "the kid asked for movie 101, not Show B")

    def test_requester_is_matched_by_media_type_too(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=101, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        out = self.activity()
        by = {(x["media_type"], x["tmdb_id"]): x.get("requested_by") for x in out["searching"]}
        self.assertIsNone(by[("series", 101)], "Show B was not requested by the kid")

    def test_admin_sees_who_asked(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=1, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        out = self.activity()
        by = {x["tmdb_id"]: x.get("requested_by") for x in out["searching"]}
        self.assertEqual("Kid", by[1])
        self.assertIsNone(by[2])

    def test_per_user_edits_do_not_leak_into_the_cache(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=1, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        self.activity()                              # admin: sets requested_by
        out = self.activity(user=self.kid)           # served from cache
        self.assertNotIn("requested_by", out["searching"][0])

    def test_posters_are_filled_in(self):
        out = self.activity()
        by = {x["tmdb_id"]: x for x in out["searching"]}
        self.assertEqual("/1.jpg", by[1]["poster_path"])

    def test_capped(self):
        many = [_movie(1000 + i, f"M{i}") for i in range(40)]
        global RADARR_MOVIES
        saved = list(RADARR_MOVIES)
        try:
            RADARR_MOVIES[:] = many
            out = self.activity()
        finally:
            RADARR_MOVIES[:] = saved
        self.assertEqual(activity.SEARCHING_LIMIT, len(out["searching"]))


if __name__ == "__main__":
    unittest.main()


class TestSearchesStartedInSonarr(unittest.TestCase):
    """Re-monitor an old episode in Sonarr and press Search: Activity must show it."""

    OLD_SHOW = {"id": 20, "title": "Old Show", "tmdbId": 200, "tvdbId": 2000, "year": 2010,
                "monitored": True, "added": _iso(NOW - timedelta(days=900)), "images": []}

    def _get(self, recent_records):
        def get(url, headers=None, params=None, timeout=None):
            if url.endswith("/api/v3/wanted/missing"):
                if params["sortKey"] == "episodes.lastSearchTime":
                    return _Resp({"records": recent_records})
                return _Resp(SONARR_MISSING)
            raise AssertionError(url)
        return get

    def test_an_old_episode_just_searched_is_found_and_listed_first(self):
        old = dict(_ep(self.OLD_SHOW, 1, 3, _iso(NOW - timedelta(days=800))), id=999,
                   lastSearchTime=_iso(NOW - timedelta(minutes=2)))
        with mock.patch.object(activity.requests, "get", side_effect=self._get([old])):
            out = activity._fetch_sonarr_searching("http://arr:8989", "k")
        by = {x["title"]: x for x in out}
        self.assertIn("Old Show", by, "not in the newest-aired page, found via last searched")
        self.assertEqual("S01E03", by["Old Show"]["episode"])
        self.assertIsNotNone(by["Old Show"]["last_searched"])

    def test_the_second_page_failing_keeps_the_first(self):
        def get(url, headers=None, params=None, timeout=None):
            if params["sortKey"] == "episodes.lastSearchTime":
                raise OSError("old Sonarr")
            return _Resp(SONARR_MISSING)
        with mock.patch.object(activity.requests, "get", side_effect=get):
            self.assertEqual({"Show A", "Show B"},
                             {x["title"] for x in activity._fetch_sonarr_searching("http://arr:8989", "k")})

    def test_an_episode_on_both_pages_counts_once(self):
        dup = dict(SONARR_MISSING["records"][0], id=5)
        data = {"records": [dup]}
        with mock.patch.object(activity.requests, "get", return_value=_Resp(data)):
            out = activity._fetch_sonarr_searching("http://arr:8989", "k")
        self.assertEqual(1, out[0]["missing_episodes"])


class TestSearchOrdering(_Base):
    def test_just_searched_comes_first(self):
        old = {"title": "Old", "media_type": "series", "tmdb_id": 1,
               "waiting_since": _iso(NOW - timedelta(days=900)), "last_searched": _iso(NOW - timedelta(minutes=1))}
        new = {"title": "New", "media_type": "series", "tmdb_id": 2,
               "waiting_since": _iso(NOW - timedelta(hours=3)), "last_searched": None}
        with mock.patch.object(activity, "_fetch_radarr_wanted", return_value={"unreleased": [], "searching": []}), \
                mock.patch.object(activity, "_fetch_sonarr_unreleased", return_value=[]), \
                mock.patch.object(activity, "_fetch_sonarr_file_counts", return_value={}), \
                mock.patch.object(activity, "_fetch_sonarr_searching", return_value=[new, old]), \
                mock.patch.object(activity, "_enrich_posters"):
            out = activity._get_wanted(self.db)["searching"]
        self.assertEqual(["Old", "New"], [x["title"] for x in out])
        self.assertEqual(old["last_searched"], out[0]["waiting_since"], "the wait counts from the search")


class TestCommandWatch(_Base):
    def setUp(self):
        super().setUp()
        activity._command_watch.update(ts=0, seen=None)
        self.commands = {"radarr": [], "sonarr": []}

        self.down = set()   # apps whose command list can't be read
        self.reads = []

        def get(url, headers=None, params=None, timeout=None):
            which = "radarr" if ":7878" in url else "sonarr"
            self.reads.append(which)
            if which in self.down:
                raise activity.requests.ConnectionError(f"{which} unreachable")
            return _Resp(self.commands[which])
        p = mock.patch.object(activity.requests, "get", side_effect=get)
        p.start()
        self.addCleanup(p.stop)

    def poll(self):
        activity._command_watch["ts"] = 0  # skip the throttle
        activity._unreleased_cache.update(data={"cached": True}, ts=time.time())
        activity._watch_arr_searches(self.db)
        return activity._unreleased_cache["data"] is None

    def test_a_search_started_in_sonarr_refreshes_the_list(self):
        self.assertFalse(self.poll(), "first read only learns the baseline")
        self.commands["sonarr"] = [{"id": 7, "name": "EpisodeSearch", "status": "started"}]
        self.assertTrue(self.poll())
        self.assertFalse(self.poll(), "nothing new")
        self.commands["sonarr"] = [{"id": 7, "name": "EpisodeSearch", "status": "completed"}]
        self.assertTrue(self.poll(), "finished — whatever it found is in the lists now")

    def test_other_commands_are_ignored(self):
        self.poll()
        self.commands["radarr"] = [{"id": 1, "name": "RefreshMonitoredDownloads", "status": "started"}]
        self.assertFalse(self.poll())

    def test_movie_searches_count(self):
        self.poll()
        self.commands["radarr"] = [{"id": 2, "name": "MoviesSearch", "status": "queued"}]
        self.assertTrue(self.poll())

    def test_throttled(self):
        activity._watch_arr_searches(self.db)
        with mock.patch.object(activity.requests, "get", side_effect=AssertionError("polled")):
            activity._watch_arr_searches(self.db)

    # #135: one unreachable app must not switch the watch off for the other.
    def test_a_radarr_search_is_noticed_while_sonarr_is_unreachable(self):
        self.poll()
        self.down = {"sonarr"}
        self.assertFalse(self.poll(), "Sonarr unknown, Radarr unchanged: no change")
        self.commands["radarr"] = [{"id": 2, "name": "MoviesSearch", "status": "started"}]
        self.assertTrue(self.poll(), "Radarr's new search is seen although Sonarr is down")

    def test_a_sonarr_search_is_noticed_while_radarr_is_unreachable(self):
        self.poll()
        self.down = {"radarr"}
        self.commands["sonarr"] = [{"id": 7, "name": "EpisodeSearch", "status": "started"}]
        self.assertTrue(self.poll())

    def test_unreachable_from_the_start_the_other_app_still_counts(self):
        self.down = {"sonarr"}
        self.assertFalse(self.poll(), "first reading of Radarr is only a baseline")
        self.commands["radarr"] = [{"id": 2, "name": "MoviesSearch", "status": "queued"}]
        self.assertTrue(self.poll())

    def test_an_outage_is_not_a_change(self):
        self.poll()
        self.commands["sonarr"] = [{"id": 7, "name": "EpisodeSearch", "status": "started"}]
        self.poll()
        self.down = {"sonarr"}
        self.assertFalse(self.poll(), "an unreadable list is not an empty one")
        self.down = set()
        self.assertFalse(self.poll(), "back with the same commands: nothing new")

    def test_a_search_during_the_outage_is_seen_when_it_comes_back(self):
        self.poll()
        self.down = {"sonarr"}
        self.poll()
        self.commands["sonarr"] = [{"id": 8, "name": "SeriesSearch", "status": "completed"}]
        self.down = set()
        self.assertTrue(self.poll())

    def test_both_unreachable_changes_nothing(self):
        self.poll()
        self.down = {"radarr", "sonarr"}
        self.assertFalse(self.poll())

    def test_an_app_removed_from_settings_is_forgotten(self):
        self.poll()
        mdb.set_setting(self.db, "sonarr_url", "")
        self.assertFalse(self.poll())
        self.assertNotIn("sonarr", activity._command_watch["seen"])

    def test_a_watch_in_progress_is_not_run_twice(self):
        activity._command_watch["ts"] = 0
        with activity._command_watch_lock:
            activity._watch_arr_searches(self.db)
        self.assertEqual([], self.reads, "a second request while one is reading skips it")
        self.assertEqual(0, activity._command_watch["ts"], "and does not use up the next turn")

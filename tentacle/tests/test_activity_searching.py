"""#121: Activity lists titles that are still searching for a release.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
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

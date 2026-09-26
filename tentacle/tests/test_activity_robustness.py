"""Activity under a slow or partly unreachable Radarr/Sonarr, and whose request is whose.

Run from the tentacle/ directory:  python -m unittest discover -s tests

- #135: the search watcher read both command lists as one and gave up the whole
  turn when either failed, so a Sonarr timing out hid every search started in
  a healthy Radarr.
- #171: every request that found the wanted cache empty read every list again
  (18 in flight behind an 8 s Sonarr), and the cache was stamped when the read
  STARTED, so a read slower than the TTL was stored already expired.
- #172: download requests were matched by tmdb_id alone, but TMDB numbers
  movies and shows separately: a request for movie N showed show N.
"""
import tempfile
import threading
import time
import unittest
from unittest import mock

import models.database as mdb
import routers.activity as activity
from test_activity_searching import _Resp


class _Db(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in {"radarr_url": "http://arr:7878", "radarr_api_key": "k",
                     "sonarr_url": "http://arr:8989", "sonarr_api_key": "k"}.items():
            mdb.set_setting(self.db, k, v)
        activity.invalidate_wanted_cache()
        self.addCleanup(activity.invalidate_wanted_cache)


class OneAppDownDoesNotBlindTheOther(_Db):
    def setUp(self):
        super().setUp()
        activity._command_watch.update(ts=0, seen={})
        self.radarr = []
        self.sonarr_up = True

        def get(url, headers=None, params=None, timeout=None):
            if ":8989" in url:
                if not self.sonarr_up:
                    raise TimeoutError("Sonarr read timed out")
                return _Resp([])
            return _Resp(self.radarr)
        p = mock.patch.object(activity.requests, "get", side_effect=get)
        p.start()
        self.addCleanup(p.stop)

    def poll(self):
        activity._command_watch["ts"] = 0
        activity._unreleased_cache.update(data={"cached": True}, ts=time.time())
        activity._watch_arr_searches(self.db)
        return activity._unreleased_cache["data"] is None

    def test_a_radarr_search_is_noticed_while_sonarr_is_down(self):
        self.assertFalse(self.poll())
        self.sonarr_up = False
        self.radarr = [{"id": 3, "name": "MoviesSearch", "status": "started"}]
        self.assertTrue(self.poll(), "a Sonarr timeout hid Radarr's search")

    def test_sonarr_coming_back_unchanged_is_not_a_change(self):
        self.poll()
        self.sonarr_up = False
        self.assertFalse(self.poll())
        self.sonarr_up = True
        self.assertFalse(self.poll(), "an unknown reading was taken for a change")

    def test_a_concurrent_poll_does_not_read_the_lists_again(self):
        activity._command_watch["ts"] = 0
        with activity._command_watch_lock:
            with mock.patch.object(activity.requests, "get", side_effect=AssertionError("read twice")):
                activity._watch_arr_searches(self.db)


class WantedIsReadOnce(_Db):
    def test_concurrent_requests_share_one_read(self):
        reads = []

        def slow_read(db):
            reads.append(1)
            time.sleep(0.3)
            return {"unreleased": [], "searching": [{"title": "X"}]}
        with mock.patch.object(activity, "_read_wanted", side_effect=slow_read):
            results = []
            threads = [threading.Thread(target=lambda: results.append(activity._get_wanted(self.db)))
                       for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(1, len(reads), "every waiting request read the lists again")
        self.assertEqual(6, len(results))
        self.assertTrue(all(r["searching"] == [{"title": "X"}] for r in results))

    def test_a_read_slower_than_the_ttl_is_still_stored_fresh(self):
        reads = []

        def slow_read(db):
            reads.append(1)
            time.sleep(0.3)
            return {"unreleased": [], "searching": []}
        with mock.patch.object(activity, "UNRELEASED_TTL", 0.2), \
                mock.patch.object(activity, "_read_wanted", side_effect=slow_read):
            activity._get_wanted(self.db)
            activity._get_wanted(self.db)
        self.assertEqual(1, len(reads), "the cache was stamped when the read began, so it was born stale")

    def test_a_read_that_raced_an_invalidation_is_not_stored(self):
        def racing_read(db):
            activity.invalidate_wanted_cache()   # e.g. a search started meanwhile
            return {"unreleased": [], "searching": []}
        with mock.patch.object(activity, "_read_wanted", side_effect=racing_read):
            out = activity._get_wanted(self.db)
        self.assertEqual({"unreleased": [], "searching": []}, out)
        self.assertIsNone(activity._unreleased_cache["data"])


class RequestsAreMatchedByMediaType(_Db):
    def setUp(self):
        super().setUp()
        self.admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.kid = mdb.TentacleUser(jellyfin_user_id="b" * 32, display_name="Kid", is_admin=False)
        self.db.add_all([self.admin, self.kid])
        self.db.commit()
        self.db.add(mdb.DownloadRequest(tmdb_id=1001, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        series = {"media_type": "series", "tmdb_id": 1001, "title": "Series A"}
        movie = {"media_type": "movie", "tmdb_id": 1001, "title": "Movie A"}
        from services import arr_insight
        patches = [
            mock.patch.object(activity, "_build_downloads", lambda db: [dict(series), dict(movie)]),
            mock.patch.object(activity, "_watch_arr_searches", lambda db: None),
            mock.patch.object(activity, "_get_wanted",
                              lambda db: {"unreleased": [], "searching": [dict(series)]}),
            mock.patch.object(activity, "_get_recently_downloaded", lambda db: [dict(series)]),
            mock.patch.object(arr_insight, "cached_line", lambda *a: None),
            mock.patch.object(arr_insight, "searching_problems", lambda db: []),
            mock.patch.object(arr_insight, "coming_up", lambda db: [dict(series)]),
            mock.patch.object(activity, "_enrich_posters", lambda db, items: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _as(self, user):
        return activity.get_activity(request=None, db=self.db, user=user)

    def test_a_movie_request_does_not_show_the_series_with_the_same_number(self):
        out = self._as(self.kid)
        self.assertEqual([("movie", 1001)], [(d["media_type"], d["tmdb_id"]) for d in out["downloads"]])
        for section in ("searching", "recently_downloaded", "coming_up"):
            self.assertEqual([], out[section], section)

    def test_requester_is_matched_by_media_type_too(self):
        out = self._as(self.admin)
        by = {(d["media_type"], d["tmdb_id"]): d.get("requested_by") for d in out["downloads"]}
        self.assertEqual("Kid", by[("movie", 1001)])
        self.assertIsNone(by[("series", 1001)])


if __name__ == "__main__":
    unittest.main()

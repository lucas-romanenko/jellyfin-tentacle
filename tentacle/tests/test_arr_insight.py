"""Release checks, Radarr/Sonarr problems and the week ahead (services/arr_insight.py).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import HTTPException

import models.database as mdb
import routers.activity as activity
from services import arr_insight

NOW = datetime.utcnow()


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def rel(title, quality="WEBDL-1080p", resolution=1080, rejections=(), seeders=10, temp=False, guid=None):
    return {"title": title, "quality": {"quality": {"name": quality, "resolution": resolution}},
            "size": 4 * 1024 ** 3, "seeders": seeders, "leechers": 1, "protocol": "torrent", "indexer": "Idx",
            "languages": [{"name": "English"}], "age": 3, "rejected": bool(rejections),
            "temporarilyRejected": temp, "rejections": list(rejections),
            "guid": guid or f"g-{title}", "indexerId": 4}


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, "x"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._data


class TestSummarize(unittest.TestCase):
    def test_nothing_found(self):
        s = arr_insight.summarize([], "Radarr")
        self.assertEqual("none", s["state"])
        self.assertEqual("No releases found", s["short"])

    def test_usable_ones(self):
        s = arr_insight.summarize([rel("a"), rel("b", rejections=["720p is not wanted in profile"])], "Radarr")
        self.assertEqual("usable", s["state"])
        self.assertEqual("1 usable release found", s["short"])
        # Radarr never grabs from an interactive search and never searches a
        # missing movie again by itself: it will not "grab one shortly".
        self.assertNotIn("shortly", s["summary"])
        self.assertIn("Search again", s["summary"])
        self.assertEqual("a", s["releases"][0]["title"], "usable first")

    def test_all_rejected_says_why_and_the_best_quality(self):
        s = arr_insight.summarize([
            rel("a", "HDTV-720p", 720, ["HDTV-720p is not wanted in profile"]),
            rel("b", "WEBDL-480p", 480, ["WEBDL-480p is not wanted in profile"]),
            rel("c", "Bluray-2160p", 2160, ["Maximum size exceeded: 60 GB"]),
            rel("d", "HDTV-720p", 720, ["Not enough seeders: 0. Minimum seeders: 1"]),
        ], "Sonarr")
        self.assertEqual("rejected", s["state"])
        self.assertEqual("None usable: quality not in your profile", s["short"])
        self.assertIn("Found 4 releases, none usable: 2 quality not in your profile (best was HDTV-720p)", s["summary"],
                      "best among the quality rejections, not the 2160p that was too big")
        self.assertIn("1 wrong size", s["summary"])
        self.assertEqual("Bluray-2160p", s["releases"][0]["quality"], "rejected ones best quality first")

    def test_held_by_a_delay_profile(self):
        s = arr_insight.summarize([rel("a", rejections=["Waiting for better quality release (delay)"], temp=True)], "Radarr")
        self.assertEqual("delayed", s["state"])

    def test_reason_words(self):
        for text, label in (("Language is not wanted", "wrong language"),
                            ("Language German is not wanted in profile", "wrong language"),
                            ("Release is blocklisted", "blocklisted"),
                            ("Existing file on disk is of equal or higher preference", "not better than what you have"),
                            ("Custom Formats x do not meet minimum score", "custom format score too low"),
                            ("Unknown Series", "doesn't match this title"),
                            ("Something new", "other reasons")):
            self.assertEqual(label, arr_insight.reason_of(text)[1], text)

    def test_object_rejections_from_newer_versions(self):
        s = arr_insight.summarize([rel("a", rejections=[{"reason": "Language is not wanted", "type": "permanent"}])], "Radarr")
        self.assertEqual(["wrong language"], s["releases"][0]["reasons"])


class _Db(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        for k, v in {"radarr_url": "http://r:7878", "radarr_api_key": "k",
                     "sonarr_url": "http://s:8989", "sonarr_api_key": "k"}.items():
            mdb.set_setting(self.db, k, v)
        arr_insight._checks.clear()
        arr_insight._problems_cache.update(at=0, data=None)
        arr_insight._calendar_cache.update(at=0, data=None)
        self.addCleanup(self.db.close)


class FakeSonarrSvc:
    def __init__(self, eps):
        self.eps = eps

    def get_episodes(self, sid):
        return self.eps


class TestCheck(_Db):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.releases = [rel("x", "HDTV-720p", 720, ["HDTV-720p is not wanted in profile"])]

        def get(url, headers=None, params=None, timeout=None):
            self.calls.append((url, dict(params or {}), timeout))
            return _Resp(self.releases)
        p = mock.patch.object(arr_insight.requests, "get", side_effect=get)
        p.start()
        self.addCleanup(p.stop)
        self.eps = [
            {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False,
             "airDateUtc": _iso(NOW - timedelta(days=40))},
            {"id": 2, "seasonNumber": 2, "episodeNumber": 3, "monitored": True, "hasFile": False,
             "airDateUtc": _iso(NOW - timedelta(days=2))},
            {"id": 3, "seasonNumber": 2, "episodeNumber": 4, "monitored": True, "hasFile": False,
             "airDateUtc": _iso(NOW + timedelta(days=5))},
        ]
        p2 = mock.patch.object(activity, "_find_arr_record", side_effect=self.find)
        p2.start()
        self.addCleanup(p2.stop)

    def find(self, db, title):
        if title.media_type == "movie":
            return None, {"id": 55, "title": "Rare Film"}
        return FakeSonarrSvc(self.eps), {"id": 77, "title": "Show"}

    def test_movie_searches_by_radarr_id(self):
        d = arr_insight.check(self.db, "movie", 100)
        self.assertEqual({"movieId": 55}, self.calls[0][1])
        self.assertEqual(arr_insight.SEARCH_TIMEOUT, self.calls[0][2])
        self.assertEqual("rejected", d["state"])

    def test_show_checks_its_newest_aired_missing_episode(self):
        d = arr_insight.check(self.db, "series", 200)
        self.assertEqual({"episodeId": 2}, self.calls[0][1], "not the unaired one, not the old one")
        self.assertEqual("S02E03", d["scope"])

    def test_nothing_missing(self):
        self.eps = [dict(self.eps[0], hasFile=True)]
        with self.assertRaises(arr_insight.InsightError) as e:
            arr_insight.check(self.db, "series", 200)
        self.assertEqual(409, e.exception.status)

    def test_reused_until_asked_fresh(self):
        arr_insight.check(self.db, "movie", 100)
        arr_insight.check(self.db, "movie", 100)
        self.assertEqual(1, len(self.calls))
        arr_insight.check(self.db, "movie", 100, max_age=0)
        self.assertEqual(2, len(self.calls))

    def test_the_card_line(self):
        self.assertIsNone(arr_insight.cached_line("movie", 100))
        arr_insight.check(self.db, "movie", 100)
        line = arr_insight.cached_line("movie", 100)
        self.assertEqual("None usable: quality not in your profile", line["short"])
        arr_insight.forget("movie", 100)
        self.assertIsNone(arr_insight.cached_line("movie", 100))

    def test_slow_indexers(self):
        import requests
        with mock.patch.object(arr_insight.requests, "get", side_effect=requests.Timeout()):
            with self.assertRaises(arr_insight.InsightError) as e:
                arr_insight.check(self.db, "movie", 100)
        self.assertEqual(504, e.exception.status)

    def test_one_search_per_title_at_a_time(self):
        import threading
        gate = threading.Event()

        def slow(url, headers=None, params=None, timeout=None):
            self.calls.append((url, dict(params or {}), timeout))
            gate.wait(5)
            return _Resp(self.releases)
        out = []
        with mock.patch.object(arr_insight.requests, "get", side_effect=slow):
            ts = [threading.Thread(target=lambda: out.append(arr_insight.check(self.db, "movie", 100, max_age=0)))
                  for _ in range(4)]
            [t.start() for t in ts]
            time.sleep(0.3)
            gate.set()
            [t.join(10) for t in ts]
        self.assertEqual(4, len(out))
        self.assertEqual(1, len(self.calls), "the other three waited for the running search")
        self.assertEqual({}, arr_insight._inflight)

    def test_a_failed_search_lets_the_next_caller_search(self):
        import requests
        with mock.patch.object(arr_insight.requests, "get", side_effect=requests.Timeout()):
            with self.assertRaises(arr_insight.InsightError):
                arr_insight.check(self.db, "movie", 100)
        self.assertEqual({}, arr_insight._inflight)
        arr_insight.check(self.db, "movie", 100)
        self.assertEqual(1, len(self.calls))

    def test_waiters_share_a_failure_instead_of_searching_again(self):
        import requests, threading
        gate = threading.Event()

        def slow_fail(url, headers=None, params=None, timeout=None):
            self.calls.append(url)
            gate.wait(5)
            raise requests.Timeout()
        errs = []

        def go():
            try:
                arr_insight.check(self.db, "movie", 100, max_age=0)
            except arr_insight.InsightError as e:
                errs.append(e.status)
        with mock.patch.object(arr_insight.requests, "get", side_effect=slow_fail):
            ts = [threading.Thread(target=go) for _ in range(4)]
            [t.start() for t in ts]
            time.sleep(0.3)
            gate.set()
            [t.join(10) for t in ts]
        self.assertEqual([504] * 4, errs)
        self.assertEqual(1, len(self.calls), "one failing search, not one per caller in turn")

    def test_unknown_title(self):
        activity._find_arr_record.side_effect = HTTPException(404, "This movie is not in Radarr")
        with self.assertRaises(arr_insight.InsightError) as e:
            arr_insight.check(self.db, "movie", 100)
        self.assertEqual(404, e.exception.status)


def _offer(media_type, tmdb, guid="g1", indexer_id=4, ids=None):
    """A check for this title that listed one release."""
    arr_insight._checks[arr_insight.title_key(media_type, tmdb)] = {
        "at": time.time(), "ids": ids if ids is not None else ({"movieId": 55} if media_type == "movie" else
                                                                {"seriesId": 77, "episodeId": 2}),
        "data": {"releases": [{"guid": guid, "indexer_id": indexer_id}]}}


class TestGrab(_Db):
    def test_grabs_by_guid_and_indexer(self):
        _offer("movie", 100)
        with mock.patch.object(arr_insight.requests, "post", return_value=_Resp({})) as post:
            r = arr_insight.grab(self.db, "movie", 100, 0, "g1", 4)
        self.assertTrue(r["ok"])
        self.assertEqual({"guid": "g1", "indexerId": 4}, post.call_args.kwargs["json"])
        self.assertTrue(post.call_args.args[0].startswith("http://r:7878/api/v3/release"))

    def test_an_unmatched_release_is_not_called_too_old(self):
        _offer("movie", 100)
        msg = {"message": "Unable to find matching movie, will need to be manually provided"}
        with mock.patch.object(arr_insight.requests, "post", return_value=_Resp(msg, 404)):
            with self.assertRaises(arr_insight.InsightError) as e:
                arr_insight.grab(self.db, "movie", 100, 0, "g1", 4)
        self.assertEqual(502, e.exception.status)
        self.assertIn("Unable to find matching movie", str(e.exception))
        self.assertIn(arr_insight.title_key("movie", 100), arr_insight._checks, "the list itself is fine")

    def test_an_expired_list_says_check_again(self):
        _offer("series", 5)
        with mock.patch.object(arr_insight.requests, "post", return_value=_Resp(
                {"message": "Couldn't find requested release in cache, try searching again"}, 404)):
            with self.assertRaises(arr_insight.InsightError) as e:
                arr_insight.grab(self.db, "series", 5, 0, "g1", 4)
        self.assertEqual(409, e.exception.status)
        self.assertNotIn(arr_insight.title_key("series", 5), arr_insight._checks, "stale check dropped")

    def test_refused_carries_the_reason(self):
        _offer("movie", 100)
        with mock.patch.object(arr_insight.requests, "post", return_value=_Resp({"message": "Download client unavailable"}, 500)):
            with self.assertRaises(arr_insight.InsightError) as e:
                arr_insight.grab(self.db, "movie", 100, 0, "g1", 4)
        self.assertIn("Download client unavailable", str(e.exception))


class TestProblems(_Db):
    def fake(self, health_r, health_s, disks=None, roots=None):
        def get(url, headers=None, params=None, timeout=None):
            app = "radarr" if ":7878" in url else "sonarr"
            if url.endswith("/health"):
                h = health_r if app == "radarr" else health_s
                if isinstance(h, Exception):
                    raise h
                return _Resp(h)
            if url.endswith("/rootfolder"):
                return _Resp((roots or {}).get(app, []))
            if url.endswith("/diskspace"):
                return _Resp(disks or [])
            raise AssertionError(url)
        return mock.patch.object(arr_insight.requests, "get", side_effect=get)

    def test_warnings_and_errors_only_and_never_update_notices(self):
        with self.fake([{"source": "IndexerStatusCheck", "type": "error", "message": "Indexers unavailable: Idx"},
                        {"source": "UpdateCheck", "type": "warning", "message": "New update"},
                        {"source": "SystemTimeCheck", "type": "notice", "message": "fine"}],
                       [{"source": "DownloadClientCheck", "type": "warning", "message": "qBittorrent unreachable"}]):
            p = arr_insight.problems(self.db)
        self.assertEqual([("Radarr", "indexer", "error"), ("Sonarr", "download_client", "warning")],
                         [(x["app"], x["kind"], x["level"]) for x in p])

    def test_unreachable_arr_is_a_problem(self):
        with self.fake(OSError("refused"), []):
            p = arr_insight.problems(self.db)
        self.assertEqual("unreachable", p[0]["kind"])
        self.assertIn("can't reach Radarr", p[0]["message"])

    def test_low_disk_once_per_disk(self):
        disks = [{"path": "/", "freeSpace": 900 * 1024 ** 3, "totalSpace": 1000 * 1024 ** 3},
                 {"path": "/data", "freeSpace": 8 * 1024 ** 3, "totalSpace": 4000 * 1024 ** 3}]
        roots = {"radarr": [{"path": "/data/movies"}], "sonarr": [{"path": "/data/tv"}]}
        with self.fake([], [], disks, roots):
            p = arr_insight.problems(self.db)
        self.assertEqual(1, len(p), "shared disk reported once")
        self.assertEqual("disk", p[0]["kind"])
        self.assertIn("Only 8 GB free", p[0]["message"])

    def test_low_disk_on_windows_paths(self):
        disks = [{"path": "C:\\", "freeSpace": 900 * 1024 ** 3, "totalSpace": 1000 * 1024 ** 3},
                 {"path": "D:\\", "freeSpace": 5 * 1024 ** 3, "totalSpace": 4000 * 1024 ** 3}]
        roots = {"radarr": [{"path": "d:\\Movies\\"}], "sonarr": []}
        with self.fake([], [], disks, roots):
            p = arr_insight.problems(self.db)
        self.assertEqual([("disk", "D:\\")], [(x["kind"], x["disk"]) for x in p])

    def test_cached(self):
        with self.fake([], []):
            arr_insight.problems(self.db)
        with mock.patch.object(arr_insight.requests, "get", side_effect=AssertionError("polled")):
            arr_insight.problems(self.db)

    def test_searching_problems_leave_out_the_rest(self):
        arr_insight._problems_cache.update(at=time.time(), data=[
            {"kind": "indexer"}, {"kind": "other"}, {"kind": "disk"}])
        self.assertEqual(["indexer", "disk"], [p["kind"] for p in arr_insight.searching_problems(self.db)])


class TestComingUp(_Db):
    def test_monitored_episodes_without_files_soonest_first(self):
        show = {"title": "Show", "tmdbId": 9, "tvdbId": 90, "monitored": True, "images": []}
        eps = [
            {"seasonNumber": 1, "episodeNumber": 3, "airDateUtc": _iso(NOW + timedelta(days=3)), "monitored": True, "series": show},
            {"seasonNumber": 1, "episodeNumber": 2, "airDateUtc": _iso(NOW + timedelta(days=1)), "monitored": True, "series": show},
            {"seasonNumber": 1, "episodeNumber": 1, "airDateUtc": _iso(NOW + timedelta(hours=2)), "monitored": True,
             "hasFile": True, "series": show},
            {"seasonNumber": 1, "episodeNumber": 4, "airDateUtc": _iso(NOW + timedelta(days=4)), "monitored": False, "series": show},
        ]
        with mock.patch.object(arr_insight.requests, "get", return_value=_Resp(eps)) as g:
            out = arr_insight.coming_up(self.db)
        self.assertEqual(["S01E02", "S01E03"], [x["episode"] for x in out])
        self.assertEqual("false", g.call_args.kwargs["params"]["unmonitored"])


class TestComingUpFailing(_Db):
    def test_a_failing_calendar_is_not_asked_on_every_poll(self):
        import requests
        with mock.patch.object(arr_insight.requests, "get", side_effect=requests.Timeout()) as g:
            self.assertEqual([], arr_insight.coming_up(self.db))
            self.assertEqual([], arr_insight.coming_up(self.db))
        self.assertEqual(1, g.call_count)
        arr_insight._calendar_cache["at"] -= 61
        with mock.patch.object(arr_insight.requests, "get", return_value=_Resp([])) as g:
            arr_insight.coming_up(self.db)
        self.assertEqual(1, g.call_count, "asked again after a minute")


class TestAutoChecks(_Db):
    def test_a_failing_check_does_not_hold_back_the_rest(self):
        old = _iso(NOW - timedelta(hours=3))
        wanted = {"searching": [
            {"media_type": "movie", "tmdb_id": t, "title": str(t), "waiting_since": old} for t in (1, 2, 3, 4)]}
        checked = []

        def check(db, mt, tm, tv):
            checked.append(tm)
            if tm in (1, 2):
                raise arr_insight.InsightError(504, "slow indexers")
        arr_insight._auto_failed.clear()
        self.addCleanup(arr_insight._auto_failed.clear)
        with mock.patch("models.database.SessionLocal", return_value=self.db), \
                mock.patch.object(activity, "_get_wanted", return_value=wanted), \
                mock.patch.object(arr_insight, "check", side_effect=check), \
                mock.patch.object(self.db, "close"):
            arr_insight.run_auto_checks()
            arr_insight.run_auto_checks()
        self.assertEqual([1, 2, 3, 4], checked, "the second run moves on to the others")

    def test_only_long_searching_unchecked_titles_a_couple_at_a_time(self):
        old = _iso(NOW - timedelta(hours=3))
        wanted = {"searching": [
            {"media_type": "movie", "tmdb_id": 1, "title": "a", "waiting_since": old},
            {"media_type": "movie", "tmdb_id": 2, "title": "b", "waiting_since": _iso(NOW - timedelta(minutes=5))},
            {"media_type": "movie", "tmdb_id": 3, "title": "c", "waiting_since": old},
            {"media_type": "movie", "tmdb_id": 4, "title": "d", "waiting_since": old},
            {"media_type": "movie", "tmdb_id": 5, "title": "e", "waiting_since": old},
        ]}
        arr_insight._checks[arr_insight.title_key("movie", 3)] = {"at": time.time(), "data": {
            "state": "none", "short": "", "summary": "", "checked_at": ""}}
        checked = []
        with mock.patch("models.database.SessionLocal", return_value=self.db), \
                mock.patch.object(activity, "_get_wanted", return_value=wanted), \
                mock.patch.object(arr_insight, "check", side_effect=lambda db, mt, tm, tv: checked.append(tm)), \
                mock.patch.object(self.db, "close"):
            arr_insight.run_auto_checks()
        self.assertEqual([1, 4], checked, "not the new one, not the checked one, at most two")


class TestActivityEndpoints(_Db):
    def setUp(self):
        super().setUp()
        self.admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.kid = mdb.TentacleUser(jellyfin_user_id="b" * 32, display_name="Kid", is_admin=False)
        self.db.add_all([self.admin, self.kid])
        self.db.commit()

    def test_check_needs_permission(self):
        with self.assertRaises(HTTPException) as e:
            activity.check_releases(activity.ArrTitle(media_type="movie", tmdb_id=100), db=self.db, user=self.kid)
        self.assertEqual(403, e.exception.status_code)

    def test_check_fresh_skips_the_cache(self):
        with mock.patch.object(arr_insight, "check", return_value={"state": "none"}) as c:
            activity.check_releases(activity.ArrTitle(media_type="movie", tmdb_id=100, fresh=True), db=self.db, user=self.admin)
        self.assertEqual(0, c.call_args.kwargs["max_age"])

    def test_the_pick_list_is_never_older_than_radarr_keeps_releases(self):
        # An automatic check from hours ago still feeds the Searching card, but
        # its releases can't be downloaded any more (Radarr/Sonarr keep them
        # ~30 min), so opening the list searches again.
        with mock.patch.object(arr_insight, "check", return_value={"state": "none"}) as c:
            activity.check_releases(activity.ArrTitle(media_type="movie", tmdb_id=100), db=self.db, user=self.admin)
        self.assertEqual(arr_insight.GRAB_FRESH, c.call_args.kwargs["max_age"])

    def test_grab_needs_a_release(self):
        with self.assertRaises(HTTPException) as e:
            activity.grab_release(activity.ArrTitle(media_type="movie", tmdb_id=100), db=self.db, user=self.admin)
        self.assertEqual(400, e.exception.status_code)

    def test_grab_errors_pass_through(self):
        with mock.patch.object(arr_insight, "grab", side_effect=arr_insight.InsightError(409, "too old")):
            with self.assertRaises(HTTPException) as e:
                activity.grab_release(activity.ArrTitle(media_type="movie", tmdb_id=100, guid="g", indexer_id=1),
                                      db=self.db, user=self.admin)
        self.assertEqual((409, "too old"), (e.exception.status_code, e.exception.detail))


if __name__ == "__main__":
    unittest.main()

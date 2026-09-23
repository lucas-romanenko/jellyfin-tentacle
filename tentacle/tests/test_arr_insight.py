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

    def test_unknown_title(self):
        activity._find_arr_record.side_effect = HTTPException(404, "This movie is not in Radarr")
        with self.assertRaises(arr_insight.InsightError) as e:
            arr_insight.check(self.db, "movie", 100)
        self.assertEqual(404, e.exception.status)


class TestGrab(_Db):
    def test_grabs_by_guid_and_indexer(self):
        with mock.patch.object(arr_insight.requests, "post", return_value=_Resp({})) as post:
            r = arr_insight.grab(self.db, "movie", 100, 0, "g1", 4)
        self.assertTrue(r["ok"])
        self.assertEqual({"guid": "g1", "indexerId": 4}, post.call_args.kwargs["json"])
        self.assertTrue(post.call_args.args[0].startswith("http://r:7878/api/v3/release"))

    def test_an_expired_list_says_check_again(self):
        arr_insight._checks[arr_insight.title_key("series", 5)] = {"at": time.time(), "data": {}}
        with mock.patch.object(arr_insight.requests, "post", return_value=_Resp({}, 404)):
            with self.assertRaises(arr_insight.InsightError) as e:
                arr_insight.grab(self.db, "series", 5, 0, "g1", 4)
        self.assertEqual(409, e.exception.status)
        self.assertNotIn(arr_insight.title_key("series", 5), arr_insight._checks, "stale check dropped")

    def test_refused_carries_the_reason(self):
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


class TestAutoChecks(_Db):
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

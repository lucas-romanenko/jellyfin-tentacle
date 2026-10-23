"""A Sonarr show that Sonarr lists without a tmdbId must not lose its row and
its request because TMDB failed to answer once.

scan_sonarr_library (services/sonarr.py) resolves such a show through
TMDBService.find_by_tvdb_id (then a title search). When both come back
empty the show is skipped (`continue`) and never added to
`resolved_tmdb_ids`, so the removal loop further down reads the row as
"no longer in Sonarr": it deletes the row, the person's DownloadRequest
(their "<name>'s Downloads", delete/replace rights) and the droppable
duplicate tombstones, and the Deletion log says "no longer in Sonarr"
although Sonarr still lists the show.

find_by_tvdb_id (services/tmdb.py) then stores "" for that tvdb id after
ANY failure (a 5xx, 429, 401/403, a timeout), with no ttl, so _cache_get's
30-day default applies: for a month every scan skips the show again and the
row cannot come back. `_request` itself says a refused key / rate limit /
server error "says nothing" about the title, and NEGATIVE_TTL_SECONDS
says a real no-match shouldn't be cached for 30 days either.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import unittest
from datetime import datetime
from unittest import mock

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hermetic import no_tmdb
from tmp_dirs import temp_dir

TVDB_ONLY = {"tvdbId": 77, "title": "Obscure Show", "year": 2019, "path": "/tv/Obscure Show (2019)",
             "monitorNewItems": "none", "statistics": {"episodeFileCount": 4}, "id": 3}
OTHERS = [{"tmdbId": 1000 + i, "tvdbId": 9000 + i, "title": f"Show {i}", "path": f"/tv/Show {i}",
           "monitorNewItems": "none", "statistics": {"episodeFileCount": 2}, "id": 100 + i}
          for i in range(10)]


class FakeSonarr:
    listed = None

    def __init__(self, *a, **k):
        pass

    def get_all_series(self):
        return [dict(s) for s in FakeSonarr.listed]


class FakeTmdb:
    """TMDB as the scan sees it: find_by_tvdb_id answers 500 for tvdb 77 while
    it works, None while TMDB is failing (lookup_failed() then says so)."""
    up = True

    def __init__(self, *a, **k):
        pass

    def find_by_tvdb_id(self, tvdb_id):
        return 500 if (tvdb_id == 77 and FakeTmdb.up) else None

    def search_series(self, title, year=None):
        return None

    def get_series_details(self, tmdb_id):
        return None

    def lookup_failed(self):
        return not FakeTmdb.up

    def _lookup_failed(self):
        return not FakeTmdb.up


class ScanKeepsRowsOnTmdbBlip(unittest.TestCase):
    def setUp(self):
        no_tmdb(self)
        import models.database as mdb
        self.mdb = mdb
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"), ("data_dir", self.tmp),
                     ("tmdb_bearer_token", "x")):
            mdb.set_setting(self.db, k, v)
        ann = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Ann", is_admin=False)
        self.db.add(ann)
        self.db.commit()
        self.ann = ann.id
        FakeSonarr.listed = [TVDB_ONLY] + OTHERS
        FakeTmdb.up = True

    def scan(self):
        import services.sonarr as sonarr
        with mock.patch.object(sonarr, "SonarrService", FakeSonarr), \
             mock.patch.object(sonarr, "TMDBService", FakeTmdb), \
             mock.patch("services.tmdb.get_tmdb_token", return_value="x"):
            out = sonarr.scan_sonarr_library(self.db)
        self.db.commit()
        return out

    def rows(self):
        return self.db.query(self.mdb.Series).filter_by(tmdb_id=500).all()

    def requests_for_it(self):
        return self.db.query(self.mdb.DownloadRequest).filter_by(tmdb_id=500, media_type="series").all()

    def test_one_failed_tmdb_lookup_keeps_the_row_and_the_request(self):
        self.scan()
        self.assertEqual(["sonarr"], [r.source for r in self.rows()])
        self.db.add(self.mdb.DownloadRequest(tmdb_id=500, media_type="series", user_id=self.ann))
        self.db.commit()

        FakeTmdb.up = False            # one scan while TMDB times out / answers 5xx / 429
        out = self.scan()

        self.assertEqual(1, len(self.rows()),
                         f"the show's row was deleted although Sonarr still lists it (scan said {out.get('removed')} "
                         f"removed; deletion log: {[(d.kind, d.detail) for d in self.db.query(self.mdb.DeletionLog)]})")
        self.assertEqual(1, len(self.requests_for_it()), "Ann's request for the show was deleted")

    def test_a_show_really_gone_from_sonarr_still_goes(self):
        self.scan()
        FakeSonarr.listed = list(OTHERS)   # removed from Sonarr; TMDB fine
        self.scan()
        self.assertEqual([], self.rows())


class FindByTvdbDoesNotCacheFailures(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)

    def tmdb(self):
        from services.tmdb import TMDBService
        return TMDBService("tok", self.tmp)

    @staticmethod
    def answer(status, body=None):
        resp = mock.Mock(status_code=status)
        resp.json.return_value = body or {}
        if status >= 400:
            err = requests.HTTPError(f"{status} Error")
            err.response = resp
            resp.raise_for_status.side_effect = err
        else:
            resp.raise_for_status.return_value = None
        return resp

    def test_a_server_error_is_asked_again_next_time(self):
        t = self.tmdb()
        with mock.patch.object(t.session, "get", return_value=self.answer(500)):
            self.assertIsNone(t.find_by_tvdb_id(77))
        found = self.answer(200, {"tv_results": [{"id": 500}]})
        with mock.patch.object(t.session, "get", return_value=found) as get:
            got = t.find_by_tvdb_id(77)
        self.assertTrue(get.called, "TMDB was not asked again: the 500 was cached as 'no match' "
                                    f"({t._cache_path('tvdb_to_tmdb:77').read_text()})")
        self.assertEqual(500, got)

    def test_a_rate_limit_or_refused_key_is_not_a_no_match_either(self):
        for status in (429, 401, 503):
            with self.subTest(status=status):
                t = self.tmdb()
                with mock.patch.object(t.session, "get", return_value=self.answer(status)):
                    t.find_by_tvdb_id(78)
                hit, _ = t._cache_lookup("tvdb_to_tmdb:78")
                self.assertFalse(hit, f"HTTP {status} stored as a cached answer")
                t._cache_path("tvdb_to_tmdb:78").unlink(missing_ok=True)

    def test_a_real_no_match_is_asked_again_after_the_negative_ttl(self):
        t = self.tmdb()
        with mock.patch.object(t.session, "get", return_value=self.answer(200, {"tv_results": []})):
            self.assertIsNone(t.find_by_tvdb_id(79))
        path = t._cache_path("tvdb_to_tmdb:79")
        data = json.loads(path.read_text())
        # Four days later (past NEGATIVE_TTL_SECONDS = 3 days) TMDB lists it.
        data["ts"] = datetime.now().timestamp() - 4 * 86400
        path.write_text(json.dumps(data))
        with mock.patch.object(t.session, "get", return_value=self.answer(200, {"tv_results": [{"id": 9}]})) as get:
            got = t.find_by_tvdb_id(79)
        self.assertTrue(get.called, f"a no-match from 4 days ago is still served from the cache ({data})")
        self.assertEqual(9, got)


if __name__ == "__main__":
    unittest.main()

"""Duplicates: a duplicate is resolved once (#328).

resolve_duplicate acted on a duplicate without checking that it was still
pending, and Resolve All loaded the pending list once and never looked again.
Keep Downloaded in one tab and Keep VOD in a stale one (or both at once, or
Resolve All racing a single Keep VOD) deleted the .strm and then the download:
no copy of the title left. Any resolution string was saved too, and
"pending" reopened a resolved duplicate. Now a resolution is one of
keep_radarr, keep_vod, keep_both; each duplicate is resolved under one lock
that re-reads it first, and one that isn't pending any more is refused (409)
with nothing deleted.

Run from tentacle/:  python -m unittest tests.test_duplicate_resolved_once
"""
import logging
import shutil
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from models.database import Base, Duplicate, Movie, Setting
from routers import duplicates
from tests.test_duplicate_keep_vod_merged_folder import FakeRadarr, FakeSonarr, _FakeArr
from tmp_dirs import temp_dir


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = Path(tmp)
        # As shipped: WAL, one session per request
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _wal(conn, _):
            conn.execute("PRAGMA journal_mode=WAL")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(lambda: self.db.close())
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r")):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()
        _FakeArr.titles = {}
        _FakeArr.calls = []
        _FakeArr.fail_file_delete = False
        mock.patch("services.radarr.RadarrService", FakeRadarr).start()
        mock.patch("services.sonarr.SonarrService", FakeSonarr).start()
        self.addCleanup(mock.patch.stopall)
        self.films = {}

    def film(self, tmdb_id, name):
        vod = self.root / "vod" / "movies" / name
        vod.mkdir(parents=True)
        strm, nfo = vod / f"{name}.strm", vod / f"{name}.nfo"
        strm.write_text("http://p/movie/1.mp4")
        nfo.write_text("<movie/>")
        dl = self.root / "movies" / name
        dl.mkdir(parents=True)
        mkv = dl / f"{name}.mkv"
        mkv.write_bytes(b"\0" * 8)
        _FakeArr.titles[tmdb_id] = {"id": tmdb_id, "path": str(dl), "files": [{"id": tmdb_id * 10, "path": str(mkv)}]}
        self.db.add(Movie(tmdb_id=tmdb_id, title=name, source="provider_1", strm_path=str(strm),
                          nfo_path=str(nfo), radarr_path=str(mkv)))
        dup = Duplicate(tmdb_id=tmdb_id, media_type="movie", resolution="pending",
                        sources=[{"source": "radarr", "path": str(mkv)},
                                 {"source": "provider_1", "path": str(strm)}])
        self.db.add(dup)
        self.db.commit()
        self.films[dup.id] = (strm, mkv)
        return dup.id

    def resolve(self, dup_id, resolution, db=None):
        return duplicates.resolve_duplicate(dup_id, duplicates.ResolveRequest(resolution=resolution),
                                            db=db or self.db)

    def resolve_all(self, resolution, db=None):
        return duplicates.resolve_all(duplicates.ResolveAllRequest(resolution=resolution), db=db or self.db)

    def assert_a_copy_left(self, dup_id):
        strm, mkv = self.films[dup_id]
        self.assertTrue(strm.exists() or mkv.exists(), "both copies of the title were deleted")


class Sequential(_Base):
    """A stale tab: the second action comes after the first finished."""

    def assert_second_refused(self, first, second):
        dup_id = self.film(949, "Heat (1995)")
        self.resolve(dup_id, first)
        calls = list(_FakeArr.calls)
        stale = self.Session()  # the other tab's request
        self.addCleanup(stale.close)
        with self.assertRaises(HTTPException) as cm:
            self.resolve(dup_id, second, db=stale)
        self.assertEqual(409, cm.exception.status_code)
        self.assertIn("already resolved", cm.exception.detail)
        self.assert_a_copy_left(dup_id)
        self.assertEqual(calls, _FakeArr.calls, "the arr was asked to delete something")
        self.db.expire_all()
        self.assertEqual(first, self.db.get(Duplicate, dup_id).resolution)

    def test_keep_downloaded_then_keep_vod(self):
        self.assert_second_refused("keep_radarr", "keep_vod")
        self.assertTrue(self.films[1][1].exists())

    def test_keep_vod_then_keep_downloaded(self):
        self.assert_second_refused("keep_vod", "keep_radarr")
        self.assertTrue(self.films[1][0].exists())

    def test_keep_both_then_keep_vod(self):
        self.assert_second_refused("keep_both", "keep_vod")

    def test_resolve_all_then_keep_vod(self):
        dup_id = self.film(949, "Heat (1995)")
        self.assertEqual(1, self.resolve_all("keep_radarr")["count"])
        with self.assertRaises(HTTPException) as cm:
            self.resolve(dup_id, "keep_vod", db=self.Session())
        self.assertEqual(409, cm.exception.status_code)
        self.assert_a_copy_left(dup_id)

    def test_pending_list_read_before_another_resolve(self):
        """Resolve All read the list, then a single Keep VOD resolved one of
        them before Resolve All got to it: Resolve All skips it."""
        a = self.film(949, "Heat (1995)")
        b = self.film(680, "Pulp Fiction (1994)")
        real = duplicates._apply_resolution
        other = self.Session()
        self.addCleanup(other.close)
        elsewhere = []

        def apply(dup, resolution, db):
            if not elsewhere:
                # While Resolve All works on its first duplicate, a stale tab
                # keeps VOD on the second one. (That request would wait for
                # the lock Resolve All holds; give it its own.)
                elsewhere.append(b if dup.id == a else a)
                with mock.patch.object(duplicates, "_resolve_lock", threading.Lock()):
                    self.resolve(elsewhere[0], "keep_vod", db=other)
            return real(dup, resolution, db)

        with mock.patch.object(duplicates, "_apply_resolution", side_effect=apply):
            out = self.resolve_all("keep_radarr")
        self.assertEqual({"count": 1, "total": 2, "failed": 0, "skipped": 1},
                         {k: out[k] for k in ("count", "total", "failed", "skipped")})
        self.assert_a_copy_left(a)
        self.assert_a_copy_left(b)
        self.db.expire_all()
        self.assertEqual({elsewhere[0]: "keep_vod", ({a, b} - set(elsewhere)).pop(): "keep_radarr"},
                         {d: self.db.get(Duplicate, d).resolution for d in (a, b)})


class Validation(_Base):
    def test_unknown_resolution_rejected(self):
        for bad in ("pending", "delete_all", ""):
            with self.assertRaises(ValidationError):
                duplicates.ResolveRequest(resolution=bad)
            with self.assertRaises(ValidationError):
                duplicates.ResolveAllRequest(resolution=bad)

    def test_pending_does_not_reopen_over_http(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from models.database import get_db
        from routers.auth import require_admin
        app = FastAPI()
        app.include_router(duplicates.router)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[require_admin] = lambda: None
        dup_id = self.film(949, "Heat (1995)")
        self.resolve(dup_id, "keep_radarr")
        r = TestClient(app).post(f"/api/duplicates/{dup_id}/resolve", json={"resolution": "pending"})
        self.assertEqual(422, r.status_code)
        self.db.expire_all()
        self.assertEqual("keep_radarr", self.db.get(Duplicate, dup_id).resolution)


class Concurrent(_Base):
    """Two requests at once, each with its own session, as uvicorn's thread pool runs them."""

    def race(self, *jobs):
        start = threading.Barrier(len(jobs))
        errors = []

        def run(job):
            db = self.Session()
            try:
                start.wait()
                job(db)
            except HTTPException as e:
                errors.append(e.status_code)
            finally:
                db.close()
        threads = [threading.Thread(target=run, args=(j,)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return errors

    def slow_arr(self):
        """Widen the window: each arr call yields to the other thread."""
        import time
        real = FakeRadarr.get_movie_by_tmdb

        def slow(self_, tmdb_id):
            time.sleep(0.02)
            return real(self_, tmdb_id)
        mock.patch.object(FakeRadarr, "get_movie_by_tmdb", slow).start()

    def test_keep_vod_races_resolve_all(self):
        ids = [self.film(100 + i, f"Film {i} (2000)") for i in range(5)]
        self.slow_arr()
        target = ids[-1]
        self.race(lambda db: self.resolve_all("keep_radarr", db=db),
                  lambda db: self.resolve(target, "keep_vod", db=db))
        for dup_id in ids:
            self.assert_a_copy_left(dup_id)

    def test_keep_downloaded_and_keep_vod_at_once(self):
        dup_id = self.film(949, "Heat (1995)")
        self.slow_arr()
        errors = self.race(lambda db: self.resolve(dup_id, "keep_radarr", db=db),
                           lambda db: self.resolve(dup_id, "keep_vod", db=db))
        self.assertEqual([409], errors)
        self.assert_a_copy_left(dup_id)

    def test_two_resolve_alls_in_opposite_directions(self):
        ids = [self.film(100 + i, f"Film {i} (2000)") for i in range(5)]
        self.slow_arr()
        self.race(lambda db: self.resolve_all("keep_radarr", db=db),
                  lambda db: self.resolve_all("keep_vod", db=db))
        for dup_id in ids:
            self.assert_a_copy_left(dup_id)


if __name__ == "__main__":
    unittest.main()

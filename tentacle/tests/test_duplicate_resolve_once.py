"""A duplicate is resolved once.

resolve_duplicate never checked that the duplicate was still pending, and
Resolve All did not check again after loading its list. A duplicate already
resolved Keep Downloaded (VOD copy deleted) could then be resolved Keep VOD
from a tab loaded earlier, the API or a Resolve All running at the same time:
the download was deleted too and the film was gone. Now a request first
claims the duplicate (pending -> resolving, committed before anything is
deleted); any other request for it gets 409 and changes nothing. A failed
resolution puts it back to pending, and so does a restart that interrupted
one. The resolution is marked in the same commit as its database changes.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import logging
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from models.database import Base, Duplicate, Movie, Setting
from routers import duplicates
from tmp_dirs import temp_dir


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class FakeRadarr:
    """Radarr holding one downloaded file per film; deleting it removes it from disk."""
    files = {}      # tmdb -> Path of the download
    delay = 0.0

    def __init__(self, *a, **k):
        pass

    def get_movie_by_tmdb(self, tmdb):
        time.sleep(self.delay)
        return {"id": tmdb, "path": str(self.files[tmdb].parent)} if tmdb in self.files else None

    def get_movie_files(self, movie_id):
        time.sleep(self.delay)
        f = self.files.get(movie_id)
        return [{"id": movie_id, "path": str(f)}] if f is not None and f.exists() else []

    def delete_movie_file(self, file_id):
        time.sleep(self.delay)
        self.files[file_id].unlink()

    def delete_movie_by_id(self, movie_id, delete_files=False):
        time.sleep(self.delay)
        self.files.pop(movie_id, None)
        return True


class _Base(unittest.TestCase):
    merged = True

    def setUp(self):
        self.root = Path(temp_dir(self))
        self.engine = create_engine(f"sqlite:///{self.root}/t.db", connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def _pragmas(conn, _):   # as models/database.py
            cur = conn.cursor(); cur.execute("PRAGMA journal_mode=WAL"); cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session(); self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r")):
            self.db.add(Setting(key=k, value=v))
        FakeRadarr.files, FakeRadarr.delay = {}, 0.0
        p = mock.patch("services.radarr.RadarrService", FakeRadarr); p.start(); self.addCleanup(p.stop)
        self.films = {}
        self.db.commit()

    def add_film(self, tmdb, name):
        vod = self.root / "vod" / name; vod.mkdir(parents=True)
        strm = vod / f"{name}.strm"; strm.write_text("http://p/movie/1.mp4")
        dl = vod if self.merged else self.root / "movies" / f"{name} [1080p]"; dl.mkdir(parents=True, exist_ok=True)
        mkv = dl / f"{name}.mkv"; mkv.write_bytes(b"\0" * 8)
        FakeRadarr.files[tmdb] = mkv
        self.db.add(Movie(tmdb_id=tmdb, title=name, source="provider_1", strm_path=str(strm), radarr_path=str(dl)))
        dup = Duplicate(tmdb_id=tmdb, media_type="movie", resolution="pending",
                        sources=[{"source": "radarr", "path": str(mkv)}, {"source": "provider_1", "path": str(strm)}])
        self.db.add(dup); self.db.commit()
        self.films[tmdb] = (strm, mkv)
        return dup.id

    def resolve(self, dup_id, resolution, db=None):
        return duplicates.resolve_duplicate(dup_id, duplicates.ResolveRequest(resolution=resolution), db=db or self.db)

    def state(self, tmdb):
        s = self.Session()
        try:
            return s.query(Duplicate).filter(Duplicate.tmdb_id == tmdb).one().resolution
        finally:
            s.close()


class ResolvedOnce(_Base):
    def test_keep_vod_after_keep_downloaded_is_refused(self):
        dup_id = self.add_film(949, "Heat (1995)")
        strm, mkv = self.films[949]
        self.resolve(dup_id, "keep_radarr")
        self.assertFalse(strm.exists())
        with self.assertRaises(HTTPException) as cm:
            self.resolve(dup_id, "keep_vod")    # a tab loaded before the first click
        self.assertEqual(409, cm.exception.status_code)
        self.assertTrue(mkv.exists(), "the download was deleted after the VOD copy: the film is gone")
        self.assertEqual("keep_radarr", self.state(949))

    def test_keep_downloaded_after_keep_vod_is_refused(self):
        dup_id = self.add_film(949, "Heat (1995)")
        strm, mkv = self.films[949]
        self.resolve(dup_id, "keep_vod")
        self.assertFalse(mkv.exists())
        with self.assertRaises(HTTPException) as cm:
            self.resolve(dup_id, "keep_radarr")
        self.assertEqual(409, cm.exception.status_code)
        self.assertTrue(strm.exists(), "the VOD copy was deleted after the download: the film is gone")
        self.assertEqual("keep_vod", self.state(949))

    def test_keep_both_is_final_too(self):
        dup_id = self.add_film(949, "Heat (1995)")
        self.resolve(dup_id, "keep_both")
        with self.assertRaises(HTTPException) as cm:
            self.resolve(dup_id, "keep_vod")
        self.assertEqual(409, cm.exception.status_code)
        self.assertTrue(all(p.exists() for p in self.films[949]))
        self.assertEqual("keep_both", self.state(949))

    def test_an_unknown_resolution_cannot_reopen_a_duplicate(self):
        dup_id = self.add_film(949, "Heat (1995)")
        self.resolve(dup_id, "keep_radarr")
        for bad in ("pending", "resolving", "keep_everything"):
            with self.assertRaises(HTTPException) as cm:
                self.resolve(dup_id, bad)
            self.assertEqual(400, cm.exception.status_code)
        self.assertEqual("keep_radarr", self.state(949))

    def test_resolve_all_leaves_resolved_ones_alone(self):
        a = self.add_film(949, "Heat (1995)")
        self.add_film(603, "Film (1999)")
        self.resolve(a, "keep_radarr")
        r = duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_vod"), db=self.db)
        self.assertEqual((1, 0), (r["count"], r["failed"]))
        self.assertTrue(self.films[949][1].exists())
        self.assertEqual({"keep_radarr", "keep_vod"}, {self.state(949), self.state(603)})


class SeparateFolders(ResolvedOnce):
    merged = False


class FailureAndRestart(_Base):
    def test_a_failed_resolution_can_be_retried(self):
        dup_id = self.add_film(949, "Heat (1995)")
        mkv = FakeRadarr.files.pop(949)            # Radarr can't find the film: 502
        with self.assertRaises(HTTPException) as cm:
            self.resolve(dup_id, "keep_vod")
        self.assertEqual(502, cm.exception.status_code)
        self.assertEqual("pending", self.state(949))
        FakeRadarr.files[949] = mkv
        self.resolve(dup_id, "keep_vod")
        self.assertEqual("keep_vod", self.state(949))

    def test_a_restart_releases_an_interrupted_resolution(self):
        a = self.add_film(949, "Heat (1995)")
        self.add_film(603, "Film (1999)")
        self.resolve(a, "keep_radarr")
        self.db.query(Duplicate).filter(Duplicate.tmdb_id == 603).update({Duplicate.resolution: "resolving"})
        self.db.commit()
        duplicates.release_interrupted_resolutions(self.db)
        self.assertEqual(("keep_radarr", "pending"), (self.state(949), self.state(603)))

    def test_resolution_is_marked_in_the_same_commit_as_its_changes(self):
        # a crash right after _apply_resolution must not leave the duplicate
        # pending while its VOD copy is deleted and the row already converted
        self.add_film(949, "Heat (1995)")
        dup = self.db.query(Duplicate).one()
        duplicates._apply_resolution(dup, "keep_radarr", self.db)
        self.assertEqual("keep_radarr", self.state(949))


class Concurrent(_Base):
    def test_a_single_keep_vod_racing_resolve_all_never_deletes_both_copies(self):
        for n in range(3):
            self.add_film(100 + n, f"F{n} (200{n})")
        FakeRadarr.delay = 0.003
        ids = [d.id for d in self.db.query(Duplicate)]
        errors = []

        def run(fn):
            s = self.Session()
            try:
                fn(s)
            except HTTPException:
                pass
            except Exception as e:  # pragma: no cover - reported below
                errors.append(repr(e))
            finally:
                s.close()

        threads = [threading.Thread(target=run, args=(lambda s: duplicates.resolve_all(
                       duplicates.ResolveAllRequest(resolution="keep_radarr"), db=s),)),
                   threading.Thread(target=run, args=(lambda s: self.resolve(ids[1], "keep_vod", db=s),))]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual([], errors)
        for tmdb, (strm, mkv) in self.films.items():
            self.assertTrue(strm.exists() or mkv.exists(), f"tmdb:{tmdb} lost both copies")
            self.assertIn(self.state(tmdb), ("keep_radarr", "keep_vod"))


if __name__ == "__main__":
    unittest.main()

"""Duplicates: one title with two pending duplicate rows (#504).

Nothing makes (tmdb_id, media_type) unique: two scans running at once each
read the duplicate list before the other commits and both write a row (a live
install had a show listed twice, rows 64 ms apart). The resolve-once lock
(#328) refuses a second resolution of the SAME row, but the twin row stayed
pending. For a film, Keep VOD on one row deleted the download, then Keep
Downloaded on the other row (or Resolve All -> Keep All Downloaded) deleted
the .strm: no copy left. Film Keep Downloaded never asked Radarr whether a
download was still there.

Invariant: never delete the last copy of a title. Resolving one row of a
title resolves its pending twins with it.

Run from tentacle/:  python -m unittest tests.test_duplicate_twin_rows
"""
import logging
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Duplicate, Movie, Setting
from routers import duplicates
from tmp_dirs import temp_dir


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class FakeRadarr:
    """Radarr holding one film: lists it while it has a file record."""
    files = {}

    def __init__(self, *a, **k):
        pass

    def get_movie_by_tmdb(self, tmdb):
        return {"id": tmdb, "path": str(self.files[tmdb].parent)} if tmdb in self.files else None

    def get_movie_files(self, movie_id):
        f = self.files.get(movie_id)
        return [{"id": movie_id, "path": str(f)}] if f is not None and f.exists() else []

    def delete_movie_file(self, file_id):
        self.files[file_id].unlink()

    def delete_movie_by_id(self, movie_id, delete_files=False):
        self.files.pop(movie_id, None)
        return True


class TwinRowsOneFilm(unittest.TestCase):
    def setUp(self):
        self.root = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.root}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r")):
            self.db.add(Setting(key=k, value=v))
        FakeRadarr.files = {}
        p = mock.patch("services.radarr.RadarrService", FakeRadarr)
        p.start()
        self.addCleanup(p.stop)
        vod = self.root / "vod" / "Heat (1995)"
        vod.mkdir(parents=True)
        self.strm = vod / "Heat (1995).strm"
        self.strm.write_text("http://p/movie/1.mp4")
        dl = self.root / "movies" / "Heat (1995) [1080p]"
        dl.mkdir(parents=True)
        self.mkv = dl / "Heat (1995).mkv"
        self.mkv.write_bytes(b"\0" * 8)
        FakeRadarr.files[949] = self.mkv
        self.db.add(Movie(tmdb_id=949, title="Heat", source="provider_1", strm_path=str(self.strm),
                          radarr_path=str(self.mkv)))
        srcs = [{"source": "radarr", "path": str(self.mkv)}, {"source": "provider_1", "path": str(self.strm)}]
        a = Duplicate(tmdb_id=949, media_type="movie", resolution="pending", sources=list(srcs))
        b = Duplicate(tmdb_id=949, media_type="movie", resolution="pending", sources=list(srcs))
        self.db.add_all([a, b])
        self.db.commit()
        self.a, self.b = a.id, b.id

    def resolve(self, dup_id, resolution):
        return duplicates.resolve_duplicate(dup_id, duplicates.ResolveRequest(resolution=resolution), db=self.db)

    def resolution(self, dup_id):
        self.db.expire_all()
        return self.db.get(Duplicate, dup_id).resolution

    def assert_a_copy_left(self):
        self.assertTrue(self.strm.exists() or self.mkv.exists(),
                        "both copies of the film are gone (download deleted by Keep VOD on one row, "
                        ".strm by Keep Downloaded on the other row of the same title)")

    def test_keep_vod_then_keep_downloaded_on_the_twin(self):
        self.resolve(self.a, "keep_vod")
        self.assertFalse(self.mkv.exists())
        with self.assertRaises(HTTPException) as cm:
            self.resolve(self.b, "keep_radarr")
        self.assertEqual(409, cm.exception.status_code)
        self.assert_a_copy_left()

    def test_keep_vod_then_resolve_all_keep_downloaded(self):
        self.resolve(self.a, "keep_vod")
        duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_radarr"), db=self.db)
        self.assert_a_copy_left()

    def test_keep_downloaded_then_keep_vod_on_the_twin_is_refused(self):
        # control: #332's on-disk check already covers this order
        self.resolve(self.a, "keep_radarr")
        with self.assertRaises(HTTPException):
            self.resolve(self.b, "keep_vod")
        self.assert_a_copy_left()

    def test_resolving_one_row_resolves_its_twin(self):
        self.resolve(self.a, "keep_vod")
        self.assertEqual("keep_vod", self.resolution(self.b),
                         "the twin row of a resolved title is still pending")
        self.assertEqual(0, duplicates.get_duplicates(db=self.db)["pending"])

    def test_resolve_all_resolves_the_title_once(self):
        r = duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_radarr"), db=self.db)
        self.assertFalse(self.strm.exists())
        self.assertTrue(self.mkv.exists())
        self.assertEqual(("keep_radarr", "keep_radarr"), (self.resolution(self.a), self.resolution(self.b)))
        self.assertEqual(0, r["failed"])

    def test_keep_downloaded_with_a_download_still_works(self):
        self.resolve(self.a, "keep_radarr")
        self.assertFalse(self.strm.exists())
        self.assertTrue(self.mkv.exists())
        self.assertEqual("radarr", self.db.query(Movie).one().source)


if __name__ == "__main__":
    unittest.main()

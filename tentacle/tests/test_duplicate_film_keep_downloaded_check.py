"""Keep Downloaded on a film checks that Radarr still has a download.

Keep Downloaded deletes the VOD copy. #255 made it check Sonarr first for a
show; a film was not checked, so it deleted the last copy when nothing was
downloaded any more:
  - after a Keep VOD that failed half-way (Radarr deleted the file, then
    removing the title failed or its answer was lost: 502, still pending);
  - for a duplicate between two providers, which Resolve All ("Keep All
    Downloaded") resolves with Keep Downloaded too: both .strm were deleted.

Run from the tentacle/ directory:  python -m unittest discover -s tests
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


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.root}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)(); self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r")):
            self.db.add(Setting(key=k, value=v))
        folder = self.root / "vod" / "movies" / "Film (1999)"; folder.mkdir(parents=True)
        self.strm = folder / "Film (1999).strm"; self.strm.write_text("x")
        (folder / "Film (1999).nfo").write_text("<movie/>")
        self.db.add(Movie(tmdb_id=603, title="Film", source="provider_1", strm_path=str(self.strm)))
        self.db.commit()
        p = mock.patch("services.radarr.RadarrService"); self.radarr = p.start().return_value; self.addCleanup(p.stop)
        self.radarr.get_movie_by_tmdb.return_value = {"id": 3, "path": "/movies/Film (1999)"}
        self.radarr.get_movie_files.return_value = []   # the download is gone

    def add_dup(self, sources):
        dup = Duplicate(tmdb_id=603, media_type="movie", resolution="pending", sources=sources)
        self.db.add(dup); self.db.commit()
        return dup

    def assert_untouched(self):
        self.assertTrue(self.strm.exists(), "Keep Downloaded deleted the VOD copy although nothing is downloaded")
        self.assertEqual("provider_1", self.db.query(Movie).one().source)
        self.assertTrue(all(d.resolution == "pending" for d in self.db.query(Duplicate)))


class FilmKeepDownloaded(_Base):
    def setUp(self):
        super().setUp()
        self.dup = self.add_dup([{"source": "radarr", "path": "/movies/Film (1999)/Film.mkv"},
                                 {"source": "provider_1", "path": str(self.strm)}])

    def test_refused_when_radarr_has_no_file_any_more(self):
        with self.assertRaises(HTTPException) as cm:
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(409, cm.exception.status_code)
        self.assert_untouched()

    def test_refused_when_radarr_no_longer_lists_the_film(self):
        self.radarr.get_movie_by_tmdb.return_value = None
        with self.assertRaises(HTTPException) as cm:
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(409, cm.exception.status_code)
        self.assert_untouched()

    def test_refused_when_radarr_files_cannot_be_read(self):
        self.radarr.get_movie_files.side_effect = RuntimeError("timeout")
        with self.assertRaises(HTTPException) as cm:
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(502, cm.exception.status_code)
        self.assert_untouched()

    def test_a_strm_is_not_a_download(self):
        self.radarr.get_movie_files.return_value = [{"id": 1, "path": "/movies/Film (1999)/Film (1999).strm"}]
        with self.assertRaises(HTTPException):
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assert_untouched()

    def test_refused_when_radarr_is_not_configured(self):
        # as for shows (Sonarr not configured): nothing can be checked, so nothing is deleted
        self.db.query(Setting).filter(Setting.key.in_(["radarr_url", "radarr_api_key"])).delete(synchronize_session=False)
        self.db.commit()
        with self.assertRaises(HTTPException) as cm:
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(409, cm.exception.status_code)
        self.radarr.get_movie_by_tmdb.assert_not_called()
        self.assert_untouched()

    def test_with_a_download_the_vod_copy_is_still_removed(self):
        self.radarr.get_movie_files.return_value = [{"id": 1, "path": "/movies/Film (1999)/Film.mkv"}]
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())
        self.assertEqual("radarr", self.db.query(Movie).one().source)


class ResolveAllTwoProviders(_Base):
    def test_resolve_all_keeps_both_vod_copies_of_a_provider_duplicate(self):
        other = self.root / "vod2" / "Film (1999).strm"; other.parent.mkdir(parents=True); other.write_text("y")
        self.add_dup([{"source": "provider_1", "path": str(self.strm)},
                      {"source": "provider_2", "path": str(other)}])
        r = duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_radarr"), db=self.db)
        self.assertEqual((0, 1), (r["count"], r["failed"]))
        self.assertTrue(other.exists())
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main()

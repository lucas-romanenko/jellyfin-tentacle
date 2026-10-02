"""Duplicates → "Keep VOD" must not delete the download when the VOD copy is gone (#332).

Keep VOD deletes the downloaded copy and keeps the VOD one, but never checked
that the VOD copy is still on disk. Two ways it isn't:
  - Keep Downloaded deletes the VOD .strm/.nfo first and saves the resolution
    last; a restart in between leaves the duplicate pending with the .strm
    gone (and, once the deletion log has committed, the row already handed to
    Radarr). Keep VOD then deleted the download too: no copy left.
  - A film downloaded first, then offered by a provider: the VOD sync records
    the duplicate but never writes the provider's .strm.
Keep VOD now answers 409 and changes nothing unless the VOD copy is there: a
film's .strm, or a show folder with at least one .strm.

The fake Radarr/Sonarr are the ones of test_duplicate_keep_vod_merged_folder
(a file delete removes that file, a title delete with files removes the folder).

Run from tentacle/:  python -m unittest tests.test_duplicate_keep_vod_needs_vod_copy
"""
import logging
import shutil
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Duplicate, Movie, Series, Setting
from routers import duplicates
from tests.test_duplicate_keep_vod_merged_folder import FakeRadarr, FakeSonarr, _FakeArr
from tmp_dirs import temp_dir


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class _Interrupted(BaseException):
    """A restart: nothing after it runs, only committed state survives."""


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = Path(tmp)
        self.engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.addCleanup(lambda: self.db.close())
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r"),
                     ("sonarr_url", "http://sonarr"), ("sonarr_api_key", "s")):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()
        _FakeArr.titles = {}
        _FakeArr.calls = []
        _FakeArr.fail_file_delete = False
        mock.patch("services.radarr.RadarrService", FakeRadarr).start()
        mock.patch("services.sonarr.SonarrService", FakeSonarr).start()
        self.addCleanup(mock.patch.stopall)

    def film(self, write_strm=True, row_source="provider_1"):
        vod = self.root / "vod" / "movies" / "Heat (1995)"
        vod.mkdir(parents=True)
        self.strm, self.nfo = vod / "Heat (1995).strm", vod / "Heat (1995).nfo"
        if write_strm:
            self.strm.write_text("http://p/movie/1.mp4")
            self.nfo.write_text("<movie/>")
        dl = self.root / "movies" / "Heat (1995)"
        dl.mkdir(parents=True)
        self.mkv = dl / "Heat (1995).mkv"
        self.mkv.write_bytes(b"\0" * 8)
        FakeRadarr.titles[949] = {"id": 7, "path": str(dl), "files": [{"id": 70, "path": str(self.mkv)}]}
        if row_source == "radarr":
            row = Movie(tmdb_id=949, title="Heat", source="radarr", radarr_path=str(self.mkv))
        else:
            row = Movie(tmdb_id=949, title="Heat", source=row_source, strm_path=str(self.strm),
                        nfo_path=str(self.nfo), radarr_path=str(self.mkv))
        self.db.add(row)
        self.db.add(Duplicate(tmdb_id=949, media_type="movie", resolution="pending",
                              sources=[{"source": "radarr", "path": str(self.mkv)},
                                       {"source": "provider_1", "path": str(self.strm)}]))
        self.db.commit()
        return self.db.query(Duplicate).one().id

    def resolve(self, dup_id, resolution):
        return duplicates.resolve_duplicate(dup_id, duplicates.ResolveRequest(resolution=resolution), db=self.db)

    def assert_refused(self, dup_id):
        with self.assertRaises(HTTPException) as cm:
            self.resolve(dup_id, "keep_vod")
        self.db.rollback()
        self.assertEqual(409, cm.exception.status_code)
        self.assertIn("isn't on disk", cm.exception.detail)
        self.assertTrue(self.mkv.exists(), "the download, now the only copy, was deleted")
        self.assertEqual([], _FakeArr.calls, "Radarr was asked to delete something")
        self.assertIn(949, FakeRadarr.titles)


class TestFilm(_Base):
    def test_restart_mid_keep_downloaded_then_keep_vod_keeps_the_download(self):
        """The reported sequence: Keep Downloaded deletes the .strm, a restart
        ends it before anything is committed, Keep VOD."""
        dup_id = self.film()

        def restart(*a, **k):
            raise _Interrupted()
        with mock.patch.object(duplicates, "convert_record_to_downloaded", restart):
            with self.assertRaises(_Interrupted):
                self.resolve(dup_id, "keep_radarr")
        self.assertFalse(self.strm.exists())          # the VOD copy is already gone
        self.db.close()
        self.db = self.Session()                       # restart: committed state only
        if hasattr(duplicates, "release_interrupted_resolutions"):
            duplicates.release_interrupted_resolutions(self.db)   # what the startup does
        self.assertEqual("pending", self.db.query(Duplicate).one().resolution)
        self.assert_refused(dup_id)

    def test_strm_deleted_row_still_provider_owned(self):
        dup_id = self.film()
        self.strm.unlink()
        self.nfo.unlink()
        self.assert_refused(dup_id)
        row = self.db.query(Movie).one()
        self.assertEqual("provider_1", row.source)
        self.assertEqual(str(self.mkv), row.radarr_path)
        self.assertEqual("pending", self.db.query(Duplicate).one().resolution)

    def test_row_already_handed_to_radarr(self):
        """Interrupted after the deletion log committed the converted row."""
        dup_id = self.film(write_strm=False, row_source="radarr")
        self.assert_refused(dup_id)
        self.assertEqual(1, self.db.query(Movie).count())

    def test_downloaded_first_provider_strm_never_written(self):
        dup_id = self.film(write_strm=False, row_source="radarr")
        self.assertFalse(self.strm.parent.joinpath("Heat (1995).strm").exists())
        self.assert_refused(dup_id)

    def test_resolve_all_keeps_it_pending(self):
        dup_id = self.film()
        self.strm.unlink()
        out = duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_vod"), db=self.db)
        self.assertEqual((0, 1), (out["count"], out["failed"]))
        self.assertTrue(self.mkv.exists())
        self.assertEqual([], _FakeArr.calls)
        self.assertEqual("pending", self.db.get(Duplicate, dup_id).resolution)

    def test_vod_copy_present_keep_vod_still_works(self):
        dup_id = self.film()
        self.resolve(dup_id, "keep_vod")
        self.assertTrue(self.strm.exists())
        self.assertFalse(self.mkv.exists())
        self.assertNotIn(949, FakeRadarr.titles)
        self.assertEqual("keep_vod", self.db.query(Duplicate).one().resolution)

    def test_vod_copy_moved_since_the_duplicate_was_recorded(self):
        """The duplicate's provider path is stale, the row's strm_path is current."""
        dup_id = self.film()
        new = self.root / "vod" / "movies" / "Heat (1995) [tmdbid-949]"
        new.mkdir()
        moved = new / "Heat (1995) [tmdbid-949].strm"
        self.strm.rename(moved)
        row = self.db.query(Movie).one()
        row.strm_path = str(moved)
        self.db.commit()
        self.resolve(dup_id, "keep_vod")
        self.assertTrue(moved.exists())
        self.assertFalse(self.mkv.exists())


class TestShow(_Base):
    def show(self, strm=True):
        vod = self.root / "vod" / "shows" / "Show (2010)"
        (vod / "Season 01").mkdir(parents=True)
        (vod / "tvshow.nfo").write_text("<tvshow/>")
        if strm:
            (vod / "Season 01" / "Show S01E01.strm").write_text("http://p/s.mp4")
        dl = self.root / "tv" / "Show (2010)"
        (dl / "Season 01").mkdir(parents=True)
        self.mkv = dl / "Season 01" / "Show - S01E01.mkv"
        self.mkv.write_bytes(b"\0" * 8)
        FakeSonarr.titles[1418] = {"id": 5, "path": str(dl), "files": [{"id": 50, "path": str(self.mkv)}]}
        self.db.add(Series(tmdb_id=1418, title="Show", source="provider_1", strm_path=str(vod),
                           sonarr_path=str(dl)))
        self.db.add(Duplicate(tmdb_id=1418, media_type="series", resolution="pending",
                              sources=[{"source": "sonarr", "path": str(dl)},
                                       {"source": "provider_1", "path": str(vod)}]))
        self.db.commit()
        return self.db.query(Duplicate).one().id

    def test_show_folder_without_strm_is_refused(self):
        dup_id = self.show(strm=False)
        with self.assertRaises(HTTPException) as cm:
            self.resolve(dup_id, "keep_vod")
        self.db.rollback()
        self.assertEqual(409, cm.exception.status_code)
        self.assertTrue(self.mkv.exists())
        self.assertEqual([], _FakeArr.calls)

    def test_show_with_strm_still_resolves(self):
        dup_id = self.show()
        self.resolve(dup_id, "keep_vod")
        self.assertFalse(self.mkv.exists())
        self.assertEqual("keep_vod", self.db.query(Duplicate).one().resolution)


if __name__ == "__main__":
    unittest.main()

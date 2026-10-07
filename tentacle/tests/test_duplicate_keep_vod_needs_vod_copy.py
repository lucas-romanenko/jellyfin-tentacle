"""Duplicates → "Keep VOD" refuses when the VOD copy is not on disk (#332).

Keep VOD deletes the download and keeps the VOD copy, but never checked that
the VOD copy was there. Keep Downloaded deletes the VOD .strm/.nfo first and
saves the resolution last, so a restart in between leaves the duplicate
pending with the .strm gone (and, once the deletion log has committed, the
row already handed to Radarr). Keep VOD then deleted the download too, and
the title had no copy left. The same for a film downloaded first, whose
provider .strm the VOD sync never writes. Keep VOD now answers 409 and
changes nothing unless the VOD copy is on disk: a film's .strm, or a show
folder with at least one .strm.

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


class _Restart(BaseException):
    """Tentacle stops here: nothing after it runs, only what was committed stays."""


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = Path(tmp)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
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

    def resolve(self, dup_id, resolution):
        return duplicates.resolve_duplicate(dup_id, duplicates.ResolveRequest(resolution=resolution), db=self.db)

    def assert_refused(self, dup_id):
        with self.assertRaises(HTTPException) as cm:
            self.resolve(dup_id, "keep_vod")
        self.db.rollback()
        self.assertEqual(409, cm.exception.status_code)
        self.assertIn("isn't on disk", cm.exception.detail)
        self.assertTrue(self.mkv.exists(), "Keep VOD deleted the download, the only copy left")
        self.assertEqual([], _FakeArr.calls, "the arr was asked to delete something")
        self.assertEqual("pending", self.db.get(Duplicate, dup_id).resolution)


class Film(_Base):
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
        _FakeArr.titles[949] = {"id": 7, "path": str(dl), "files": [{"id": 70, "path": str(self.mkv)}]}
        if row_source == "radarr":
            row = Movie(tmdb_id=949, title="Heat", source="radarr", radarr_path=str(self.mkv))
        else:
            row = Movie(tmdb_id=949, title="Heat", source=row_source, strm_path=str(self.strm),
                        nfo_path=str(self.nfo), radarr_path=str(self.mkv))
        self.db.add(row)
        dup = Duplicate(tmdb_id=949, media_type="movie", resolution="pending",
                        sources=[{"source": "radarr", "path": str(self.mkv)},
                                 {"source": "provider_1", "path": str(self.strm)}])
        self.db.add(dup)
        self.db.commit()
        return dup.id

    def test_keep_downloaded_cut_short_by_a_restart_then_keep_vod(self):
        """The reported sequence: Keep Downloaded deletes the .strm, Tentacle
        restarts before anything is committed, the duplicate is pending, Keep VOD."""
        dup_id = self.film()
        with mock.patch.object(duplicates, "convert_record_to_downloaded", side_effect=_Restart):
            with self.assertRaises(_Restart):
                self.resolve(dup_id, "keep_radarr")
        self.assertFalse(self.strm.exists())
        self.db.close()
        self.db = self.Session()
        self.assertEqual("pending", self.db.get(Duplicate, dup_id).resolution)
        self.assert_refused(dup_id)
        self.assertEqual(str(self.mkv), self.db.query(Movie).one().radarr_path)

    def test_row_already_handed_to_radarr(self):
        """Cut short after the deletion log committed the converted row."""
        dup_id = self.film(write_strm=False, row_source="radarr")
        self.assert_refused(dup_id)
        self.assertEqual(1, self.db.query(Movie).count())

    def test_resolve_all_leaves_it_pending(self):
        dup_id = self.film()
        self.strm.unlink()
        out = duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_vod"), db=self.db)
        self.assertEqual((0, 1), (out["count"], out["failed"]))
        self.assertTrue(self.mkv.exists())
        self.assertEqual([], _FakeArr.calls)
        self.assertEqual("pending", self.db.get(Duplicate, dup_id).resolution)

    def test_vod_copy_on_disk_keep_vod_still_deletes_the_download(self):
        dup_id = self.film()
        self.resolve(dup_id, "keep_vod")
        self.assertTrue(self.strm.exists())
        self.assertFalse(self.mkv.exists())
        self.assertNotIn(949, _FakeArr.titles)
        self.assertEqual("keep_vod", self.db.get(Duplicate, dup_id).resolution)

    def test_vod_copy_moved_since_the_duplicate_was_recorded(self):
        """The duplicate's provider path is stale, the row's strm_path is current."""
        dup_id = self.film()
        moved = self.root / "vod" / "movies" / "Heat (1995) [tmdbid-949]" / "Heat (1995) [tmdbid-949].strm"
        moved.parent.mkdir()
        self.strm.rename(moved)
        self.db.query(Movie).one().strm_path = str(moved)
        self.db.commit()
        self.resolve(dup_id, "keep_vod")
        self.assertTrue(moved.exists())
        self.assertFalse(self.mkv.exists())


class Show(_Base):
    def show(self, with_strm=True):
        vod = self.root / "vod" / "shows" / "Dark (2017)"
        (vod / "Season 01").mkdir(parents=True)
        (vod / "tvshow.nfo").write_text("<tvshow/>")
        if with_strm:
            (vod / "Season 01" / "Dark (2017) S01E01.strm").write_text("http://p/series/1.mp4")
        dl = self.root / "tv" / "Dark (2017)"
        (dl / "Season 01").mkdir(parents=True)
        self.mkv = dl / "Season 01" / "Dark - S01E01.mkv"
        self.mkv.write_bytes(b"\0" * 8)
        _FakeArr.titles[70523] = {"id": 5, "path": str(dl), "files": [{"id": 50, "path": str(self.mkv)}]}
        self.db.add(Series(tmdb_id=70523, title="Dark", source="provider_1", strm_path=str(vod),
                           sonarr_path=str(dl)))
        dup = Duplicate(tmdb_id=70523, media_type="series", resolution="pending",
                        sources=[{"source": "sonarr", "path": str(dl)},
                                 {"source": "provider_1", "path": str(vod)}])
        self.db.add(dup)
        self.db.commit()
        return dup.id

    def test_show_folder_without_a_strm_is_refused(self):
        self.assert_refused(self.show(with_strm=False))

    def test_show_with_a_strm_still_resolves(self):
        dup_id = self.show()
        self.resolve(dup_id, "keep_vod")
        self.assertFalse(self.mkv.exists())
        self.assertEqual("keep_vod", self.db.get(Duplicate, dup_id).resolution)


if __name__ == "__main__":
    unittest.main()

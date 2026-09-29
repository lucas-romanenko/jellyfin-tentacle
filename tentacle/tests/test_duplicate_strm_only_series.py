"""A show whose only Sonarr "episode files" are Tentacle's .strm files is not
downloaded, so it is no duplicate, and Keep Downloaded must never delete its
VOD copy (#255).

Sonarr 4 treats .strm as video: a rescan of a series folder the VOD sync also
writes to lists Tentacle's .strm files as the series' episode files, and
episodeFileCount > 0. The Sonarr scan took that as "downloaded" and recorded a
duplicate; Keep Downloaded (and Resolve All, which only sends keep_radarr)
then deleted every .strm, tvshow.nfo and the empty folders, and converted the
row to Sonarr-owned so the VOD sync skipped the show for good.

Run from tentacle/:  python -m unittest tests.test_duplicate_strm_only_series
"""
import logging
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Duplicate, Series


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


SHOW_DIR = "/media/vod/shows/Fargo (2014)"


class FakeSonarr:
    series = []          # what GET /series returns
    files = {}           # series id -> episode files
    files_error = False

    def __init__(self, url, key):
        pass

    def get_all_series(self, raise_errors=False):
        return [dict(s) for s in self.series]

    def get_series_by_tmdb(self, tmdb_id, raise_errors=False):
        return next((dict(s) for s in self.series if s.get("tmdbId") == tmdb_id), None)

    def get_episode_files(self, series_id):
        if self.files_error:
            raise RuntimeError("Sonarr timed out")
        return [dict(f) for f in self.files.get(series_id, [])]


def _strm_files(show):
    return [{"id": 1, "path": f"{show}/Season 01/Fargo (2014) S01E01.strm"},
            {"id": 2, "path": f"{show}/Season 02/Fargo (2014) S02E01.strm"}]


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = Path(tmp)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("sonarr_url", "http://sonarr"), ("sonarr_api_key", "k"), ("data_dir", tmp)):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        FakeSonarr.series = [{"id": 7, "tmdbId": 60622, "tvdbId": 9, "title": "Fargo", "year": 2014,
                              "path": SHOW_DIR, "monitorNewItems": "none",
                              "statistics": {"episodeFileCount": 2}}]
        FakeSonarr.files = {7: _strm_files(SHOW_DIR)}
        FakeSonarr.files_error = False
        mock.patch("services.sonarr.SonarrService", FakeSonarr).start()
        self.addCleanup(mock.patch.stopall)


class ScanRecordsNoFalseDuplicate(_Base):
    def setUp(self):
        super().setUp()
        self.db.add(Series(tmdb_id=60622, title="Fargo", year="2014", source="provider_1", strm_path=SHOW_DIR))
        self.db.commit()

    def _scan(self):
        from services.sonarr import scan_sonarr_library
        scan_sonarr_library(self.db)
        self.db.commit()
        return self.db.query(Duplicate).filter_by(tmdb_id=60622).all()

    def test_strm_only_episode_files_are_not_a_duplicate(self):
        self.assertEqual([], self._scan(), "a show with only Tentacle's .strm files was recorded as downloaded")

    def test_a_real_download_is_still_a_duplicate(self):
        FakeSonarr.files[7].append({"id": 3, "path": f"{SHOW_DIR}/Season 01/Fargo - S01E02.mkv"})
        self.assertEqual(1, len(self._scan()))

    def test_unreadable_files_still_record_it(self):
        """Unknown is not "no download": the duplicate is recorded as before
        (sonarr_path is set by this scan, so a later scan would never
        record it), and Keep Downloaded checks again before deleting."""
        FakeSonarr.files_error = True
        self.assertEqual(1, len(self._scan()))


class ScanDismissesFalseDuplicates(_Base):
    def test_pending_strm_only_duplicate_is_dismissed(self):
        self.db.add(Series(tmdb_id=60622, title="Fargo", year="2014", source="provider_1",
                           strm_path=SHOW_DIR, sonarr_path=SHOW_DIR))
        self.db.add(Duplicate(tmdb_id=60622, media_type="series", resolution="pending",
                              sources=[{"source": "sonarr", "path": SHOW_DIR},
                                       {"source": "provider_1", "path": SHOW_DIR}]))
        self.db.commit()
        from services.sonarr import scan_sonarr_library
        scan_sonarr_library(self.db)
        self.db.commit()
        dup = self.db.query(Duplicate).one()
        self.assertEqual("keep_both", dup.resolution)
        self.assertIsNotNone(dup.resolved_at)
        self.assertEqual("provider_1", self.db.query(Series).one().source)

    def test_real_pending_duplicate_stays(self):
        FakeSonarr.files[7].append({"id": 3, "path": f"{SHOW_DIR}/Season 01/Fargo - S01E02.mkv"})
        self.db.add(Series(tmdb_id=60622, title="Fargo", year="2014", source="provider_1",
                           strm_path=SHOW_DIR, sonarr_path=SHOW_DIR))
        self.db.add(Duplicate(tmdb_id=60622, media_type="series", resolution="pending",
                              sources=[{"source": "sonarr", "path": SHOW_DIR},
                                       {"source": "provider_1", "path": SHOW_DIR}]))
        self.db.commit()
        from services.sonarr import scan_sonarr_library
        scan_sonarr_library(self.db)
        self.db.commit()
        self.assertEqual("pending", self.db.query(Duplicate).one().resolution)


class KeepDownloadedRefusesWithoutADownload(_Base):
    def setUp(self):
        super().setUp()
        self.show = self.root / "vod" / "shows" / "Fargo (2014)"
        for s in ("Season 01", "Season 02"):
            (self.show / s).mkdir(parents=True)
        self.eps = [self.show / "Season 01" / "Fargo (2014) S01E01.strm",
                    self.show / "Season 02" / "Fargo (2014) S02E01.strm"]
        for p in self.eps:
            p.write_text("http://p/series/1.mp4")
        (self.show / "tvshow.nfo").write_text("<tvshow/>")
        FakeSonarr.series[0]["path"] = str(self.show)
        FakeSonarr.files = {7: _strm_files(str(self.show))}
        self.db.add(Series(tmdb_id=60622, title="Fargo", year="2014", source="provider_1",
                           strm_path=str(self.show), sonarr_path=str(self.show)))
        self.dup = Duplicate(tmdb_id=60622, media_type="series", resolution="pending",
                             sources=[{"source": "sonarr", "path": str(self.show)},
                                      {"source": "provider_1", "path": str(self.show)}])
        self.db.add(self.dup)
        self.db.commit()

    def _untouched(self):
        for p in self.eps:
            self.assertTrue(p.exists(), "Keep Downloaded deleted the VOD show although nothing was downloaded")
        self.assertTrue((self.show / "tvshow.nfo").exists())
        row = self.db.query(Series).one()
        self.assertEqual("provider_1", row.source)
        self.assertEqual(str(self.show), row.strm_path)

    def test_keep_downloaded_refuses(self):
        from routers.duplicates import _apply_resolution
        with self.assertRaises(HTTPException) as cm:
            _apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(409, cm.exception.status_code)
        self.assertIn("Nothing downloaded", cm.exception.detail)
        self._untouched()

    def test_keep_downloaded_refuses_when_sonarr_cannot_be_asked(self):
        from routers.duplicates import _apply_resolution
        FakeSonarr.files_error = True
        with self.assertRaises(HTTPException) as cm:
            _apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(502, cm.exception.status_code)
        self._untouched()

    def test_resolve_all_leaves_the_false_one_pending(self):
        from routers.duplicates import resolve_all, ResolveAllRequest
        out = resolve_all(ResolveAllRequest(resolution="keep_radarr"), db=self.db)
        self.assertEqual({"success": False, "count": 0, "total": 1, "failed": 1}, out)
        self.assertEqual("pending", self.db.query(Duplicate).one().resolution)
        self._untouched()

    def test_keep_downloaded_with_a_real_download_deletes_the_strm(self):
        from routers.duplicates import _apply_resolution
        mkv = self.show / "Season 01" / "Fargo - S01E02.mkv"
        mkv.write_bytes(b"\0" * 64)
        FakeSonarr.files[7].append({"id": 3, "path": str(mkv)})
        _apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(any(p.exists() for p in self.eps))
        self.assertTrue(mkv.exists())
        self.assertEqual("sonarr", self.db.query(Series).one().source)


if __name__ == "__main__":
    unittest.main()

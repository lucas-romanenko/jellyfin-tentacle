"""#268: two Radarr (or Sonarr) library scans at once no longer both insert a new title.

Each scan loads every row, asks TMDB about each new title, and commits once.
Two scans that overlap (two webhooks for different titles, a webhook during
the nightly scan, "Scan now") both added the same new title, and the second
failed with "UNIQUE constraint failed" and rolled back everything it did.
A barrier holds both scans at the TMDB lookup of the new title, standing in
for TMDB's latency, on a real SQLite file.
"""
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import services.radarr as radarr
import services.sonarr as sonarr
import services.tmdb as tmdb

NEW_MOVIE = {"tmdbId": 588009, "title": "New Film", "year": 2020, "hasFile": True,
             "path": "/nonexistent/movies/New Film (2020)",
             "movieFile": {"path": "/nonexistent/movies/New Film (2020)/f.mkv"}}
NEW_SERIES = {"tmdbId": 4607, "tvdbId": 0, "title": "New Show", "year": 2020,
              "path": "/nonexistent/tv/New Show (2020)",
              "statistics": {"episodeFileCount": 3}}


class ConcurrentArrScans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db",
                               connect_args={"check_same_thread": False, "timeout": 30})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"),
                     ("data_dir", self.tmp)):
            mdb.set_setting(db, k, v)
        db.commit()
        db.close()

    def _two_scans(self, scan):
        barrier = threading.Barrier(2, timeout=2)

        def details(self_, tmdb_id):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return None

        errors = []

        def run():
            db = self.Session()
            try:
                scan(db)
            except Exception as e:
                errors.append(f"{type(e).__name__}: {getattr(e, 'orig', e)}")
                db.rollback()
            finally:
                db.close()

        with mock.patch.object(tmdb.TMDBService, "_request", lambda *a, **k: None), \
             mock.patch.object(tmdb.TMDBService, "get_movie_details", details), \
             mock.patch.object(tmdb.TMDBService, "get_series_details", details):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        return errors

    def test_two_overlapping_radarr_scans_both_succeed(self):
        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return [dict(NEW_MOVIE)]

        with mock.patch.object(radarr, "RadarrService", Fake), \
             mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            errors = self._two_scans(radarr.scan_radarr_library)
        self.assertEqual([], errors)
        db = self.Session()
        self.assertEqual(1, db.query(mdb.Movie).filter_by(tmdb_id=588009).count())
        db.close()

    def test_two_overlapping_sonarr_scans_both_succeed(self):
        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self, raise_errors=False):
                return [dict(NEW_SERIES)]

            def __getattr__(self, name):
                # Anything else the scan asks Sonarr (episodes, files): nothing.
                return lambda *a, **k: []

        with mock.patch.object(sonarr, "SonarrService", Fake), \
             mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None):
            errors = self._two_scans(sonarr.scan_sonarr_library)
        self.assertEqual([], errors)
        db = self.Session()
        self.assertEqual(1, db.query(mdb.Series).filter_by(tmdb_id=4607).count())
        db.close()


if __name__ == "__main__":
    unittest.main()

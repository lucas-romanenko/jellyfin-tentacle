"""A scan that removes a download clears its duplicate tombstone (#334).

Run from the tentacle/ directory:  tests/hermetic.py discover -s tests -p "test_scan_clears_duplicate_tombstone.py"

Duplicates -> Keep Downloaded deletes the VOD copy and keeps the duplicate as
resolution "keep_radarr": a tombstone that stops the VOD sync re-importing the
provider copy (services/sync.check_and_record_duplicate). The delete webhooks
clear it with the download ("a clean slate"), but when the download goes while
Tentacle misses the webhook, the next Radarr/Sonarr scan removes the row and
left the tombstone: the film was in neither copy, for good.
"""
import random
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tmp_dirs import temp_dir

import models.database as mdb


def _movie(tmdb_id, has_file=True):
    return {"tmdbId": tmdb_id, "title": f"Film {tmdb_id}", "year": 2000, "hasFile": has_file,
            "path": f"/movies/Film {tmdb_id} (2000)",
            "movieFile": {"path": f"/movies/Film {tmdb_id} (2000)/f.mkv"} if has_file else None}


def _show(tmdb_id, files=3):
    return {"tmdbId": tmdb_id, "tvdbId": tmdb_id, "title": f"Show {tmdb_id}", "path": f"/tv/Show {tmdb_id}",
            "monitorNewItems": "none", "statistics": {"episodeFileCount": files}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        # As the app (models.database.SessionLocal): no autoflush.
        self.db = sessionmaker(bind=engine, autoflush=False)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"), ("data_dir", self.tmp)):
            mdb.set_setting(self.db, k, v)
        import services.tmdb as tmdb
        p = mock.patch.object(tmdb.TMDBService, "_request", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def radarr_scan(self, movies):
        import services.radarr as radarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return movies

        with mock.patch.object(radarr, "RadarrService", Fake), \
                mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            out = radarr.scan_radarr_library(self.db)
        self.db.expire_all()
        return out

    def sonarr_scan(self, shows):
        import services.sonarr as sonarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self):
                return shows

            def __getattr__(self, name):
                return lambda *a, **k: []

        with mock.patch.object(sonarr, "SonarrService", Fake), \
                mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None):
            out = sonarr.scan_sonarr_library(self.db)
        self.db.expire_all()
        return out

    def dup(self, tmdb_id, resolution, media_type="movie"):
        self.db.add(mdb.Duplicate(tmdb_id=tmdb_id, media_type=media_type, resolution=resolution,
                                  sources=[{"source": "radarr" if media_type == "movie" else "sonarr", "path": "/x"},
                                           {"source": "provider_1", "path": "/vod/x.strm"}]))
        self.db.commit()

    def resolutions(self, media_type="movie"):
        return sorted((d.tmdb_id, d.resolution) for d in
                      self.db.query(mdb.Duplicate).filter(mdb.Duplicate.media_type == media_type))

    def provider_copy_blocked(self, tmdb_id, media_type="movie"):
        """What the next VOD sync does with the provider's copy."""
        from services.sync import check_and_record_duplicate
        p = mdb.Provider(id=1, name="P", server_url="http://p", username="u", password="p")
        return check_and_record_duplicate(tmdb_id, media_type, "provider_1", "/vod/x.strm", p, self.db)


class RadarrScanClearsTombstone(_Base):
    def kept_download(self, tmdb_id):
        # After Keep Downloaded: the row is the download's, the tombstone stays.
        self.db.add(mdb.Movie(tmdb_id=tmdb_id, title=f"Film {tmdb_id}", year="2000", source="radarr",
                              radarr_path=f"/movies/Film {tmdb_id} (2000)/f.mkv"))
        self.dup(tmdb_id, "keep_radarr")

    def library(self, *ids):
        for i in ids:
            self.db.add(mdb.Movie(tmdb_id=i, title=f"Film {i}", year="2000", source="radarr",
                                  radarr_path=f"/movies/Film {i} (2000)/f.mkv"))
        self.db.commit()

    def test_film_removed_from_radarr_brings_the_provider_copy_back(self):
        self.library(1, 2, 3, 4, 5)
        self.kept_download(100)
        self.radarr_scan([_movie(i) for i in (1, 2, 3, 4, 5)])
        self.assertEqual([], self.resolutions())
        self.assertFalse(self.provider_copy_blocked(100), "the VOD copy never comes back")

    def test_file_reported_missing_brings_the_provider_copy_back(self):
        self.library(1, 2, 3, 4, 5)
        self.kept_download(100)
        self.radarr_scan([_movie(i) for i in (1, 2, 3, 4, 5)] + [_movie(100, has_file=False)])
        self.assertIsNone(self.db.query(mdb.Movie).filter_by(tmdb_id=100).first())
        self.assertEqual([], self.resolutions())
        self.assertFalse(self.provider_copy_blocked(100))

    def test_download_still_there_keeps_the_tombstone(self):
        self.library(1, 2, 3, 4, 5)
        self.kept_download(100)
        self.radarr_scan([_movie(i) for i in (1, 2, 3, 4, 5, 100)])
        self.assertEqual([(100, "keep_radarr")], self.resolutions())
        self.assertTrue(self.provider_copy_blocked(100), "Keep Downloaded no longer enforced")

    def test_outage_refusal_keeps_rows_and_tombstones(self):
        self.kept_download(100)
        self.kept_download(101)
        self.kept_download(102)
        self.library(1)
        out = self.radarr_scan([_movie(i, has_file=False) for i in (100, 101, 102)] + [_movie(1)])
        self.assertEqual(3, out["removals_refused"])
        self.assertEqual([(100, "keep_radarr"), (101, "keep_radarr"), (102, "keep_radarr")], self.resolutions())

    def test_history_and_in_progress_resolutions_are_left_alone(self):
        self.library(1)
        for i, res in ((200, "keep_vod"), (201, "keep_both"), (202, "resolving")):
            self.dup(i, res)
        self.radarr_scan([_movie(1)])
        self.assertEqual([(200, "keep_vod"), (201, "keep_both"), (202, "resolving")], self.resolutions())

    def test_stale_pending_duplicate_goes_but_one_with_a_row_stays(self):
        self.library(1)
        self.db.add(mdb.Movie(tmdb_id=300, title="VOD", year="2000", source="provider_1", strm_path="/vod/x.strm"))
        self.db.commit()
        self.dup(300, "pending")   # the VOD row is still there
        self.dup(301, "pending")   # no copy at all
        self.radarr_scan([_movie(1)])
        self.assertEqual([(300, "pending")], self.resolutions())

    def test_tombstone_left_by_an_earlier_scan_is_healed(self):
        """Installs hit before the fix: no row, tombstone left."""
        self.library(1)
        self.dup(100, "keep_radarr")
        self.assertTrue(self.provider_copy_blocked(100))
        self.radarr_scan([_movie(1)])
        self.assertFalse(self.provider_copy_blocked(100))

    def test_scan_that_cannot_read_radarr_changes_nothing(self):
        self.dup(100, "keep_radarr")
        out = self.radarr_scan([])
        self.assertIn("error", out)
        self.assertEqual([(100, "keep_radarr")], self.resolutions())


class SonarrScanClearsTombstone(_Base):
    def test_show_removed_from_sonarr_brings_the_provider_copy_back(self):
        self.db.add(mdb.Series(tmdb_id=1, title="Show 1", source="sonarr", sonarr_path="/tv/Show 1"))
        self.db.add(mdb.Series(tmdb_id=500, title="Show 500", source="sonarr", sonarr_path="/tv/Show 500"))
        self.db.commit()
        self.dup(500, "keep_radarr", "series")
        self.sonarr_scan([_show(1)])
        self.assertIsNone(self.db.query(mdb.Series).filter_by(tmdb_id=500).first())
        self.assertEqual([], self.resolutions("series"))
        self.assertFalse(self.provider_copy_blocked(500, "series"))

    def test_show_still_downloaded_keeps_the_tombstone(self):
        self.db.add(mdb.Series(tmdb_id=500, title="Show 500", source="sonarr", sonarr_path="/tv/Show 500"))
        self.db.commit()
        self.dup(500, "keep_radarr", "series")
        self.sonarr_scan([_show(500)])
        self.assertEqual([(500, "keep_radarr")], self.resolutions("series"))


class ScanTombstoneProperty(_Base):
    """Random libraries: after any scan, a tombstone exists exactly when its
    title still has a row; a refused (outage) scan keeps every row; history
    and in-progress resolutions are never touched."""
    SEEDS = 1000

    def test_random_libraries(self):
        import services.radarr as radarr
        resolutions = ("pending", "keep_radarr", "keep_vod", "keep_both", "resolving")
        for seed in range(self.SEEDS):
            rnd = random.Random(seed)
            self.db.query(mdb.Duplicate).delete()
            self.db.query(mdb.Movie).delete()
            self.db.query(mdb.DeletionLog).delete()
            self.db.commit()
            listing, nofile = [], set()
            n = rnd.randint(1, 12)
            for i in range(1, n + 1):
                kind = rnd.choice(("radarr", "provider", "none"))
                if kind == "radarr":
                    self.db.add(mdb.Movie(tmdb_id=i, title=f"F{i}", source="radarr", radarr_path=f"/m/{i}/f.mkv"))
                elif kind == "provider":
                    self.db.add(mdb.Movie(tmdb_id=i, title=f"F{i}", source="provider_1", strm_path=f"/v/{i}.strm"))
                if rnd.random() < 0.6:
                    self.db.add(mdb.Duplicate(tmdb_id=i, media_type="movie", resolution=rnd.choice(resolutions),
                                              sources=[]))
                state = rnd.choice(("file", "nofile", "unlisted"))
                if state != "unlisted":
                    listing.append(_movie(i, has_file=state == "file"))
                if state == "nofile":
                    nofile.add(i)
            listing.append(_movie(10_000))   # never an empty listing
            self.db.commit()
            before = {d.tmdb_id: d.resolution for d in self.db.query(mdb.Duplicate)}
            radarr_rows_before = {m.tmdb_id for m in self.db.query(mdb.Movie).filter_by(source="radarr")}
            out = self.radarr_scan(listing)
            rows = {m.tmdb_id for m in self.db.query(mdb.Movie)}
            after = {d.tmdb_id: d.resolution for d in self.db.query(mdb.Duplicate)}
            msg = f"seed {seed}: before={before} rows={rows} after={after} out={out}"
            for t, res in before.items():
                if res in ("keep_vod", "keep_both", "resolving"):
                    self.assertEqual(res, after.get(t), msg)
                elif t in rows:
                    self.assertEqual(res, after.get(t), msg)       # a copy is left: kept
                else:
                    self.assertNotIn(t, after, msg)                 # no copy left: cleared
            if out.get("removals_refused"):   # an outage: no row Radarr still lists is removed
                self.assertTrue(radarr_rows_before & nofile <= rows, msg)
            for t, res in after.items():
                if res == "keep_radarr":
                    self.assertIn(t, rows, msg)
        self.assertTrue(hasattr(radarr, "clear_duplicates_without_a_copy"))


if __name__ == "__main__":
    unittest.main()

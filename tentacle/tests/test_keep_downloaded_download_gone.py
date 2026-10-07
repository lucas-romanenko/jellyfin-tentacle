"""After Keep Downloaded, a download deleted later brings the VOD copy back (#334).

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_keep_downloaded_download_gone.py"

Keep Downloaded deletes the VOD files, converts the row to a downloaded one
and leaves the duplicate as "keep_radarr": a tombstone that stops the VOD
sync re-importing the provider copy. The delete webhooks clear it with the
row, but when Tentacle missed the webhook (down, not set up, the file gone
outside the *arr) the Radarr/Sonarr scan removed the row and left the
tombstone, and check_and_record_duplicate then refused the provider copy on
every sync: the title was in neither copy, for good. Now the scans drop the
removed title's tombstones, and any tombstone already left without a row.
"""
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Duplicate, Movie, Series
from tmp_dirs import temp_dir

FILM = 990334
SHOW = 880334
OTHER = 990335   # keeps Radarr/Sonarr non-empty


def _film(tmdb_id, has_file=True):
    return {"tmdbId": tmdb_id, "title": f"Film {tmdb_id}", "year": 2001, "hasFile": has_file,
            "path": f"/movies/Film {tmdb_id}", "movieFile": {"path": f"/movies/Film {tmdb_id}/f.mkv"}}


def _show(tmdb_id, files=3):
    return {"tmdbId": tmdb_id, "tvdbId": tmdb_id, "title": f"Show {tmdb_id}", "path": f"/tv/Show {tmdb_id}",
            "monitorNewItems": "none", "statistics": {"episodeFileCount": files}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        # As production's SessionLocal: no autoflush.
        self.db = sessionmaker(bind=engine, autoflush=False)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"), ("data_dir", self.tmp)):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        import services.tmdb as tmdb
        p = mock.patch.object(tmdb.TMDBService, "_request", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _tombstone(self, tmdb_id, media_type, resolution="keep_radarr"):
        self.db.add(Duplicate(tmdb_id=tmdb_id, media_type=media_type, resolution=resolution,
                              sources=[{"source": "provider_1", "path": "/vod/x.strm"}]))

    def _vod_sync_skips(self, tmdb_id, media_type):
        from services.sync import check_and_record_duplicate
        return check_and_record_duplicate(tmdb_id, media_type, "provider_1", "/vod/x.strm", None, self.db)

    def _dups(self, tmdb_id, media_type):
        return self.db.query(Duplicate).filter_by(tmdb_id=tmdb_id, media_type=media_type).count()


class RadarrScan(_Base):
    def _scan(self, movies):
        import services.radarr as radarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return movies

        with mock.patch.object(radarr, "RadarrService", Fake), \
                mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None):
            out = radarr.scan_radarr_library(self.db)
        self.db.commit()
        return out

    def _kept_download(self):
        """The state Keep Downloaded leaves: a converted row and a tombstone."""
        self.db.add(Movie(tmdb_id=FILM, title="Film", year="2001", source="radarr",
                          radarr_path=f"/movies/Film {FILM}/f.mkv"))
        self.db.add(Movie(tmdb_id=OTHER, title="Other", year="2001", source="radarr",
                          radarr_path=f"/movies/Film {OTHER}/f.mkv"))
        self._tombstone(FILM, "movie")
        self.db.commit()
        self.assertTrue(self._vod_sync_skips(FILM, "movie"), "precondition: the tombstone holds")

    def test_film_gone_from_radarr_comes_back_from_vod(self):
        self._kept_download()
        self._scan([_film(OTHER)])
        self.assertIsNone(self.db.query(Movie).filter_by(tmdb_id=FILM).first())
        self.assertEqual(0, self._dups(FILM, "movie"))
        self.assertFalse(self._vod_sync_skips(FILM, "movie"),
                         "the provider copy is refused for good after the download went")

    def test_file_gone_but_film_still_in_radarr_comes_back_from_vod(self):
        self._kept_download()
        self._scan([_film(FILM, has_file=False), _film(OTHER)] + [_film(OTHER + 1 + i) for i in range(10)])
        self.assertEqual(0, self._dups(FILM, "movie"))
        self.assertFalse(self._vod_sync_skips(FILM, "movie"))

    def test_tombstone_already_left_without_a_row_is_dropped(self):
        """A scan before the fix removed the row and kept the tombstone."""
        self.db.add(Movie(tmdb_id=OTHER, title="Other", source="radarr", radarr_path="/m/o.mkv"))
        self._tombstone(FILM, "movie")
        self.db.commit()
        out = self._scan([_film(OTHER)])
        self.assertEqual(1, out.get("tombstones_dropped"))
        self.assertFalse(self._vod_sync_skips(FILM, "movie"))

    def test_download_still_there_keeps_the_tombstone(self):
        self._kept_download()
        self._scan([_film(FILM), _film(OTHER)])
        self.assertEqual(1, self._dups(FILM, "movie"))
        self.assertTrue(self._vod_sync_skips(FILM, "movie"))

    def test_download_still_there_without_a_row_yet_keeps_the_tombstone(self):
        """The scan adds the row for a download first, so its tombstone stays."""
        self._tombstone(FILM, "movie")
        self.db.add(Movie(tmdb_id=OTHER, title="Other", source="radarr", radarr_path="/m/o.mkv"))
        self.db.commit()
        self._scan([_film(FILM), _film(OTHER)])
        self.assertEqual(1, self._dups(FILM, "movie"))
        self.assertTrue(self._vod_sync_skips(FILM, "movie"))

    def test_storage_outage_keeps_rows_and_tombstones(self):
        self._kept_download()
        for i in range(10):
            self.db.add(Movie(tmdb_id=OTHER + 1 + i, title=f"F{i}", source="radarr", radarr_path=f"/m/{i}.mkv"))
        self.db.commit()
        out = self._scan([_film(FILM, has_file=False), _film(OTHER, has_file=False)]
                         + [_film(OTHER + 1 + i, has_file=False) for i in range(10)])
        self.assertTrue(out.get("removals_refused"))
        self.assertEqual(1, self._dups(FILM, "movie"))

    def test_other_resolutions_without_a_row_are_left_alone(self):
        self.db.add(Movie(tmdb_id=OTHER, title="Other", source="radarr", radarr_path="/m/o.mkv"))
        self._tombstone(FILM, "movie", resolution="keep_both")
        self._tombstone(SHOW, "series")   # a series tombstone is the Sonarr scan's
        self.db.commit()
        self._scan([_film(OTHER)])
        self.assertEqual(1, self._dups(FILM, "movie"))
        self.assertEqual(1, self._dups(SHOW, "series"))


class SonarrScan(_Base):
    def _scan(self, shows):
        import services.sonarr as sonarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self):
                return shows

            def get_episode_files(self, series_id):
                return []

        with mock.patch.object(sonarr, "SonarrService", Fake), \
                mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None):
            out = sonarr.scan_sonarr_library(self.db)
        self.db.commit()
        return out

    def _kept_download(self):
        self.db.add(Series(tmdb_id=SHOW, title="Show", source="sonarr", sonarr_path=f"/tv/Show {SHOW}"))
        self.db.add(Series(tmdb_id=OTHER, title="Other", source="sonarr", sonarr_path=f"/tv/Show {OTHER}"))
        self._tombstone(SHOW, "series")
        self.db.commit()
        self.assertTrue(self._vod_sync_skips(SHOW, "series"), "precondition: the tombstone holds")

    def test_show_gone_from_sonarr_comes_back_from_vod(self):
        self._kept_download()
        self._scan([_show(OTHER)])
        self.assertEqual(0, self._dups(SHOW, "series"))
        self.assertFalse(self._vod_sync_skips(SHOW, "series"))

    def test_every_episode_file_deleted_comes_back_from_vod(self):
        """The show stays in Sonarr with no files left (EpisodeFileDelete only rescans)."""
        self._kept_download()
        self._scan([_show(SHOW, files=0), _show(OTHER)] + [_show(OTHER + 1 + i) for i in range(10)])
        self.assertEqual(0, self._dups(SHOW, "series"))
        self.assertFalse(self._vod_sync_skips(SHOW, "series"))

    def test_tombstone_already_left_without_a_row_is_dropped(self):
        self.db.add(Series(tmdb_id=OTHER, title="Other", source="sonarr", sonarr_path="/tv/o"))
        self._tombstone(SHOW, "series")
        self.db.commit()
        out = self._scan([_show(OTHER)])
        self.assertEqual(1, out.get("tombstones_dropped"))
        self.assertFalse(self._vod_sync_skips(SHOW, "series"))

    def test_download_still_there_keeps_the_tombstone(self):
        self._kept_download()
        self._scan([_show(SHOW), _show(OTHER)])
        self.assertEqual(1, self._dups(SHOW, "series"))
        self.assertTrue(self._vod_sync_skips(SHOW, "series"))


if __name__ == "__main__":
    unittest.main()

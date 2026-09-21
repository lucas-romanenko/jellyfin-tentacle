"""Resolving a *series* duplicate must act on series.

Series duplicates are created by the Sonarr scan (source "sonarr", the VOD
source's path is the show *folder*), but _apply_resolution only knew movies:
  - "Keep VOD" asked Radarr to delete — with its files — the MOVIE whose TMDB
    id equals the series' TMDB id. TMDB movie and TV ids are separate number
    spaces, so that is an unrelated film when one exists, and a 502 (the series
    can never be resolved) when none does. Sonarr's copy was never touched.
  - "Keep Downloaded" / "Resolve All" handed the show folder to a helper that
    only deletes a path ending in .strm: nothing was deleted, yet the record
    was converted to downloaded-only (so Tentacle stopped managing the VOD
    folder, now orphaned in Jellyfin) and the deletion log recorded ".strm/.nfo
    files deleted".
The dashboard makes the first one the likely click: for a series duplicate
it only offers "Keep VOD" (the "Keep Downloaded" button checks source == radarr).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Duplicate, Movie, Series, Setting
from routers import duplicates


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        engine = create_engine(f"sqlite:///{self.tmp.name}/t.db",
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r"),
                     ("sonarr_url", "http://sonarr"), ("sonarr_api_key", "s")):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()
        self.radarr = mock.patch("services.radarr.RadarrService").start()
        self.sonarr = mock.patch("services.sonarr.SonarrService").start()
        self.radarr.return_value.delete_movie.return_value = True
        self.sonarr.return_value.delete_series.return_value = True

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.tmp.cleanup()


class TestSeriesDuplicates(_Base):
    def setUp(self):
        super().setUp()
        self.show = Path(self.tmp.name) / "vod" / "shows" / "Show (2010)"
        (self.show / "Season 01").mkdir(parents=True)
        for f in ("Season 01/Show S01E01.strm", "Season 01/Show S01E01.nfo", "tvshow.nfo"):
            (self.show / f).write_text("x")
        self.db.add(Series(tmdb_id=1418, title="Show", source="provider_1",
                           strm_path=str(self.show), sonarr_path="/tv/Show (2010)"))
        self.dup = Duplicate(tmdb_id=1418, media_type="series", resolution="pending",
                             sources=[{"source": "sonarr", "path": "/tv/Show (2010)"},
                                      {"source": "provider_1", "path": str(self.show)}])
        self.db.add(self.dup); self.db.commit()

    def test_keep_vod_never_deletes_a_radarr_movie(self):
        duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.radarr.return_value.delete_movie.assert_not_called()

    def test_keep_vod_removes_the_sonarr_copy(self):
        duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.sonarr.return_value.delete_series.assert_called_once()
        self.assertEqual(self.sonarr.return_value.delete_series.call_args.args[0], 1418)
        self.assertIsNone(self.db.query(Series).one().sonarr_path)

    def test_keep_downloaded_removes_the_vod_episodes(self):
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(list(self.show.rglob("*.strm")), [])
        self.assertEqual(self.db.query(Series).one().source, "sonarr")


class TestMovieDuplicatesUnchanged(_Base):
    def setUp(self):
        super().setUp()
        folder = Path(self.tmp.name) / "vod" / "movies" / "Film (1999)"
        folder.mkdir(parents=True)
        self.strm = folder / "Film (1999).strm"
        self.strm.write_text("x")
        (folder / "Film (1999).nfo").write_text("x")
        self.db.add(Movie(tmdb_id=603, title="Film", source="provider_1",
                          strm_path=str(self.strm), radarr_path="/movies/Film"))
        self.dup = Duplicate(tmdb_id=603, media_type="movie", resolution="pending",
                             sources=[{"source": "radarr", "path": "/movies/Film"},
                                      {"source": "provider_1", "path": str(self.strm)}])
        self.db.add(self.dup); self.db.commit()

    def test_keep_vod_deletes_from_radarr(self):
        duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.radarr.return_value.delete_movie.assert_called_once_with(603, delete_files=True)
        self.sonarr.return_value.delete_series.assert_not_called()

    def test_keep_downloaded_deletes_the_strm(self):
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())


if __name__ == "__main__":
    unittest.main()

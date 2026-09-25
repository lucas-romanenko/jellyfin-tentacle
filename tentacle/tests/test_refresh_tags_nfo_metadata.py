"""POST /api/sync/refresh-tags rewrites every non-Radarr NFO from the thin DB
row: the imdbid, cast, directors, studios, tagline the sync wrote are gone,
a Sonarr show's tvdbid/uniqueid is dropped, and <dateadded> is reset to now.
Self-contained, no network.  Tentacle 755ea67.

Run from tentacle/:  python -m unittest discover -s tests -p "test_refresh_tags_nfo_metadata.py"
"""
import logging, shutil, tempfile, unittest
from pathlib import Path
from unittest import mock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import models.database as mdb
from models.database import Movie, Series, Setting
from services.nfo import write_movie_nfo, write_series_nfo


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


FULL = {"tmdb_id": 603, "title": "The Matrix", "year": "1999", "overview": "Neo.",
        "runtime": 136, "rating": 8.2, "genres": ["Action"], "imdb_id": "tt0133093",
        "tagline": "Welcome to the Real World.", "studios": ["Warner Bros."],
        "directors": ["Lana Wachowski"], "cast": [{"name": "Keanu Reeves", "character": "Neo"}],
        "poster_path": "/p.jpg", "backdrop_path": "/b.jpg"}


class RefreshTagsKeepsNfoMetadata(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db"); mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)(); self.addCleanup(self.db.close)
        self.db.add(Setting(key="data_dir", value=tmp))
        root = Path(tmp)
        # A VOD movie exactly as _sync_movies wrote it (full TMDB details).
        mdir = root / "movies" / "The Matrix (1999)"; mdir.mkdir(parents=True)
        self.mnfo = mdir / "The Matrix (1999).nfo"
        (mdir / "The Matrix (1999).strm").write_text("http://p/movie/u/p/1.mp4")
        write_movie_nfo(self.mnfo, FULL, ["Netflix Movies"])
        self.mnfo.write_text(self.mnfo.read_text().replace(
            self.mnfo.read_text().split("<dateadded>")[1].split("</dateadded>")[0], "2024-01-01 00:00:00"))
        self.db.add(Movie(tmdb_id=603, title="The Matrix", year="1999", source="provider_1", provider_id=1,
                          overview="Neo.", runtime=136, rating=8.2, genres=["Action"],
                          strm_path=str(mdir / "The Matrix (1999).strm"), nfo_path=str(self.mnfo),
                          tags=["Netflix Movies"]))
        # A Sonarr show whose tvshow.nfo the Sonarr scan wrote (with its tvdbid).
        sdir = root / "shows" / "Cheers (1982)"; sdir.mkdir(parents=True)
        self.snfo = sdir / "tvshow.nfo"
        write_series_nfo(self.snfo, {"tmdb_id": 1414, "tvdb_id": 77623, "title": "Cheers", "year": "1982"},
                         ["Downloaded TV"])
        self.db.add(Series(tmdb_id=1414, title="Cheers", year="1982", source="sonarr",
                           sonarr_path=str(sdir), nfo_path=str(self.snfo), tags=["Downloaded TV"]))
        self.db.commit()

    def _refresh(self):
        import routers.sync as sync_router
        with mock.patch.object(sync_router, "refresh_recently_added_tags", lambda db: (0, 0)), \
                mock.patch("services.smartlists.refresh_smartlist_playlists", lambda db: None):
            sync_router.refresh_tags(db=self.db)

    def test_movie_nfo_keeps_ids_and_credits(self):
        self._refresh()
        nfo = self.mnfo.read_text()
        for needle in ("<imdbid>tt0133093</imdbid>", "<name>Keanu Reeves</name>",
                       "<director>Lana Wachowski</director>", "<studio>Warner Bros.</studio>"):
            self.assertIn(needle, nfo, f"Refresh Tags erased {needle} from the VOD NFO")

    def test_movie_nfo_keeps_dateadded(self):
        self._refresh()
        self.assertIn("<dateadded>2024-01-01 00:00:00</dateadded>", self.mnfo.read_text(),
                      "Refresh Tags reset <dateadded> to now")

    def test_sonarr_show_keeps_tvdbid(self):
        self._refresh()
        self.assertIn("<tvdbid>77623</tvdbid>", self.snfo.read_text(),
                      "Refresh Tags rewrote a Sonarr show's tvshow.nfo and dropped its tvdbid")

    def test_tags_are_still_brought_up_to_date(self):
        movie = self.db.query(Movie).filter_by(tmdb_id=603).one()
        movie.tags = ["Netflix Movies", "Watchlist"]
        self.db.commit()
        self._refresh()
        nfo = self.mnfo.read_text()
        self.assertIn("<tag>Watchlist</tag>", nfo)
        self.assertIn("<tag>Netflix Movies</tag>", nfo)
        self.assertEqual(nfo.count("<tag>"), 2)

    def test_a_missing_nfo_is_still_written(self):
        self.mnfo.unlink()
        self._refresh()
        self.assertIn("<title>The Matrix</title>", self.mnfo.read_text())


if __name__ == "__main__":
    unittest.main()

"""A provider show that Sonarr already has is a duplicate, not "one of ours".

Run from the tentacle/ directory:  python -m unittest discover -s tests

Two defects hid each other in the VOD series sync:
  * its "already in the DB" shortcut matched ANY Series row for the TMDB id,
    where the movie path (rightly) matches only rows owned by this provider --
    so a Sonarr-owned show was waved through as existing VOD and never reached
    check_and_record_duplicate;
  * check_and_record_duplicate gives downloaded content priority only when the
    row's source is "radarr". A "sonarr" row fell through to `return False`,
    which tells the caller to insert -- and tmdb_id is UNIQUE, so that insert
    raises IntegrityError and loses the whole category batch.
Fixing the first without the second would turn a missed duplicate into a crash.
"""
import re
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SYNC = (Path(__file__).resolve().parents[1] / "services" / "sync.py").read_text(encoding="utf-8")


class RecorderTreatsSonarrLikeRadarr(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        self.mdb = mdb
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.provider = mdb.Provider(name="P", server_url="http://p.example", username="u", password="p")
        self.db.add(self.provider)
        self.db.commit()

    def _check(self, media_type, tmdb_id=1403):
        from services.sync import check_and_record_duplicate
        out = check_and_record_duplicate(tmdb_id, media_type, f"provider_{self.provider.id}",
                                         "/media/vod/x", self.provider, self.db)
        self.db.commit()
        return out

    def test_a_sonarr_owned_show_wins_and_is_recorded(self):
        self.db.add(self.mdb.Series(tmdb_id=1403, title="Show", source="sonarr", sonarr_path="/tv/Show"))
        self.db.commit()
        self.assertTrue(self._check("series"),
                        "False tells the caller to INSERT a second row for a UNIQUE tmdb_id")
        dup = self.db.query(self.mdb.Duplicate).filter_by(media_type="series").one()
        self.assertEqual({"sonarr", f"provider_{self.provider.id}"}, {s["source"] for s in dup.sources})

    def test_radarr_still_wins_for_movies(self):
        self.db.add(self.mdb.Movie(tmdb_id=603, title="Film", source="radarr", radarr_path="/m/Film"))
        self.db.commit()
        self.assertTrue(self._check("movie", 603))

    def test_an_existing_row_never_yields_insert(self):
        """Whatever its source: tmdb_id is unique, so False is always a crash."""
        self.db.add(self.mdb.Series(tmdb_id=1403, title="Show", source="something_else"))
        self.db.commit()
        self.assertTrue(self._check("series"))

    def test_a_new_title_is_still_new(self):
        self.assertFalse(self._check("series"))
        self.assertEqual(0, self.db.query(self.mdb.Duplicate).count())


class SeriesShortcutMatchesThisProviderOnly(unittest.TestCase):
    def test_the_series_shortcut_is_scoped_like_the_movie_one(self):
        movie = re.search(r"if db\.query\(Movie\)\.filter\(([^\n]*)\)\.first\(\):\n\s+existing_provider_tmdb_ids\.add", SYNC)
        series = re.search(r"if db\.query\(Series\)\.filter\(([^\n]*)\)\.first\(\):\n\s+existing_provider_tmdb_ids\.add", SYNC)
        self.assertIsNotNone(movie)
        self.assertIsNotNone(series)
        self.assertIn("provider_id == provider.id", movie.group(1))
        self.assertIn("provider_id == provider.id", series.group(1),
                      "any Series row -- a Sonarr-owned one included -- is taken for existing VOD, "
                      "so the overlap never reaches check_and_record_duplicate")


if __name__ == "__main__":
    unittest.main()

"""A VOD title must never be written under a dot-prefixed name.

Jellyfin ignores every path matching `**/.*`. Titles that start with dots are
real ("...And Justice for All", ".hack//Sign", "...Watch Out, We're Mad"):
Tentacle wrote their .strm/.nfo into "...And Justice for All (1979)/", which
Jellyfin never scanned, so they were missing from the library with no error.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Movie, Series
from services import nfo


class TestVodFolderName(unittest.TestCase):
    def test_leading_dots_are_dropped(self):
        for title, want in (("...And Justice for All", "And Justice for All (1979)"),
                            (".hack//Sign", "hackSign (1979)"),
                            (". . .Title", "Title (1979)")):
            with self.subTest(title=title):
                name = nfo.vod_folder_name(title, "1979")
                self.assertFalse(name.startswith("."))
                self.assertEqual(name, want)

    def test_all_dots_still_gets_a_name(self):
        self.assertEqual(nfo.vod_folder_name("...", "2000"), "Unknown (2000)")

    def test_ordinary_titles_are_unchanged(self):
        self.assertEqual(nfo.vod_folder_name("Top Gun: Maverick", "2022"),
                         nfo.make_folder_name("Top Gun: Maverick", "2022"))

    def test_make_folder_name_itself_is_untouched(self):
        # Used to locate folders Radarr/Sonarr named; must keep their spelling.
        self.assertEqual(nfo.make_folder_name("...And Justice for All", "1979"),
                         "...And Justice for All (1979)")

    def test_the_name_the_vod_sync_writes_is_visible_to_jellyfin(self):
        from services import sync
        name_for = getattr(sync, "vod_folder_name", None) or sync.make_folder_name
        self.assertFalse(name_for("...And Justice for All", "1979").startswith("."))


class TestUnhideExisting(unittest.TestCase):
    def setUp(self):
        from services import sync
        self.sync = sync
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        engine = create_engine(f"sqlite:///{root}/t.db")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

        self.mdir = root / "movies" / "...And Justice for All (1979)"
        self.mdir.mkdir(parents=True)
        (self.mdir / "...And Justice for All (1979).strm").write_text("http://x")
        (self.mdir / "...And Justice for All (1979).nfo").write_text("<movie/>")
        self.db.add(Movie(tmdb_id=1, title="...And Justice for All", source="provider_1",
                          strm_path=str(self.mdir / "...And Justice for All (1979).strm"),
                          nfo_path=str(self.mdir / "...And Justice for All (1979).nfo"),
                          jellyfin_item_id="stale"))
        self.sdir = root / "shows" / ".hackSign (2002)"
        (self.sdir / "Season 01").mkdir(parents=True)
        (self.sdir / "Season 01" / ".hackSign (2002) S01E01.strm").write_text("http://x")
        (self.sdir / "tvshow.nfo").write_text("<tvshow/>")
        self.db.add(Series(tmdb_id=2, title=".hack//Sign", source="provider_1",
                           strm_path=str(self.sdir), nfo_path=str(self.sdir / "tvshow.nfo")))
        self.db.add(Movie(tmdb_id=3, title="Normal", source="provider_1",
                          strm_path=str(root / "movies" / "Normal (2000)" / "Normal (2000).strm")))
        self.db.commit()

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def test_moves_movie_and_series_and_updates_paths(self):
        self.assertEqual(self.sync.unhide_vod_paths(self.db), 2)
        m = self.db.query(Movie).filter_by(tmdb_id=1).one()
        self.assertTrue(Path(m.strm_path).exists())
        self.assertEqual(Path(m.strm_path).name, "And Justice for All (1979).strm")
        self.assertEqual(Path(m.nfo_path).name, "And Justice for All (1979).nfo")
        self.assertIsNone(m.jellyfin_item_id)
        self.assertFalse(self.mdir.exists())
        s = self.db.query(Series).filter_by(tmdb_id=2).one()
        self.assertEqual(Path(s.strm_path).name, "hackSign (2002)")
        self.assertTrue((Path(s.strm_path) / "Season 01" / "hackSign (2002) S01E01.strm").exists())
        self.assertTrue(Path(s.nfo_path).exists())

    def test_does_not_overwrite_an_existing_visible_folder(self):
        (self.mdir.parent / "And Justice for All (1979)").mkdir()
        self.sync.unhide_vod_paths(self.db)
        self.assertTrue(self.mdir.exists())

    def test_is_idempotent(self):
        self.sync.unhide_vod_paths(self.db)
        self.assertEqual(self.sync.unhide_vod_paths(self.db), 0)


if __name__ == "__main__":
    unittest.main()

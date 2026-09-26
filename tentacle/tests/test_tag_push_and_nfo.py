"""The Jellyfin tag push and Refresh Tags keep Jellyfin and the NFOs in step (#180, #165).

Run from the tentacle/ directory:  python -m unittest discover -s tests

- #180: push_tags_to_jellyfin only pushed when a tag was MISSING and then sent
  existing | desired, so a tag taken off a row never left Jellyfin: 5,860
  titles on a live install were still "Recently Added" after the window.
- #165: Refresh Tags rebuilt every non-Radarr NFO from the DB row, which threw
  away <imdbid>, cast and <tvdbid>, and reset <dateadded> to now.
"""
import logging
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Series, Setting


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeJellyfin:
    items = {}
    written = {}

    def __init__(self, *a, **k):
        pass

    def get_tmdb_lookup_with_fallback(self, media_type="Movie"):
        return {tid: it for tid, it in self.items.items() if it["type"] == media_type}, {}

    def set_item_tags(self, item_id, tags):
        FakeJellyfin.written[item_id] = list(tags)
        return True

    _normalize_title = staticmethod(lambda t: t.lower())


class _Db(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("jellyfin_url", "http://jf"), ("jellyfin_api_key", "k"), ("data_dir", self.tmp)):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()
        FakeJellyfin.written = {}


class PushReplacesTentaclesTags(_Db):
    def test_an_expired_recency_tag_comes_off_and_foreign_tags_stay(self):
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="provider_1", source_tag="Netflix",
                          tags=["Netflix Movies"]))
        self.db.add(Movie(tmdb_id=604, title="Right", source="provider_1", source_tag="Netflix",
                          tags=["Netflix Movies"]))
        self.db.commit()
        FakeJellyfin.items = {
            603: {"Id": "m1", "type": "Movie", "Tags": ["Netflix Movies", "Recently Added Movies",
                                                         "Netflix Recently Added Movies", "youtube"]},
            604: {"Id": "m2", "type": "Movie", "Tags": ["Netflix Movies"]},
        }
        import services.jellyfin as jellyfin
        with mock.patch.object(jellyfin, "JellyfinService", FakeJellyfin):
            pushed = jellyfin.push_tags_to_jellyfin(self.db)
        self.assertEqual(["youtube", "Netflix Movies"], FakeJellyfin.written["m1"])
        self.assertNotIn("m2", FakeJellyfin.written, "an item that needed nothing was written")
        self.assertEqual(1, pushed)


class RefreshTagsKeepsNfoMetadata(_Db):
    def test_only_the_tag_lines_of_an_existing_nfo_change(self):
        from services.nfo import write_movie_nfo, write_series_nfo
        mpath = Path(self.tmp) / "movie.nfo"
        spath = Path(self.tmp) / "tvshow.nfo"
        write_movie_nfo(mpath, {"tmdb_id": 603, "title": "The Matrix", "year": 1999, "imdb_id": "tt0133093"},
                        ["Old Tag"])
        write_series_nfo(spath, {"tmdb_id": 1399, "title": "Friends", "year": 1994, "tvdb_id": 79168},
                         ["Old Tag"])
        before_m, before_s = mpath.read_text(), spath.read_text()
        self.db.add(Movie(tmdb_id=603, title="The Matrix", year=1999, source="provider_1", source_tag="Netflix",
                          tags=["Netflix Movies"], nfo_path=str(mpath)))
        self.db.add(Series(tmdb_id=1399, title="Friends", year=1994, source="sonarr", tags=["Downloaded TV"],
                           nfo_path=str(spath)))
        self.db.commit()
        import routers.sync as sync_router
        with mock.patch.object(sync_router, "refresh_recently_added_tags", lambda db: (0, 0)), \
                mock.patch("services.smartlists.refresh_smartlist_playlists", lambda db: None):
            self.db.query(Setting).filter_by(key="jellyfin_url").delete()
            self.db.commit()
            sync_router.refresh_tags(db=self.db)
        movie, show = mpath.read_text(), spath.read_text()
        self.assertIn("<tag>Netflix Movies</tag>", movie)
        self.assertNotIn("Old Tag", movie)
        self.assertIn("tt0133093", movie)
        self.assertIn("<tag>Downloaded TV</tag>", show)
        self.assertIn("79168", show)
        strip = lambda t: "\n".join(l for l in t.splitlines() if "<tag>" not in l)
        self.assertEqual(strip(before_m), strip(movie), "anything but the tags changed (dateadded?)")
        self.assertEqual(strip(before_s), strip(show))


class UpdateNfoTags(unittest.TestCase):
    def test_rewriting_tags_leaves_every_other_line_alone_and_is_idempotent(self):
        from services.nfo import update_nfo_tags
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        nfo = tmp / "movie.nfo"
        nfo.write_text("<?xml version=\"1.0\"?>\n<movie>\n  <title>X</title>\n  <tag>A</tag>\n"
                       "  <tag>B</tag>\n  <dateadded>2020-01-01</dateadded>\n</movie>\n", encoding="utf-8")
        update_nfo_tags(nfo, ["C"])
        once = nfo.read_text()
        update_nfo_tags(nfo, ["C"])
        self.assertEqual(once, nfo.read_text())
        self.assertIn("  <title>X</title>\n  <dateadded>2020-01-01</dateadded>\n", once)
        self.assertIn("  <tag>C</tag>\n</movie>", once)
        update_nfo_tags(nfo, [])
        update_nfo_tags(nfo, [])
        self.assertIn("</dateadded>\n</movie>", nfo.read_text(), "an empty tag list adds blank lines")


if __name__ == "__main__":
    unittest.main()

"""A list keeps a film and a show that have the same TMDB number (#365).

TMDB numbers films and shows separately: movie/1396 and tv/1396 are two
titles. store_list_items de-duplicated on tmdb_id alone and list_items was
UNIQUE(list_id, tmdb_id), so a mixed list kept only the first of the two; the
other was never counted, offered to Radarr/Sonarr or shown. The key is now
(list_id, tmdb_id, type), and a database made by an older version is rebuilt
to it at startup, every row kept.

Run from tentacle/:  python -m unittest discover -s tests -p test_list_items_film_and_show.py
"""
import logging
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import ListItem, ListSubscription, Movie, Series, TentacleUser
from tmp_dirs import temp_dir


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


FILM = {"tmdb_id": 1396, "title": "A Film", "media_type": "movie"}
SHOW = {"tmdb_id": 1396, "title": "A Show", "media_type": "series"}


class _Db(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = TentacleUser(jellyfin_user_id="a" * 32, display_name="A", is_admin=True)
        self.db.add(self.user)
        self.db.commit()
        self.lst = ListSubscription(user_id=self.user.id, name="Mixed", type="trakt",
                                    url="https://example.invalid/l", tag="Mixed", active=True)
        self.db.add(self.lst)
        self.db.commit()

    def stored(self):
        return sorted((r.tmdb_id, r.media_type, r.title) for r in
                      self.db.query(ListItem).filter(ListItem.list_id == self.lst.id))


class TestStore(_Db):
    def test_a_film_and_a_show_with_the_same_tmdb_number_are_both_kept(self):
        from routers.lists import store_list_items
        stats = store_list_items(self.lst, [FILM, SHOW], self.db)
        self.db.commit()
        self.assertEqual([(1396, "movie", "A Film"), (1396, "series", "A Show")], self.stored())
        self.assertEqual((2, 2, 0), (stats["stored"], stats["new"], stats["skipped_duplicate"]))

    def test_the_same_title_twice_is_still_stored_once(self):
        from routers.lists import store_list_items
        stats = store_list_items(self.lst, [FILM, dict(FILM, imdb_id="tt2"), {"tmdb_id": 1396, "title": "x"}],
                                 self.db)
        self.assertEqual(1, stats["stored"])     # no type = a film, as before
        self.assertEqual(2, stats["skipped_duplicate"])

    def test_new_and_removed_count_titles(self):
        from routers.lists import store_list_items
        store_list_items(self.lst, [FILM], self.db)
        self.db.commit()
        stats = store_list_items(self.lst, [SHOW], self.db)
        self.assertEqual((1, 1), (stats["new"], stats["removed"]))
        # an old row without a type is the film
        self.db.query(ListItem).delete()
        self.db.add(ListItem(list_id=self.lst.id, tmdb_id=1396, media_type=None, title="A Film"))
        self.db.commit()
        stats = store_list_items(self.lst, [FILM], self.db)
        self.assertEqual((0, 0), (stats["new"], stats["removed"]))

    def test_a_partial_fetch_keeps_the_stored_show(self):
        """The movies-only fallback re-reads the film; the stored show stays."""
        from routers.lists import ListFetch, keep_unread_items, store_list_items
        store_list_items(self.lst, [FILM, SHOW], self.db)
        self.db.commit()
        partial = ListFetch([FILM], source="servarr", complete=True, missing_types={"series"})
        store_list_items(self.lst, keep_unread_items(self.lst, partial, self.db), self.db)
        self.db.commit()
        self.assertEqual([(1396, "movie", "A Film"), (1396, "series", "A Show")], self.stored())


class TestReaders(_Db):
    def setUp(self):
        super().setUp()
        self.db.add_all([ListItem(list_id=self.lst.id, tmdb_id=1396, media_type="movie", title="A Film",
                                  poster_path="/f.jpg"),
                         ListItem(list_id=self.lst.id, tmdb_id=1396, media_type="series", title="A Show",
                                  poster_path="/s.jpg")])
        self.db.commit()

    def test_list_page_shows_each_in_its_own_type(self):
        from routers.library import _get_list_items
        self.db.add(Movie(tmdb_id=1396, title="A Film", source="provider_1"))
        self.db.commit()
        out = _get_list_items(self.lst.id, None, None, None, 100, 0, self.db)
        got = sorted((i["media_type"], i["title"], i["in_library"]) for i in out["items"])
        self.assertEqual([("movie", "A Film", True), ("series", "A Show", False)], got)

    def test_discover_list_row_offers_both(self):
        import routers.discover as discover
        with mock.patch.object(discover, "_get_jellyfin_tmdb_items", return_value=set()):
            got = discover._get_missing_from_lists(self.db, {"movie": set(), "series": set()}, "all",
                                                   user=self.user, shuffle=False)
        self.assertEqual({("movie", 1396), ("series", 1396)}, {(i["media_type"], i["tmdb_id"]) for i in got})


# The list_items table as releases up to 1.9.0 create it.
OLD_DDL = """CREATE TABLE list_items (
	id INTEGER NOT NULL,
	list_id INTEGER NOT NULL,
	tmdb_id INTEGER,
	imdb_id VARCHAR,
	media_type VARCHAR,
	title VARCHAR,
	year VARCHAR,
	poster_path VARCHAR,
	added_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_list_item UNIQUE (list_id, tmdb_id),
	FOREIGN KEY(list_id) REFERENCES list_subscriptions (id)
)"""
OLD_INDEXES = ["CREATE INDEX ix_list_items_list_id ON list_items (list_id)",
               "CREATE INDEX ix_list_items_tmdb_id ON list_items (tmdb_id)",
               "CREATE INDEX ix_list_items_imdb_id ON list_items (imdb_id)"]


class TestUpgrade(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = sqlite3.connect(str(Path(self.tmp) / "old.db"))
        self.addCleanup(self.conn.close)
        self.conn.execute(OLD_DDL)
        for sql in OLD_INDEXES:
            self.conn.execute(sql)
        self.conn.executemany(
            "INSERT INTO list_items (id, list_id, tmdb_id, imdb_id, media_type, title, year, poster_path, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(1, 1, 603, "tt0133093", "movie", "The Matrix", "1999", "/m.jpg", "2026-09-01 10:00:00.000000"),
             (2, 1, 1396, None, "series", "A Show", None, None, None),
             (5, 2, 1396, "tt9", None, "Old row", "2001", None, "2026-01-01 00:00:00"),
             (9, 2, None, "tt7", "movie", "IMDb only", None, None, None)])
        self.conn.commit()

    def rows(self):
        return self.conn.execute("SELECT * FROM list_items ORDER BY id").fetchall()

    def test_upgrade_keeps_every_row_and_takes_the_new_key(self):
        before = self.rows()
        mdb._migrate_list_items_key(self.conn)
        self.assertEqual(before, self.rows())
        keys = mdb._unique_keys(self.conn.cursor(), "list_items")
        self.assertNotIn(["list_id", "tmdb_id"], keys)
        self.assertIn(["list_id", "tmdb_id", None], keys)
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='list_items'")}
        self.assertTrue({"uq_list_item", "ix_list_items_list_id", "ix_list_items_tmdb_id",
                         "ix_list_items_imdb_id"} <= names)
        # Now a film can join its show namesake, and a NULL-typed film still can't be doubled
        self.conn.execute("INSERT INTO list_items (list_id, tmdb_id, media_type) VALUES (1, 1396, 'movie')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO list_items (list_id, tmdb_id, media_type) VALUES (2, 1396, 'movie')")

    def test_a_second_start_changes_nothing(self):
        mdb._migrate_list_items_key(self.conn)
        schema = self.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        rows = self.rows()
        mdb._migrate_list_items_key(self.conn)
        self.assertEqual(schema, self.conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall())
        self.assertEqual(rows, self.rows())

    def test_a_fresh_install_needs_nothing(self):
        fresh = sqlite3.connect(str(Path(self.tmp) / "fresh.db"))
        self.addCleanup(fresh.close)
        engine = create_engine(f"sqlite:///{self.tmp}/fresh.db")
        mdb.Base.metadata.create_all(engine)
        engine.dispose()
        schema = fresh.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        mdb._migrate_list_items_key(fresh)
        self.assertEqual(schema, fresh.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall())
        self.assertIn(["list_id", "tmdb_id", None], mdb._unique_keys(fresh.cursor(), "list_items"))


if __name__ == "__main__":
    unittest.main()

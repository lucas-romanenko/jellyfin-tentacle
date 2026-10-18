"""A list keeps a film and a show that have the same TMDB number (#365).

TMDB numbers films and shows separately: movie/1396 and tv/1396 are two
titles. A list was stored de-duplicated on the number alone (and the table's
unique key was (list_id, tmdb_id)), so a mixed list kept only the first of
the two: the other was never counted, sent to Radarr/Sonarr or shown in
Discover. The upgrade rebuilds list_items with the key
(list_id, tmdb_id, media_type) and types the legacy NULL rows as films.
"""
import ast
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routers.discover as discover
import routers.lists as lists
from models.database import Base, ListItem, ListSubscription
from routers.lists import ListFetch
from services.tagger import get_list_tags_for_tmdb_id
from tmp_dirs import temp_dir

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)

FILM = {"tmdb_id": 1396, "title": "A Film", "media_type": "movie", "poster_path": "/f.jpg"}
SHOW = {"tmdb_id": 1396, "title": "A Show", "media_type": "series", "poster_path": "/s.jpg"}


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class SameNumberInOneList(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.lst = ListSubscription(name="Mixed", type="trakt", url="u", tag="Mixed", active=True)
        self.db.add(self.lst)
        self.db.commit()

    def stored(self):
        return sorted((r.media_type, r.title) for r in
                      self.db.query(ListItem).filter(ListItem.list_id == self.lst.id))

    def store(self, items):
        stats = lists.store_list_items(self.lst, items, self.db)
        self.db.commit()
        return stats

    def test_a_film_and_a_show_with_the_same_tmdb_number_are_both_kept(self):
        stats = self.store([dict(FILM), dict(SHOW)])
        self.assertEqual([("movie", "A Film"), ("series", "A Show")], self.stored())
        self.assertEqual((2, 2, 0), (stats["stored"], stats["new"], stats["skipped_duplicate"]))

    def test_the_same_title_twice_is_still_one_item(self):
        stats = self.store([dict(FILM), dict(FILM, imdb_id="tt2"), dict(SHOW)])
        self.assertEqual([("movie", "A Film"), ("series", "A Show")], self.stored())
        self.assertEqual(1, stats["skipped_duplicate"])

    def test_an_item_without_a_type_is_a_film(self):
        self.store([{"tmdb_id": 7, "title": "Untyped", "media_type": None}, {"tmdb_id": 7, "title": "Film"}])
        self.assertEqual([("movie", "Untyped")], self.stored())

    def test_new_and_removed_count_films_and_shows_apart(self):
        self.store([dict(FILM)])
        stats = self.store([dict(SHOW)])
        self.assertEqual((1, 1), (stats["new"], stats["removed"]))
        self.assertEqual([("series", "A Show")], self.stored())

    def test_a_partial_fetch_keeps_the_stored_show_with_the_films_number(self):
        self.store([dict(FILM), dict(SHOW)])
        partial = ListFetch([dict(FILM)], source="imdb", missing_types={"series"})
        merged = lists.keep_unread_items(self.lst, partial, self.db)
        self.store(merged)
        self.assertEqual([("movie", "A Film"), ("series", "A Show")], self.stored())

    def test_discover_shows_both_in_the_mixed_row(self):
        self.store([dict(FILM), dict(SHOW)])
        with mock.patch.object(discover, "_get_jellyfin_tmdb_items", lambda media_type: {}):
            rows = discover._get_missing_from_lists(self.db, {"movie": set(), "series": set()}, "all",
                                                    shuffle=False)
        self.assertEqual([("movie", 1396), ("series", 1396)],
                         sorted((r["media_type"], r["tmdb_id"]) for r in rows))

    def test_a_list_tag_goes_only_to_the_type_the_list_has(self):
        self.store([dict(SHOW)])
        self.assertEqual(["Mixed"], get_list_tags_for_tmdb_id(1396, "series", self.db))
        self.assertEqual([], get_list_tags_for_tmdb_id(1396, "movie", self.db))


OLD_LIST_ITEMS = (
    "CREATE TABLE list_items (id INTEGER NOT NULL, list_id INTEGER NOT NULL, tmdb_id INTEGER, "
    "imdb_id VARCHAR, media_type VARCHAR, title VARCHAR, year VARCHAR, poster_path VARCHAR, "
    "added_at DATETIME, PRIMARY KEY (id), CONSTRAINT uq_list_item UNIQUE (list_id, tmdb_id), "
    "FOREIGN KEY(list_id) REFERENCES list_subscriptions (id))")

PROBE = """
    import models.database as mdb
    mdb.create_tables()
    db = mdb.SessionLocal()
    out = {"rows": sorted((r.id, r.list_id, r.tmdb_id, r.media_type, r.title)
                          for r in db.query(mdb.ListItem).all())}
    try:
        db.add(mdb.ListItem(list_id=1, tmdb_id=1396, media_type="series", title="A Show"))
        db.commit()
        out["show_added"] = "ok"
    except Exception as e:
        db.rollback()
        out["show_added"] = "ERROR " + str(e).splitlines()[0]
    try:
        db.add(mdb.ListItem(list_id=1, tmdb_id=1396, media_type="movie", title="Again"))
        db.commit()
        out["same_film_again"] = "ok"
    except Exception:
        db.rollback()
        out["same_film_again"] = "refused"
    print(repr(out))
"""


class UpgradeRebuildsTheKey(unittest.TestCase):
    """Runs the real startup (create_tables) in a child process: models.database
    binds its engine to DATA_DIR at import."""

    def setUp(self):
        self.dir = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db_path = os.path.join(self.dir, "tentacle.db")

    def _start(self):
        env = dict(os.environ, DATA_DIR=self.dir, PYTHONPATH=APP)
        r = subprocess.run([sys.executable, "-c", textwrap.dedent(PROBE)], env=env, cwd=APP,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr[-3000:])
        return ast.literal_eval(r.stdout.strip().splitlines()[-1])

    def test_an_old_database_gets_the_typed_key_and_keeps_its_rows(self):
        c = sqlite3.connect(self.db_path)
        c.execute("CREATE TABLE list_subscriptions (id INTEGER NOT NULL, name VARCHAR, PRIMARY KEY (id))")
        c.execute("INSERT INTO list_subscriptions (id, name) VALUES (1, 'Mixed')")
        c.execute(OLD_LIST_ITEMS)
        c.execute("CREATE INDEX ix_list_items_list_id ON list_items (list_id)")
        c.execute("INSERT INTO list_items (id, list_id, tmdb_id, media_type, title) "
                  "VALUES (5, 1, 1396, NULL, 'A Film'), (6, 1, 20, 'series', 'Other Show')")
        c.commit()
        c.close()
        out = self._start()
        self.assertEqual([(5, 1, 1396, "movie", "A Film"), (6, 1, 20, "series", "Other Show")], out["rows"])
        self.assertEqual("ok", out["show_added"])
        self.assertEqual("refused", out["same_film_again"])
        c = sqlite3.connect(self.db_path)
        left = c.execute("SELECT name FROM sqlite_master WHERE name LIKE '%list_items_old%'").fetchall()
        indexes = {r[1] for r in c.execute("PRAGMA index_list(list_items)")}
        c.close()
        self.assertEqual([], left)
        self.assertIn("ix_list_items_list_id", indexes)

    def test_fresh_and_current_databases_are_left_alone(self):
        out = self._start()
        self.assertEqual(([], "ok", "ok"), (out["rows"], out["show_added"], out["same_film_again"]))
        out = self._start()  # a second start is a no-op: both rows stay, the key holds
        self.assertEqual([("movie", "Again"), ("series", "A Show")], sorted(r[3:] for r in out["rows"]))
        self.assertTrue(out["show_added"].startswith("ERROR"), out["show_added"])
        self.assertEqual("refused", out["same_film_again"])


if __name__ == "__main__":
    unittest.main()

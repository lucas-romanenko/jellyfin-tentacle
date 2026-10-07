"""The Library page's list view looks each list item up in its own type (#510).

TMDB numbers films and shows separately. Since #365 a mixed list stores both
film 1396 and show 1396, but _get_list_items looked every item up by number in
the films first: the show was shown as the library's film (title, poster, "in
library"), so the film appeared twice and the show not at all. The same
happened before #365 to a list show whose number a library film had.

Test by therobmilne (#510, #511).

Run from tentacle/:  python tests/hermetic.py discover -s tests -p test_list_page_film_and_show.py
"""
import shutil
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import ListItem, ListSubscription, Movie, Series
from tmp_dirs import temp_dir


class ListPageByType(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(engine.dispose)
        self.addCleanup(self.db.close)
        self.lst = ListSubscription(name="Mixed", type="trakt", url="https://example.invalid/list", tag="Mixed")
        self.db.add(self.lst)
        self.db.commit()

    def items(self):
        from routers.library import _get_list_items
        out = _get_list_items(self.lst.id, None, None, None, 100, 0, self.db)
        return sorted((i["media_type"], i["title"], i["in_library"]) for i in out["items"])

    def add(self, tmdb_id, media_type, title):
        self.db.add(ListItem(list_id=self.lst.id, tmdb_id=tmdb_id, media_type=media_type, title=title))
        self.db.commit()

    def test_a_film_and_a_show_with_one_number(self):
        self.add(1396, "movie", "A Film")
        self.add(1396, "series", "A Show")
        self.db.add(Movie(tmdb_id=1396, title="A Film", source="provider_1"))
        self.db.commit()
        self.assertEqual([("movie", "A Film", True), ("series", "A Show", False)], self.items())

    def test_a_list_show_is_not_the_library_film_of_its_number(self):
        self.add(1396, "series", "A Show")
        self.db.add(Movie(tmdb_id=1396, title="A Film", source="provider_1"))
        self.db.commit()
        self.assertEqual([("series", "A Show", False)], self.items())

    def test_a_list_film_is_not_the_library_show_of_its_number(self):
        self.add(1396, "movie", "A Film")
        self.db.add(Series(tmdb_id=1396, title="A Show", source="provider_1"))
        self.db.commit()
        self.assertEqual([("movie", "A Film", False)], self.items())

    def test_both_in_the_library(self):
        self.add(1396, "movie", "A Film")
        self.add(1396, "series", "A Show")
        self.db.add(Movie(tmdb_id=1396, title="A Film", source="provider_1"))
        self.db.add(Series(tmdb_id=1396, title="A Show", source="provider_1"))
        self.db.commit()
        self.assertEqual([("movie", "A Film", True), ("series", "A Show", True)], self.items())

    def test_an_untyped_row_is_a_film(self):
        li = ListItem(list_id=self.lst.id, tmdb_id=1396, title="A Film")
        self.db.add(li)
        self.db.flush()
        li.media_type = None
        self.db.add(Movie(tmdb_id=1396, title="A Film", source="provider_1"))
        self.db.add(Series(tmdb_id=1396, title="A Show", source="provider_1"))
        self.db.commit()
        self.assertEqual([("movie", "A Film", True)], self.items())


if __name__ == "__main__":
    unittest.main()

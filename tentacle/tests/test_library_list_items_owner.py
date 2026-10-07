"""GET /api/library/items?list_id=N must only show the caller's own list.

Lists are per user and every /api/lists route checks ListSubscription.user_id,
but the library view of a list's contents did not: a signed-in user asking
for another user's list id got that list's items. Owner or admin now;
anyone else (or an unknown id) gets 404, as on /api/lists.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
import routers.library as library  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class TestListItemsOwner(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.admin = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.mom = mdb.TentacleUser(jellyfin_user_id="c" * 32, display_name="Owner", is_admin=False)
        self.kid = mdb.TentacleUser(jellyfin_user_id="b" * 32, display_name="Other", is_admin=False)
        self.db.add_all([self.admin, self.mom, self.kid])
        self.db.commit()
        lst = mdb.ListSubscription(user_id=self.mom.id, name="Mine", type="trakt",
                                   url="https://trakt.tv/users/x/lists/y", tag="mine")
        self.db.add(lst)
        self.db.commit()
        self.list_id = lst.id
        self.db.add(mdb.ListItem(list_id=lst.id, tmdb_id=42, title="A Pick", media_type="movie"))
        self.db.commit()

    def _items(self, user):
        return library.get_library_items(list_id=self.list_id, db=self.db, user=user, media_type=None,
                                         source=None, source_tag=None, search=None, sort=None,
                                         list_status=None, limit=48, offset=0)

    def test_other_user_gets_404(self):
        with self.assertRaises(HTTPException) as e:
            self._items(self.kid)
        self.assertEqual(404, e.exception.status_code)

    def test_owner_sees_it(self):
        self.assertEqual(1, self._items(self.mom)["total"])

    def test_admin_sees_it(self):
        self.assertEqual(1, self._items(self.admin)["total"])

    def test_unknown_list_is_404(self):
        self.list_id = 9999
        with self.assertRaises(HTTPException):
            self._items(self.mom)


if __name__ == "__main__":
    unittest.main()

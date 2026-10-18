"""A list's tag is library-wide; refreshing one list must not strip another's.

Lists are per user, but the tag a list applies lives on the shared library row
and NFO, and the dashboard suggests the list name as the tag. Two users who
each add a list called "Watchlist" therefore share one tag, and
apply_list_tags_to_library() removed the tag from every title not on the list
being refreshed: Rob's refresh stripped Mom's titles, Mom's stripped Rob's,
and whichever ran last won — while both users' "Watchlist" playlists (built
from the tag) showed whatever that was.

No network: lists are refreshed from stubbed Trakt answers.
Run from tentacle/:  python -m unittest discover -s tests -p "test_list_tag_shared.py"
"""
import unittest
from unittest import mock

from test_imdb_partial_list import _fresh_db, _Resp


def _trakt(*tmdb_ids):
    return [{"movie": {"title": f"M{i}", "ids": {"tmdb": i}}} for i in tmdb_ids]


class TestTwoUsersOneTag(unittest.TestCase):
    TAG = "Watchlist"

    def setUp(self):
        from models.database import ListSubscription, Movie, TentacleUser
        self.db = _fresh_db()
        self.rob = TentacleUser(jellyfin_user_id="u-rob", display_name="Rob")
        self.mom = TentacleUser(jellyfin_user_id="u-mom", display_name="Mom")
        self.db.add_all([self.rob, self.mom])
        self.db.commit()
        self.rob_list = ListSubscription(user_id=self.rob.id, name="Watchlist", type="trakt",
                                         tag=self.TAG, url="https://trakt.tv/users/rob/lists/w")
        self.mom_list = ListSubscription(user_id=self.mom.id, name="Watchlist", type="trakt",
                                         tag=self.TAG, url="https://trakt.tv/users/mom/lists/w")
        self.db.add_all([self.rob_list, self.mom_list])
        for tmdb_id in (1, 2, 3, 4):
            self.db.add(Movie(tmdb_id=tmdb_id, title=f"M{tmdb_id}", source="provider_1", tags=[]))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _refresh(self, lst, answer):
        from routers import lists
        with mock.patch.object(lists.requests, "get", return_value=_Resp(200, answer)), \
             mock.patch.object(lists, "get_setting", return_value="cid"), \
             mock.patch.object(lists, "_get_tmdb_service", return_value=None), \
             mock.patch("services.smartlists._notify_jellyfin_plugin"):
            user = self.rob if lst is self.rob_list else self.mom
            lists.fetch_list(lst.id, db=self.db, user=user)

    def _tagged(self):
        from models.database import Movie
        return sorted(m.tmdb_id for m in self.db.query(Movie) if self.TAG in (m.tags or []))

    def test_refreshing_one_users_list_keeps_the_other_users_titles(self):
        self._refresh(self.rob_list, _trakt(1, 2))
        self._refresh(self.mom_list, _trakt(3, 4))
        self.assertEqual(self._tagged(), [1, 2, 3, 4])
        self._refresh(self.rob_list, _trakt(1, 2))
        self.assertEqual(self._tagged(), [1, 2, 3, 4])

    def test_a_title_that_left_every_list_still_loses_the_tag(self):
        self._refresh(self.rob_list, _trakt(1, 2))
        self._refresh(self.mom_list, _trakt(3))
        self._refresh(self.rob_list, _trakt(1))
        self.assertEqual(self._tagged(), [1, 3])


class TestCreateRefusesATakenTag(unittest.TestCase):
    def setUp(self):
        from models.database import ListSubscription, Movie, TentacleUser
        self.db = _fresh_db()
        self.rob = TentacleUser(jellyfin_user_id="u-rob", display_name="Rob")
        self.mom = TentacleUser(jellyfin_user_id="u-mom", display_name="Mom")
        self.db.add_all([self.rob, self.mom])
        self.db.commit()
        self.db.add(ListSubscription(user_id=self.mom.id, name="Watchlist", type="trakt",
                                     tag="Watchlist", url="https://trakt.tv/users/mom/lists/w"))
        self.db.add(Movie(tmdb_id=1, title="M1", source="provider_1", source_tag="Netflix", tags=[]))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _create(self, tag, user=None):
        from routers import lists
        body = lists.ListCreate(name="n", type="trakt", url="https://trakt.tv/users/rob/lists/x", tag=tag)
        return lists.create_list(body, db=self.db, user=user or self.rob)

    def test_another_users_list_tag_is_refused(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            self._create("watchlist")
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_tentacle_tag_is_refused(self):
        from fastapi import HTTPException
        for tag in ("Netflix Movies", "Recently Added Movies", "Downloaded TV"):
            with self.subTest(tag=tag), self.assertRaises(HTTPException):
                self._create(tag)

    def test_a_users_own_tag_and_a_fresh_tag_are_accepted(self):
        self.assertTrue(self._create("Rob's picks")["success"])
        self.assertTrue(self._create("Watchlist", user=self.mom)["success"])


class TestADeletedListsTagCanBeUsedAgain(unittest.TestCase):
    """Deleting a list (or a rule, or renaming a rule's tag) retires its tag so
    Refresh Tags still takes it off titles. create_list() counted retired tags
    as taken, so once a list was gone nobody could add a list with its tag
    again, not even the same user re-adding the same list (#542)."""
    URL = "https://trakt.tv/users/rob/lists/watchlist"

    def setUp(self):
        from models.database import ListSubscription, TentacleUser
        self.db = _fresh_db()
        self.rob = TentacleUser(jellyfin_user_id="u-rob", display_name="Rob")
        self.mom = TentacleUser(jellyfin_user_id="u-mom", display_name="Mom")
        self.db.add_all([self.rob, self.mom])
        self.db.commit()
        self.db.add(ListSubscription(user_id=self.mom.id, name="Mom's", type="trakt",
                                     tag="Mom's picks", url="https://trakt.tv/users/mom/lists/p"))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _create(self, tag, user=None):
        from routers import lists
        body = lists.ListCreate(name=tag, type="trakt", url=self.URL, tag=tag)
        return lists.create_list(body, db=self.db, user=user or self.rob)

    def _delete(self, list_id, user=None):
        from routers import lists
        return lists.delete_list(list_id, db=self.db, user=user or self.rob)

    def test_the_same_user_can_add_a_deleted_list_again(self):
        self._delete(self._create("Watchlist")["id"])
        self.assertTrue(self._create("Watchlist")["success"])

    def test_another_user_can_use_a_deleted_lists_tag(self):
        self._delete(self._create("Watchlist")["id"])
        self.assertTrue(self._create("watchlist", user=self.mom)["success"])

    def test_a_deleted_or_renamed_rules_tag_can_become_a_list_tag(self):
        from services.tagger import retire_tag
        retire_tag(self.db, "Christmas")   # what delete_rule and a tag rename record
        self.db.commit()
        self.assertTrue(self._create("Christmas")["success"])

    def test_a_retired_tag_another_user_still_uses_stays_refused(self):
        from fastapi import HTTPException
        from services.tagger import retire_tag
        retire_tag(self.db, "Mom's picks")
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            self._create("Mom's picks")
        self.assertEqual(caught.exception.status_code, 400)

    def test_the_deleted_lists_tag_is_still_tentacles(self):
        from services.tagger import tentacle_owned_tags
        self._delete(self._create("Watchlist")["id"])
        self.assertIn("Watchlist", tentacle_owned_tags(self.db))


if __name__ == "__main__":
    unittest.main()

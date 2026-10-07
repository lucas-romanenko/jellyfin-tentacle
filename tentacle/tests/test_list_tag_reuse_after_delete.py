"""A deleted list's tag can be used again.

Deleting a list (or a tag rule, or renaming a rule's tag) records its tag as
"retired", so Refresh Tags still recognises it as Tentacle's and takes it off
the titles that kept it. create_list() refused every tag in
tentacle_owned_tags(), which includes the retired ones, so once a list was
deleted nobody could ever add a list with that tag again: the same user
re-adding the same IMDb list (the dashboard fills the tag in from the list
name) got "already used by Tentacle or by another user's list".

Run from tentacle/:  python -m unittest discover -s tests -p "test_list_tag_reuse_after_delete.py"
"""
import unittest
from unittest import mock  # noqa: F401  (unittest.mock below)

from test_imdb_partial_list import _fresh_db


class TestDeletedListTagCanBeReused(unittest.TestCase):
    URL = "https://trakt.tv/users/alice/lists/watchlist"

    def setUp(self):
        from models.database import ListSubscription, TentacleUser
        self.db = _fresh_db()
        self.alice = TentacleUser(jellyfin_user_id="u-alice", display_name="Alice")
        self.bob = TentacleUser(jellyfin_user_id="u-bob", display_name="Bob")
        self.db.add_all([self.alice, self.bob])
        self.db.commit()
        self.db.add(ListSubscription(user_id=self.bob.id, name="Bob's", type="trakt",
                                     tag="Bob's picks", url="https://trakt.tv/users/bob/lists/p"))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _create(self, tag, user=None, name="Watchlist"):
        from routers import lists
        body = lists.ListCreate(name=name, type="trakt", url=self.URL, tag=tag)
        return lists.create_list(body, db=self.db, user=user or self.alice)

    def _delete(self, list_id, user=None):
        from routers import lists
        return lists.delete_list(list_id, db=self.db, user=user or self.alice)

    def test_the_same_user_can_add_a_deleted_list_again(self):
        first = self._create("Watchlist")
        self._delete(first["id"])
        self.assertTrue(self._create("Watchlist")["success"])

    def test_another_user_can_use_a_deleted_lists_tag(self):
        first = self._create("Watchlist")
        self._delete(first["id"])
        self.assertTrue(self._create("Watchlist", user=self.bob)["success"])

    def test_a_deleted_rules_tag_can_become_a_list_tag(self):
        from models.database import TagRule
        from routers import tags
        rule = TagRule(user_id=self.alice.id, name="Xmas", output_tag="Christmas",
                       conditions=[], active=True)
        self.db.add(rule)
        self.db.commit()
        # The playlist clean-up after the delete is not under test (and must not
        # touch /data): it stops at its first step.
        with unittest.mock.patch("services.smartlists._user_smartlists_path",
                                 side_effect=ValueError("not under test")):
            tags.delete_rule(rule.id, db=self.db, user=self.alice)
        self.assertTrue(self._create("Christmas", name="Christmas")["success"])

    def test_a_retired_tag_another_user_still_uses_stays_refused(self):
        from fastapi import HTTPException
        from services.tagger import retire_tag
        # Retired once (a rule of Alice's that is gone), still Bob's list tag now.
        retire_tag(self.db, "Bob's picks")
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            self._create("Bob's picks")
        self.assertEqual(caught.exception.status_code, 400)

    def test_builtin_tags_stay_refused(self):
        from fastapi import HTTPException
        for tag in ("Recently Added Movies", "Downloaded TV"):
            with self.subTest(tag=tag), self.assertRaises(HTTPException):
                self._create(tag)

    def test_the_tag_is_still_recognised_as_tentacles_after_the_delete(self):
        from services.tagger import tentacle_owned_tags
        first = self._create("Watchlist")
        self._delete(first["id"])
        self.assertIn("Watchlist", tentacle_owned_tags(self.db))


if __name__ == "__main__":
    unittest.main()

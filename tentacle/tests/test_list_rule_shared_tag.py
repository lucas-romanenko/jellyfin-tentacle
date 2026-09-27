"""A list refresh must not strip titles a tag rule with the same tag gives (#162).

A user can point a list and a tag rule at one tag (one playlist fed by both;
a live install does). apply_list_tags_to_library() removed the tag from
every title not on the list — including every title the rule gives — and the
nightly rule pass added it back hours later, so each night those titles left
the tag, their NFO and the playlist. Also: create_list refused the user's own
rule tag, and a rule could take a tag another user's list or rule applies.

Run from tentacle/:  python -m unittest discover -s tests -p "test_list_rule_shared_tag.py"
"""
import unittest
from unittest import mock

from test_imdb_partial_list import _fresh_db, _Resp

TAG = "Rom-Com"
SHORT = [{"field": "runtime", "operator": "less_than", "value": "100"}]


def _trakt(*ids):
    return [{"movie": {"title": f"M{i}", "ids": {"tmdb": i}}} for i in ids]


class _Case(unittest.TestCase):
    def setUp(self):
        from models.database import ListSubscription, Movie, TagRule, TentacleUser
        self.db = _fresh_db()
        self.rob = TentacleUser(jellyfin_user_id="u-rob", display_name="Rob")
        self.mom = TentacleUser(jellyfin_user_id="u-mom", display_name="Mom")
        self.db.add_all([self.rob, self.mom])
        self.db.commit()
        self.lst = ListSubscription(user_id=self.rob.id, name="Rom-Com", type="trakt", tag=TAG,
                                    url="https://trakt.tv/users/rob/lists/rc")
        self.rule = TagRule(user_id=self.rob.id, name="Short", output_tag=TAG, conditions=SHORT, active=True)
        self.db.add_all([self.lst, self.rule])
        # 1: on the list; 2: short (the rule gives it); 3: long, on neither any more.
        for tmdb_id, runtime in ((1, 120), (2, 90), (3, 150)):
            self.db.add(Movie(tmdb_id=tmdb_id, title=f"M{tmdb_id}", source="provider_1",
                              runtime=runtime, tags=[TAG]))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _refresh(self, answer):
        from routers import lists
        with mock.patch.object(lists.requests, "get", return_value=_Resp(200, answer)), \
             mock.patch.object(lists, "get_setting", return_value="cid"), \
             mock.patch.object(lists, "_get_tmdb_service", return_value=None), \
             mock.patch("services.smartlists._notify_jellyfin_plugin"):
            lists.fetch_list(self.lst.id, db=self.db, user=self.rob)

    def _tagged(self):
        from models.database import Movie
        return sorted(m.tmdb_id for m in self.db.query(Movie) if TAG in (m.tags or []))


class TestListRefreshKeepsRuleTitles(_Case):
    def test_a_title_the_rule_gives_keeps_the_tag(self):
        self._refresh(_trakt(1))
        self.assertEqual(self._tagged(), [1, 2])

    def test_an_inactive_rule_gives_nothing(self):
        self.rule.active = False
        self.db.commit()
        self._refresh(_trakt(1))
        self.assertEqual(self._tagged(), [1])


class TestTagCollisions(_Case):
    def _rule_body(self, tag):
        from routers import tags
        return tags.TagRuleCreate(name="r", output_tag=tag,
                                  conditions=[tags.ConditionSchema(**SHORT[0])])

    def test_a_list_on_the_users_own_rule_tag_is_accepted(self):
        from models.database import TagRule
        from routers import lists
        self.db.add(TagRule(user_id=self.rob.id, name="Kids", output_tag="Kids Picks", conditions=SHORT))
        self.db.commit()
        body = lists.ListCreate(name="k", type="trakt", url="https://trakt.tv/users/rob/lists/k", tag="Kids Picks")
        self.assertTrue(lists.create_list(body, db=self.db, user=self.rob)["success"])

    def test_a_list_on_another_users_rule_tag_is_refused(self):
        from fastapi import HTTPException
        from routers import lists
        body = lists.ListCreate(name="k", type="trakt", url="https://trakt.tv/users/mom/lists/k", tag="rom-com")
        with self.assertRaises(HTTPException):
            lists.create_list(body, db=self.db, user=self.mom)

    def test_a_rule_on_another_users_tag_is_refused(self):
        from fastapi import HTTPException
        from routers import tags
        with self.assertRaises(HTTPException) as cm:
            tags.create_rule(self._rule_body("ROM-COM"), db=self.db, user=self.mom)
        self.assertEqual(cm.exception.status_code, 400)

    def test_a_rule_renamed_onto_another_users_tag_is_refused(self):
        from fastapi import HTTPException
        from routers import tags
        rid = tags.create_rule(self._rule_body("Mom's shorts"), db=self.db, user=self.mom)["id"]
        with self.assertRaises(HTTPException):
            tags.update_rule(rid, tags.TagRuleUpdate(output_tag=TAG), db=self.db, user=self.mom)

    def test_the_same_user_may_share_a_tag_between_a_list_and_rules(self):
        from routers import tags
        self.assertTrue(tags.create_rule(self._rule_body(TAG), db=self.db, user=self.rob)["success"])
        rid = self.rule.id
        self.assertTrue(tags.update_rule(rid, tags.TagRuleUpdate(name="Shorter"), db=self.db, user=self.rob)["success"])


if __name__ == "__main__":
    unittest.main()

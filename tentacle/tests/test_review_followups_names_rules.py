"""Review follow-ups for 3bf4ce4 and 7f27447.

- A YouTube source shares get_desired_smartlists()' one name space with list,
  rule, source, built-in and Downloads playlists; a later same-named entry is
  silently skipped. So names are unique across all of them, both ways, and
  compared NFC-normalised and case-folded. Two titles that differ only past
  the 120 characters safe_name() keeps must not share a folder.
- One malformed tag rule must not abort a list refresh; a rule conditioned on
  its own tag must not keep that tag for ever.
- update_rule compares the output tag the way the checks do.

Run from tentacle/:  python -m unittest discover -s tests -p "test_review_followups_names_rules.py"
"""
import unicodedata
import unittest
from unittest import mock

import test_youtube
from test_list_rule_shared_tag import _Case as _ListRuleCase, _trakt, TAG


class TestYouTubeNamesAreUniqueAcrossSmartLists(unittest.TestCase):
    setUp = test_youtube.TestAddingAChannel.setUp
    tearDown = test_youtube.TestAddingAChannel.tearDown
    _Req = test_youtube.TestAddingAChannel._Req
    _add = test_youtube.TestAddingAChannel._add

    def _pl(self, pid, title, owner="Owner"):
        self.info.update(kind="playlist", playlist_id=pid, title=title, owner=owner,
                         channel_id="UC" + "q" * 22,
                         canonical=f"https://www.youtube.com/playlist?list={pid}")

    def _titles(self):
        return sorted(r.title for r in self.db.query(self.YouTubeChannel))

    def test_a_tag_rules_name_is_taken(self):
        from models.database import TagRule
        self.db.add(TagRule(user_id=1, name="Xmas", output_tag="Christmas",
                            conditions=[{"field": "genre", "operator": "contains", "value": "Family"}]))
        self.db.commit()
        self._pl("PL1", "Christmas")
        self._add()
        self.assertEqual(self._titles(), ["Christmas (Owner)"])

    def test_list_builtin_and_downloads_names_are_taken(self):
        from models.database import ListSubscription
        self.db.add(ListSubscription(user_id=1, name="w", type="trakt", tag="Watchlist",
                                     url="https://trakt.tv/users/x/lists/w"))
        self.db.commit()
        for pid, t in (("PL1", "watchlist"), ("PL2", "Recently Added Movies"), ("PL3", "u's Downloads")):
            self._pl(pid, t, owner=t)
            self._add()
        self.assertEqual(self._titles(), ["Recently Added Movies (2)", "u's Downloads (2)", "watchlist (2)"])

    def test_nfc_and_nfd_are_the_same_name(self):
        name = "Šeimos vaizdo įrašai"
        self._pl("PL1", unicodedata.normalize("NFC", name), owner="Ann")
        self._add()
        self._pl("PL2", unicodedata.normalize("NFD", name), owner="Bob")
        self._add()
        self.assertEqual(len({unicodedata.normalize("NFC", t) for t in self._titles()}), 2)

    def test_long_titles_get_distinct_folders(self):
        from services.youtube import library
        base = "Ilgas grojaraštis " + "x" * 120
        self._pl("PL1", base, owner="Ann")
        self._add()
        self._pl("PL2", base, owner="Bob")
        self._add()
        folders = {library.safe_name(t) for t in self._titles()}
        self.assertEqual(len(folders), 2)

    def test_a_rule_or_list_named_like_a_youtube_playlist_is_refused(self):
        from fastapi import HTTPException
        from routers import lists, tags
        from models.database import TentacleUser
        self._pl("PL1", "Christmas")
        self._add()
        user = self.db.query(TentacleUser).first()
        body = tags.TagRuleCreate(name="r", output_tag="christmas", conditions=[
            tags.ConditionSchema(field="genre", operator="contains", value="Family")])
        with self.assertRaises(HTTPException):
            tags.create_rule(body, db=self.db, user=user)
        lbody = lists.ListCreate(name="c", type="trakt", url="https://trakt.tv/users/x/lists/c", tag="Christmas")
        with self.assertRaises(HTTPException):
            lists.create_list(lbody, db=self.db, user=user)


class TestRuleRobustness(_ListRuleCase):
    def test_a_malformed_rule_does_not_abort_the_list_refresh(self):
        from models.database import TagRule
        self.db.add(TagRule(user_id=self.rob.id, name="broken", output_tag=TAG, active=True,
                            conditions=[{"field": "genre", "operator": "contains", "value": None}]))
        self.db.commit()
        self._refresh(_trakt(1))              # raised AttributeError on 7f27447
        self.assertEqual(self._tagged(), [1, 2])

    def test_a_rule_on_its_own_tag_does_not_keep_it(self):
        self.rule.conditions = [{"field": "list", "operator": "equals", "value": TAG}]
        self.db.commit()
        self._refresh(_trakt(1))
        self.assertEqual(self._tagged(), [1])

    def test_editing_a_rule_whose_stored_tag_differs_only_in_space_or_case(self):
        from models.database import ListSubscription, TagRule
        from routers import tags
        # Mom's rule predates the collision check and shares Rob's list tag.
        r = TagRule(user_id=self.mom.id, name="m", output_tag="Rom-Com ", conditions=[
            {"field": "genre", "operator": "contains", "value": "Comedy"}])
        self.db.add(r)
        self.db.commit()
        out = tags.update_rule(r.id, tags.TagRuleUpdate(name="m2", output_tag="rom-com"),
                               db=self.db, user=self.mom)
        self.assertTrue(out["success"])


if __name__ == "__main__":
    unittest.main()

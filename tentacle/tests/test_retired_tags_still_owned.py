"""A tag from a deleted list or a deleted/renamed rule is still Tentacle's to remove.

Run from the tentacle/ directory:  python -m unittest discover -s tests

tentacle_owned_tags() derives ownership from the ListSubscription and TagRule
rows that EXIST. Delete the list (or the rule, or rename the rule's tag) and
nothing produces that tag any more -- so it read as a user's own tag, and
"Refresh Tags" kept it on every item for ever: the case #107's own docstring
names ("a deleted list") was the one it could not handle.
"""
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import ListSubscription, TagRule, TentacleUser
from services.tagger import merge_owned_tags, tentacle_owned_tags


class RetiredTags(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.user = TentacleUser(jellyfin_user_id="u1", display_name="u", is_admin=True)
        self.db.add(self.user)
        self.db.commit()

    def test_a_deleted_lists_tag_is_still_owned_and_comes_off(self):
        from routers.lists import delete_list
        lst = ListSubscription(name="Old", type="trakt", url="http://trakt.tv/x", tag="Old List",
                               active=True, user_id=self.user.id)
        self.db.add(lst)
        self.db.commit()
        delete_list(lst.id, db=self.db, user=self.user)
        owned = tentacle_owned_tags(self.db)
        self.assertIn("Old List", owned, "the deleted list's tag now reads as somebody else's")
        self.assertEqual(["date-night"], merge_owned_tags(["Old List", "date-night"], [], owned))

    def test_a_deleted_rules_tag_is_still_owned(self):
        import routers.tags as tags
        rule = TagRule(name="r", output_tag="Comedy Picks", user_id=self.user.id, conditions=[])
        self.db.add(rule)
        self.db.commit()
        saved = tags.JellyfinService
        tags.JellyfinService = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no jellyfin in this test"))
        try:
            tags.delete_rule(rule.id, db=self.db, user=self.user)
        except Exception:
            pass
        finally:
            tags.JellyfinService = saved
        self.assertIsNone(self.db.query(TagRule).filter_by(id=rule.id).first())
        self.assertIn("Comedy Picks", tentacle_owned_tags(self.db))

    def test_a_renamed_rules_old_tag_is_still_owned(self):
        from routers.tags import TagRuleUpdate, update_rule
        rule = TagRule(name="r", output_tag="Comedy Picks", user_id=self.user.id, conditions=[])
        self.db.add(rule)
        self.db.commit()
        update_rule(rule.id, TagRuleUpdate(output_tag="Funny Picks"), db=self.db, user=self.user)
        owned = tentacle_owned_tags(self.db)
        self.assertIn("Comedy Picks", owned)
        self.assertIn("Funny Picks", owned)

    def test_a_users_own_tag_is_still_not_owned(self):
        self.assertNotIn("date-night", tentacle_owned_tags(self.db))


if __name__ == "__main__":
    unittest.main()

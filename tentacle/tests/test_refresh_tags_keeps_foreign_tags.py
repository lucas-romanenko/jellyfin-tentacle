"""Refresh Tags must replace only Tentacle's own tags on a Jellyfin item (#107).

It replaced the whole tag list with Tentacle's computed set, so a `youtube`
keyword from a TMDB import, or any tag a user added by hand in Jellyfin, was
wiped from every item Tentacle has tags for — while a stale Tentacle tag still
has to come off, which is why the endpoint replaces rather than merges.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import logging
import shutil
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Series, Setting, ListSubscription, TagRule, TentacleUser
from services.tagger import tentacle_owned_tags, merge_owned_tags


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeJellyfin:
    """Two items with tags that Tentacle did not write next to ones it did."""
    items = {}
    written = {}

    def __init__(self, *a, **k):
        pass

    @classmethod
    def reset(cls):
        cls.items = {
            603: {"Id": "m1", "Name": "The Matrix", "Tags": ["youtube", "Netflix Movies",
                                                              "Recently Added Movies", "Old List"]},
            1399: {"Id": "s1", "Name": "Game of Thrones", "Tags": ["hand-added", "Downloaded TV", "Netflix TV"]},
        }
        cls.written = {}

    def get_tmdb_lookup_with_fallback(self, media_type="Movie"):
        wanted = 603 if media_type == "Movie" else 1399
        return {wanted: self.items[wanted]}, {}

    def set_item_tags(self, item_id, tags):
        self.written[item_id] = list(tags)
        return True


class TestRefreshTagsKeepsForeignTags(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("jellyfin_url", "http://jf"), ("jellyfin_api_key", "k"), ("data_dir", tmp)):
            self.db.add(Setting(key=k, value=v))
        u = TentacleUser(jellyfin_user_id="u1", display_name="u", is_admin=True)
        self.db.add(u); self.db.commit()
        # "Old List" was a list subscription that is no longer active: its tag
        # is still Tentacle's to remove.
        self.db.add(ListSubscription(name="Old", type="trakt", url="http://trakt.tv/x",
                                     tag="Old List", active=False, user_id=u.id))
        self.db.add(TagRule(name="r", output_tag="Comedy Picks", user_id=u.id, conditions=[]))
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="radarr",
                          source_tag="Netflix", tags=["Netflix Movies", "Downloaded Movies"]))
        self.db.add(Series(tmdb_id=1399, title="Game of Thrones", source="sonarr",
                           source_tag="Netflix", tags=["Netflix TV"]))
        self.db.commit()
        FakeJellyfin.reset()

    def test_owned_set_covers_every_tag_tentacle_can_write(self):
        owned = tentacle_owned_tags(self.db)
        for t in ("Netflix Movies", "Netflix TV", "Netflix Recently Added Movies", "Recently Added TV",
                  "Downloaded Movies", "Old List", "Comedy Picks"):
            self.assertIn(t, owned)
        for t in ("youtube", "hand-added"):
            self.assertNotIn(t, owned)

    def test_merge_replaces_only_our_tags(self):
        owned = tentacle_owned_tags(self.db)
        merged = merge_owned_tags(["youtube", "Netflix Movies", "Recently Added Movies", "Old List"],
                                  ["Netflix Movies", "Downloaded Movies"], owned)
        self.assertEqual(merged, ["youtube", "Netflix Movies", "Downloaded Movies"])

    def test_refresh_tags_keeps_foreign_tags_and_drops_stale_own_tags(self):
        import routers.sync as sync_router
        with mock.patch("services.jellyfin.JellyfinService", FakeJellyfin), \
                mock.patch.object(sync_router, "refresh_recently_added_tags", lambda db: (0, 0)):
            sync_router.refresh_tags(db=self.db)
        self.assertEqual(FakeJellyfin.written["m1"], ["youtube", "Netflix Movies", "Downloaded Movies"])
        self.assertEqual(FakeJellyfin.written["s1"], ["hand-added", "Netflix TV"])

    def test_a_title_whose_tags_all_expired_loses_its_stale_tentacle_tags(self):
        """A row whose tag list is now EMPTY (its "Recently Added" window ran out
        and nothing else applies) was skipped outright, so the expired tag stayed
        on the Jellyfin item for ever. Empty means "no Tentacle tags", not "leave
        it alone" -- and a tag Tentacle does not own still stays."""
        import routers.sync as sync_router
        self.db.query(Movie).filter_by(tmdb_id=603).one().tags = []
        self.db.commit()
        with mock.patch("services.jellyfin.JellyfinService", FakeJellyfin), \
                mock.patch.object(sync_router, "refresh_recently_added_tags", lambda db: (0, 0)):
            sync_router.refresh_tags(db=self.db)
        self.assertEqual(FakeJellyfin.written["m1"], ["youtube"])

    def test_an_untagged_title_with_no_tentacle_tags_is_not_written(self):
        import routers.sync as sync_router
        self.db.query(Movie).filter_by(tmdb_id=603).one().tags = []
        self.db.commit()
        FakeJellyfin.items[603]["Tags"] = ["youtube"]
        with mock.patch("services.jellyfin.JellyfinService", FakeJellyfin), \
                mock.patch.object(sync_router, "refresh_recently_added_tags", lambda db: (0, 0)):
            sync_router.refresh_tags(db=self.db)
        self.assertNotIn("m1", FakeJellyfin.written, "a POST for an item that needed nothing")


if __name__ == "__main__":
    unittest.main()

"""refresh_recently_added_tags() deletes every tag that merely CONTAINS the
words "Recently Added" (services/tagger.py bad_tags), not just Tentacle's own
stale recency tags. A list subscription whose tag contains them (e.g. a Trakt
list "Recently Added on Netflix") or a custom playlist named that way loses its
tag from every title each nightly run, and nothing in the function puts a list
tag back — so that list playlist empties.

Run from tentacle/:  python -m unittest discover -s tests -p "test_recently_added_cleanup_scope.py"
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import tagger


class TestRecentlyAddedStripIsTooBroad(unittest.TestCase):
    def test_list_tag_containing_recently_added_survives(self):
        tmp = Path(tempfile.mkdtemp())
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        db.add(mdb.TentacleUser(id=1, jellyfin_user_id="jf-1", display_name="Rob"))
        db.add(mdb.ListSubscription(id=1, user_id=1, name="Recently Added on Netflix", type="trakt",
                                    url="https://trakt.tv/x", tag="Recently Added on Netflix",
                                    active=True, playlist_enabled=True))
        db.add(mdb.Movie(id=1, tmdb_id=100, title="Film", source="provider_1", source_tag="Netflix",
                         date_added=datetime.utcnow() - timedelta(days=2),
                         tags=["Netflix Movies", "Recently Added on Netflix"]))
        db.commit()
        tagger.refresh_recently_added_tags(db)
        tags = db.query(mdb.Movie).get(1).tags
        self.assertIn("Recently Added on Netflix", tags,
                      f"list tag stripped by the recency cleanup: {tags}")

    def test_a_stale_tentacle_recency_tag_is_still_removed(self):
        tmp = Path(tempfile.mkdtemp())
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        db.add(mdb.TagRule(name="Kids", output_tag="Kids Recently Added Picks", active=False, conditions=[]))
        db.add(mdb.Movie(id=1, tmdb_id=100, title="Film", source="provider_1", source_tag="Netflix",
                         date_added=datetime.utcnow() - timedelta(days=2),
                         tags=["Netflix Movies", "HBO Recently Added Movies", "Recently Added",
                               "Kids Recently Added Picks"]))
        db.commit()
        tagger.refresh_recently_added_tags(db)
        tags = db.query(mdb.Movie).get(1).tags
        self.assertNotIn("HBO Recently Added Movies", tags)
        self.assertNotIn("Recently Added", tags)
        self.assertIn("Recently Added Movies", tags)
        self.assertIn("Netflix Recently Added Movies", tags)
        self.assertIn("Kids Recently Added Picks", tags)


if __name__ == "__main__":
    unittest.main()

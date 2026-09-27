"""List and rule tags follow what currently holds each title (#153, #162, #168).

Run from the tentacle/ directory:  python -m unittest discover -s tests

- #153: rule tags were only ever added. After a rule was edited or deleted,
  titles that no longer matched kept the tag, and their playlist entry, for ever.
- #162: a list's refresh stripped its tag from every title not on THAT list,
  so two lists sharing a tag (two users' "Watchlist", or a list and a rule)
  took each other's titles away on alternate refreshes.
- #168: the nightly recency clean-up stripped any tag containing "Recently
  Added", so a list called "Recently Added on Netflix" was emptied every night.
- A deleted list's tag stayed on the rows, so Refresh Tags pushed it back.
"""
import logging
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import ListItem, ListSubscription, Movie, Series, TagRule, TentacleUser
from routers.lists import apply_list_tags_to_library
from services.nfo import write_movie_nfo
from services.tagger import refresh_recently_added_tags, retire_tag


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


OLD = datetime.utcnow() - timedelta(days=400)


class _Db(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.a = TentacleUser(jellyfin_user_id="a" * 32, display_name="A", is_admin=True)
        self.b = TentacleUser(jellyfin_user_id="b" * 32, display_name="B")
        self.db.add_all([self.a, self.b])
        self.db.commit()

    def movie(self, tmdb_id, tags=(), runtime=100, genres=("Drama",), nfo=False):
        m = Movie(tmdb_id=tmdb_id, title=f"M{tmdb_id}", year=2000, source="provider_1", source_tag="Netflix",
                  tags=list(tags), runtime=runtime, genres=list(genres), date_added=OLD)
        if nfo:
            path = Path(self.tmp) / f"m{tmdb_id}.nfo"
            write_movie_nfo(path, {"tmdb_id": tmdb_id, "title": m.title, "year": 2000,
                                   "imdb_id": "tt0133093"}, list(tags))
            m.nfo_path = str(path)
        self.db.add(m)
        self.db.commit()
        return m

    def tags(self, tmdb_id):
        self.db.expire_all()
        return self.db.query(Movie).filter_by(tmdb_id=tmdb_id).one().tags

    def lst(self, user, tag, tmdb_ids):
        l = ListSubscription(user_id=user.id, name=tag, type="trakt", url="https://trakt.tv/x", tag=tag, active=True)
        self.db.add(l)
        self.db.commit()
        for t in tmdb_ids:
            self.db.add(ListItem(list_id=l.id, tmdb_id=t, media_type="movie"))
        self.db.commit()
        return l


class RuleTags(_Db):
    def test_an_edited_rule_takes_its_tag_off_titles_it_no_longer_matches(self):
        rule = TagRule(user_id=self.a.id, name="Short", output_tag="Short Films", active=True,
                       conditions=[{"field": "runtime", "operator": "less_than", "value": "90"}])
        self.db.add(rule)
        self.movie(1, runtime=80)
        self.movie(2, runtime=60)
        refresh_recently_added_tags(self.db)
        self.assertIn("Short Films", self.tags(1))
        rule.conditions = [{"field": "runtime", "operator": "less_than", "value": "70"}]
        self.db.commit()
        refresh_recently_added_tags(self.db)
        self.assertNotIn("Short Films", self.tags(1))
        self.assertIn("Short Films", self.tags(2))

    def test_a_deleted_rule_or_list_tag_comes_off_the_rows(self):
        rule = TagRule(user_id=self.a.id, name="r", output_tag="Comedy Picks", active=True,
                       conditions=[{"field": "genre", "operator": "contains", "value": "Drama"}])
        self.db.add(rule)
        l = self.lst(self.a, "Old List", [1])
        self.movie(1, tags=["Netflix Movies"])
        refresh_recently_added_tags(self.db)
        self.assertTrue({"Comedy Picks", "Old List"} <= set(self.tags(1)))
        retire_tag(self.db, rule.output_tag)
        retire_tag(self.db, l.tag)
        self.db.delete(rule)
        self.db.delete(l)
        self.db.commit()
        refresh_recently_added_tags(self.db)
        self.assertEqual(["Netflix Movies"], self.tags(1))

    def test_foreign_and_builtin_tags_are_left_alone(self):
        self.movie(1, tags=["Netflix Movies", "date-night"])
        refresh_recently_added_tags(self.db)
        self.assertEqual(["Netflix Movies", "date-night"], self.tags(1))

    def test_a_rule_named_like_a_builtin_tag_never_strips_it(self):
        self.db.add(TagRule(user_id=self.a.id, name="bad", output_tag="Netflix Movies", active=True,
                            conditions=[{"field": "genre", "operator": "contains", "value": "Horror"}]))
        self.movie(1, tags=["Netflix Movies"])
        refresh_recently_added_tags(self.db)
        self.assertIn("Netflix Movies", self.tags(1))

    def test_the_nfo_follows_the_row(self):
        # The list holds another title: a list that has never stored one is
        # left alone (see paused_tags).
        self.lst(self.a, "Old List", [2])
        self.movie(1, tags=["Netflix Movies", "Old List"], nfo=True)
        refresh_recently_added_tags(self.db)
        nfo = Path(self.tmp, "m1.nfo").read_text(encoding="utf-8")
        self.assertNotIn("<tag>Old List</tag>", nfo)
        self.assertIn("<tag>Netflix Movies</tag>", nfo)
        self.assertIn("<imdbid>tt0133093</imdbid>", nfo, "only the tags are rewritten")


class SharedListTags(_Db):
    def test_two_users_lists_with_one_tag_keep_both_sets(self):
        for t in (1, 2, 3, 4):
            self.movie(t)
        la = self.lst(self.a, "Watchlist", [1, 2])
        lb = self.lst(self.b, "Watchlist", [3, 4])
        apply_list_tags_to_library([{"tmdb_id": 1, "media_type": "movie"}, {"tmdb_id": 2, "media_type": "movie"}],
                                   "Watchlist", self.db)
        apply_list_tags_to_library([{"tmdb_id": 3, "media_type": "movie"}, {"tmdb_id": 4, "media_type": "movie"}],
                                   "Watchlist", self.db)
        tagged = [t for t in (1, 2, 3, 4) if "Watchlist" in self.tags(t)]
        self.assertEqual([1, 2, 3, 4], tagged)
        refresh_recently_added_tags(self.db)
        self.assertEqual([1, 2, 3, 4], [t for t in (1, 2, 3, 4) if "Watchlist" in self.tags(t)])

    def test_a_list_refresh_keeps_the_tag_a_rule_gives(self):
        """Production: a list and a rule of one user share a tag; the 07:00 list
        refresh stripped the rule's 154 titles until the 11:10 tag refresh."""
        self.db.add(TagRule(user_id=self.a.id, name="r", output_tag="Rom-Com", active=True,
                            conditions=[{"field": "genre", "operator": "contains", "value": "Romance"}]))
        self.movie(1, tags=["Rom-Com"], genres=["Romance"])
        self.movie(2)
        self.lst(self.a, "Rom-Com", [2])
        apply_list_tags_to_library([{"tmdb_id": 2, "media_type": "movie"}], "Rom-Com", self.db)
        self.assertIn("Rom-Com", self.tags(1))
        self.assertIn("Rom-Com", self.tags(2))

    def test_a_title_that_left_every_holder_loses_the_tag(self):
        self.movie(1, tags=["Watchlist"])
        self.lst(self.a, "Watchlist", [])
        apply_list_tags_to_library([], "Watchlist", self.db)
        self.assertNotIn("Watchlist", self.tags(1))


class RecencyCleanUpScope(_Db):
    def test_a_list_tag_containing_recently_added_survives(self):
        self.lst(self.a, "Recently Added on Netflix", [1])
        self.movie(1, tags=["Netflix Movies", "Recently Added on Netflix"])
        refresh_recently_added_tags(self.db)
        self.assertIn("Recently Added on Netflix", self.tags(1))

    def test_stale_tentacle_recency_tags_still_go(self):
        self.movie(1, tags=["Netflix Movies", "Recently Added Movies", "Netflix Recently Added Movies",
                            "Amazon Recently Added Movies"])
        refresh_recently_added_tags(self.db)
        self.assertEqual(["Netflix Movies"], self.tags(1))

    def test_a_recent_title_gets_its_recency_tags(self):
        m = self.movie(1, tags=["Netflix Movies"])
        m.date_added = datetime.utcnow()
        self.db.commit()
        refresh_recently_added_tags(self.db)
        self.assertEqual(["Netflix Movies", "Recently Added Movies", "Netflix Recently Added Movies"],
                         self.tags(1))


class TagCollisionsAreRefused(_Db):
    """#153/#162: a second user's list or rule may not reuse a tag."""

    def _create_list(self, user, tag):
        from routers.lists import ListCreate, create_list
        return create_list(ListCreate(name=tag, type="trakt", url="https://trakt.tv/users/x/lists/y", tag=tag),
                           db=self.db, user=user)

    def _create_rule(self, user, tag):
        from routers.tags import TagRuleCreate, create_rule
        return create_rule(TagRuleCreate(name=tag, output_tag=tag, conditions=[
            {"field": "genre", "operator": "contains", "value": "Drama"}]), db=self.db, user=user)

    def test_another_users_list_tag_is_refused_for_a_list_and_a_rule(self):
        from fastapi import HTTPException
        self._create_list(self.a, "Watchlist")
        for create in (self._create_list, self._create_rule):
            with self.assertRaises(HTTPException) as cm:
                create(self.b, "watchlist")
            self.assertEqual(400, cm.exception.status_code)

    def test_the_same_user_may_share_a_name_between_a_list_and_a_rule(self):
        self._create_list(self.a, "Rom-Com")
        self.assertTrue(self._create_rule(self.a, "Rom-Com")["success"])

    def test_tentacles_own_tags_are_refused(self):
        from fastapi import HTTPException
        self.movie(1)  # gives the source tag "Netflix"
        for tag in ("Netflix Movies", "Recently Added TV", "downloaded movies"):
            with self.assertRaises(HTTPException):
                self._create_list(self.a, tag)

    def test_renaming_a_rule_onto_another_users_tag_is_refused(self):
        from fastapi import HTTPException
        from routers.tags import TagRuleUpdate, update_rule
        self._create_list(self.a, "Watchlist")
        rid = self._create_rule(self.b, "Mine")["id"]
        with self.assertRaises(HTTPException):
            update_rule(rid, TagRuleUpdate(output_tag="Watchlist"), db=self.db, user=self.b)


if __name__ == "__main__":
    unittest.main()

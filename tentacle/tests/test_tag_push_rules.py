"""The nightly tag push removes Tentacle's stale tags without harming anything else (#180).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Removing owned tags the row no longer has is right, but done naively it:
- stripped "Downloaded Movies" from a real download whose TMDB id is also a
  provider .strm (two Jellyfin items, one Tentacle row);
- removed tags from a title-fallback match, which may be another film;
- merged into the listing's tags, which can be stale, wiping a keyword added
  since, or into [] when the item's GET failed, wiping every keyword;
- wrote thousands of items in one burst.
Runs the real sync_owned_tags / JellyfinService.set_item_owned_tags; only the
HTTP layer is faked.
"""
import logging
import shutil
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import services.jellyfin as jellyfin
from models.database import Movie, Series, TentacleUser
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Post:
    status_code = 204
    text = ""


class FakeServer:
    """Jellyfin's items: `listed` is what the /Items listing says, `fresh` what
    a GET of the item says (they differ when the listing is stale)."""

    def __init__(self):
        self.listed = {"Movie": [], "Series": []}
        self.fresh = {}
        self.fail_get = set()
        self.posts = []

    def add(self, media_type, item_id, tmdb, name, year, tags, fresh_tags=None):
        item = {"Id": item_id, "Name": name, "ProductionYear": year, "Tags": list(tags),
                "ProviderIds": {"Tmdb": str(tmdb)} if tmdb else {}}
        self.listed[media_type].append(item)
        self.fresh[item_id] = dict(item, Tags=list(fresh_tags if fresh_tags is not None else tags))

    def service(self):
        jf = jellyfin.JellyfinService("http://jf", "k", "u1")
        jf._fetch_all_items = lambda media_type="Movie": [dict(i, Tags=list(i["Tags"])) for i in self.listed[media_type]]
        jf._fresh_tags_by_id = lambda media_type="Movie": {}
        server = self

        def get(path, params=None):
            item_id = path.rsplit("/", 1)[-1]
            if item_id in server.fail_get or item_id not in server.fresh:
                return None
            return dict(server.fresh[item_id])
        jf._get = get

        class Session:
            def post(self, url, json=None, timeout=None):
                server.posts.append((url.rsplit("/", 1)[-1], json))
                server.fresh[url.rsplit("/", 1)[-1]]["Tags"] = list(json["Tags"])
                for items in server.listed.values():
                    for it in items:
                        if it["Id"] == url.rsplit("/", 1)[-1]:
                            it["Tags"] = list(json["Tags"])
                return _Post()
        jf.session = Session()
        return jf

    def tags(self, item_id):
        return self.fresh[item_id]["Tags"]


class _Db(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.server = FakeServer()

    def push(self):
        return jellyfin.sync_owned_tags(self.db, self.server.service())


RECENT = "Recently Added Movies"


class RemovesOnlyWhatIsSafe(_Db):
    def test_an_expired_recency_tag_comes_off_and_a_keyword_stays(self):
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="provider_1", tags=["Netflix Movies"]))
        self.db.commit()
        self.server.add("Movie", "m1", 603, "The Matrix", 1999, ["Netflix Movies", RECENT, "cyberpunk"])
        self.assertEqual(1, self.push()["written"])
        self.assertEqual(["Netflix Movies", "cyberpunk"], sorted(self.server.tags("m1")))

    def test_a_row_with_no_tags_left_still_loses_them(self):
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="provider_1", tags=[]))
        self.db.commit()
        self.server.add("Movie", "m1", 603, "The Matrix", 1999, [RECENT, "cyberpunk"])
        self.push()
        self.assertEqual(["cyberpunk"], self.server.tags("m1"))

    def test_two_items_for_one_row_never_lose_a_tag(self):
        """A provider .strm next to the downloaded file: one row, two items."""
        self.db.add(Movie(tmdb_id=949, title="Heat", source="provider_1", tags=["Netflix Movies"]))
        self.db.commit()
        self.server.add("Movie", "strm", 949, "Heat", 1995, ["Netflix Movies"])
        self.server.add("Movie", "mkv", 949, "Heat", 1995, ["Downloaded Movies", RECENT])
        self.push()
        self.assertIn("Downloaded Movies", self.server.tags("mkv"))
        self.assertIn(RECENT, self.server.tags("mkv"))

    def test_a_title_match_is_only_added_to(self):
        self.db.add(Movie(tmdb_id=12345, title="Brothers", year="2009", source="provider_1",
                          tags=["Netflix Movies"]))
        self.db.commit()
        self.server.add("Movie", "b1", None, "Brothers", 2009, ["Downloaded Movies", "drama"])
        self.push()
        self.assertEqual(["Downloaded Movies", "Netflix Movies", "drama"], sorted(self.server.tags("b1")))

    def test_a_failed_get_writes_nothing(self):
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="provider_1", tags=[]))
        self.db.commit()
        self.server.add("Movie", "m1", 603, "The Matrix", 1999, [RECENT, "cyberpunk"])
        self.server.fail_get.add("m1")
        counts = self.push()
        self.assertEqual([], self.server.posts)
        self.assertEqual(1, counts["errors"])

    def test_a_stale_listing_does_not_wipe_a_keyword_added_since(self):
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="provider_1", tags=["Netflix Movies"]))
        self.db.commit()
        self.server.add("Movie", "m1", 603, "The Matrix", 1999, ["Netflix Movies", RECENT],
                        fresh_tags=["Netflix Movies", RECENT, "added-by-hand"])
        self.push()
        self.assertEqual(["Netflix Movies", "added-by-hand"], sorted(self.server.tags("m1")))

    def test_the_second_run_writes_nothing(self):
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="provider_1", tags=["Netflix Movies"]))
        self.db.add(Series(tmdb_id=1399, title="Friends", source="provider_1", tags=["Netflix TV"]))
        self.db.commit()
        self.server.add("Movie", "m1", 603, "The Matrix", 1999, [RECENT])
        self.server.add("Series", "s1", 1399, "Friends", 1994, ["Recently Added TV", "sitcom"])
        self.assertEqual(2, self.push()["written"])
        self.server.posts.clear()
        self.assertEqual(0, self.push()["written"])
        self.assertEqual([], self.server.posts)

    def test_series_follow_the_same_rules(self):
        self.db.add(Series(tmdb_id=1399, title="Friends", source="provider_1", tags=["Netflix TV"]))
        self.db.commit()
        self.server.add("Series", "s1", 1399, "Friends", 1994, ["Netflix TV", "Recently Added TV", "sitcom"])
        self.push()
        self.assertEqual(["Netflix TV", "sitcom"], sorted(self.server.tags("s1")))

    def test_a_stale_requester_downloads_tag_comes_off(self):
        self.db.add(TentacleUser(jellyfin_user_id="u2", display_name="Bob"))
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="radarr", tags=["Downloaded Movies"]))
        self.db.commit()
        self.server.add("Movie", "m1", 603, "The Matrix", 1999, ["Downloaded Movies", "Bob's Downloads"])
        self.push()
        self.assertEqual(["Downloaded Movies"], self.server.tags("m1"))


class WritesArePaced(_Db):
    def test_a_pause_after_each_batch(self):
        for n in range(5):
            self.db.add(Movie(tmdb_id=100 + n, title=f"T{n}", source="provider_1", tags=[]))
            self.server.add("Movie", f"m{n}", 100 + n, f"T{n}", 2000, [RECENT])
        self.db.commit()
        pauses = []
        with mock.patch.object(jellyfin, "TAG_PUSH_BATCH", 2), \
                mock.patch("time.sleep", lambda s: pauses.append(s)):
            self.assertEqual(5, self.push()["written"])
        self.assertEqual(2, len(pauses))


if __name__ == "__main__":
    unittest.main()

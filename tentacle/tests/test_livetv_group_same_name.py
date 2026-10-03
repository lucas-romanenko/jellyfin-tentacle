"""Live TV (Xtream): two provider categories with the same name.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Xtream panels do not require category names to be unique: one can list two
"SPORTS" categories with different ids. A group is one per name
(uq_live_group) and fetches one category_id, and _sync_groups() keyed the
groups it had by name without adding the ones it created in the same run:

- Both categories new (a first sync, or a provider that adds two at once):
  both were added under one name, the flush failed with "UNIQUE constraint
  failed: live_channel_groups.provider_id, live_channel_groups.name", and the
  group sync saved no group at all, not even the unrelated ones. The nightly
  discovery refresh failed the same way. A category listed twice did too.
- The name already had a group: every same-name category overwrote its
  category_id, so it ended on the one listed last. The channel sync fetches
  only each enabled group's category_id, so the category the user enabled was
  silently swapped for its namesake and never fetched again.

Each category now has a group of its own: the name stays with the category
its group already fetches (else the first one listed), and a namesake's group
is named after its id, "SPORTS (2)".
"""
import logging
import random
import re
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv
import services.discovery as discovery
import services.xtream_client as xc
from models.database import LiveChannel, LiveChannelGroup, Provider
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


SPORTS_1 = {"category_id": "1", "category_name": "SPORTS"}
SPORTS_2 = {"category_id": "2", "category_name": "SPORTS"}
NEWS_3 = {"category_id": "3", "category_name": "NEWS"}
STREAMS = {
    "1": [{"stream_id": 101, "name": "Sports One", "category_id": "1"}],
    "2": [{"stream_id": 201, "name": "Sports Two", "category_id": "2"}],
    "3": [{"stream_id": 301, "name": "News One", "category_id": "3"}],
}


class _Client:
    """Stand-in for XtreamClient: no network. Lists `categories`."""
    categories = [SPORTS_1, SPORTS_2, NEWS_3]
    fetched = []

    def __init__(self, **kwargs):
        pass

    def get_live_categories(self):
        return [dict(c) for c in self.categories]

    def get_live_streams(self, category_id=None):
        if category_id is None:
            return [dict(s) for v in STREAMS.values() for s in v]
        _Client.fetched.append(str(category_id))
        return [dict(s) for s in STREAMS.get(str(category_id), [])]

    def live_stream_url(self, sid, extension="m3u8"):
        return f"http://192.0.2.10/live/u/p/{sid}.{extension}"

    def close(self):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.provider = Provider(name="P", server_url="http://192.0.2.10", username="u",
                                 password="p", active=True, live_tv_enabled=True)
        self.db.add(self.provider)
        self.db.commit()
        self.pid = self.provider.id
        self.provider_data = {
            "id": self.pid, "name": "P", "server_url": "http://192.0.2.10",
            "username": "u", "password": "p", "user_agent": "TiviMate/4.7.0",
        }

    def _groups(self):
        """{category_id: (name, enabled)} of the provider's groups."""
        self.db.expire_all()
        return {g.category_id: (g.name, bool(g.enabled))
                for g in self.db.query(LiveChannelGroup).filter_by(provider_id=self.pid)}


class SameNameCategories(_Base):
    def setUp(self):
        super().setUp()
        orig = xc.XtreamClient
        xc.XtreamClient = _Client
        self.addCleanup(setattr, xc, "XtreamClient", orig)
        _Client.categories = [SPORTS_1, SPORTS_2, NEWS_3]
        _Client.fetched = []
        with livetv._sync_status_lock:
            livetv._sync_status.clear()
        self.addCleanup(livetv._sync_status.clear)

    def _sync_groups(self):
        try:
            livetv._sync_groups_from_xtream(self.provider_data, self.db)
        except Exception as e:   # sqlalchemy IntegrityError on uq_live_group
            self.db.rollback()
            self.fail(f"the group sync failed: {e}")
        self.db.commit()

    def _sync_channels(self):
        livetv._sync_channels_from_xtream(self.provider_data, self.db)
        self.db.commit()

    def _channels(self):
        self.db.expire_all()
        return {c.stream_id: c for c in self.db.query(LiveChannel).filter_by(provider_id=self.pid)}

    def _add_group(self, name, category_id, enabled):
        self.db.add(LiveChannelGroup(provider_id=self.pid, name=name, category_id=category_id,
                                     enabled=enabled, channel_count=1))
        self.db.commit()

    def test_a_first_sync_saves_every_group(self):
        self._sync_groups()
        self.assertEqual({"1": ("SPORTS", False), "2": ("SPORTS (2)", False), "3": ("NEWS", False)},
                         self._groups())
        self.assertEqual("complete", livetv._get_sync_status(self.pid).get("phase"))

    def test_a_category_listed_twice_is_one_group(self):
        _Client.categories = [SPORTS_1, NEWS_3, dict(SPORTS_1)]
        self._sync_groups()
        self.assertEqual({"1": ("SPORTS", False), "3": ("NEWS", False)}, self._groups())

    def test_the_nightly_refresh_reports_the_new_groups(self):
        try:
            new = discovery._refresh_live_groups(self.db, self.provider)
        except Exception as e:
            self.db.rollback()
            self.fail(f"the nightly group refresh failed: {e}")
        self.assertEqual(["NEWS", "SPORTS", "SPORTS (2)"], new)

    def _keeps_its_category(self, enabled_cat, namesake):
        # An install with the "SPORTS" group on `enabled_cat`, switched on;
        # the provider then adds a second "SPORTS".
        self._add_group("SPORTS", enabled_cat, True)
        self._add_group("NEWS", "3", False)
        self._sync_groups()
        self._sync_channels()
        self.assertEqual({enabled_cat: ("SPORTS", True), namesake: (f"SPORTS ({namesake})", False),
                          "3": ("NEWS", False)}, self._groups())
        self.assertEqual([enabled_cat], _Client.fetched)
        self.assertEqual({str(STREAMS[enabled_cat][0]["stream_id"])}, set(self._channels()))

    def test_an_enabled_group_keeps_its_category_when_a_namesake_is_listed_after_it(self):
        self._keeps_its_category("1", "2")

    def test_an_enabled_group_keeps_its_category_when_a_namesake_is_listed_before_it(self):
        self._keeps_its_category("2", "1")

    def test_a_namesake_group_syncs_its_own_channels_and_stays_put(self):
        self._sync_groups()
        before = {g.id: (g.name, g.category_id)
                  for g in self.db.query(LiveChannelGroup).filter_by(provider_id=self.pid)}
        namesake = self.db.query(LiveChannelGroup).filter_by(name="SPORTS (2)").one()
        livetv.update_group(namesake.id, livetv.GroupUpdate(enabled=True), self.db)
        self._sync_channels()

        self.assertEqual(["2"], _Client.fetched)
        chans = self._channels()
        self.assertEqual({"201"}, set(chans))
        self.assertEqual("SPORTS (2)", chans["201"].group_title)
        self.assertTrue(chans["201"].enabled, "a new channel follows its group (#158)")

        # Later syncs, in another order: every group stays on its category.
        _Client.categories = [NEWS_3, SPORTS_2, SPORTS_1]
        self._sync_groups()
        self.db.expire_all()
        after = {g.id: (g.name, g.category_id)
                 for g in self.db.query(LiveChannelGroup).filter_by(provider_id=self.pid)}
        self.assertEqual(before, after)

    def test_a_namesake_never_takes_another_categorys_name(self):
        real = {"category_id": "7", "category_name": "SPORTS (2)"}
        for order in ([SPORTS_1, SPORTS_2, real], [real, SPORTS_2, SPORTS_1]):
            with self.subTest(order=[c["category_id"] for c in order]):
                self.db.query(LiveChannelGroup).delete()
                self.db.commit()
                _Client.categories = order
                self._sync_groups()
                groups = self._groups()
                self.assertEqual({"1", "2", "7"}, set(groups))
                self.assertEqual("SPORTS (2)", groups["7"][0])


class GroupSyncProperty(_Base):
    """Random listings over three syncs: names repeat (also one that looks
    like a namesake's group, "SPORTS (3)"), a category may be listed twice,
    the order changes, categories come, go and are renumbered."""

    SEEDS = 1000
    NAMES = ["SPORTS", "SPORTS", "NEWS", "SPORTS (3)", ""]

    @staticmethod
    def _of(group_name, cat):
        """Whether a group of this name is the category's: "NAME", or
        "NAME (id)" (a namesake's group)."""
        name, cid = cat
        return re.fullmatch(re.escape(name) + rf"(?: \({re.escape(cid)}\))*", group_name) is not None

    def _sync(self, cats):
        try:
            livetv._sync_groups(self.pid, cats, self.db, {})
            self.db.commit()
        except Exception as e:   # sqlalchemy IntegrityError on uq_live_group
            self.db.rollback()
            self.fail(f"seed {self.seed}: {cats}: the group sync failed: {e}")
        self.db.expire_all()
        return {g.id: g for g in self.db.query(LiveChannelGroup).filter_by(provider_id=self.pid)}

    def test_random_listings(self):
        for seed in range(self.SEEDS):
            self.seed = seed
            rnd = random.Random(seed)
            self.db.query(LiveChannelGroup).delete()
            self.db.commit()
            pool = {str(i): rnd.choice(self.NAMES) for i in range(1, 9)}
            was = {}
            for _ in range(3):
                listed = [(pool[i], i) for i in rnd.sample(sorted(pool), rnd.randint(1, len(pool)))]
                listed += rnd.sample(listed, rnd.randint(0, min(2, len(listed))))
                rnd.shuffle(listed)
                groups = self._sync([{"category_id": i, "category_name": n} for n, i in listed])

                for cat in set(listed):
                    mine = [(g.name, g.category_id) for g in groups.values()
                            if g.category_id == cat[1] and self._of(g.name, cat)]
                    self.assertEqual(1, len(mine), f"seed {seed}: category {cat} has the groups {mine}")
                for gid, (name, cid, enabled) in was.items():
                    g = groups[gid]
                    self.assertEqual((name, enabled), (g.name, g.enabled),
                                     f"seed {seed}: a group's name or switch changed")
                    if any(c[1] == cid and self._of(name, c) for c in listed):
                        self.assertEqual(cid, g.category_id,
                                         f"seed {seed}: group {name!r} moved off category {cid}, "
                                         f"which is still listed")

                for g in rnd.sample(list(groups.values()), rnd.randint(0, len(groups))):
                    g.enabled = not g.enabled
                self.db.commit()
                was = {g.id: (g.name, g.category_id, bool(g.enabled)) for g in groups.values()}
                if rnd.random() < 0.3:   # the provider renumbers a category
                    pool[str(rnd.randint(9, 12))] = pool.pop(rnd.choice(sorted(pool)))


if __name__ == "__main__":
    unittest.main()

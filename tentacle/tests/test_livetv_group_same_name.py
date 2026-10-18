"""Live TV (Xtream): two provider categories with the same name (#462).

Xtream panels don't require category names to be unique, but a Live TV group
is unique on (provider, name) and holds one category id. _sync_groups looked
groups up by name only, so:

  A. two new same-name categories were both added and the flush raised
     IntegrityError: the whole group sync saved nothing, every run.
  B. an existing group followed whichever same-name category was listed
     last, so an enabled group silently switched to the other category and
     its own channels were never fetched again.

Each category now gets a group of its own: the first one listed keeps the
name, the others get "NAME (id)".

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import logging
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv
import services.xtream_client as xc
from models.database import LiveChannel, LiveChannelGroup, Provider
from tmp_dirs import temp_dir

CATEGORIES = [
    {"category_id": "1", "category_name": "SPORTS"},
    {"category_id": "2", "category_name": "SPORTS"},
    {"category_id": "3", "category_name": "NEWS"},
]
STREAMS = {
    "1": [{"stream_id": 101, "name": "Sports One", "category_id": "1"}],
    "2": [{"stream_id": 201, "name": "Sports Two", "category_id": "2"}],
    "3": [{"stream_id": 301, "name": "News One", "category_id": "3"}],
}


class _Client:
    categories = CATEGORIES

    def __init__(self, **kwargs):
        pass

    def get_live_categories(self):
        return [dict(c) for c in self.categories]

    def get_live_streams(self, category_id=None):
        if category_id is None:
            return [dict(s) for v in STREAMS.values() for s in v]
        return [dict(s) for s in STREAMS.get(str(category_id), [])]

    def live_stream_url(self, sid, extension="m3u8"):
        return f"http://192.0.2.10/live/u/p/{sid}.{extension}"

    def close(self):
        pass


class SameNameCategories(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        p = Provider(name="P", server_url="http://192.0.2.10", username="u",
                     password="p", active=True, live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        self.provider_data = {"id": p.id, "name": "P", "server_url": "http://192.0.2.10",
                              "username": "u", "password": "p", "user_agent": "TiviMate/4.7.0"}
        orig = xc.XtreamClient
        xc.XtreamClient = _Client
        self.addCleanup(setattr, xc, "XtreamClient", orig)
        self.addCleanup(setattr, _Client, "categories", CATEGORIES)
        self.addCleanup(livetv._sync_status.clear)

    def _list(self, *cats):
        _Client.categories = [{"category_id": i, "category_name": n} for i, n in cats]

    def _sync_groups(self):
        livetv._sync_groups_from_xtream(self.provider_data, self.db)
        self.db.expire_all()

    def _groups(self):
        return {g.name: g.category_id
                for g in self.db.query(LiveChannelGroup).filter_by(provider_id=self.pid)}

    def test_a_first_group_sync_saves_every_category(self):
        try:
            self._sync_groups()
        except Exception as e:
            self.db.rollback()
            self.fail(f"group sync failed: {e}")
        self.assertEqual(self._groups(), {"SPORTS": "1", "SPORTS (2)": "2", "NEWS": "3"})

    def test_a_second_sync_keeps_the_same_groups(self):
        self._sync_groups()
        ids = {g.name: g.id for g in self.db.query(LiveChannelGroup)}
        self._sync_groups()
        self.assertEqual({g.name: g.id for g in self.db.query(LiveChannelGroup)}, ids)
        self.assertEqual(self._groups(), {"SPORTS": "1", "SPORTS (2)": "2", "NEWS": "3"})

    def test_reordering_the_list_keeps_each_group_on_its_category(self):
        self._sync_groups()
        self._list(("3", "NEWS"), ("2", "SPORTS"), ("1", "SPORTS"))
        self._sync_groups()
        self.assertEqual(self._groups(), {"SPORTS": "1", "SPORTS (2)": "2", "NEWS": "3"})

    def test_a_category_listed_twice_is_one_group(self):
        self._list(("1", "SPORTS"), ("1", "SPORTS"), ("3", "NEWS"))
        self._sync_groups()
        self.assertEqual(self._groups(), {"SPORTS": "1", "NEWS": "3"})

    def test_a_group_still_follows_its_category_to_a_new_id(self):
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="SPORTS", category_id="1", enabled=True))
        self.db.commit()
        self._list(("9", "SPORTS"))
        self._sync_groups()
        self.assertEqual(self._groups(), {"SPORTS": "9"})

    def test_an_enabled_group_keeps_its_category(self):
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="SPORTS", category_id="1", enabled=True))
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="NEWS", category_id="3", enabled=False))
        self.db.commit()
        self._sync_groups()
        livetv._sync_channels_from_xtream(self.provider_data, self.db)
        self.db.expire_all()
        got = {c.stream_id for c in self.db.query(LiveChannel).filter_by(provider_id=self.pid)}
        self.assertIn("101", got, "category 1 was never fetched")
        self.assertEqual(self._groups(), {"SPORTS": "1", "SPORTS (2)": "2", "NEWS": "3"})

    def test_a_new_same_name_category_listed_first_gets_its_own_group(self):
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="SPORTS", category_id="1", enabled=True))
        self.db.commit()
        self._list(("2", "SPORTS"), ("1", "SPORTS"))
        self._sync_groups()
        self.assertEqual(self._groups(), {"SPORTS": "1", "SPORTS (2)": "2"})

    def test_a_group_of_its_own_stays_when_the_other_category_goes(self):
        self._sync_groups()
        self._list(("2", "SPORTS"), ("3", "NEWS"))
        self._sync_groups()
        groups = self._groups()
        self.assertEqual(groups["SPORTS (2)"], "2")
        self.assertEqual(groups["SPORTS"], "1", "the old group must not take category 2 over")

    def test_a_provider_category_already_named_like_the_fallback(self):
        self._list(("1", "SPORTS"), ("5", "SPORTS (2)"), ("2", "SPORTS"))
        self._sync_groups()
        groups = self._groups()
        self.assertEqual(groups["SPORTS"], "1")
        self.assertEqual(groups["SPORTS (2)"], "5")
        self.assertEqual(len(set(groups.values())), 3, groups)
        self._sync_groups()
        self.assertEqual(self._groups(), groups)

    def test_channels_of_the_second_category_go_to_its_own_group(self):
        self._sync_groups()
        for g in self.db.query(LiveChannelGroup):
            g.enabled = True
        self.db.commit()
        livetv._sync_channels_from_xtream(self.provider_data, self.db)
        self.db.expire_all()
        got = {c.stream_id: c.group_title for c in self.db.query(LiveChannel)}
        self.assertEqual(got, {"101": "SPORTS", "201": "SPORTS (2)", "301": "NEWS"})


if __name__ == "__main__":
    unittest.main()

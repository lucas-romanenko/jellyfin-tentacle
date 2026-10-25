"""Nightly discovery must look for new Live TV groups of a provider set up on
the Live TV page.

Run from the tentacle/ directory:  python -m unittest discover -s tests

save_live_provider() creates its provider with active=False (`active` is the
VOD sync switch; a Live TV provider must never get a VOD sync). The Live TV
step of discover_new_provider_content() selected `active == True AND
live_tv_enabled == True`, so such a provider, the normal Live TV setup, never
got its new groups found at night and no "new content" notice was shown for
them. Same filter mistake as #174 (nightly guide), which live_tv_providers()
fixed (#623).
"""
import logging
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Provider
from services import discovery
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class DiscoveryLiveGroupsOfLivePageProvider(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.visited = []

    def add(self, name, active, live):
        p = Provider(name=name, server_url="http://prov.test", username="u", password="p",
                     active=active, live_tv_enabled=live)
        self.db.add(p)
        self.db.commit()
        return p

    def fake_live(self, db, provider, *a, **k):
        self.visited.append(provider.name)
        return ["EN | New Sports"]

    def run_discovery(self):
        with mock.patch("routers.providers.fetch_provider_categories",
                        return_value=([], [], {}, {})), \
             mock.patch.object(discovery, "_refresh_live_groups", side_effect=self.fake_live):
            return discovery.discover_new_provider_content(self.db)

    def test_live_tv_page_provider_gets_its_new_groups_found(self):
        self.add("Live page", active=False, live=True)
        out = self.run_discovery()
        self.assertEqual(self.visited, ["Live page"])
        self.assertEqual(out["live_new"], ["EN | New Sports"])

    def test_vod_and_live_provider_unchanged(self):
        self.add("Both", active=True, live=True)
        self.assertEqual(self.run_discovery()["live_new"], ["EN | New Sports"])

    def test_live_tv_off_stays_out(self):
        self.add("VOD only", active=True, live=False)
        self.add("Off", active=False, live=False)
        self.run_discovery()
        self.assertEqual(self.visited, [])


if __name__ == "__main__":
    unittest.main()

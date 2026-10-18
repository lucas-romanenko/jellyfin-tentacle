"""Nightly discovery must look for new VOD categories of a provider that also
has Live TV.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The provider test sets live_tv_enabled on every Xtream account that lists live
channels (routers/providers.py), so the usual provider is active AND
live-enabled. The VOD step selected `live_tv_enabled == False` and skipped it:
a category the provider added later never appeared, and no "new content"
notice was ever shown for it. A provider made on the Live TV page
(active=False) must still be left out of the VOD step.
"""
import logging
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Provider, ProviderCategory
from services import discovery
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class DiscoveryVodOfLiveProvider(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.fetched = []

    def add(self, name, active, live):
        p = Provider(name=name, server_url="http://prov.test", username="u", password="p",
                     active=active, live_tv_enabled=live)
        self.db.add(p)
        self.db.commit()
        self.db.add(ProviderCategory(provider_id=p.id, category_id="1", category_name="EN Movies",
                                     type="movie", whitelisted=True, title_count=5))
        self.db.commit()
        return p

    def fake_fetch(self, provider):
        self.fetched.append(provider.name)
        return ([{"category_id": "1", "category_name": "EN Movies"},
                 {"category_id": "9", "category_name": "EN New Releases"}], [], {"1": 5, "9": 3}, {})

    def run_discovery(self):
        with mock.patch("routers.providers.fetch_provider_categories", side_effect=self.fake_fetch), \
             mock.patch.object(discovery, "_refresh_live_groups", return_value=[]):
            return discovery.discover_new_provider_content(self.db)

    def test_new_vod_category_of_a_vod_and_live_provider_is_found(self):
        p = self.add("Both", active=True, live=True)
        out = self.run_discovery()
        self.assertEqual(out["vod_new"], ["EN New Releases"])
        new = self.db.query(ProviderCategory).filter_by(provider_id=p.id, category_id="9").one()
        self.assertFalse(new.whitelisted)          # found, not synced until the user enables it
        kept = self.db.query(ProviderCategory).filter_by(provider_id=p.id, category_id="1").one()
        self.assertTrue(kept.whitelisted)

    def test_vod_only_provider_unchanged(self):
        self.add("VOD", active=True, live=False)
        self.assertEqual(self.run_discovery()["vod_new"], ["EN New Releases"])

    def test_live_tv_page_provider_stays_out_of_the_vod_step(self):
        self.add("Live page", active=False, live=True)
        out = self.run_discovery()
        self.assertEqual(out["vod_new"], [])
        self.assertEqual(self.fetched, [])


if __name__ == "__main__":
    unittest.main()

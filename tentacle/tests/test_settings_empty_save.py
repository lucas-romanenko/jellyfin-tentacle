"""A cleared number field in Settings must not break every sync (#157).

Run from the tentacle/ directory:  python -m unittest discover -s tests

A cleared field shows its placeholder (30 / 0.7), so it looks like the default,
but it was stored as "", and get_setting(db, key, "30") returns the stored "",
so int("") / float("") failed in the provider sync, the tag refresh and the
playlist build until the value was typed back in. A Save with nothing edited
also created empty rows for keys that were never set.
"""
import logging
import shutil
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import NON_EMPTY_DEFAULTS, Setting, get_setting


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class EmptySave(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.seed_defaults(self.db)

    def _save(self, **settings):
        import routers.settings as settings_router
        return settings_router.update_settings(settings_router.SettingsUpdate(settings=settings), db=self.db)

    def test_a_cleared_number_is_saved_as_its_default(self):
        self._save(recently_added_days="", tmdb_match_threshold="  ")
        self.assertEqual(30, int(get_setting(self.db, "recently_added_days", "30")))
        self.assertEqual(0.7, float(get_setting(self.db, "tmdb_match_threshold", "0.7")))

    def test_an_empty_value_already_stored_reads_as_the_default(self):
        """Installs that saved "" before this fix."""
        self.db.query(Setting).filter_by(key="recently_added_days").update({"value": ""})
        self.db.commit()
        self.assertEqual("30", get_setting(self.db, "recently_added_days", "30"))
        from services.tagger import refresh_recently_added_tags
        refresh_recently_added_tags(self.db)  # int("") used to raise here

    def test_a_blank_for_a_key_never_set_stores_nothing(self):
        self._save(jellyfin_public_url="", mdblist_api_key="")
        keys = {k for (k,) in self.db.query(Setting.key).all()}
        self.assertNotIn("jellyfin_public_url", keys)
        self.assertNotIn("mdblist_api_key", keys)

    def test_an_existing_value_can_still_be_cleared(self):
        self._save(logodev_api_key="abc")
        self._save(logodev_api_key="")
        self.assertEqual("", get_setting(self.db, "logodev_api_key"))

    def test_the_keys_are_the_ones_with_non_empty_defaults(self):
        self.assertEqual({"recently_added_days", "tmdb_match_threshold", "hybrid_series_layout", "sync_schedule"},
                         set(NON_EMPTY_DEFAULTS))


if __name__ == "__main__":
    unittest.main()

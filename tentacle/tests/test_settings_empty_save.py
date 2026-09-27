"""Saving Settings with an empty field must not break the sync (A7).

saveSettings() posts every field as typed. A cleared "Recently added days" or
"TMDB match threshold" shows its placeholder (30 / 0.7), so it looks like the
default, but was stored as "" — and get_setting(db, key, "30") returns the
stored "" rather than the default, so int("")/float("") raised in the
provider sync, the nightly tag refresh and the playlist build. A Save with
nothing edited also created empty rows for keys that were never set.

Run from tentacle/:  python -m unittest discover -s tests -p "test_settings_empty_save.py"
"""
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Setting, get_setting


class TestEmptySave(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        mdb.seed_defaults(self.db)

    def tearDown(self):
        self.db.close()

    def _save(self, **settings):
        from routers import settings as r
        r.update_settings(r.SettingsUpdate(settings=settings), db=self.db)
        self.db.commit()

    def test_a_cleared_number_field_keeps_the_nightly_tag_refresh_working(self):
        from services import tagger
        self._save(recently_added_days="", tmdb_match_threshold="")
        tagger.refresh_recently_added_tags(self.db)          # raised ValueError
        self.assertEqual(get_setting(self.db, "recently_added_days"), "30")
        self.assertEqual(float(get_setting(self.db, "tmdb_match_threshold", "0.7")), 0.7)

    def test_a_save_with_nothing_edited_adds_no_rows(self):
        before = {s.key for s in self.db.query(Setting)}
        self._save(jellyfin_public_url="", webhook_host="", sonarr_webhook_host="")
        self.assertEqual({s.key for s in self.db.query(Setting)}, before)

    def test_an_existing_value_can_still_be_cleared(self):
        self._save(webhook_host="10.0.0.2")
        self._save(webhook_host="")
        self.assertEqual(get_setting(self.db, "webhook_host", "x"), "")

    def test_a_real_value_is_stored(self):
        self._save(recently_added_days="14")
        self.assertEqual(get_setting(self.db, "recently_added_days"), "14")


if __name__ == "__main__":
    unittest.main()

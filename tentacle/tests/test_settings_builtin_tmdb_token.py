"""The built-in TMDB token stays a default: a Settings save never stores it (#383).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The Settings page fills its fields from GET /api/settings/raw (loadSettings in
static/js/app.js) and Save posts every text field back (saveSettings). /raw put
the built-in token into tmdb_bearer_token when none was stored, so the first
Save of any setting stored it as the user's own: the field's "Using built-in
key" placeholder never showed again, and a release that changes the built-in
token would never reach that install. /raw now serves no built-in token, and a
save that sends the built-in token stores nothing (an install that already
stored it is cleared on its next save).
"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
import routers.settings as settings  # noqa: E402
from services.tmdb import TMDB_DEFAULT_TOKEN, get_tmdb_token  # noqa: E402

OWN_TOKEN = "o" * 200


class BuiltinTmdbToken(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add(mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="admin", is_admin=True))
        self.db.commit()

    def page_save(self, **edits):
        """What saveSettings() sends: every text field as /raw filled it, plus the edits."""
        raw = settings.get_settings_raw(self.db)
        fields = {"tmdb_bearer_token": raw.get("tmdb_bearer_token", ""), "recently_added_days": "14"}
        fields.update(edits)
        settings.update_settings(settings.SettingsUpdate(settings=fields), self.db)

    def test_raw_serves_no_builtin_token(self):
        self.assertNotIn(TMDB_DEFAULT_TOKEN, settings.get_settings_raw(self.db).values())

    def test_saving_another_setting_stores_no_token(self):
        self.page_save()
        self.assertFalse(mdb.get_setting(self.db, "tmdb_bearer_token"))
        self.assertEqual("14", mdb.get_setting(self.db, "recently_added_days"))
        self.assertEqual(TMDB_DEFAULT_TOKEN, get_tmdb_token(self.db))

    def test_builtin_token_typed_in_is_not_stored(self):
        self.page_save(tmdb_bearer_token=TMDB_DEFAULT_TOKEN)
        self.assertFalse(mdb.get_setting(self.db, "tmdb_bearer_token"))

    def test_install_that_stored_the_builtin_token_is_cleared(self):
        mdb.set_setting(self.db, "tmdb_bearer_token", TMDB_DEFAULT_TOKEN)
        self.assertNotIn(TMDB_DEFAULT_TOKEN, settings.get_settings_raw(self.db).values())
        self.page_save()
        self.assertFalse(mdb.get_setting(self.db, "tmdb_bearer_token"))
        self.assertEqual(TMDB_DEFAULT_TOKEN, get_tmdb_token(self.db))

    def test_own_token_is_kept_and_shown(self):
        mdb.set_setting(self.db, "tmdb_bearer_token", OWN_TOKEN)
        self.assertEqual(OWN_TOKEN, settings.get_settings_raw(self.db)["tmdb_bearer_token"])
        self.page_save()
        self.assertEqual(OWN_TOKEN, mdb.get_setting(self.db, "tmdb_bearer_token"))
        self.assertEqual(OWN_TOKEN, get_tmdb_token(self.db))

    def test_plugin_keys_still_get_the_builtin_token(self):
        self.assertEqual(TMDB_DEFAULT_TOKEN, settings.get_plugin_keys(self.db)["tmdb_bearer_token"])


if __name__ == "__main__":
    unittest.main()

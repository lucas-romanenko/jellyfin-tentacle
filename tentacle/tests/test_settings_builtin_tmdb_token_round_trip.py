"""A Settings save keeps the built-in TMDB token out of the database (#383), and
an install where an earlier Save already stored it is cleared at start-up.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The Settings page fills its fields from GET /api/settings/raw
(static/js/app.js loadSettings: a field is set only when the value is
non-empty, else it shows its placeholder, "Using built-in key" for the TMDB
token) and Save posts every text field back (saveSettings). /raw used to fill
in the built-in token when none was stored, so the first Save of ANY setting
stored it as the user's own: the placeholder never showed again and a later
built-in token would never reach that install.

Both sides are checked: an install without a token of its own keeps none (and
one that was already saved that way is cleared at start-up), and an install
with its own token keeps exactly that token. The token TMDB calls use
(get_tmdb_token) and the one the plugin gets (/plugin-keys) never change.
"""
import random
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Setting, get_setting, set_setting
from tmp_dirs import temp_dir
from services.tmdb import TMDB_DEFAULT_TOKEN, get_tmdb_token

OWN = "eyJown-token-of-this-user.abc"


class BuiltinTmdbToken(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.seed_defaults(self.db)
        # The Settings page is used by a signed-in admin (a DB with no user at
        # all is first-run setup).
        self.db.add(mdb.TentacleUser(jellyfin_user_id="u1", display_name="admin", is_admin=True))
        self.db.commit()

    # What the Settings page does: load the fields, change one, Save them all.
    def page_fields(self):
        from routers.settings import get_settings_raw
        raw = get_settings_raw(db=self.db)
        return {k: (raw.get(k) or "") for k in ("tmdb_bearer_token", "tmdb_api_key", "recently_added_days")}

    def save(self, fields):
        from routers.settings import update_settings, SettingsUpdate
        update_settings(SettingsUpdate(settings=dict(fields)), db=self.db)

    def save_unrelated_edit(self):
        fields = self.page_fields()
        fields["recently_added_days"] = "14"
        self.save(fields)

    def plugin_token(self):
        from routers.settings import get_plugin_keys
        return get_plugin_keys(db=self.db).get("tmdb_bearer_token")

    def stored(self):
        return get_setting(self.db, "tmdb_bearer_token")

    # ── no token of the user's own ───────────────────────────────────────
    def test_page_shows_the_placeholder_when_none_is_stored(self):
        self.assertEqual(self.page_fields()["tmdb_bearer_token"], "")

    def test_saving_another_setting_stores_no_tmdb_token(self):
        self.save_unrelated_edit()
        self.assertEqual(self.stored(), "", "the built-in token was stored as the user's own")
        self.assertEqual(get_setting(self.db, "recently_added_days"), "14")
        self.assertEqual(get_tmdb_token(self.db), TMDB_DEFAULT_TOKEN)
        self.assertEqual(self.plugin_token(), TMDB_DEFAULT_TOKEN)

    def test_a_page_loaded_before_the_fix_posting_the_builtin_token_stores_none(self):
        fields = self.page_fields()
        fields["tmdb_bearer_token"] = TMDB_DEFAULT_TOKEN
        self.save(fields)
        self.assertEqual(self.stored(), "")
        self.assertEqual(get_tmdb_token(self.db), TMDB_DEFAULT_TOKEN)

    def test_an_install_that_already_stored_it_is_cleared_at_start_up(self):
        set_setting(self.db, "tmdb_bearer_token", TMDB_DEFAULT_TOKEN)   # what a Save did before #383
        mdb.seed_defaults(self.db)                                     # start-up
        self.assertEqual(self.stored(), "")
        self.assertEqual(self.page_fields()["tmdb_bearer_token"], "")
        self.assertEqual(get_tmdb_token(self.db), TMDB_DEFAULT_TOKEN)
        self.assertEqual(self.plugin_token(), TMDB_DEFAULT_TOKEN)

    def test_a_missing_row_is_not_created(self):
        self.db.query(Setting).filter(Setting.key == "tmdb_bearer_token").delete()
        self.db.commit()
        self.save_unrelated_edit()
        mdb.seed_defaults(self.db)
        self.assertEqual(self.stored(), "")
        self.assertEqual(get_tmdb_token(self.db), TMDB_DEFAULT_TOKEN)

    def test_a_stored_builtin_next_to_a_v3_key_is_kept_for_the_plugin(self):
        set_setting(self.db, "tmdb_bearer_token", TMDB_DEFAULT_TOKEN)
        set_setting(self.db, "tmdb_api_key", "v3key")
        mdb.seed_defaults(self.db)                                     # start-up
        self.assertEqual(self.plugin_token(), TMDB_DEFAULT_TOKEN, "the plugin lost the bearer it used")

    # ── the user's own token: unchanged ──────────────────────────────────
    def test_own_token_is_shown_and_kept_by_a_save(self):
        set_setting(self.db, "tmdb_bearer_token", OWN)
        self.assertEqual(self.page_fields()["tmdb_bearer_token"], OWN)
        self.save_unrelated_edit()
        mdb.seed_defaults(self.db)
        self.assertEqual(self.stored(), OWN)
        self.assertEqual(get_tmdb_token(self.db), OWN)
        self.assertEqual(self.plugin_token(), OWN)

    def test_own_token_can_be_set_replaced_and_cleared(self):
        fields = self.page_fields()
        fields["tmdb_bearer_token"] = OWN
        self.save(fields)
        self.assertEqual(self.stored(), OWN)
        fields = self.page_fields()
        fields["tmdb_bearer_token"] = OWN + "2"
        self.save(fields)
        self.assertEqual(self.stored(), OWN + "2")
        fields = self.page_fields()
        fields["tmdb_bearer_token"] = ""
        self.save(fields)
        self.assertEqual(self.stored(), "")
        self.assertEqual(get_tmdb_token(self.db), TMDB_DEFAULT_TOKEN)

    def test_masked_value_keeps_own_token(self):
        set_setting(self.db, "tmdb_bearer_token", OWN)
        self.save({"tmdb_bearer_token": OWN[:8] + "..." + OWN[-4:]})
        self.assertEqual(self.stored(), OWN)

    def test_own_v3_key_keeps_the_bearer_field_empty(self):
        set_setting(self.db, "tmdb_api_key", "v3key")
        self.save_unrelated_edit()
        self.assertEqual(self.stored(), "")
        self.assertEqual(get_setting(self.db, "tmdb_api_key"), "v3key")
        self.assertEqual(get_plugin_keys_dict(self.db), {"tmdb_api_key": "v3key"})

    # ── property: any sequence of page saves / own-token edits / restarts ─
    def test_property_stored_token_is_only_ever_the_users_own(self):
        """1,000 seeds of random actions. Model: `own` = the token the user
        typed last ("" = none). After every step the stored value is exactly
        `own`, the effective token is `own or built-in`, and the page shows
        `own` ("" = placeholder)."""
        from routers.settings import get_plugin_keys
        for seed in range(1000):
            rng = random.Random(seed)
            self.db.query(Setting).filter(Setting.key == "tmdb_bearer_token").delete()
            self.db.commit()
            own = ""
            if rng.random() < 0.3:       # an install a pre-#383 Save already pinned
                set_setting(self.db, "tmdb_bearer_token", TMDB_DEFAULT_TOKEN)
                mdb.seed_defaults(self.db)
            for step in range(rng.randint(1, 12)):
                action = rng.choice(["save", "save", "type", "clear", "restart", "stale", "masked"])
                fields = self.page_fields()
                if action == "type":
                    own = rng.choice([OWN, OWN + str(step), "x" * rng.randint(1, 40)])
                    fields["tmdb_bearer_token"] = own
                elif action == "clear":
                    own = ""
                    fields["tmdb_bearer_token"] = ""
                elif action == "stale":    # a tab loaded before the fix
                    fields["tmdb_bearer_token"] = own or TMDB_DEFAULT_TOKEN
                elif action == "masked" and own:
                    fields["tmdb_bearer_token"] = own[:8] + "..." + own[-4:]
                if action == "restart":
                    mdb.seed_defaults(self.db)
                else:
                    fields["recently_added_days"] = str(rng.randint(1, 90))
                    self.save(fields)
                msg = f"seed {seed} step {step} {action}"
                self.assertEqual(self.stored(), own, msg)
                self.assertEqual(self.page_fields()["tmdb_bearer_token"], own, msg)
                self.assertEqual(get_tmdb_token(self.db), own or TMDB_DEFAULT_TOKEN, msg)
                self.assertEqual(get_plugin_keys(db=self.db).get("tmdb_bearer_token"),
                                 own or TMDB_DEFAULT_TOKEN, msg)


def get_plugin_keys_dict(db):
    from routers.settings import get_plugin_keys
    return get_plugin_keys(db=db)


if __name__ == "__main__":
    unittest.main()

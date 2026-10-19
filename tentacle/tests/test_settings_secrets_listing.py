"""What the settings routes give out, and what a Save keeps.

Run from the tentacle/ directory:  python -m unittest discover -s tests

- session_secret (signs the dashboard session cookies) and vod_token_secret
  (signs the .strm playback tokens) were in GET /api/settings/raw. Nothing in
  the dashboard shows or edits them; no route serves them now and a Save
  cannot set them.
- require_admin lets any caller in while no user exists (first-run setup).
  /raw then handed out every stored key in full; it now masks every secret
  (setup only needs the addresses).
- GET /api/settings (the masked listing) left internal_secret, webhook_secret,
  logodev_api_key and the YouTube proxy's password in full, and its mask
  (first 8 + "..." + last 4) showed a value of 12 characters or fewer whole.
  Secrets now show as "••••" + the last four characters (short ones as "••••").
- A Save skipped any secret containing "...", even a real new value, and
  still answered success. It now keeps the stored value only when the exact
  masked form comes back.
The admin's own Settings page (/raw, signed in) is unchanged.
"""
import random
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
import routers.settings as settings  # noqa: E402

STORED = {
    "session_secret": "s" * 64, "vod_token_secret": "v" * 64,
    "internal_secret": "i" * 64, "webhook_secret": "w" * 32,
    "jellyfin_url": "http://jellyfin:8096", "radarr_url": "http://radarr:7878",
    "jellyfin_api_key": "j" * 32, "radarr_api_key": "r" * 32, "sonarr_api_key": "short11char",
    "tmdb_bearer_token": "t" * 200, "mdblist_api_key": "m" * 20, "trakt_client_id": "k" * 64,
    "logodev_api_key": "pk_logo123", "youtube_api_key": "y" * 39,
    "lidarr_api_key": "l" * 32, "navidrome_password": "n@v pass", "music_webhook_secret": "h" * 32,
    "youtube_proxy": "http://someone:proxypass@gluetun:8888",
}
SECRET_VALUES = [v for k, v in STORED.items() if k in settings.SENSITIVE_KEYS | settings.NEVER_SERVED]


class _Base(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in STORED.items():
            mdb.set_setting(self.db, k, v)

    def add_admin(self):
        self.db.add(mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="admin", is_admin=True))
        self.db.commit()

    def stored(self):
        return {k: mdb.get_setting(self.db, k) for k in STORED}

    def save(self, values):
        return settings.update_settings(settings.SettingsUpdate(settings=values), self.db)


class Listings(_Base):
    def test_every_sensitive_key_is_covered(self):
        for key in ("internal_secret", "webhook_secret", "logodev_api_key", "youtube_api_key",
                    "lidarr_api_key", "navidrome_password", "music_webhook_secret", "tmdb_bearer_token"):
            self.assertIn(key, settings.SENSITIVE_KEYS)

    def test_signed_in_admin_raw_keeps_the_keys_but_not_the_signing_secrets(self):
        self.add_admin()
        raw = settings.get_settings_raw(self.db)
        self.assertNotIn("session_secret", raw)
        self.assertNotIn("vod_token_secret", raw)
        for key in ("jellyfin_api_key", "radarr_api_key", "internal_secret", "youtube_proxy"):
            self.assertEqual(STORED[key], raw[key])

    def test_first_run_raw_shows_addresses_and_no_secret(self):
        raw = settings.get_settings_raw(self.db)  # no user yet
        self.assertEqual("http://jellyfin:8096", raw["jellyfin_url"])
        self.assertEqual("http://radarr:7878", raw["radarr_url"])
        text = repr(raw)
        for value in SECRET_VALUES:
            with self.subTest(value=value[:12]):
                self.assertNotIn(value, text)
        self.assertNotIn("proxypass", text)
        self.assertIn("gluetun:8888", raw["youtube_proxy"])

    def test_masked_listing(self):
        self.add_admin()
        got = settings.get_settings(self.db)
        self.assertNotIn("session_secret", got)
        self.assertNotIn("vod_token_secret", got)
        text = repr(got)
        for value in SECRET_VALUES:
            with self.subTest(value=value[:12]):
                self.assertNotIn(value, text)
        self.assertEqual("••••" + "r" * 4, got["radarr_api_key"])
        self.assertEqual("••••", got["logodev_api_key"][:4])
        self.assertEqual("••••", got["navidrome_password"])
        for key in ("internal_secret", "webhook_secret", "music_webhook_secret"):
            self.assertEqual("••••", got[key], key)  # passwords and shared secrets: nothing shown
        self.assertEqual("http://someone:••••@gluetun:8888", got["youtube_proxy"])


class Saves(_Base):
    def test_raw_then_save_keeps_every_value(self):
        for signed_in in (False, True):
            with self.subTest(signed_in=signed_in):
                if signed_in:
                    self.add_admin()
                before = self.stored()
                self.save({k: v for k, v in settings.get_settings_raw(self.db).items() if isinstance(v, str)})
                self.assertEqual(before, self.stored())

    def test_masked_listing_then_save_keeps_every_value(self):
        self.add_admin()
        before = self.stored()
        self.save(settings.get_settings(self.db))
        self.assertEqual(before, self.stored())

    def test_a_new_value_is_saved_even_with_dots(self):
        self.add_admin()
        self.save({"radarr_api_key": "abc...xyz", "mdblist_api_key": "••••new-value"})
        self.assertEqual("abc...xyz", mdb.get_setting(self.db, "radarr_api_key"))
        self.assertEqual("••••new-value", mdb.get_setting(self.db, "mdblist_api_key"))

    def test_a_password_shown_as_bullets_is_kept(self):
        self.add_admin()
        mdb.set_setting(self.db, "navidrome_password", "a-long-password-1")
        self.save({"navidrome_password": settings.get_settings(self.db)["navidrome_password"]})
        self.assertEqual("a-long-password-1", mdb.get_setting(self.db, "navidrome_password"))

    def test_connection_tests_take_the_saved_key_for_a_masked_field(self):
        from services.secret_mask import looks_masked
        for shown in ("••••", "••••abcd", "abcd1234...wxyz"):
            self.assertTrue(looks_masked(shown), shown)
        self.assertFalse(looks_masked("eyJhbGciOiJIUzI1NiJ9.real"))
        src = open(settings.__file__, encoding="utf-8").read()
        self.assertNotIn('"..." not in body', src)

    def test_the_older_mask_form_still_means_unchanged(self):
        self.add_admin()
        self.save({"radarr_api_key": "r" * 8 + "..." + "r" * 4})
        self.assertEqual("r" * 32, mdb.get_setting(self.db, "radarr_api_key"))

    def test_signing_secrets_are_not_settable(self):
        self.save({"session_secret": "x", "vod_token_secret": "y", "jellyfin_url": "http://jf:8096"})
        self.assertEqual("s" * 64, mdb.get_setting(self.db, "session_secret"))
        self.assertEqual("v" * 64, mdb.get_setting(self.db, "vod_token_secret"))
        self.assertEqual("http://jf:8096", mdb.get_setting(self.db, "jellyfin_url"))

    def test_proxy_password_is_kept_when_the_masked_form_comes_back(self):
        self.add_admin()
        self.save({"youtube_proxy": "http://someone:••••@gluetun:8889"})  # port changed
        self.assertEqual("http://someone:proxypass@gluetun:8889", mdb.get_setting(self.db, "youtube_proxy"))
        self.save({"youtube_proxy": "http://other:••••@gluetun:8889"})  # another user: taken as typed
        self.assertEqual("http://other:••••@gluetun:8889", mdb.get_setting(self.db, "youtube_proxy"))

    def test_random_values_round_trip(self):
        """Property, through the routes: listing -> Save keeps any stored secret;
        any other value is saved as sent (seed in the message)."""
        self.add_admin()
        for seed in range(1, 41):
            value = _random_secret(random.Random(seed))
            mdb.set_setting(self.db, "radarr_api_key", value)
            self.save({"radarr_api_key": settings.get_settings(self.db)["radarr_api_key"]})
            self.assertEqual(value, mdb.get_setting(self.db, "radarr_api_key"), f"seed {seed}")
            self.save({"radarr_api_key": value + "x"})
            self.assertEqual(value + "x", mdb.get_setting(self.db, "radarr_api_key"), f"seed {seed}")


def _random_secret(rnd):
    alphabet = "abcXYZ0123456789-_.~!@#$%^&*()+=/\\\"' •…"
    return "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 80)))


class MaskProperties(unittest.TestCase):
    def test_mask_never_shows_more_than_four_characters_and_is_recognised(self):
        from services.secret_mask import is_shown_form, mask
        for seed in range(1, 2001):
            value = _random_secret(random.Random(seed))
            shown = mask(value)
            self.assertTrue(shown.startswith("••••"), f"seed {seed}")
            self.assertLessEqual(len(shown) - 4, 4, f"seed {seed}")
            if len(value) <= 8:
                self.assertEqual("••••", shown, f"seed {seed}")
            self.assertTrue(is_shown_form(shown, value), f"seed {seed}")
            self.assertFalse(is_shown_form(value + "x", value), f"seed {seed}")
            self.assertEqual("••••", mask(value, whole=True), f"seed {seed}")
            self.assertTrue(is_shown_form(mask(value, whole=True), value), f"seed {seed}")

    def test_proxy_login_mask_and_restore(self):
        from services.secret_mask import mask_url_login, restore_url_login
        for seed in range(1, 1001):
            rnd = random.Random(seed)
            user = "u" + "".join(rnd.choice("abc123-_.") for _ in range(rnd.randint(1, 10)))
            pw = "p" + "".join(rnd.choice("abc123-_.~!$&'()*+,;=%") for _ in range(rnd.randint(1, 20)))
            url = f"http://{user}:{pw}@gluetun:{rnd.randint(1, 65535)}"
            shown = mask_url_login(url)
            self.assertNotIn(":" + pw + "@", shown, f"seed {seed}")
            self.assertEqual(url, restore_url_login(shown, url), f"seed {seed}")
        self.assertEqual("http://gluetun:8888", mask_url_login("http://gluetun:8888"))
        self.assertEqual("", mask_url_login(""))

if __name__ == "__main__":
    unittest.main()

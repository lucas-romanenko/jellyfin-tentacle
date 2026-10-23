"""A Settings save changes only what the admin changed, even when the settings didn't load.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_settings_save_after_failed_read.py"

loadSettings() fills the Settings fields from GET /api/settings/raw and
swallows any error; saveSettings() posts every text field, checkbox and picker
as the page holds it. When that read failed (Tentacle restarting, a proxy
timeout, a dropped connection) or hadn't answered yet, every field was still
empty, so changing one Pushover key and pressing Save posted
jellyfin_url = "", jellyfin_api_key = "", every Radarr/Sonarr/Lidarr address
and key = "", music_enabled = "false", every root folder and quality profile
= "", and POST /api/settings stored them all. Requests signed with a
Jellyfin access token (the plugin, the TV app) need jellyfin_url to verify
it, so they answered 401 within minutes; sign-in needs it too (POST
/api/auth/login answers 400 "Jellyfin URL not configured"), so once the
admin's session ended nobody could sign in again.

Runs the real loadSettings() and saveSettings() under node, then hands what
the page posted to the real POST /api/settings handler.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import get_setting, set_setting
from tmp_dirs import temp_dir

APP = (Path(__file__).resolve().parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
FIELDS = APP[APP.index("const SETTINGS_TEXT_FIELDS"):APP.index("async function loadSettings(")]


def _fn(name):
    start = APP.index(f"function {name}(")
    if APP[start - 6:start] == "async ":
        start -= 6
    return APP[start:APP.index("\n}\n", start) + 2]


# What an install has stored (no real addresses or keys).
STORED = {
    "jellyfin_url": "http://jellyfin.test:8096", "jellyfin_api_key": "j" * 32,
    "radarr_url": "http://radarr.test:7878", "radarr_api_key": "r" * 32,
    "sonarr_url": "http://sonarr.test:8989", "sonarr_api_key": "s" * 32,
    "lidarr_url": "http://lidarr.test:8686", "lidarr_api_key": "l" * 32,
    "radarr_root_folder": "/movies", "radarr_quality_profile_id": "4",
    "sonarr_root_folder": "/tv", "sonarr_quality_profile_id": "6",
    "music_enabled": "true", "music_last_reconcile": "2026-10-20T03:00:00",
    "pushover_app_token": "a" * 30, "pushover_user_key": "old-user-key",
    "recently_added_days": "14",
}

# One Settings page: load it (the read answers, fails, or hasn't answered
# yet), type into the given fields, press Save. Prints what Save posted.
SCRIPT = """
%(fields)s
const state = {};
let els = {};
for (const k of [...SETTINGS_TEXT_FIELDS, ...SETTINGS_PICKER_FIELDS]) els[k] = { value: '', dataset: {} };
for (const k of SETTINGS_CHECKBOX_FIELDS) els[k] = { checked: false, dataset: {} };
els.sync_schedule_time = { value: '03:00', defaultValue: '03:00', dataset: {} };
const document = { getElementById: id => els[id] || null };
const sc = %(scenario)s;
let posts = [], toasts = [], release = null;
async function api(path, opts) {
  if (opts && opts.method === 'POST') { posts.push(opts.body.settings); return {}; }
  if (path === '/api/settings/raw') {
    if (sc.read === 'fails') throw new Error('Request failed');
    if (sc.read === 'slow') return new Promise(r => { release = () => r(Object.assign({}, sc.stored)); });
    return Object.assign({}, sc.stored);
  }
  throw new Error('unexpected ' + path);
}
const tick = () => new Promise(r => setTimeout(r, 0));
function toast(msg, type) { toasts.push([String(msg), type || '']); }
function loadServicePickers() {}
function applyMusicVisibility() {}
function loadScheduleInfo() {}
function loadConnectionStatus() {}
%(fns)s
(async () => {
  const loading = loadSettings();
  if (sc.read !== 'slow') await loading;
  await tick();
  for (const [k, v] of Object.entries(sc.edits)) els[k].value = v;
  await saveSettings();
  if (release) { release(); await loading; }
  process.stdout.write(JSON.stringify({ posts, toasts }));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SettingsSaveAfterFailedRead(unittest.TestCase):
    maxDiff = None

    def page(self, read, edits):
        script = SCRIPT % {"fields": FIELDS, "fns": "\n".join(_fn(n) for n in ("cronToTime", "timeToCron",
                                                                              "loadSettings", "saveSettings")),
                           "scenario": json.dumps({"read": read, "edits": edits, "stored": STORED})}
        run = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=120)
        if run.returncode:
            raise AssertionError(run.stderr)
        return json.loads(run.stdout)

    def saved(self, read, edits):
        """Stored settings after the page's Save went through the real POST
        /api/settings, and the page's messages."""
        out = self.page(read, edits)
        posts = out["posts"]
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        mdb.seed_defaults(db)
        for k, v in STORED.items():
            set_setting(db, k, v)
        import main
        from routers import settings as r
        with mock.patch("services.smartlists._notify_jellyfin_plugin"), \
                mock.patch.object(main, "reschedule_main_sync"), \
                mock.patch.object(main, "reschedule_music_reconcile"):
            for posted in posts:
                r.update_settings(r.SettingsUpdate(settings=posted), db=db)
        return {k: get_setting(db, k) for k in STORED}, out["toasts"]

    def assertOnlyEdited(self, read):
        """Settings the admin didn't touch keep their stored value. The edit is
        stored, or (while the settings aren't loaded) the page says it wasn't."""
        after, toasts = self.saved(read, {"pushover_user_key": "new-user-key"})
        changed = {k: (STORED[k], after[k]) for k in STORED if k != "pushover_user_key" and after[k] != STORED[k]}
        self.assertEqual({}, changed, "settings the admin never touched were changed (stored, after)")
        if after["pushover_user_key"] != "new-user-key":
            self.assertTrue([m for m, kind in toasts if kind == "error"], f"the edit was dropped silently: {toasts}")
            self.assertFalse([m for m, kind in toasts if m == "Settings saved"], f"said saved: {toasts}")
        return after

    def test_a_save_after_the_settings_failed_to_load_changes_only_the_edited_field(self):
        self.assertOnlyEdited("fails")

    def test_a_save_before_the_settings_finished_loading_changes_only_the_edited_field(self):
        self.assertOnlyEdited("slow")

    def test_a_save_after_a_good_load_changes_only_the_edited_field(self):
        self.assertEqual("new-user-key", self.assertOnlyEdited("ok")["pushover_user_key"])

    def test_a_field_cleared_on_purpose_is_still_cleared(self):
        after, _ = self.saved("ok", {"pushover_user_key": ""})
        self.assertEqual("", after["pushover_user_key"])
        self.assertEqual(STORED["jellyfin_url"], after["jellyfin_url"])


class JellyfinAddressAndKeyCantBeCleared(unittest.TestCase):
    """The server's own belt: whatever a page posts, Jellyfin's address and
    key (sign-in needs both) are never stored empty over a stored one."""

    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        mdb.seed_defaults(self.db)
        set_setting(self.db, "jellyfin_url", STORED["jellyfin_url"])
        set_setting(self.db, "jellyfin_api_key", STORED["jellyfin_api_key"])
        set_setting(self.db, "pushover_user_key", "old-user-key")

    def post(self, settings):
        from fastapi import HTTPException
        from routers import settings as r
        with mock.patch("services.smartlists._notify_jellyfin_plugin"):
            try:
                r.update_settings(r.SettingsUpdate(settings=settings), db=self.db)
            except HTTPException as e:
                return e.status_code
        return 200

    def test_an_empty_address_or_key_is_refused_and_nothing_is_stored(self):
        for key in ("jellyfin_url", "jellyfin_api_key"):
            with self.subTest(key=key):
                self.assertEqual(400, self.post({key: "", "pushover_user_key": "new"}))
                self.assertEqual(STORED[key], get_setting(self.db, key))
                self.assertEqual("old-user-key", get_setting(self.db, "pushover_user_key"))

    def test_a_new_address_is_stored(self):
        self.assertEqual(200, self.post({"jellyfin_url": "http://jf.test:8096"}))
        self.assertEqual("http://jf.test:8096", get_setting(self.db, "jellyfin_url"))

    def test_other_keys_can_still_be_cleared(self):
        self.assertEqual(200, self.post({"pushover_user_key": ""}))
        self.assertEqual("", get_setting(self.db, "pushover_user_key"))


if __name__ == "__main__":
    unittest.main()

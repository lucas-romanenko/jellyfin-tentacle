"""First run: a Jellyfin address that doesn't work must not end the setup wizard (#389).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Step 1 of the wizard saves the Jellyfin address and key without testing them.
Two things then locked a new install out:
  * every settings save with an address and a key set setup_complete, so the
    wizard never came back after step 1 (steps 2-6 were skipped for good);
  * /api/auth/users answered 502 for an address that doesn't answer, and the
    dashboard opens the wizard only on a 400. It said "Check Settings", which
    needs a session, and nobody had signed in yet.
Nothing about who may call what changes: the 400 is only for an install with
no user, whose wizard routes are open anyway (require_admin's bootstrap).

The dashboard half runs the real functions, lifted out of static/js/app.js,
under node with a stub DOM (skipped when node is not installed).
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from models.database import Base, Setting, TentacleUser, get_setting
from routers import auth as auth_router
from routers import settings as settings_router
from tmp_dirs import temp_dir

APP_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "app.js"
SAVED_URL = "http://jellyfin.invalid:9999"


def fresh_db(test):
    engine = create_engine(f"sqlite:///{temp_dir(test)}/t.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    test.addCleanup(engine.dispose)
    test.addCleanup(db.close)
    return db


def put(db, **settings):
    for k, v in settings.items():
        db.add(Setting(key=k, value=v))
    db.commit()


def save(db, **settings):
    settings_router.update_settings(settings_router.SettingsUpdate(settings=settings), db)


def anonymous_request():
    return Request({"type": "http", "method": "GET", "path": "/api/settings", "headers": [],
                    "query_string": b"", "scheme": "http", "server": ("tentacle", 8888)})


FAILURES = {
    "refused": requests.ConnectionError("refused"),
    "timeout": requests.Timeout("timed out"),
    "starting": requests.HTTPError("503 Server Error"),
    "not json": ValueError("Expecting value: line 1 column 1 (char 0)"),
}


class UsersBeforeTheFirstSignIn(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db(self)
        put(self.db, jellyfin_url=SAVED_URL, jellyfin_api_key="k" * 32)

    def users(self, failure):
        with mock.patch.object(auth_router.requests, "get", side_effect=failure):
            with self.assertRaises(HTTPException) as cm:
                auth_router.get_jellyfin_users(self.db)
        return cm.exception

    def test_an_address_that_does_not_answer_reopens_the_wizard(self):
        for name, failure in FAILURES.items():
            with self.subTest(name):
                self.assertEqual(400, self.users(failure).status_code,
                                 "the login screen says 'Check Settings', which nobody can open yet")

    def test_the_reply_names_the_problem_but_not_the_address(self):
        detail = self.users(requests.ConnectionError(f"{SAVED_URL}/Users/Public refused")).detail
        self.assertIn("Could not reach Jellyfin", detail)
        self.assertNotIn("jellyfin.invalid", detail)

    def test_once_someone_has_signed_in_it_stays_a_502(self):
        self.db.add(TentacleUser(jellyfin_user_id="a" * 32, display_name="admin", is_admin=True))
        self.db.commit()
        self.assertEqual(502, self.users(requests.ConnectionError("refused")).status_code)

    def test_no_address_at_all_is_still_the_400_it_was(self):
        self.db.query(Setting).filter(Setting.key == "jellyfin_url").delete()
        self.db.commit()
        with self.assertRaises(HTTPException) as cm:
            auth_router.get_jellyfin_users(self.db)
        self.assertEqual((400, "Jellyfin URL not configured"), (cm.exception.status_code, cm.exception.detail))


class SetupCompleteIsTheWizardsToSet(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db(self)
        put(self.db, setup_complete="false")

    def test_saving_step_one_does_not_finish_setup(self):
        save(self.db, jellyfin_url="http://jellyfin:8096", jellyfin_api_key="k" * 32)
        self.assertEqual("false", get_setting(self.db, "setup_complete"))

    def test_saving_step_two_and_three_does_not_finish_setup(self):
        save(self.db, jellyfin_url="http://jellyfin:8096", jellyfin_api_key="k" * 32)
        save(self.db, jellyfin_user_id="b" * 32, jellyfin_user_name="admin")
        save(self.db, radarr_url="http://radarr:7878", radarr_api_key="r" * 32)
        self.assertEqual("false", get_setting(self.db, "setup_complete"))

    def test_get_started_and_skip_still_finish_it(self):
        save(self.db, tmdb_bearer_token="", setup_complete="true")
        self.assertEqual("true", get_setting(self.db, "setup_complete"))

    def test_an_install_already_set_up_stays_set_up(self):
        save(self.db, setup_complete="true")
        save(self.db, jellyfin_url="http://jellyfin:8096", home_row_limit="25")
        self.assertEqual("true", get_setting(self.db, "setup_complete"))


class SettingsStayClosedAfterTheFirstSignIn(unittest.TestCase):
    """The 400 above is for an empty install only: once a user exists, an
    anonymous caller still gets nothing from the settings routes."""

    def test_an_anonymous_caller_is_refused_once_a_user_exists(self):
        db = fresh_db(self)
        put(db, jellyfin_url=SAVED_URL, jellyfin_api_key="k" * 32)
        db.add(TentacleUser(jellyfin_user_id="a" * 32, display_name="admin", is_admin=True))
        db.commit()
        with self.assertRaises(HTTPException) as cm:
            auth_router.require_admin(anonymous_request(), db)
        self.assertEqual(401, cm.exception.status_code)


HARNESS = r"""
var els = {};
function el(id) {
  if (!els[id]) els[id] = { id: id, style: {}, value: '', innerHTML: '', textContent: '',
                            setAttribute: function () {}, removeAttribute: function () {} };
  return els[id];
}
var document = { getElementById: el, querySelector: function () { return null; },
                 querySelectorAll: function () { return []; } };
var state = { currentUser: { is_admin: true } };
var reply = null;
async function fetch(path) { return reply; }
var raw = {};
async function api(path) { return raw; }
var steps = [];
function setupGoTo(step) { steps.push(step); }
%FUNCS%
function answer(status, body) {
  return { ok: status < 300, status: status, json: async function () { return body; } };
}
async function main() {
  var out = {};
  // 1. no address saved yet: the wizard, with no error shown
  reply = answer(400, { detail: 'Jellyfin URL not configured' });
  await showLoginOverlay();
  out.fresh = { wizard: el('setup-overlay').style.display, login: el('login-overlay').style.display,
                result: el('setup-jellyfin-result').innerHTML };
  // 2. saved address that doesn't answer, nobody signed in: the wizard, with the reason
  els = {};
  reply = answer(400, { detail: 'Could not reach Jellyfin at the saved address. <b>' });
  await showLoginOverlay();
  out.unreachable = { wizard: el('setup-overlay').style.display, result: el('setup-jellyfin-result').innerHTML };
  // 3. someone has signed in already: the login screen, as before
  els = {};
  reply = answer(502, { detail: 'Could not reach Jellyfin' });
  await showLoginOverlay();
  out.withUsers = { wizard: el('setup-overlay').style.display || 'none', login: el('login-overlay').style.display,
                    grid: el('login-user-grid').innerHTML };
  // 4. signed in, wizard not finished, address and key saved: on at step 3
  els = {}; steps = [];
  raw = { setup_complete: 'false', jellyfin_url: 'http://jellyfin:8096', jellyfin_api_key: 'abcd' };
  await checkSetup();
  out.resume = { wizard: el('setup-overlay').style.display, steps: steps, url: el('setup-jellyfin-url').value };
  // 5. signed in, nothing saved yet: step 1
  els = {}; steps = [];
  raw = { setup_complete: 'false' };
  await checkSetup();
  out.noAddress = { wizard: el('setup-overlay').style.display, steps: steps };
  // 6. finished: no wizard
  els = {}; steps = [];
  raw = { setup_complete: 'true', jellyfin_url: 'http://jellyfin:8096', jellyfin_api_key: 'abcd' };
  await checkSetup();
  out.done = { wizard: el('setup-overlay').style.display || 'none', steps: steps };
  process.stdout.write(JSON.stringify(out));
}
main().catch(function (e) { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def _function(src: str, name: str) -> str:
    for head in ("async function %s(" % name, "function %s(" % name):
        if head in src:
            start = src.index(head)
            break
    else:
        raise AssertionError("app.js has no function %s" % name)
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[start:i]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DashboardOpensTheWizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = APP_JS.read_text(encoding="utf-8")
        funcs = "\n".join(_function(src, n) for n in ("showLoginOverlay", "checkSetup", "escHtml"))
        out = subprocess.run(["node", "-e", HARNESS.replace("%FUNCS%", funcs)],
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise AssertionError(out.stderr)
        cls.r = json.loads(out.stdout)

    def test_no_address_yet_opens_the_wizard_without_an_error(self):
        self.assertEqual(("flex", "none", ""), (self.r["fresh"]["wizard"], self.r["fresh"]["login"],
                                                self.r["fresh"]["result"]))

    def test_an_address_that_does_not_answer_opens_the_wizard_with_the_reason(self):
        self.assertEqual("flex", self.r["unreachable"]["wizard"])
        self.assertIn("Could not reach Jellyfin", self.r["unreachable"]["result"])
        self.assertNotIn("<b>", self.r["unreachable"]["result"], "the reason is shown as text")

    def test_with_users_it_is_still_the_login_screen(self):
        self.assertNotEqual("flex", self.r["withUsers"]["wizard"])
        self.assertEqual("flex", self.r["withUsers"]["login"])
        self.assertIn("Cannot connect to Jellyfin", self.r["withUsers"]["grid"])

    def test_a_reload_after_signing_in_goes_on_at_step_three(self):
        self.assertEqual(("flex", [3], "http://jellyfin:8096"),
                         (self.r["resume"]["wizard"], self.r["resume"]["steps"], self.r["resume"]["url"]))

    def test_without_an_address_the_wizard_starts_at_step_one(self):
        self.assertEqual(("flex", []), (self.r["noAddress"]["wizard"], self.r["noAddress"]["steps"]))

    def test_a_finished_setup_shows_no_wizard(self):
        self.assertEqual(("none", []), (self.r["done"]["wizard"], self.r["done"]["steps"]))


if __name__ == "__main__":
    unittest.main()

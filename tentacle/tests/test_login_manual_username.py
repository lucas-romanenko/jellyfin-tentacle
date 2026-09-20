"""The dashboard login must be usable when Jellyfin lists no users.

Run from the tentacle/ directory:  python -m unittest discover -s tests

/api/auth/users is (rightly) fed by Jellyfin's unauthenticated /Users/Public,
which leaves out every account marked "hide this user from login screens" --
Jellyfin's default for new accounts. The picker then said "No users found.
Check Jellyfin connection." and offered nothing else, so such a server could
not sign in to Tentacle at all.

Runs the real functions, lifted out of static/js/app.js, under node with a stub
DOM. Skipped when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "static"
APP_JS = ROOT / "js" / "app.js"
INDEX = ROOT / "index.html"

HARNESS = r"""
var els = {};
function el(id) {
  if (!els[id]) els[id] = { id: id, style: {}, value: '', textContent: '', focus: function () {},
                            classList: { add: function () {}, remove: function () {} } };
  return els[id];
}
var document = { getElementById: el, querySelectorAll: function () { return []; } };
var state = {};
function setTimeout() {}
var logins = [];
async function doLogin(u, p) { logins.push([u, p]); }
%FUNCS%
async function main() {
  var out = {};
  // 1. nothing picked, nothing typed: the old code returned silently here for ever
  loginShowManual();
  out.formShown = el('login-password-form').style.display;
  out.nameShown = el('login-username').style.display !== 'none';
  await submitLogin();
  out.emptyNameLogins = logins.length;
  out.emptyNameError = el('login-error').textContent;
  // 2. typed name + password
  el('login-username').value = '  hidden_admin ';
  el('login-password').value = 'pw';
  await submitLogin();
  out.manual = logins.slice();
  // 3. picking a card afterwards must go back to card mode
  logins.length = 0;
  selectLoginUser({ name: 'Alice', has_password: true }, el('card'));
  out.nameHiddenAfterPick = el('login-username').style.display === 'none';
  el('login-username').value = 'someone_else';
  el('login-password').value = 'pw2';
  await submitLogin();
  out.afterPick = logins.slice();
  process.stdout.write(JSON.stringify(out));
}
main();
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
class ManualUsernameLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = APP_JS.read_text(encoding="utf-8")
        funcs = "\n".join(_function(src, n) for n in
                          ("submitLogin", "loginShowManual", "selectLoginUser", "loginBackToUsers"))
        out = subprocess.run(["node", "-e", HARNESS.replace("%FUNCS%", funcs)],
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise AssertionError(out.stderr)
        cls.r = json.loads(out.stdout)

    def test_a_typed_username_signs_in(self):
        self.assertEqual([["hidden_admin", "pw"]], self.r["manual"])

    def test_the_form_and_the_name_field_are_shown(self):
        self.assertEqual("flex", self.r["formShown"])
        self.assertTrue(self.r["nameShown"])

    def test_an_empty_username_is_refused_with_a_message(self):
        self.assertEqual(0, self.r["emptyNameLogins"])
        self.assertTrue(self.r["emptyNameError"])

    def test_picking_a_card_afterwards_uses_the_card_not_the_stale_text(self):
        self.assertTrue(self.r["nameHiddenAfterPick"])
        self.assertEqual([["Alice", "pw2"]], self.r["afterPick"])


class ManualUsernameMarkup(unittest.TestCase):
    def test_the_page_has_the_field_and_the_link(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('id="login-username"', html)
        self.assertIn("loginShowManual()", html)

    def test_an_empty_user_list_opens_the_manual_form(self):
        src = APP_JS.read_text(encoding="utf-8")
        empty = src[src.index("if (!users.length)"):]
        self.assertIn("loginShowManual()", empty[:empty.index("return;")],
                      "an empty /Users/Public list still dead-ends the login page")


if __name__ == "__main__":
    unittest.main()

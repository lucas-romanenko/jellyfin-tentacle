"""The plugin's early config load must survive the login page.

Run from the tentacle/ directory:  python -m unittest discover -s tests

tentacle-mdblist.js fetched /Tentacle/Config as soon as ApiClient existed. On the
login page there is no token yet, the request answers 401, and the catch cached
{mdblistEnabled: false, tmdbEnabled: false} for good -- jellyfin-web does not
reload the page after sign-in, so ratings stayed off for the whole session.

Runs the real IIFE from the script under node with a fake ApiClient, clock and
fetch. Skipped when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject" / "tentacle-mdblist.js"

HARNESS = r"""
var timers = [], fetches = [], token = null, answer = 401;
var window = { ApiClient: { serverAddress: function () { return 'http://jf'; },
                            accessToken: function () { return token; } } };
var document = { readyState: 'complete', addEventListener: function () {} };
var console = { log: function () {} };
function setTimeout(fn, ms) { timers.push(fn); }
function fetch(url, opts) {
  fetches.push({ url: url, auth: opts.headers.Authorization });
  var code = answer;
  return Promise.resolve({ ok: code === 200, status: code,
                           json: function () { return Promise.resolve({ mdblistEnabled: true, tmdbEnabled: true }); } });
}
function tick() { var t = timers; timers = []; t.forEach(function (f) { f(); }); }
function settle() { return new Promise(function (r) { setImmediate(r); }); }
%IIFE%
(async function () {
  var out = {};
  await settle();
  out.fetchesWhileSignedOut = fetches.length;          // no token: must not even ask
  tick(); tick(); await settle();
  out.fetchesStillSignedOut = fetches.length;
  token = 'abc'; answer = 401;                          // signed in, but the first answer fails
  tick(); await settle(); await settle();
  out.afterFailure = window.TentacleConfig;
  answer = 200; tick(); await settle(); await settle();
  out.afterRetry = window.TentacleConfig;
  out.totalFetches = fetches.length;
  out.sentToken = fetches.length ? fetches[fetches.length - 1].auth : null;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _iife(src: str) -> str:
    start = src.index("// Load TentacleConfig early")
    return src[start:]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ConfigAfterSignIn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "-e", HARNESS.replace("%IIFE%", _iife(JS.read_text(encoding="utf-8")))],
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise AssertionError(out.stderr)
        cls.r = json.loads(out.stdout)

    def test_nothing_is_requested_before_there_is_a_token(self):
        self.assertEqual(0, self.r["fetchesWhileSignedOut"])
        self.assertEqual(0, self.r["fetchesStillSignedOut"], "a request that can only answer 401")

    def test_a_failed_answer_is_a_safe_default_not_the_final_word(self):
        self.assertEqual({"mdblistEnabled": False, "tmdbEnabled": False}, self.r["afterFailure"])
        self.assertEqual({"mdblistEnabled": True, "tmdbEnabled": True}, self.r["afterRetry"],
                         "a failure on the way in switched ratings off for the whole session")

    def test_the_request_carries_the_token(self):
        self.assertEqual('MediaBrowser Token="abc"', self.r["sentToken"])


if __name__ == "__main__":
    unittest.main()

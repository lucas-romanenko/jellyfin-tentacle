"""The plugin's toolbar fetch sends the access token in a header (#149).

Run from the tentacle/ directory:  python -m unittest discover -s tests

tentacle-navbar.js put ?api_key=<the user's access token> in the URL on every
page load, where reverse-proxy/CDN access logs and browser history keep it.
Runs the real fetchToolbarConfig() under node with a fake ApiClient and fetch.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

NAVBAR = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject" / "tentacle-navbar.js"


def _fetch_toolbar_source():
    src = NAVBAR.read_text(encoding="utf-8")
    start = src.index("fetchToolbarConfig: function () {")
    end = src.index("\n        },\n", start) + len("\n        }")
    return src[start + len("fetchToolbarConfig: "):end]


SCRIPT = """
const console = { log() {}, warn() {} };
const calls = [];
const window = { ApiClient: {
  getCurrentUserId: () => 'u 1',
  serverAddress: () => 'https://jf.example',
  accessToken: () => 'SECRET-TOKEN',
}};
function fetch(url, opts) { calls.push({url, headers: (opts || {}).headers || {}});
  return Promise.resolve({ ok: true, json: () => Promise.resolve({buttons: [{id: 'search', enabled: true}]}) }); }
const self = { toolbarConfig: null };
const fn = %s;
fn.call(self).then(() => process.stdout.write(JSON.stringify({calls, config: self.toolbarConfig})));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ToolbarTokenInHeader(unittest.TestCase):
    def setUp(self):
        run = subprocess.run(["node", "-e", SCRIPT % _fetch_toolbar_source()],
                             capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        self.out = json.loads(run.stdout)

    def test_the_token_is_not_in_the_url(self):
        url = self.out["calls"][0]["url"]
        self.assertNotIn("SECRET-TOKEN", url)
        self.assertNotIn("api_key", url)
        self.assertEqual("https://jf.example/TentacleHome/Toolbar?userId=u%201", url)

    def test_it_is_in_the_authorization_header(self):
        self.assertEqual('MediaBrowser Token="SECRET-TOKEN"', self.out["calls"][0]["headers"]["Authorization"])

    def test_the_config_still_loads(self):
        self.assertEqual([{"id": "search", "enabled": True}], self.out["config"])


if __name__ == "__main__":
    unittest.main()

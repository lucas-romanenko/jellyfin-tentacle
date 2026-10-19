"""Live TV → Jellyfin setup never hands out a localhost URL (#291), and asks
for no address of its own.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Until an admin typed and saved a Server Address, the setup card showed and
copied http://localhost:8888 as the tuner and guide URL. In a Docker install
Jellyfin runs in another container, so that tuner points at Jellyfin itself.
Tentacle's address is now set once, in Settings → Services; the setup card
shows the tuner and guide addresses made from it: the saved one, else the one
Tentacle works out for itself (said to be unsaved), else an older Live TV
address, else this browser's address unless that is localhost. Nothing is
saved from here. Runs the real fillSetupUrls() under node.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGES = (Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js").read_text(encoding="utf-8")


def _fn(name):
    start = PAGES.index(f"function {name}(")
    if PAGES[start - 6:start] == "async ":
        start -= 6
    return PAGES[start:PAGES.index("\n}\n", start) + 2]


SCRIPT = """
const els = {};
function el(id) { return els[id] || (els[id] = { id, value: '', textContent: '', style: {} }); }
const document = { getElementById: el, activeElement: null };
const location = %(location)s;
const liveState = {};
const calls = [];
const saved = [];
async function api(path, opts) {
  calls.push(path);
  if (opts && opts.method === 'POST') { saved.push(opts.body); return {}; }
  if (path === '/api/settings/raw') return %(settings)s;
  if (path === '/api/youtube/status') { if (%(yt)s === null) throw new Error('down'); return %(yt)s; }
  throw new Error('unexpected ' + path);
}
%(fns)s
fillSetupUrls().then(() => {
  process.stdout.write(JSON.stringify({
    shown: el('live-setup-address-display').textContent,
    tuner: el('live-setup-tuner-preview').textContent,
    xmltv: el('live-setup-xmltv-preview').textContent,
    hint: el('live-setup-detected').textContent,
    limit: el('livetv_max_concurrent_streams').value,
    calls, saved }));
});
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SetupAddress(unittest.TestCase):
    def _run(self, settings, yt, location=None):
        fns = "\n".join(_fn(n) for n in ("fillSetupUrls", "getSetupBase", "updateSetupUrls"))
        loc = location or {"hostname": "localhost", "host": "localhost:8888", "protocol": "http:"}
        script = SCRIPT % {"settings": json.dumps(settings), "yt": json.dumps(yt),
                           "location": json.dumps(loc), "fns": fns}
        run = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual([], out["saved"], "the setup card saves nothing")
        return out

    def test_the_saved_tentacle_address_is_used(self):
        out = self._run({}, {"base_url": "http://192.0.2.10:8888/", "detected": {"url": "http://192.0.2.99:8888"}})
        self.assertEqual("http://192.0.2.10:8888", out["tuner"])
        self.assertEqual("http://192.0.2.10:8888/hdhr/xmltv.xml", out["xmltv"])
        self.assertEqual("", out["hint"])

    def test_a_worked_out_address_is_shown_as_not_saved(self):
        out = self._run({}, {"base_url": "", "detected": {"url": "http://192.0.2.10:8888"}})
        self.assertEqual("http://192.0.2.10:8888", out["tuner"])
        self.assertIn("not saved yet", out["hint"])

    def test_an_older_live_tv_address_still_works(self):
        out = self._run({"live_setup_host": "10.0.0.5", "live_setup_port": "9999"}, {"base_url": "", "detected": None})
        self.assertEqual("http://10.0.0.5:9999", out["tuner"])

    def test_without_anything_the_browser_address_is_used_not_localhost(self):
        out = self._run({}, None, {"hostname": "tentacle.lan", "host": "tentacle.lan:8888", "protocol": "http:"})
        self.assertEqual("http://tentacle.lan:8888", out["tuner"])
        self.assertIn("From this browser", out["hint"])

    def test_never_localhost(self):
        out = self._run({}, {"base_url": "", "detected": {"url": None}})
        self.assertNotIn("localhost", out["tuner"] + out["xmltv"])
        self.assertEqual("Not set", out["shown"])
        self.assertIn("Settings → Services", out["hint"])

    def test_the_connection_limit_is_shown(self):
        out = self._run({"livetv_max_concurrent_streams": "2"}, {"base_url": "http://192.0.2.10:8888"})
        self.assertEqual("2", out["limit"])


if __name__ == "__main__":
    unittest.main()

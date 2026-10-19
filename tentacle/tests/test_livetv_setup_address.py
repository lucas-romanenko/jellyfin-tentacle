"""Live TV → Jellyfin Setup never hands out a localhost URL (#291).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Until an admin typed and saved a Server Address, the setup card showed and
copied http://localhost:8888 as the tuner and guide URL. In a Docker install
Jellyfin runs in another container, so that tuner points at Jellyfin itself.
The field is now prefilled (not saved) with the address the YouTube page
detects for Jellyfin, or the address the browser uses. Runs the real
fillSetupUrls() and getSetupBase() under node.
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
el('live-setup-port').value = '8888';
const document = { getElementById: el };
const location = %(location)s;
const calls = [];
const saved = [];
async function api(path, opts) {
  calls.push(path);
  if (opts && opts.method === 'POST') { saved.push(opts.body); return {}; }
  if (path === '/api/settings/raw') return %(settings)s;
  if (path === '/api/youtube/status') { if (%(yt)s === null) throw new Error('down'); return %(yt)s; }
  throw new Error('unexpected ' + path);
}
let locked = false;
function showSetupLocked() { locked = true; updateSetupUrls(); }
%(fns)s
fillSetupUrls().then(() => {
  process.stdout.write(JSON.stringify({
    host: el('live-setup-host').value, port: el('live-setup-port').value,
    tuner: el('live-setup-tuner-preview').textContent,
    xmltv: el('live-setup-xmltv-preview').textContent,
    hint: el('live-setup-detected').textContent,
    locked, calls, saved }));
});
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SetupAddress(unittest.TestCase):
    def _run(self, settings, yt, location=None):
        fns = "\n".join(_fn(n) for n in ("fillSetupUrls", "getSetupBase", "updateSetupUrls"))
        loc = location or {"hostname": "localhost", "port": "8888", "href": "http://localhost:8888/"}
        script = SCRIPT % {"settings": json.dumps(settings), "yt": json.dumps(yt),
                           "location": json.dumps(loc), "fns": fns}
        run = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        return json.loads(run.stdout)

    def test_fresh_install_prefills_the_address_jellyfin_reaches(self):
        out = self._run({}, {"suggested_base_url": "http://192.0.2.10:8888"})
        self.assertEqual("http://192.0.2.10:8888", out["tuner"])
        self.assertEqual("http://192.0.2.10:8888/hdhr/xmltv.xml", out["xmltv"])
        self.assertNotIn("localhost", out["tuner"])
        self.assertTrue(out["hint"], "the prefill is marked as detected")
        self.assertEqual([], out["saved"], "a prefill is never saved")
        self.assertFalse(out["locked"])

    def test_without_a_detection_the_browser_address_is_used_not_localhost(self):
        out = self._run({}, None, {"hostname": "tentacle.lan", "port": "", "href": "http://tentacle.lan/"})
        self.assertEqual("http://tentacle.lan:8888", out["tuner"])

    def test_a_saved_host_still_wins_and_nothing_is_detected(self):
        out = self._run({"live_setup_host": "10.0.0.5", "live_setup_port": "9999"},
                        {"suggested_base_url": "http://192.0.2.10:8888"})
        self.assertEqual("http://10.0.0.5:9999", out["tuner"])
        self.assertNotIn("/api/youtube/status", out["calls"])
        self.assertTrue(out["locked"])

    def test_a_saved_youtube_address_that_does_not_answer_is_not_prefilled(self):
        # The YouTube page keeps showing its saved address even when it doesn't
        # answer; the setup card must offer the one that does.
        out = self._run({}, {"base_url": "http://192.0.2.99:8888", "suggested_base_url": "http://192.0.2.99:8888",
                             "reachable": {"ok": False}, "detected": {"url": "http://192.0.2.10:8888"}})
        self.assertEqual("http://192.0.2.10:8888", out["tuner"])
        self.assertEqual("Detected: check it, then Save", out["hint"])

    def test_a_dead_youtube_address_and_nothing_detected_falls_back_to_the_browser(self):
        out = self._run({}, {"base_url": "http://192.0.2.99:8888", "suggested_base_url": "http://192.0.2.99:8888",
                             "reachable": {"ok": False}, "detected": {"url": None}},
                        {"hostname": "tentacle.lan", "port": "", "href": "http://tentacle.lan/"})
        self.assertEqual("http://tentacle.lan:8888", out["tuner"])
        self.assertEqual("From this browser: check it, then Save", out["hint"])

    def test_a_saved_youtube_address_that_answers_is_still_used(self):
        out = self._run({}, {"base_url": "http://192.0.2.10:8888", "suggested_base_url": "http://192.0.2.10:8888",
                             "reachable": {"ok": True}, "detected": None})
        self.assertEqual("http://192.0.2.10:8888", out["tuner"])


if __name__ == "__main__":
    unittest.main()

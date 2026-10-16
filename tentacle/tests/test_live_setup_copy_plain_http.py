"""Live TV > Jellyfin Setup: the "Copy" buttons for the tuner and guide URLs
work, or say why not, on a dashboard opened over plain HTTP.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Browsers only offer navigator.clipboard on HTTPS or localhost. A dashboard on
a LAN address over http:// has none, and copyLiveSetup() called
navigator.clipboard.writeText() unguarded: a click threw "Cannot read
properties of undefined (reading 'writeText')" (seen in a headless Chromium
run against a test install), nothing was copied and nothing was shown. Every
other Copy button in the dashboard (webhooks, plugin URL, music webhook)
catches this and says "Copy failed - use HTTPS or copy manually". Runs the
real copyLiveSetup() from pages.js under node.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js"


def _source():
    src = PAGES.read_text(encoding="utf-8")
    start = src.index("function copyLiveSetup(")
    if src[start - 6:start] == "async ":
        start -= 6
    end = src.index("\n}\n", start) + 2
    return src[start:end]


SCRIPT = """
const toasts = [];
function toast(msg, kind) { toasts.push([msg, kind || 'success']); }
function getSetupBase() { return 'http://tentacle.lan:8888'; }
let navigator = %s;
%s
(async () => {
  const btn = { textContent: 'Copy' };
  let threw = null;
  process.on('unhandledRejection', e => { threw = String(e); });
  try { await copyLiveSetup('tuner', btn); } catch (e) { threw = String(e); }
  await new Promise(r => setTimeout(r, 20));
  process.stdout.write(JSON.stringify({ threw, toasts, btn: btn.textContent, copied: globalThis.copied || null }));
  process.exit(0);
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class LiveSetupCopy(unittest.TestCase):
    def _run(self, navigator_js):
        run = subprocess.run(["node", "-e", SCRIPT % (navigator_js, _source())],
                             capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        return json.loads(run.stdout)

    def test_plain_http_no_clipboard_says_so_instead_of_throwing(self):
        out = self._run("{}")
        self.assertIsNone(out["threw"], out)
        self.assertTrue(any(kind == "error" for _, kind in out["toasts"]), out)

    def test_a_refused_clipboard_says_so(self):
        out = self._run("{ clipboard: { writeText: () => Promise.reject(new Error('denied')) } }")
        self.assertIsNone(out["threw"], out)
        self.assertTrue(any(kind == "error" for _, kind in out["toasts"]), out)

    def test_a_working_clipboard_copies_the_tuner_url(self):
        out = self._run("{ clipboard: { writeText: (t) => { globalThis.copied = t; return Promise.resolve(); } } }")
        self.assertIsNone(out["threw"], out)
        self.assertEqual("http://tentacle.lan:8888", out["copied"])
        self.assertEqual("Copied!", out["btn"])


if __name__ == "__main__":
    unittest.main()

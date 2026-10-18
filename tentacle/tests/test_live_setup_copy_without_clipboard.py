"""Live TV → Jellyfin Setup: the Copy buttons say so when the browser can't copy (#653).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Browsers give navigator.clipboard only in a secure context (HTTPS or
localhost). On a dashboard opened over http://<lan-address>:8888 it is
undefined, so copyLiveSetup() threw a TypeError and the tuner and guide Copy
buttons did nothing, without a word. A refused write (permission denied) was
an unhandled rejection, also silent. Like every other Copy button it now
shows the "Copy failed" toast. Runs the real copyLiveSetup() under node.
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
el('live-setup-host').value = '192.0.2.10';
el('live-setup-port').value = '8888';
const document = { getElementById: el };
const location = { hostname: '192.0.2.10', port: '8888', href: 'http://192.0.2.10:8888/' };
const copied = [];
const navigator = %(navigator)s;
const toasts = [];
function toast(msg, type) { toasts.push([msg, type || 'success']); }
const errors = [];
process.on('unhandledRejection', e => errors.push(String(e)));
%(fns)s
const btn = { textContent: 'Copy' };
let thrown = null;
let ret;
try { ret = copyLiveSetup(%(type)s, btn); } catch (e) { thrown = String(e); }
Promise.resolve(ret).catch(e => { thrown = String(e); }).then(() => setTimeout(() => {
  process.stdout.write(JSON.stringify({ thrown, errors, toasts, copied, label: btn.textContent }));
  process.exit(0);
}, 50));
"""

NO_CLIPBOARD = "{}"
REFUSED = "{ clipboard: { writeText: async () => { throw new Error('NotAllowedError'); } } }"
WORKING = "{ clipboard: { writeText: async t => { copied.push(t); } } }"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class LiveSetupCopy(unittest.TestCase):
    def _run(self, navigator, type_="tuner"):
        fns = "\n".join(_fn(n) for n in ("getSetupBase", "copyLiveSetup"))
        script = SCRIPT % {"navigator": navigator, "fns": fns, "type": json.dumps(type_)}
        run = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        return json.loads(run.stdout)

    def _assert_says_copy_failed(self, out):
        self.assertIsNone(out["thrown"], "the click must not throw")
        self.assertEqual([], out["errors"], "no unhandled rejection")
        self.assertEqual(1, len(out["toasts"]), out["toasts"])
        msg, kind = out["toasts"][0]
        self.assertIn("Copy failed", msg)
        self.assertEqual("error", kind)
        self.assertEqual("Copy", out["label"], "the button doesn't claim it copied")

    def test_plain_http_without_a_clipboard_says_copy_failed(self):
        self._assert_says_copy_failed(self._run(NO_CLIPBOARD))
        self._assert_says_copy_failed(self._run(NO_CLIPBOARD, "xmltv"))

    def test_a_refused_clipboard_says_copy_failed(self):
        self._assert_says_copy_failed(self._run(REFUSED))

    def test_a_working_clipboard_copies_and_the_button_says_copied(self):
        out = self._run(WORKING)
        self.assertEqual(["http://192.0.2.10:8888"], out["copied"])
        self.assertEqual("Copied!", out["label"])
        self.assertIsNone(out["thrown"])
        self.assertEqual([], out["errors"])
        out = self._run(WORKING, "xmltv")
        self.assertEqual(["http://192.0.2.10:8888/hdhr/xmltv.xml"], out["copied"])


if __name__ == "__main__":
    unittest.main()

"""toast(msg, 'warning') must not print "undefined" (#146).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The icon map had no `warning` entry, so `${icons[type]}` rendered the literal
text "undefined" in front of every warning ("Saved but Jellyfin plugin didn't
respond", the row-shape warning, a failed playlist sync). Runs the real
toast() from app.js under node with a minimal DOM.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "static" / "js" / "app.js"


def _toast_source():
    src = APP.read_text(encoding="utf-8")
    start = src.index("function toast(")
    end = src.index("\n}\n", start) + 2
    return src[start:end]


def _render(kind):
    script = """
    const made = [];
    const document = {
      createElement() { const el = { className: '', innerHTML: '', remove() {} }; made.push(el); return el; },
      getElementById() { return { appendChild() {} }; },
    };
    const setTimeout = () => {};
    %s
    toast('Saved but check the plugin', %s, 0);
    process.stdout.write(JSON.stringify(made[0]));
    """ % (_toast_source(), json.dumps(kind))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ToastWarning(unittest.TestCase):
    def test_a_warning_has_an_icon_not_undefined(self):
        el = _render("warning")
        self.assertNotIn("undefined", el["innerHTML"])
        self.assertIn("⚠", el["innerHTML"])
        self.assertIn("var(--amber)", el["innerHTML"])
        self.assertEqual("toast warning", el["className"])

    def test_the_other_kinds_are_unchanged(self):
        self.assertIn("✓", _render("success")["innerHTML"])
        self.assertIn("var(--red)", _render("error")["innerHTML"])
        self.assertIn("var(--blue)", _render("info")["innerHTML"])
        self.assertIn("toast-spinner", _render("loading")["innerHTML"])

    def test_an_unknown_kind_prints_no_undefined(self):
        self.assertNotIn("undefined", _render("notice")["innerHTML"])

    def test_every_kind_used_has_a_border_style(self):
        html = (APP.parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r"\.toast\.warning\s*\{")


if __name__ == "__main__":
    unittest.main()

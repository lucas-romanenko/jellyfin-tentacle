"""The first-sign-in welcome shows names and addresses as text (welcome.js).

Run from the tentacle/ directory:  python -m unittest tests.test_dashboard_welcome_text

The person's and the admin's names, row names and the household's addresses
are typed by people: every value interpolated into markup in welcome.js
goes through escHtml. Read from the source (no node needed).
"""
import re
import unittest
from pathlib import Path

WELCOME = (Path(__file__).resolve().parents[1] / "static" / "js" / "welcome.js").read_text(encoding="utf-8")


class WelcomeTextIsEscaped(unittest.TestCase):
    def test_every_interpolated_value_is_escaped(self):
        raw = []
        for m in re.finditer(r"\$\{([^{}]+)\}", WELCOME):
            expr = m.group(1).strip()
            if expr.startswith("escHtml(") or expr in ("i + 1", "p.on", "!!c.favourite"):
                continue
            # A choice between fixed words, or between markup built with escHtml.
            if re.fullmatch(r".+ \? '[^'$]*' : '[^'$]*'", expr, re.S):
                continue
            raw.append(expr)
        # The title is set with textContent; the rest builds markup from escaped parts.
        allowed = {"d.name", "d.home_rows.length ? ':' : '.'",
                   # The counts: numbers and fixed words, put together
                   # (_welcomeCounts).
                   "_welcomeCounts(d)", "Number(v).toLocaleString()", "v === 1 ? one : many",
                   "parts.slice(0, -1).join(', ')", "parts[parts.length - 1]", "list"}
        self.assertEqual([], sorted(e for e in set(raw) - allowed if "escHtml(" not in e))

    def test_names_and_addresses_go_through_escHtml(self):
        for needle in ("escHtml(d.server)", "escHtml(d.admin)", "escHtml(r)", "escHtml(d.jellyfin_address)",
                       "escHtml(d.tv_app_address)", "escHtml(d.requests.approver)", "escHtml(label)",
                       "escHtml(c.name)", "escHtml(c.key)"):
            with self.subTest(needle=needle):
                self.assertIn(needle, WELCOME)

    def test_the_title_is_text(self):
        self.assertIn("getElementById('welcome-title').textContent = title", WELCOME)


if __name__ == "__main__":
    unittest.main()

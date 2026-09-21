"""The dashboard login overlay must be usable on a small screen and by keyboard.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Source-level, in the style of tests/test_frontend_state.py; each of these was
measured in a real browser first:
  * `.login-overlay` is a fixed, centred flex column with no overflow rule, so
    it cannot scroll -- at 375x667 with six users the Back link was off screen,
    with eight the "Sign in" button was unreachable.
  * the dashboard underneath stayed focusable: 20 Tab stops through covered
    controls before the first user card, and Space/Enter acted on them.
  * "<- Back" is an <a> with no href and no tabindex, so Tab skipped it.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "static"
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "js" / "app.js").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", HTML)
    assert m, "no CSS rule for %s" % selector
    return re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)


class LoginOverlay(unittest.TestCase):
    def test_the_overlay_can_scroll(self):
        self.assertRegex(_rule(".login-overlay"), r"overflow(-y)?\s*:\s*auto")

    def test_an_over_tall_column_keeps_its_top_on_screen(self):
        """Plain `center` pushes the top of an overflowing column above the
        scrollable area, where no scrolling can reach it."""
        self.assertRegex(_rule(".login-overlay"), r"justify-content\s*:\s*safe\s+center")

    def test_the_dashboard_underneath_is_inert_while_the_overlay_is_up(self):
        fn = APP[APP.index("async function showLoginOverlay"):APP.index("function selectLoginUser")]
        self.assertRegex(fn, r"setAttribute\(\s*'inert'")
        self.assertRegex(fn, r"removeAttribute\(\s*'inert'\s*\)",
                         "the setup wizard takes over from the overlay and must get the page back")

    def test_the_overlay_itself_is_not_inside_what_is_made_inert(self):
        lines = HTML.split("\n")
        start = next(i for i, l in enumerate(lines) if '<div class="app">' in l)
        depth = 0
        for i in range(start, len(lines)):
            line = re.sub(r"<!--.*?-->", "", lines[i])
            depth += len(re.findall(r"<div\b", line)) - len(re.findall(r"</div>", line))
            if depth == 0:
                end = i
                break
        overlay = next(i for i, l in enumerate(lines) if 'id="login-overlay"' in l)
        self.assertGreater(overlay, end, "the login overlay would be made inert along with the app")

    def test_back_can_be_reached_and_pressed_from_the_keyboard(self):
        back = re.search(r"<a[^>]*loginBackToUsers\(\)[^>]*>", HTML).group(0)
        self.assertIn('tabindex="0"', back)
        self.assertIn("onkeydown", back)


if __name__ == "__main__":
    unittest.main()

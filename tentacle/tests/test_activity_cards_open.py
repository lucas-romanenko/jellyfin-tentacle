"""Dashboard Activity cards open the title they show (#143).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Every card on the Activity tab (Downloading, Searching, Recently Downloaded,
Coming up, Upcoming) had a hover border but no handler, so clicking did
nothing. The behaviour is checked in a browser; this keeps every card
template wired to the one delegated listener.
"""
import re
import unittest
from pathlib import Path

PAGES = (Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js").read_text(encoding="utf-8")


class ActivityCardsOpen(unittest.TestCase):
    def setUp(self):
        start = PAGES.index("function renderActivity(data) {")
        self.render = PAGES[start:PAGES.index("// ── DISCOVER PAGE", start)]

    def test_every_card_is_openable(self):
        cards = re.findall(r'<div class="activity-card"([^>]*)>', self.render)
        self.assertEqual(5, len(cards), "one per section")
        for attrs in cards:
            self.assertRegex(attrs, r"\$\{_actOpenAttrs\((dl|item)\)\}")

    def test_the_listener_is_bound_and_keyboard_works(self):
        self.assertIn("_bindActivityCards(content);", self.render)
        attrs = PAGES[PAGES.index("function _actOpenAttrs("):PAGES.index("function _activityOpen(")]
        self.assertIn('role="button" tabindex="0"', attrs)
        bind = PAGES[PAGES.index("function _bindActivityCards("):PAGES.index("function renderActivity(")]
        self.assertIn("'keydown'", bind)
        self.assertIn("e.target.closest('button, a, input, select, textarea')", bind,
                      "a card's own buttons must not also open the detail")


if __name__ == "__main__":
    unittest.main()

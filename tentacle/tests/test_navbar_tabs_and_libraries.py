"""Tentacle's header and Jellyfin's page tabs, and the libraries button on touch.

- Live TV (and Music) keep Jellyfin's tab bar, which Jellyfin puts at the very
  top: Tentacle's user/back buttons and pill sat on top of "Programs / Guide /
  Channels ...". The JS marks the page (`tentacle-page-tabs`) and the CSS moves
  the bar, and the page under it, below the header.
- The libraries list is a fixed element on <body>. On a phone it was never
  given a top/left, so it opened at its place at the end of the page, far
  below the screen: the first tap seemed to do nothing. On a touch screen
  wider than a phone, the tap's emulated mouseenter opened it and the click
  right after closed it again.
- Jellyfin's guide cuts channel numbers at 30% of the cell ("414..."); the
  provider's 6-7 digit numbers are the channel keys, so the CSS makes room.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

INJECT = Path("../tentacle-plugin/Inject")
NAVBAR_JS = INJECT / "tentacle-navbar.js"
NAVBAR_CSS = INJECT / "tentacle-navbar.css"
LIVETV_CSS = INJECT / "tentacle-livetv.css"


def method(src: str, name: str) -> str:
    start = src.index(f"        {name}: function")
    end = src.index("\n        },\n", start)
    return src[start:end]


def rule(css: str, selector: str) -> str:
    start = css.index(selector + " {")
    return css[start:css.index("}", start)]


class TestPageTabsBelowHeader(unittest.TestCase):
    def setUp(self):
        self.js = method(NAVBAR_JS.read_text(), "_showPageTabs")
        self.css = NAVBAR_CSS.read_text()

    def test_the_page_is_marked_only_while_its_tabs_show(self):
        remove = self.js.index("classList.remove('tentacle-page-tabs')")
        add = self.js.index("classList.add('tentacle-page-tabs')")
        self.assertLess(remove, self.js.index("if (!needsTabs) return;"),
                        "the mark must go before the early return, or it stays on "
                        "after leaving Live TV")
        self.assertGreater(add, self.js.index("data-tentacle-tab-ancestor', '')"))

    def test_the_tab_bar_moves_below_the_header(self):
        r = rule(self.css, "body.moonfin-navbar-active.tentacle-page-tabs .skinHeader")
        self.assertIn("top: var(--tentacle-tabs-top)", r)

    def test_the_page_moves_below_the_tab_bar(self):
        for sel in ("body.moonfin-navbar-active.tentacle-page-tabs .libraryPage.withTabs",
                    "body.moonfin-navbar-active.tentacle-page-tabs .pageWithAbsoluteTabs .absolutePageTabContent"):
            r = rule(self.css, sel)
            self.assertIn("var(--tentacle-tabs-top) + var(--tentacle-tabs-height)", r, sel)

    def test_phones_have_their_own_offset(self):
        mobile = self.css[self.css.index("@media (max-width: 768px)"):]
        self.assertRegex(mobile, r"tentacle-page-tabs \{\s*--tentacle-tabs-top: \d+px;")


class TestLibrariesOnTouch(unittest.TestCase):
    def setUp(self):
        self.src = NAVBAR_JS.read_text()

    def test_the_list_is_positioned_on_phones_too(self):
        pos = method(self.src, "positionLibrariesDropdown")
        self.assertNotIn("isMobile()", pos,
                         "on a phone the list keeps no top/left and opens off-screen")
        self.assertIn("window.innerWidth", pos, "the list must stay inside the screen")

    def test_hover_listens_to_a_real_mouse_only(self):
        listeners = method(self.src, "setupEventListeners")
        self.assertNotRegex(listeners, r"addEventListener\('mouse(enter|leave)'",
                            "a tap sends mouseenter too, which opened the list "
                            "just before the click closed it")
        self.assertEqual(len(re.findall(r"e\.pointerType === 'mouse'", listeners)), 4)

    def test_a_click_keeps_a_list_hover_opened(self):
        toggle = method(self.src, "toggleLibraries")
        self.assertIn("this._librariesHoverOpened", toggle)
        self.assertLess(toggle.index("return;"),
                        toggle.index("this.librariesExpanded = !this.librariesExpanded"))

    def test_a_tap_elsewhere_closes_the_list(self):
        listeners = method(self.src, "setupEventListeners")
        self.assertIn("document.addEventListener('pointerdown'", listeners)

    def test_phone_buttons_do_not_grow_a_label_on_focus(self):
        css = NAVBAR_CSS.read_text()
        mobile = css[css.index("@media (max-width: 768px)"):]
        self.assertIn(".moonfin-expandable-btn:not(.active):focus .moonfin-expand-label", mobile)


class TestGuideChannelNumbers(unittest.TestCase):
    def test_numbers_get_the_room_left_of_the_logo(self):
        r = rule(LIVETV_CSS.read_text(), ".guide-channelHeaderCell .guideChannelNumber")
        self.assertIn("max-width: calc(100% -", r)
        self.assertIn("text-overflow: clip", r)


if __name__ == "__main__":
    unittest.main()

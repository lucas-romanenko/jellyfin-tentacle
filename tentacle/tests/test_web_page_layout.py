"""Jellyfin web pages in Tentacle's look: things that once looked broken.

* Search: the filters were drawn at a fixed 140 px from the top while
  Jellyfin's search box sits wherever its page puts it, so at ~1400 px the
  filters covered the box (and on phones the "Search Results" title sat on
  it). The results now start under the box's real position.
* Details: the overlay's own back button sat under the header's logo
  (PC) and avatar (phone) while CSS hid the header's: a grey circle behind
  the logo, no reachable Back.
* Settings: the navbar's list of user pages had route names Jellyfin 10.11
  doesn't use, so Display, Playback, Subtitles, Home, Controls, Profile and
  Quick Connect fell back to Jellyfin's header with its hamburger.
* Phones: with the open page's label the header's pill ran off the screen.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p test_web_page_layout.py
"""
import re
import unittest
from pathlib import Path

INJECT = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject"


def read(name: str) -> str:
    return (INJECT / name).read_text(encoding="utf-8")


def rule(css: str, selector: str) -> str:
    """The body of the first rule whose selector list is exactly `selector`."""
    m = re.search(r"(?:^|[}/])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.M)
    return m.group(1) if m else ""


class SearchSitsUnderItsBox(unittest.TestCase):
    def test_no_fixed_offset_for_the_results(self):
        body = rule(read("tentacle-search.css"), ".tentacle-search-section")
        self.assertTrue(body, "results container rule not found")
        self.assertNotIn("padding-top", body,
                         "a fixed offset puts the filters on top of the search box at some widths")

    def test_the_script_places_the_results_under_the_box(self):
        js = read("tentacle-search.js")
        self.assertIn("function placeResults()", js)
        self.assertIn("getBoundingClientRect()", js)
        self.assertIn("addEventListener('resize', placeResults)", js)
        self.assertIn("removeEventListener('resize', placeResults)", js, "left behind after the page")

    def test_no_title_beside_the_box(self):
        self.assertNotIn("Search Results", read("tentacle-search.js"))


class DetailsBackIsTheHeaders(unittest.TestCase):
    def test_the_header_back_is_not_hidden_on_details(self):
        css = read("tentacle-details.css")
        self.assertNotRegex(css, r"moonfin-details-visible\s+\.moonfin-nav-back\s*\{[^}]*display:\s*none")

    def test_the_overlay_back_steps_aside_for_the_header(self):
        body = rule(read("tentacle-details.css"), "body.moonfin-navbar-active .moonfin-details-back")
        self.assertIn("display: none", body)

    def test_the_header_back_goes_back_through_details(self):
        self.assertIn("Details.goBack()", read("tentacle-navbar.js"))


class SettingsPagesKeepTheHeader(unittest.TestCase):
    def test_jellyfin_10_11_user_menu_routes(self):
        js = read("tentacle-navbar.js")
        routes = js[js.index("var USER_ROUTES"):js.index("];", js.index("var USER_ROUTES"))]
        for r in ("userprofile", "quickconnect", "mypreferencesdisplay", "mypreferenceshome",
                  "mypreferencesplayback", "mypreferencessubtitles", "mypreferencescontrols"):
            self.assertIn("'" + r + "'", routes, r)


class PhoneHeaderFits(unittest.TestCase):
    def test_no_labels_on_phones(self):
        css = read("tentacle-navbar.css")
        m = re.search(r"@media \(max-width: 480px\) \{(.*?)\n\}", css, re.S)
        self.assertTrue(m, "no phone rule for the header")
        self.assertIn(".moonfin-expandable-btn.active .moonfin-expand-label", m.group(1))
        self.assertIn("max-width: 0", m.group(1))


class BadgeOnTheIcon(unittest.TestCase):
    def test_badge_follows_the_icon_not_the_label(self):
        js = read("tentacle-navbar.js")
        build = js[js.index("_buildButtonHtml: function"):]
        build = build[:build.index("\n        },")]
        self.assertLess(build.rindex("def.extra"), build.rindex("moonfin-expand-label"),
                        "the badge must come right after the icon, before the label")


if __name__ == "__main__":
    unittest.main()

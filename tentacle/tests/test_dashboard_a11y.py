"""#281: dashboard accessibility basics (WCAG 2.2 AA, as axe-core checks them).

- Pinch-zoom is allowed (no user-scalable=no / maximum-scale in the viewport);
  16 px inputs on every touch screen keep focusing a field from zooming.
- Every Live TV on/off switch has a name and a pressed state, and the hidden
  checkboxes behind drawn switches have a name.
- The filters and pickers axe flagged have a name.
- Images carry alt (decorative ones alt="").
- Secondary text (--text3) reaches 4.5:1 on the card background.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import pathlib
import re
import unittest


def _lum(hex_):
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _contrast(a, b):
    a, b = _lum(a), _lum(b)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


class TestDashboardA11y(unittest.TestCase):
    def setUp(self):
        self.html = pathlib.Path("static/index.html").read_text(encoding="utf-8")
        self.js = pathlib.Path("static/js/pages.js").read_text(encoding="utf-8")
        self.app = pathlib.Path("static/js/app.js").read_text(encoding="utf-8")

    def test_pinch_zoom_is_allowed(self):
        vp = re.search(r'<meta name="viewport" content="([^"]*)"', self.html).group(1)
        self.assertNotIn("user-scalable=no", vp)
        self.assertNotIn("maximum-scale", vp)

    def test_touch_screens_of_any_width_get_16px_inputs(self):
        self.assertRegex(self.html, r"@media \(pointer: coarse\) \{\s*input, select, textarea \{ font-size: 16px !important; \}")

    def test_live_switches_have_a_name_and_a_state(self):
        toggles = re.findall(r'<button[^>]*class="live-toggle[^>]*>', self.js)
        self.assertEqual(len(toggles), 3)
        for t in toggles:
            self.assertIn("aria-label=", t)
            self.assertIn("aria-pressed=", t)
        # Every place that flips a switch keeps aria-pressed in step.
        self.assertNotIn("classList.add('on'); else btn.classList.remove('on')", self.js)
        self.assertNotIn("classList.add('on'); else row.classList.remove('on')", self.js)

    def test_hidden_switch_checkboxes_have_a_name(self):
        self.assertRegex(self.js, r'<input type="checkbox" \$\{checked\} aria-label=[^>]*toggleAutoPlaylist')
        self.assertRegex(self.js, r'<input type="checkbox"[^>]*aria-label=[^>]*toggleToolbarButton')
        self.assertRegex(self.html, r'id="notif-enabled-checkbox" aria-label=')

    def test_filters_and_pickers_have_a_name(self):
        for id_ in ("lib-sort", "vod-provider-select", "live-ch-group-filter", "live-ch-epg-filter",
                    "home-hero-select", "home-hero-sort", "home-hero-item-count", "card-previews-select"):
            with self.subTest(id=id_):
                self.assertRegex(self.html, rf'id="{id_}" aria-label="[^"]+"')

    def test_every_image_has_alt(self):
        for name, src in (("pages.js", self.js), ("app.js", self.app)):
            missing = [m for m in re.findall(r"<img\s[^>]*>", src) if "alt=" not in m]
            self.assertEqual(missing, [], name)

    def test_secondary_text_contrast(self):
        tokens = dict(re.findall(r"--(text3|bg1|bg2):\s*(#[0-9a-fA-F]{6})", self.html))
        self.assertGreaterEqual(_contrast(tokens["text3"], tokens["bg2"]), 4.5)
        self.assertGreaterEqual(_contrast(tokens["text3"], tokens["bg1"]), 4.5)


if __name__ == "__main__":
    unittest.main()

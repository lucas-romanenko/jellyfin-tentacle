"""#281 (rest): the active navigation item reaches 4.5:1, and the Live TV
setup's address fields have a name (WCAG 2.2 AA, as axe-core checks them).

- `.nav-item.active` / `.settings-nav-item.active` drew #6c63ff on the 15 %
  accent wash: 3.7:1 on the sidebar. They use --accent-text now.
- `#live-setup-port` had no label at all (axe `label`), and the host field
  only a placeholder.

Run from the tentacle/ directory:  python -m unittest tests.test_dashboard_a11y_nav_and_setup
"""
import pathlib
import re
import unittest


def _rgb(hex_):
    return [int(hex_[i:i + 2], 16) for i in (1, 3, 5)]


def _lum(rgb):
    c = [x / 255 for x in rgb]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _contrast(a, b):
    a, b = _lum(a), _lum(b)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


class NavAndSetupA11y(unittest.TestCase):
    def setUp(self):
        self.html = pathlib.Path("static/index.html").read_text(encoding="utf-8")
        self.tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", self.html))
        m = re.search(r"--accent-dim:\s*rgba\((\d+),\s*(\d+),\s*(\d+),\s*([0-9.]+)\)", self.html)
        self.dim = [int(m.group(i)) for i in (1, 2, 3)], float(m.group(4))

    def _on_dim(self, bg):
        rgb, a = self.dim
        return [round(a * c + (1 - a) * b) for c, b in zip(rgb, _rgb(self.tokens[bg]))]

    def _active_colour(self, selector):
        rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.html).group(1)
        var = re.search(r"(?<![-\w])color:\s*var\(--([a-z0-9-]+)\)", rule).group(1)
        return _rgb(self.tokens[var])

    def test_the_active_nav_item_reaches_4_5_to_1(self):
        for selector in (".nav-item.active", ".settings-nav-item.active"):
            fg = self._active_colour(selector)
            for bg in ("bg1", "bg2"):
                with self.subTest(selector=selector, bg=bg):
                    self.assertGreaterEqual(_contrast(fg, self._on_dim(bg)), 4.5)

    def test_the_setup_address_fields_have_a_name(self):
        for id_ in ("live-setup-host", "live-setup-port"):
            with self.subTest(id=id_):
                self.assertRegex(self.html, rf'id="{id_}" aria-label="[^"]+"')


if __name__ == "__main__":
    unittest.main()

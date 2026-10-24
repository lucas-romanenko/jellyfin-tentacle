"""#281 (rest): every form control in the dashboard has a name, and the last
contrast and nesting findings of a real axe-core run are gone.

- An `<input>`, `<select>` or `<textarea>` in index.html is named by an
  aria-label, a `<label for>`, a wrapping `<label>` with text, or (inputs
  only) a placeholder. A `<div class="form-label">` above it names nothing.
- Activity cards are no `role="button"` of their own: they hold their own
  buttons (Search again, Why?, Remove), so the title is the open button.
- The active Activity tab, the "Jellyfin" row badge and the My Channels
  counts reach 4.5:1.

Run from the tentacle/ directory:  python -m unittest tests.test_dashboard_a11y_names
"""
import pathlib
import re
import unittest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"


def _lum(hex_):
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _contrast(a, b):
    a, b = _lum(a), _lum(b)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _unnamed(html):
    fors = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', html))
    out = []
    for m in re.finditer(r"<(select|input|textarea)\b([^>]*)>", html):
        kind, attrs = m.groups()
        if re.search(r'type="(hidden|button|submit)"', attrs) or "aria-label" in attrs:
            continue
        ident = re.search(r'id="([^"]+)"', attrs)
        if ident and ident.group(1) in fors:
            continue
        if kind != "select" and "placeholder=" in attrs and 'type="checkbox"' not in attrs:
            continue
        before = html[:m.start()]
        if before.rfind("<label") > before.rfind("</label>"):
            label = before[before.rfind("<label"):] + html[m.end():html.index("</label>", m.end())]
            if re.sub(r"<[^>]+>|\s", "", label.split(">", 1)[1]):
                continue
        out.append(ident.group(1) if ident else attrs[:40])
    return out


class DashboardNames(unittest.TestCase):
    def setUp(self):
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.pages = (STATIC / "js" / "pages.js").read_text(encoding="utf-8")
        self.tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", self.html))

    def test_every_control_in_the_page_has_a_name(self):
        self.assertEqual([], _unnamed(self.html))

    def test_the_merge_switch_and_service_pickers_say_what_they_set(self):
        for ident, name in (("merge-continue-checkbox", "Combine Continue Watching and Next Up"),
                            ("radarr_quality_profile_id", "Radarr default quality profile"),
                            ("sonarr_root_folder", "Sonarr root folder"),
                            ("navidrome_password", "Navidrome password")):
            with self.subTest(ident=ident):
                self.assertRegex(self.html, rf'aria-label="{re.escape(name)}"[^>]*id="{ident}"|id="{ident}"[^>]*aria-label="{re.escape(name)}"')

    def test_activity_cards_are_no_control_around_controls(self):
        self.assertNotIn('role="button" tabindex="0" data-open', self.pages)
        self.assertIn('class="activity-open"', self.pages)

    def test_last_contrast_findings(self):
        bg1 = self.tokens["bg1"]
        self.assertGreaterEqual(_contrast(self.tokens["accent-text"], bg1), 4.5)
        self.assertRegex(self.html, r"\.live-tab\.activity-tab\.active \{\s*color: var\(--accent-text\)")
        badge = re.search(r'background:(#[0-9a-f]{6});color:#fff;white-space:nowrap">Jellyfin<', self.pages).group(1)
        self.assertGreaterEqual(_contrast("#ffffff", badge), 4.5)
        count = re.search(r"\.mych-count \{([^}]*)\}", self.html).group(1)
        self.assertNotIn("opacity", count)


if __name__ == "__main__":
    unittest.main()

"""The sidebar shows the release version, and Settings → About shows names as
text (app.js versionLabel, _aboutRender), run under node.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import unittest

from dashboard_js import HAVE_NODE, Page, functions, node

SRC = functions("app.js", ["escHtml", "versionLabel", "_aboutRow", "_aboutRender"])

ABOUT = {
    "server": {"version": "v1.23.0+3", "changes": 3, "commit": "ee28fde40ba7" + "0" * 28,
               "built": "2026-10-22T18:00:00Z", "update": True, "releases": "https://example.invalid/r",
               "latest": {"version": "v1.24.0", "page": "https://example.invalid/v1.24.0"}},
    "jellyfin": {"version": "10.11.8"},
    "plugin": {"version": "2.285.0.0", "minimum": "2.286.0", "too_old": True, "restart_for": None,
               "catalog": True, "catalog_url": "https://example.invalid/manifest.json"},
    "tv_apps": [{"name": "<b>Ann</b>", "version": "1.25.2", "up_to_date": True,
                 "waiting": ["the On Now row on Home"]}],
    "tv_latest": {"version": "1.25.2"},
    "problem": None,
}


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class VersionsInTheDashboard(unittest.TestCase):
    def test_sidebar_label(self):
        sha = "ee28fde40ba7" + "0" * 28
        self.assertEqual("v1.23.0", node(SRC + f"\nprocess.stdout.write(JSON.stringify(versionLabel({{version: 'v1.23.0', commit: '{sha}'}})));"))
        self.assertEqual("v1.23.0 + 3 changes", node(SRC + "\nprocess.stdout.write(JSON.stringify(versionLabel({version: 'v1.23.0+3'})));"))
        self.assertEqual("v1.23.0 + 1 change", node(SRC + "\nprocess.stdout.write(JSON.stringify(versionLabel({version: 'v1.23.0+1'})));"))
        # no version known: the commit, as before
        self.assertEqual("build ee28fde", node(SRC + f"\nprocess.stdout.write(JSON.stringify(versionLabel({{version: null, commit: '{sha}'}})));"))
        self.assertEqual("development build", node(SRC + "\nprocess.stdout.write(JSON.stringify(versionLabel({version: null, commit: 'unknown'})));"))

    def test_about_panel(self):
        html = node(SRC + f"\nprocess.stdout.write(JSON.stringify(_aboutRender({json.dumps(ABOUT)})));")
        page = Page(html)
        self.assertIn("<b>Ann</b>", page.text)          # a name is text, never markup
        self.assertNotIn("b", [tag for tag, _ in page.elements])
        self.assertIn("v1.23.0 + 3 changes", page.text)
        self.assertIn("New version available: v1.24.0", page.text)
        self.assertIn("https://example.invalid/v1.24.0", page.attrs("href"))
        self.assertIn("older than this server needs", page.text)
        self.assertIn("the On Now row on Home", page.text)
        self.assertIn("10.11.8", page.text)

    def test_about_without_jellyfin(self):
        d = dict(ABOUT, plugin=None, jellyfin=None, tv_apps=[], problem="Jellyfin can't be reached right now.")
        page = Page(node(SRC + f"\nprocess.stdout.write(JSON.stringify(_aboutRender({json.dumps(d)})));"))
        self.assertIn("Jellyfin can't be reached right now.", page.text)
        self.assertIn("v1.23.0 + 3 changes", page.text)


if __name__ == "__main__":
    unittest.main()

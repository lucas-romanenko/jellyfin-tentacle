"""The setup docs describe the screens the dashboard really has (#393).

Run from the tentacle/ directory:  python -m unittest discover -s tests

docs/getting-started/setup-wizard.md described four wizard steps (one of them
a "TMDB (Automatic)" step) while the wizard has six, and the Live TV docs sent
users to a provider form on the Live TV page that index.html no longer has:
only dead functions in pages.js still filled it in. One provider, added in
Settings -> Providers, serves VOD and Live TV.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PAGES_JS = (ROOT / "static" / "js" / "pages.js").read_text(encoding="utf-8")


def _wizard_steps():
    """(number, title) of each step in the setup overlay, in order."""
    steps = re.findall(
        r'<div class="setup-step" id="setup-step-(\d+)"[^>]*>.*?<h2>(.*?)</h2>', HTML, re.S)
    return [(int(n), title.strip()) for n, title in steps]


class WizardDocMatchesTheWizard(unittest.TestCase):
    def test_every_step_is_documented_under_its_own_title(self):
        steps = _wizard_steps()
        self.assertEqual([n for n, _ in steps], list(range(1, 7)))
        doc = (REPO / "docs" / "getting-started" / "setup-wizard.md").read_text(encoding="utf-8")
        documented = [(int(n), re.sub(r"\s*\(Optional\)$", "", title.strip()))
                      for n, title in re.findall(r"^## Step (\d+): (.+)$", doc, re.M)]
        self.assertEqual(documented, steps)


class LiveTvPageHasNoProviderForm(unittest.TestCase):
    def test_the_page_sends_users_to_settings_providers(self):
        notice = re.search(r'<div id="live-no-provider".*?</div>', HTML, re.S)
        self.assertIsNotNone(notice)
        self.assertIn("showSettingsSection('providers')", notice.group(0))
        doc = (REPO / "docs" / "features" / "live-tv.md").read_text(encoding="utf-8")
        self.assertTrue("Settings → Providers" in doc,
                        "live-tv.md must send users to Settings → Providers")

    def test_every_live_element_the_script_reads_exists(self):
        ids = set(re.findall(r"getElementById\('(live-[\w-]+)'\)", PAGES_JS))
        self.assertTrue(ids)
        missing = sorted(i for i in ids
                         if f'id="{i}"' not in HTML and f'id="{i}"' not in PAGES_JS)
        self.assertEqual(missing, [], "pages.js reads Live TV elements index.html doesn't have")


if __name__ == "__main__":
    unittest.main()

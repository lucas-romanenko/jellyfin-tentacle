"""A user with no home config must still get toolbar buttons.

write_home_config() skips a user who has no smart playlists, so a new household
member never gets a "toolbar" in their home config. GET /TentacleHome/Toolbar
then answered {"buttons": []}. The web navbar falls back to its own defaults on
an empty list, but the Android TV app replaced its defaults with it and hid every
button — Search and Libraries included (seen on a Google TV Streamer). The plugin
must answer with the backend's own defaults.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import ast
import re
import unittest
from pathlib import Path

HOME = Path("../tentacle-plugin/Api/HomeScreenController.cs")
SMARTLISTS = Path("services/smartlists.py")


def _python_defaults():
    src = SMARTLISTS.read_text(encoding="utf-8")
    block = re.search(r"existing_toolbar = (\[\s*\{.*?\}\s*,?\s*\])", src, re.S).group(1)
    return [(b["id"], b["enabled"]) for b in ast.literal_eval(block)]


def _plugin_defaults(src):
    block = re.search(r"DefaultToolbar\s*=\s*\{(.*?)\};", src, re.S)
    if not block:
        return None
    return [(m.group(1), m.group(2) == "true")
            for m in re.finditer(r'id = "(\w+)", enabled = (true|false)', block.group(1))]


class DefaultToolbar(unittest.TestCase):
    def setUp(self):
        self.src = HOME.read_text(encoding="utf-8")
        start = self.src.index('[HttpGet("Toolbar")]')
        self.endpoint = self.src[start:self.src.index("[Http", start + 10)]

    def test_no_config_does_not_answer_an_empty_list(self):
        self.assertNotIn("Array.Empty<object>()", self.endpoint)
        self.assertIn("DefaultToolbar", self.endpoint)

    def test_plugin_defaults_match_the_backends(self):
        self.assertEqual(_python_defaults(), _plugin_defaults(self.src))

    def test_search_and_libraries_are_on_by_default(self):
        d = dict(_plugin_defaults(self.src) or [])
        self.assertTrue(d.get("search"))
        self.assertTrue(d.get("libraries"))


if __name__ == "__main__":
    unittest.main()

"""The plugin's Sections answer tells a client each row's item limit (androidtv #54).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The TV app refreshes rows in place and must tell a complete answer from a
playlist caught mid-rebuild. Without the limit it kept 20 old cards after the
dashboard shortened a row to 5. The limit must be the one GetSectionItems
applies: default 20, capped at 30.
"""
import re
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api" / "HomeScreenController.cs").read_text(encoding="utf-8")


class SectionsCarryTheRowLimit(unittest.TestCase):
    def test_each_row_section_has_max_items(self):
        row = SRC[SRC.index('type = "row",'):]
        row = row[:row.index("});")]
        self.assertIn("maxItems = Math.Min(row.MaxItems is > 0 ? row.MaxItems.Value : 20, 30)", row)

    def test_it_matches_the_limit_the_row_endpoint_applies(self):
        body = SRC[SRC.index("var limit = 20;"):]
        body = body[:body.index("var dtoOptions")]
        self.assertIn("limit = row.MaxItems.Value;", body)
        self.assertRegex(body, r"limit = Math\.Min\(limit, 30\);")


if __name__ == "__main__":
    unittest.main()

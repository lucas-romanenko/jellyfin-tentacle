"""Un-favoriting must update the Favorites page's total, not only the section's.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Source-level, in the style of tests/test_favorites_section_removal.py. Seen in
a real browser: after the heart removed a card the section said "Movies 215"
while the page header still said "216 items" until a reload.
"""
import re
import unittest

from test_favorites_section_removal import JS, unfavorite_source


class HeaderTotal(unittest.TestCase):
    def setUp(self):
        self.src = JS.read_text()
        self.fn = unfavorite_source(self.src)

    def test_the_header_the_page_renders_is_the_one_that_is_updated(self):
        rendered = re.search(r'class="(tfav-count)"', self.src)
        self.assertIsNotNone(rendered, "the page no longer renders a .tfav-count header")
        self.assertRegex(self.fn, r"querySelector\(\s*'\.tfav-count'\s*\)",
                         "unfavorite() never touches the page total, so it goes stale")

    def test_the_total_counts_both_kinds_of_card(self):
        m = re.search(r"var\s+all\s*=\s*[\w.]+\.querySelectorAll\(\s*'([^']*)'\s*\)", self.fn)
        self.assertIsNotNone(m, "the page total is not recounted from the cards")
        kinds = {k.strip() for k in m.group(1).split(",")}
        self.assertEqual({".tfav-card", ".tltv-card"}, kinds,
                         "Live TV favorites render as .tltv-card; leaving them out is #57 again")

    def test_the_total_counts_only_the_favorites_page(self):
        """Seen in a real browser: open Live TV, then Favorites, un-favorite one of
        824 -> the header said "826 items". The Live TV page keeps its own
        `.tltv-card`s in a hidden container on the same document, so a recount
        taken from `document` adds every channel card on that page."""
        m = re.search(r"var\s+all\s*=\s*([\w.]+)\.querySelectorAll\(", self.fn)
        self.assertIsNotNone(m, "the page total is not recounted from the cards")
        self.assertNotEqual("document", m.group(1),
                            "the total is counted across the whole document, so the hidden "
                            "Live TV page's channel cards are added to it")
        self.assertRegex(self.fn, r"var\s+root\s*=\s*FAV\.container\b",
                         "the recount is not scoped to the Favorites container")

    def test_the_total_keeps_its_singular_and_plural(self):
        self.assertRegex(self.fn, r"' item' \+ \(all !== 1 \? 's' : ''\)")


if __name__ == "__main__":
    unittest.main()

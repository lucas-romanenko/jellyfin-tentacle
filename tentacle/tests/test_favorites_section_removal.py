"""Unfavoriting one Live TV channel must not drop the whole Live TV section.

The favorites page renders ordinary items as `.tfav-card` and Live TV channels
with the Live TV page's markup, `.tltv-card`. After an unfavorite, `unfavorite()`
counted the cards left in the grid to decide whether the section should go —
but counted only `.tfav-card`. A Live TV grid holds only `.tltv-card`, so the
count was always 0 and the entire section was removed, taking still-favorited
channels with it until a reload.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

JS = Path("../tentacle-plugin/Inject/tentacle-favorites.js")

LIVE_TV_CARD = ".tltv-card"
FAV_CARD = ".tfav-card"


def unfavorite_source(src: str) -> str:
    """The body of `function unfavorite(...)`, up to the next top-level function."""
    start = src.index("function unfavorite(")
    rest = src[start:]
    nxt = re.search(r"\n  function ", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


class TestUnfavoriteSectionRemoval(unittest.TestCase):
    def setUp(self):
        self.fn = unfavorite_source(JS.read_text())
        # The one `querySelectorAll` that decides whether the section survives.
        m = re.search(r"var\s+left\s*=\s*grid\.querySelectorAll\(\s*'([^']*)'\s*\)",
                      self.fn)
        self.assertIsNotNone(m, "unfavorite() no longer counts remaining cards "
                                "with grid.querySelectorAll — update this test")
        self.selector = m.group(1)
        self.classes = {c.strip() for c in self.selector.split(",")}

    def test_remaining_cards_are_counted_for_live_tv_too(self):
        self.assertIn(LIVE_TV_CARD, self.classes,
                      f"remaining-card selector {self.selector!r} misses Live TV "
                      "cards, so one unfavorite empties the whole section")

    def test_ordinary_favorites_are_still_counted(self):
        self.assertIn(FAV_CARD, self.classes)

    def test_the_card_lookup_and_the_count_agree(self):
        # The card being removed is found by either class; anything findable
        # must also be countable, or its section dies on the first removal.
        found = set(re.findall(r"btnEl\.closest\(\s*'([.\w-]+)'\s*\)", self.fn))
        self.assertTrue(found, "unfavorite() no longer locates the card")
        self.assertTrue(
            found <= self.classes,
            f"cards located as {sorted(found - self.classes)} are removed but "
            f"never counted by {self.selector!r}",
        )

    def test_an_emptied_section_is_still_removed(self):
        self.assertRegex(self.fn.replace("\n", " "),
                         r"if\s*\(\s*left\s*===?\s*0\s*\)\s*section\.remove\(\)")

"""A poster that fails to load must not take the card's badge and "+" with it (#173).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The Library and Discover card posters replaced their PARENT on error, and the
status badge and the Discover "+" Add button live in that parent.
"""
import unittest
from pathlib import Path

PAGES = (Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js").read_text(encoding="utf-8")


class PosterFailure(unittest.TestCase):
    def test_no_poster_error_handler_replaces_its_parent(self):
        self.assertNotIn("this.parentElement.innerHTML", PAGES)

    def test_only_the_image_is_replaced(self):
        for card in ("function renderLibCard(", "function renderDiscoverGrid("):
            body = PAGES[PAGES.index(card):]
            body = body[:body.index("\n}\n")]
            self.assertIn("""onerror="this.outerHTML='<div class=\\\\'lib-card-poster-placeholder\\\\'>◫</div>'\"""", body, card)


if __name__ == "__main__":
    unittest.main()

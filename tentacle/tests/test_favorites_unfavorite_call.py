"""The unfavorite heart has to reach the server at all.

`unfavorite()` called `ApiClient.updateFavoriteStatus(itemId, false)`.
jellyfin-apiclient's signature is `(userId, itemId, isFavorite)`, so `itemId`
arrived as `false`, the client threw "null itemId" before making any request,
and the surrounding try/catch swallowed it. Measured in Chromium against
Jellyfin 10.11.8: clicking the heart sent no request, removed no card and left
the favourite on the server — for movies and Live TV channels alike. It also
meant the #57 section-count fix sat behind a handler that never ran.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

JS = Path("../tentacle-plugin/Inject/tentacle-favorites.js")


def unfavorite_source(src: str) -> str:
    start = src.index("function unfavorite(")
    rest = src[start:]
    nxt = re.search(r"\n  function ", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


class TestUnfavoriteCallsTheApiCorrectly(unittest.TestCase):
    def setUp(self):
        self.fn = unfavorite_source(JS.read_text())

    def test_update_favorite_status_gets_user_item_flag(self):
        calls = re.findall(r"updateFavoriteStatus\(([^)]*)\)", self.fn)
        self.assertTrue(calls, "unfavorite() no longer calls updateFavoriteStatus — update this test")
        for args in calls:
            parts = [a.strip() for a in args.split(",")]
            self.assertEqual(len(parts), 3,
                             f"updateFavoriteStatus({args}) — the signature is (userId, itemId, isFavorite)")
            self.assertEqual(parts[1], "itemId", f"second argument must be the item id, got {parts[1]!r}")
            self.assertEqual(parts[2], "false")
            self.assertNotEqual(parts[0], "itemId", "first argument must be the user id")

    def test_the_user_id_comes_from_the_api_client(self):
        self.assertIn("getCurrentUserId()", self.fn)


if __name__ == "__main__":
    unittest.main()

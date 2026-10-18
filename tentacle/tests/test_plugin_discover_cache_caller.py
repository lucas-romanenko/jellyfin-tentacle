"""GET /TentacleDiscover/Items must cache per real caller, not per ?userId=.

The backend's /api/discover answer is per user (the "From Your Lists"
section: that user's list names and items). The plugin cached it for 30
minutes under `{type}_userId=<whatever the caller sent>` and served the cache
before anything checked who was asking, so any signed-in Jellyfin user could
pass another user's id and get that user's cached page; clients that sent no
userId all shared one entry.

There is no C# test host here, so this reads the controller source.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api" / "DiscoverController.cs"


class TestDiscoverItemsCacheKey(unittest.TestCase):
    def setUp(self):
        src = SRC.read_text()
        start = src.index("public async Task<ActionResult> GetDiscoverItems(")
        self.body = src[start:src.index("[HttpGet(", start)]

    def test_cache_key_is_the_resolved_caller(self):
        self.assertNotIn("GetUserIdParam", self.body, "cache keyed on the unverified ?userId=")
        resolve = self.body.find("CallerIdentity.ResolveAsync(")
        key = self.body.find('var cacheKey = $"{type}_{caller.UserId:N}";')
        lookup = self.body.find("_itemsCache.TryGetValue(cacheKey")
        self.assertGreater(resolve, 0)
        self.assertTrue(0 < resolve < key < lookup, "identity must be resolved before the cache is read")

    def test_mismatched_user_is_refused(self):
        self.assertRegex(self.body, r"if \(!caller\.Allowed\)\s*\{\s*return Forbid\(\);")


if __name__ == "__main__":
    unittest.main()

"""Rating-icon URLs carry no version stamp, so they must not be cached forever.

`TentacleAssetsController.GetAsset` (`/Tentacle/Assets/{fileName}`) answered every
request with `Cache-Control: public, max-age=31536000, immutable`, under a comment
saying the assets "are versioned by plugin version". They are not: every caller
in Inject/ builds the URL as `serverUrl + '/Tentacle/Assets/' + name`, with no
`?v=`. `immutable` on an unversioned URL is the inverse of #56 -- a plugin update
that changes an icon is never seen by a browser that already has the old one,
for a year, with no way to invalidate it short of clearing the cache.

The rule that already exists for the injected JS/CSS (Api/AssetCaching.cs) is the
right one: long-lived only when the request carries the current boot stamp.

There is no C# test host in this repo, so this reads the source the way
tests/test_frontend_state.py reads the dashboard JS.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

CONTROLLER = Path("../tentacle-plugin/Api/AssetsController.cs")
INJECT = Path("../tentacle-plugin/Inject")


def _code(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


class TestIconAssetCaching(unittest.TestCase):
    def setUp(self):
        self.src = _code(CONTROLLER.read_text())

    def test_the_premise_call_sites_are_unversioned(self):
        """If this ever fails the call sites were versioned and the policy can be
        revisited; until then the server must assume no stamp."""
        urls = []
        for f in INJECT.glob("*.js"):
            urls += re.findall(r"/Tentacle/Assets/[^;\n]*", f.read_text())
        code_urls = [u for u in urls if "'" in u]
        self.assertTrue(code_urls)
        self.assertFalse([u for u in code_urls if "v=" in u])

    def test_a_long_lived_header_is_never_unconditional(self):
        for m in re.finditer(r'Cache-Control"\]\s*=\s*([^;]+);', self.src):
            self.assertNotRegex(
                m.group(1), r'^"[^"]*(immutable|max-age=[1-9]\d{4,})',
                "GetAsset pins a long-lived Cache-Control on a URL that carries "
                "no version: a changed icon is never re-fetched")

    def test_the_policy_comes_from_the_stamp_rule(self):
        self.assertRegex(self.src, r"AssetCaching\.CacheControlFor\(\s*Request\s*\)",
                         "GetAsset does not consult the ?v= stamp rule the "
                         "injected JS/CSS use (Api/AssetCaching.cs)")

    def test_an_unstamped_icon_can_still_be_revalidated_cheaply(self):
        """no-store on an icon drawn on every card would trade staleness for a
        full download per render; a validator keeps it to a 304."""
        self.assertIn('"ETag"', self.src)
        self.assertIn("If-None-Match", self.src)
        self.assertRegex(self.src, r"304|Status304NotModified")


if __name__ == "__main__":
    unittest.main()

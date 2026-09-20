"""EVERY asset index.html injects with ?v= must honour the stamp, not just some (#56).

#56 taught `HomeScreenController.ServeAsset` to answer a request carrying the
current `?v=<CacheBust>` with a long-lived header. But `IndexHtmlPatch` injects 21
assets and 8 of them -- discover, search, livetv and favorites, .js and .css -- are
served by `DiscoverController`, whose actions each hard-code
`no-store, no-cache, must-revalidate` (and carry `[ResponseCache(NoStore = true)]`).

Phase-3 QA measured it against the compiled plugin in a real Jellyfin 10.11.8:
with the correct stamp, 13 assets (429 KB) came back `immutable` and these 8
(173 KB of 602 KB, 29%) still came back `no-store` -- re-downloaded on every page
load, which is the very thing #56 reports.

The test is over the property, not a list of names: every action in Api/ whose
route is an injected `/Tentacle/*.js|css` asset must get its Cache-Control from
the stamp comparison, and none may pin `no-store` unconditionally.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

API = Path("../tentacle-plugin/Api")
PATCH = Path("../tentacle-plugin/Patching/IndexHtmlPatch.cs")

ACTION = re.compile(
    r'\[HttpGet\("(/Tentacle/[\w.-]+\.(?:js|css))"\)\]\s*'
    r'((?:\[[^\]]+\]\s*)*)'                       # further attributes
    r'public\s+ActionResult\s+(\w+)\(\)\s*'
    r'(=>[^;]+;|\{.*?\n    \})', re.S)


def asset_actions():
    for f in sorted(API.glob("*.cs")):
        for m in ACTION.finditer(f.read_text()):
            yield f.name, m.group(1), m.group(2), m.group(3), m.group(4)


class TestEveryInjectedAssetHonoursTheStamp(unittest.TestCase):
    def test_every_injected_asset_has_an_action(self):
        injected = set(re.findall(r'(/Tentacle/[\w.-]+\.(?:js|css))\?v=', PATCH.read_text()))
        served = {route for _, route, _, _, _ in asset_actions()}
        self.assertGreaterEqual(len(injected), 20)
        self.assertEqual(set(), injected - served, "injected assets this test cannot see")

    def test_no_asset_action_pins_no_store(self):
        offenders = [f"{f}:{name} ({route})" for f, route, attrs, name, body in asset_actions()
                     if "NoStore" in attrs or re.search(r'Cache-Control"\]\s*=\s*"no-store', body)]
        self.assertEqual([], offenders,
                         "these injected assets are sent no-store even with the current ?v= stamp")

    def test_every_asset_action_goes_through_the_stamp_check(self):
        offenders = [f"{f}:{name}" for f, _, _, name, body in asset_actions()
                     if not re.search(r"\bServeAsset\(|AssetCaching\.", body)]
        self.assertEqual([], offenders)

    def test_the_helper_compares_the_real_boot_stamp(self):
        helper = (API / "AssetCaching.cs")
        self.assertTrue(helper.exists(), "no AssetCaching helper")
        src = helper.read_text().replace("\n", " ")
        self.assertRegex(src, r"string\.Equals\(\s*stamp\s*,\s*Patching\.IndexHtmlPatch\.CacheBust")


if __name__ == "__main__":
    unittest.main()

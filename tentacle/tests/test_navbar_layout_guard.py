"""The plugin must not rewrite a display preference it did not create.

`tentacle-navbar.js`'s layoutGuard detected Jellyfin's TV layout, wrote
`localStorage.layout = 'desktop'` and reloaded the page. localStorage is
permanent, so the user's deliberate per-device choice was destroyed, not merely
overridden — it did not come back when the plugin was disabled. The loop guard
lived in sessionStorage, so it repeated in every new tab, window and session,
roughly 1.5 s into each page load.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

INJECT = Path("../tentacle-plugin/Inject")
NAVBAR = INJECT / "tentacle-navbar.js"


def layout_guard(src: str) -> str:
    start = src.index("function layoutGuard()")
    rest = src[start:]
    end = rest.index("\n    // ──")
    return rest[:end]


class TestLayoutGuardStandsDown(unittest.TestCase):
    def setUp(self):
        self.guard = layout_guard(NAVBAR.read_text())

    def test_no_injected_script_writes_the_layout_preference(self):
        offenders = []
        for js in sorted(INJECT.glob("*.js")):
            for m in re.finditer(r"localStorage\.setItem\(\s*['\"]layout['\"]", js.read_text()):
                offenders.append(f"{js.name}:{m.start()}")
        self.assertEqual(offenders, [],
                         f"the plugin overwrites the user's layout setting: {offenders}")

    def test_the_guard_does_not_reload_the_page(self):
        self.assertNotIn("location.reload()", self.guard,
                         "the TV-layout guard still reloads the page out from "
                         "under the user")

    def test_the_guard_still_detects_the_tv_layout(self):
        self.assertIn("layout-tv", self.guard,
                      "the guard no longer notices the TV layout at all")

    def test_the_guard_stands_down_instead(self):
        self.assertIn("TentacleDisabled", self.guard,
                      "the guard does not signal that Tentacle should stand "
                      "aside on a TV-layout device")


if __name__ == "__main__":
    unittest.main()

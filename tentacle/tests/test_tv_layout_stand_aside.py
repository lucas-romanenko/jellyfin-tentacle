"""Standing aside under Jellyfin's TV layout has to actually happen.

The #64 fix stopped rewriting `localStorage.layout` and set
`window.TentacleDisabled = true` instead — but nothing ever read that flag. In a
real browser with the TV layout every injected script still mounted (navbar,
media bar, home rows, favourites, Live TV, search, Discover), and because the
guard also removed the `tentacle-home-active` body class 1.5 s in, the native
home was un-hidden underneath Tentacle's and the two drew on top of each other.

So: the guard must publish a question the other scripts can ask
(`window.TentacleStandAside()`), it must be answerable synchronously from the
device's own setting (the scripts mount long before the old 1.5 s timer), and
every script that mounts UI must ask it before doing so.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

INJECT = Path("../tentacle-plugin/Inject")
NAVBAR = INJECT / "tentacle-navbar.js"

# script -> the function whose body mounts that script's UI
ENTRY_POINTS = {
    "tentacle-navbar.js": "boot",
    "tentacle-mediabar.js": "boot",
    "tentacle-home.js": "onHomePage",
    "tentacle-search.js": "init",
    "tentacle-livetv.js": "activate",
    "tentacle-favorites.js": "activate",
    "tentacle-discover.js": "tryInject",
    "tentacle-details.js": "boot",
}
ASKS = re.compile(r"window\.TentacleStandAside\s*&&\s*window\.TentacleStandAside\(\)\s*\)\s*return")


def function_head(src: str, name: str, lines: int = 4) -> str:
    """The first few lines of `function <name>(...) {`."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    if not m:
        raise AssertionError(f"function {name}() not found")
    return "\n".join(src[m.end():].split("\n")[:lines + 1])


class TestTvLayoutStandAside(unittest.TestCase):
    def test_the_guard_publishes_a_synchronous_question(self):
        src = NAVBAR.read_text()
        self.assertTrue(re.search(r"window\.TentacleStandAside\s*=\s*function", src),
                        "navbar.js must define window.TentacleStandAside for the other scripts")
        start = src.index("window.TentacleStandAside =")
        body = src[start:start + 500]
        self.assertTrue(re.search(r"localStorage\.getItem\(\s*['\"]layout['\"]\s*\)\s*===\s*['\"]tv['\"]", body),
                        "the device's explicit TV choice must be readable before Jellyfin has applied its classes")
        self.assertIn("layout-tv", body, "Jellyfin's auto-detected TV layout must count too")

    def test_every_script_that_mounts_ui_asks_before_mounting(self):
        missing = []
        for name, fn in ENTRY_POINTS.items():
            head = function_head((INJECT / name).read_text(), fn)
            if not ASKS.search(head):
                missing.append(f"{name}:{fn}()")
        self.assertEqual(missing, [],
                         f"these still mount under the TV layout (nothing reads the stand-aside state): {missing}")

    def test_no_new_ui_script_was_added_without_a_decision(self):
        """A new Inject/*.js must be listed here or deliberately exempted."""
        exempt = {
            "tentacle-mdblist.js", "tentacle-tmdb.js",   # data helpers, mount nothing
            "tentacle-notifications.js",                   # a toast is layout-independent
        }
        unknown = {p.name for p in INJECT.glob("*.js")} - set(ENTRY_POINTS) - exempt
        self.assertEqual(unknown, set(), f"decide whether these stand aside under the TV layout: {unknown}")

    def test_the_discover_overlay_route_is_guarded_too(self):
        src = (INJECT / "tentacle-discover.js").read_text()
        start = src.index("var navHandler = function ()")
        self.assertTrue(ASKS.search("\n".join(src[start:].split("\n")[:4])),
                        "a '?tentacle=discover' deep link must not open the overlay under the TV layout")


if __name__ == "__main__":
    unittest.main()

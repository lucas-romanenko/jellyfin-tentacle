"""A backend outage must not look like "every row changed".

Two faults in the home page's live-update loop:

1. `GET /TentacleHome/Version` answered `200 {"version": 0}` when the Tentacle
   backend was unreachable. The injected poller treats any change of version as
   "the library changed", so an outage read as a version change and triggered a
   full re-fetch of the sections list plus every playlist row — every 5 s, per
   open tab, for as long as the backend flapped.
2. The structural rebuild called `loadBuiltinSection(container, section)` where
   the function declares `(container, section, renderMerged)`. The flag was
   silently `undefined`, so the "merge Continue Watching" setting turned itself
   off and the user got both a Continue Watching row and a Next Up row until the
   next full navigation.

There is no C# test host in this repo, so both halves are read from source the
way tests/test_frontend_state.py reads the dashboard JS.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

CONTROLLER = Path("../tentacle-plugin/Api/HomeScreenController.cs")
HOME_JS = Path("../tentacle-plugin/Inject/tentacle-home.js")

CACHE_FIELD = "_lastKnownVersionJson"


def js_function(src: str, name: str) -> str:
    """The body of a top-level `function <name>(` in the injected script."""
    start = src.index("function %s(" % name)
    rest = src[start:]
    nxt = re.search(r"\n  function ", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


def cs_method(src: str, name: str) -> str:
    start = src.index(name)
    rest = src[start:]
    end = re.search(r"\n    /// <summary>", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestVersionEndpointOutage(unittest.TestCase):
    def setUp(self):
        self.src = CONTROLLER.read_text()
        self.method = cs_method(self.src, "public async Task<ActionResult> GetPlaylistVersion()")

    def test_a_successful_poll_remembers_what_the_backend_said(self):
        self.assertRegex(
            self.method,
            rf"{CACHE_FIELD}\s*=\s*response\s*;",
            "the version endpoint never records the backend's answer",
        )

    def test_the_cache_field_is_declared_and_process_wide(self):
        self.assertRegex(
            self.src,
            rf"private static string\?\s+{CACHE_FIELD}\s*;",
            f"{CACHE_FIELD} is not declared as a static field",
        )

    def test_a_failed_poll_replays_the_last_known_version(self):
        catch = self.method[self.method.index("catch"):]
        self.assertIn(CACHE_FIELD, catch,
                      "the failure path invents a version instead of replaying "
                      "the last one the backend gave us")
        self.assertRegex(
            catch.replace("\n", " "),
            rf"if\s*\(\s*{CACHE_FIELD}\s*!=\s*null\s*\).*Content\(\s*{CACHE_FIELD}",
        )

    def test_a_fresh_install_that_never_reached_the_backend_still_answers_zero(self):
        catch = self.method[self.method.index("catch"):]
        self.assertIn("version = 0", catch,
                      "with nothing ever cached the endpoint must still answer 0")


class TestRebuildDrawsTentaclesRowsLikeTheFirstRender(unittest.TestCase):
    """Next episodes are part of Continue Watching's list from the plugin now
    (2026-10-26): no merge rule left in the page to forget on a rebuild."""

    def setUp(self):
        self.js = HOME_JS.read_text()
        self.refresh = js_function(self.js, "refreshPlaylistRows")

    def test_every_builtin_render_is_the_same_call(self):
        calls = re.findall(r"(?<!function )loadBuiltinSection\(([^)]*)\)", self.js)
        self.assertEqual(2, len(calls), "the first render and the rebuild")
        self.assertEqual({"rowsContainer, section"}, set(calls))

    def test_no_merge_rule_is_left_in_the_page(self):
        for gone in ("mergeContinueWatching", "hasResumeRow", "renderMerged"):
            self.assertNotIn(gone, self.js)


if __name__ == "__main__":
    unittest.main()

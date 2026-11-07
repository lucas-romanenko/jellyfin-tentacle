"""A refused caller is told so, not "Tentacle did not answer" (#951).

When Tentacle answers home-config with 401/403 (a server API key instead of a
user session, a session Tentacle no longer accepts), `FetchFromApiAsync` knows
it was a refusal, but `HomeConfigResult` didn't carry that, so
`/TentacleHome/Sections` and `/TentacleHome/HeroConfig` said "Tentacle did not
answer" although Tentacle had answered. The 503 stays (clients keep their rows
on it, #257); only the words change. No client reads `error`.

There is no C# test host in this repo, so this reads the source the way
test_plugin_home_config_refusal_not_cached.py does.
"""
import re
import unittest
from pathlib import Path

MANAGER = Path("../tentacle-plugin/HomeScreen/HomeScreenManager.cs")
CONTROLLER = Path("../tentacle-plugin/Api/HomeScreenController.cs")


def _strip_comments(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


def _method(src: str, signature_start: str) -> str:
    start = src.index(signature_start)
    rest = src[start:]
    end = re.search(r"\n    \[Http|\n    private |\n    public ", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestRefusalWording(unittest.TestCase):
    def setUp(self):
        self.manager = _strip_comments(MANAGER.read_text())
        self.controller = _strip_comments(CONTROLLER.read_text())

    def test_the_result_carries_the_refusal(self):
        record = re.search(r"record struct HomeConfigResult\(([^)]*)\)", self.manager).group(1)
        self.assertRegex(record, r"\bbool Refused\b",
                         "HomeConfigResult can't tell a refusal from an outage")

    def test_a_401_or_403_sets_it(self):
        fetch = _method(self.manager, "private async Task<(HomeConfigResult Home, bool CallerRefused)> FetchFromApiAsync(")
        failure = fetch[fetch.index("IsSuccessStatusCode"):fetch.index("ReadAsStringAsync")]
        self.assertRegex(failure, r"new HomeConfigResult\([^;]*Refused\s*:\s*callerRefused|new HomeConfigResult\(null,\s*true,\s*0,\s*callerRefused\)",
                         "the 401/403 result doesn't record that the caller was refused")

    def test_sections_and_hero_say_refused(self):
        for name in ("GetSections(", "GetHeroConfig("):
            with self.subTest(endpoint=name):
                body = _method(self.controller, f"public async Task<ActionResult> {name}")
                failed = body[body.index("if (home.Failed)"):]
                failed = failed[:failed.index("}") + 1]
                self.assertIn("StatusCode(503", failed, "the status must stay 503 (#257)")
                self.assertIn("home.Refused", failed,
                              f"{name[:-1]} answers a refusal with \"Tentacle did not answer\"")
                self.assertIn("Tentacle refused this sign-in", failed)
                self.assertIn("Tentacle did not answer", failed)


if __name__ == "__main__":
    unittest.main()

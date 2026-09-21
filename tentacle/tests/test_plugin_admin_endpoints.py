"""Plugin endpoints that do privileged, server-wide work must require elevation (#79, #72).

`[Authorize]` alone means any signed-in Jellyfin account. POST /Tentacle/Refresh
wipes every plugin cache and reloads every connected client; GET
/Tentacle/HomeConfig returns any user's home config for a caller-supplied
userId. Both are for the Tentacle server (whose API key satisfies Jellyfin's
elevation policy) or an admin.

There is no C# test host here, so this reads the controller source.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api" / "TentacleController.cs"


def attributes_before(src: str, name: str) -> str:
    m = re.search(r"((?:[ \t]*(?:\[[^\]]+\]|//[^\n]*)[ \t]*\n)+)[ \t]*public[^\n]*\b" + re.escape(name) + r"\(", src)
    return m.group(1) if m else ""


class TestAdminOnlyEndpoints(unittest.TestCase):
    def test_refresh_and_home_config_require_elevation(self):
        src = CONTROLLER.read_text()
        for name in ("Refresh", "GetHomeConfig", "MoveItem"):
            with self.subTest(action=name):
                attrs = attributes_before(src, name)
                self.assertIn('[Authorize(Policy = "RequiresElevation")]', attrs,
                              f"{name} is reachable by any signed-in account")

    def test_boot_stays_anonymous(self):
        # The boot stamp must work from any page state; it carries no secrets.
        src = CONTROLLER.read_text()
        self.assertNotIn("Authorize", attributes_before(src, "GetBoot"))


if __name__ == "__main__":
    unittest.main()

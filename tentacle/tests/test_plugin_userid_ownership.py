"""Every plugin endpoint that takes a caller-supplied userId must check it (#61).

`[Authorize]` only proves *somebody* is signed in. #61 added `CallerIdentity` and
wired it into Sections / Section / Hero / HeroConfig / UserSettings, but `GET
/TentacleHome/Toolbar?userId=` was missed: it still hands the requested user's
per-user home config (toolbar buttons, visibility and order) to any signed-in
caller. Phase-3 QA found it by asserting the property rather than a fixed list of
method names, so this test does the same: any action that accepts a `Guid userId`
query parameter or a `UserSectionSettings` body must resolve that id through the
authenticated identity before using it. A new endpoint added later is covered
automatically.

There is no C# test host here, so this reads the controller source the way
tests/test_plugin_asset_caching.py does.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

CONTROLLER = Path("../tentacle-plugin/Api/HomeScreenController.cs")

ACTION = re.compile(
    r"public\s+(?:async\s+)?(?:Task<)?ActionResult>?\s+(\w+)\(([^)]*)\)")


def method_body(src: str, start: int) -> str:
    rest = src[start:]
    end = re.search(r"\n    (?:public|private|internal|protected|/// )", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestCallerSuppliedUserIdIsAlwaysChecked(unittest.TestCase):
    def setUp(self):
        self.src = CONTROLLER.read_text()

    def user_scoped_actions(self):
        for m in ACTION.finditer(self.src):
            name, params = m.group(1), m.group(2)
            if "Guid userId" in params or "UserSectionSettings" in params:
                yield name, method_body(self.src, m.start())

    def test_there_are_user_scoped_actions_to_check(self):
        names = [n for n, _ in self.user_scoped_actions()]
        self.assertIn("GetToolbar", names)
        self.assertIn("SaveUserSettings", names)

    def test_every_user_scoped_action_resolves_the_caller(self):
        offenders = [n for n, body in self.user_scoped_actions()
                     if "CallerIdentity.ResolveAsync(" not in body]
        self.assertEqual(
            offenders, [],
            f"these actions use a caller-supplied userId without checking it "
            f"against the authenticated caller: {offenders}")

    def test_the_resolved_id_is_the_one_used(self):
        """Checking and then still using the raw parameter would be no check."""
        for name, body in self.user_scoped_actions():
            if name != "GetToolbar":
                continue
            self.assertNotRegex(
                body, r"GetHomeConfig\(\s*userId\b",
                "GetToolbar must fetch the config for caller.UserId, not the raw "
                "query parameter")
            self.assertIn("Forbid()", body)


if __name__ == "__main__":
    unittest.main()

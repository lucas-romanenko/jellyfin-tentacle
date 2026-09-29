"""Every plugin controller action is authenticated unless it is on the allowlist.

The backend's route inventory (test_route_auth_inventory.py) has a plugin-side
twin here. Jellyfin serves plugin controllers with no authentication unless an
action carries [Authorize]; an action that does privileged server-wide work
must also carry the RequiresElevation policy (#72, #79, #139). A new action
without either now fails this test instead of shipping anonymous.

There is no C# test host here, so this reads the controller source.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"

# (controller file, route template) -> reason. Anonymous on purpose.
ANONYMOUS = {
    ("AssetsController.cs", "{fileName}"): "embedded rating icons for <img> tags",
    ("TentacleController.cs", "Boot"): "cache-bust stamp, must work from any page state",
    ("DiscoverController.cs", "ImageProxy/{cacheKey}"): "<img> tags cannot send auth; md5(url) + TVDB host checked",
}
# Injected client assets: /Tentacle/<name>.js|.css|.png, served to the login page too.
_STATIC_ASSET = re.compile(r"\A/Tentacle/[\w-]+\.(?:js|css|png)\Z")

# Privileged, server-wide: only the Tentacle server's API key or an admin.
ELEVATED = {
    ("TentacleController.cs", "Refresh"),
    ("TentacleController.cs", "Playlists/PruneDead"),
    ("TentacleController.cs", "Playlists/Ownerless"),
    ("TentacleController.cs", "HomeConfig"),
    ("TentacleController.cs", "Deletions/{mediaType}/{tmdbId}/Confirm"),
}

_ACTION = re.compile(
    r"((?:[ \t]*(?:\[[^\n]+\]|//[^\n]*)[ \t]*\n)+)[ \t]*public[^\n]*\(", re.MULTILINE)
_HTTP = re.compile(r'\[Http(Get|Post|Put|Delete|Patch)\("([^"]*)"\)\]')


def _actions():
    for cs in sorted(PLUGIN.rglob("*.cs")):
        src = cs.read_text(encoding="utf-8")
        for m in _ACTION.finditer(src):
            attrs = m.group(1)
            h = _HTTP.search(attrs)
            if h:
                yield cs.name, h.group(1).upper(), h.group(2), attrs


class TestPluginRouteAuthInventory(unittest.TestCase):
    def test_found_the_actions(self):
        self.assertGreater(len(list(_actions())), 60)

    def test_every_action_is_authorized_or_allowlisted(self):
        missing = []
        for f, verb, route, attrs in _actions():
            if "[Authorize" in attrs:
                continue
            if (f, route) in ANONYMOUS or (verb == "GET" and _STATIC_ASSET.match(route)):
                continue
            missing.append(f"{f}: {verb} {route}")
        self.assertEqual(missing, [], "plugin actions answering without authentication")

    def test_no_anonymous_action_changes_anything(self):
        writes = [f"{f}: {verb} {route}" for f, verb, route, attrs in _actions()
                  if "[Authorize" not in attrs and verb != "GET"]
        self.assertEqual(writes, [])

    def test_privileged_actions_require_elevation(self):
        found = {(f, route): attrs for f, _, route, attrs in _actions()}
        for key in ELEVATED:
            with self.subTest(action=key):
                self.assertIn(key, found, "elevated action no longer exists")
                self.assertIn('[Authorize(Policy = "RequiresElevation")]', found[key])

    def test_allowlist_has_no_stale_entries(self):
        found = {(f, route) for f, _, route, _ in _actions()}
        self.assertEqual(sorted(set(ANONYMOUS) - found), [])


if __name__ == "__main__":
    unittest.main()

"""The plugin's playlist-move endpoint: in-process, authorized, owner-only (#32).

Jellyfin 10.11's native move endpoint cannot be used with a server API key (it
takes the user from the token), so the plugin exposes one that calls
IPlaylistManager.MoveItemAsync in-process with an explicit user. That makes it a
write endpoint taking a caller-supplied user id AND a caller-supplied playlist
id, so the properties that matter are:

  * it is not anonymous;
  * the move is done by Jellyfin's own IPlaylistManager, not by rewriting
    LinkedChildren by hand;
  * the user id is resolved through the authenticated caller (an API key may
    name any user, a user token only itself) and the RESOLVED id is what is used;
  * a playlist that user does not own is refused before anything is moved.

There is no C# test host here, so this reads the controller source the way
tests/test_plugin_userid_ownership.py does — and finds the action by what it
DOES (calls MoveItemAsync), not by its name or file.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

API_DIR = Path("../tentacle-plugin/Api")

ACTION = re.compile(
    r"((?:[ \t]*\[[^\n]*\]\s*\n)+)"                       # attribute lines
    r"[ \t]*public\s+(?:async\s+)?(?:Task<)?(?:I?ActionResult)>?\s+(\w+)\(([^)]*)\)")


def method_body(src: str, start: int) -> str:
    rest = src[start:]
    end = re.search(r"\n    (?:public|private|internal|protected|/// |\[)", rest[1:])
    return rest[: end.start() + 1] if end else rest


def strip_comments(code: str) -> str:
    return re.sub(r"//[^\n]*", "", code)


def call_args(code: str, opener: str) -> list:
    """Top-level arguments of the first `opener...)` call (balanced parentheses)."""
    i = code.index(opener) + len(opener)
    depth, args, cur = 0, [], ""
    for ch in code[i:]:
        if ch == ")" and depth == 0:
            break
        depth += ch in "([{"
        depth -= ch in ")]}"
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return args + [cur.strip()]


def move_actions():
    """(file, name, attributes, params, body) of every action that moves a playlist entry."""
    found = []
    for path in sorted(API_DIR.glob("*.cs")):
        src = path.read_text()
        for m in ACTION.finditer(src):
            body = strip_comments(method_body(src, m.end()))
            if "MoveItemAsync(" in body:
                found.append((path, src, m.group(2), m.group(1), m.group(3), body))
    return found


class TestPlaylistMoveEndpoint(unittest.TestCase):
    def setUp(self):
        actions = move_actions()
        self.assertEqual(len(actions), 1,
                         "expected exactly one plugin action that moves a playlist entry")
        self.path, self.src, self.name, self.attrs, self.params, self.body = actions[0]

    def before_move(self):
        return self.body[: self.body.index("MoveItemAsync(")]

    def test_it_is_a_post_route_under_the_plugin_that_names_playlist_entry_and_index(self):
        route = re.search(r'\[HttpPost\("([^"]+)"\)\]', self.attrs)
        self.assertIsNotNone(route, "moving is a write: it must be a POST")
        for part in ("{playlistId}", "{entryId}", "{newIndex}"):
            self.assertIn(part, route.group(1))

    def test_it_is_not_anonymous(self):
        self.assertIn("[Authorize", self.attrs)
        self.assertNotIn("AllowAnonymous", self.attrs)
        class_attrs = self.src[: self.src.index("public class")]
        self.assertNotIn("AllowAnonymous", class_attrs)

    def test_the_move_is_done_by_jellyfins_playlist_manager(self):
        field = re.search(r"IPlaylistManager\s+(_\w+)\s*;", self.src)
        self.assertIsNotNone(field, "the controller must be given Jellyfin's IPlaylistManager")
        self.assertRegex(self.body, re.escape(field.group(1)) + r"\s*\.\s*MoveItemAsync\(")
        self.assertNotRegex(self.body, r"LinkedChildren\s*=",
                            "do not re-order the playlist by hand")

    def test_the_caller_is_resolved_before_anything_moves_and_impersonation_is_refused(self):
        head = self.before_move()
        self.assertIn("Guid userId", self.params)
        resolved = re.search(r"var\s+(\w+)\s*=\s*await\s+CallerIdentity\.ResolveAsync\(", head)
        self.assertIsNotNone(resolved, "userId must be checked against the authenticated caller")
        caller = resolved.group(1)
        self.assertRegex(head, rf"if\s*\(\s*!{caller}\.Allowed\s*\)\s*\{{\s*return\s+Forbid\(\)")

    def test_the_resolved_user_is_the_one_the_move_runs_as(self):
        """Checking the id and then moving as the raw query parameter would be no check."""
        head = self.before_move()
        caller = re.search(r"var\s+(\w+)\s*=\s*await\s+CallerIdentity\.ResolveAsync\(", head).group(1)
        last_arg = call_args(self.body, "MoveItemAsync(")[-1]
        if last_arg != f"{caller}.UserId":
            self.assertRegex(
                head, rf"\b{re.escape(last_arg)}\s*=\s*{caller}\.UserId\s*;",
                f"MoveItemAsync runs as '{last_arg}', which is not the resolved caller id")

    def test_a_request_that_names_no_user_is_refused(self):
        """An API key with no userId resolves to Guid.Empty — the very thing that
        breaks Jellyfin's own endpoint."""
        self.assertRegex(self.before_move(), r"\.Equals\(default\)|Guid\.Empty")

    def test_a_playlist_the_user_does_not_own_is_refused_before_the_move(self):
        head = self.before_move()
        owner = re.search(
            r"if\s*\(\s*!\s*[\w.]*OwnerUserId\.Equals\((\w+(?:\.\w+)?)\)\s*\)\s*\{\s*return\s+Forbid\(\)",
            head)
        self.assertIsNotNone(owner, "the playlist's OwnerUserId must be compared and a mismatch Forbid()-en")
        self.assertNotIn("OpenAccess", head, "being able to SEE a playlist is not a right to reorder it")
        self.assertNotIn("CanReadPlaylist", head, "read access is not a right to reorder")

    def test_the_playlist_is_really_a_playlist(self):
        self.assertRegex(self.before_move(), r"is\s+not\s+Playlist\b|as\s+Playlist\b|GetPlaylistForUser\(")


if __name__ == "__main__":
    unittest.main()

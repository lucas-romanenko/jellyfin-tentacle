"""#186: the plugin's "Add to playlist -> New playlist" dialog must create a
private playlist.

Jellyfin 10.11.8's CreatePlaylistDto has `IsPublic = true` as its default, so
a POST /Playlists body without IsPublic creates an OpenAccess playlist that
every user on the server can see and open (measured on 10.11.8 with two
users). jellyfin-web's own new-playlist dialog sends the state of its
"public" checkbox, which is unticked by default, i.e. IsPublic false.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

INJECT = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject"


def playlist_creations(src: str):
    """The body of every fetch(... '/Playlists', {...}) call (creations only:
    '/Playlists/' + id is an add-to-existing call)."""
    out = []
    for m in re.finditer(r"fetch\([^,]*'/Playlists'\s*,", src):
        rest = src[m.end():]
        depth, end = 0, None
        for i, ch in enumerate(rest):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        out.append(rest[:end + 1])
    return out


class NewPlaylistIsPrivate(unittest.TestCase):
    def test_every_playlist_creation_sends_is_public_false(self):
        found = 0
        for js in sorted(INJECT.glob("*.js")):
            for call in playlist_creations(js.read_text(encoding="utf-8")):
                found += 1
                self.assertIn("POST", call, f"{js.name}: expected a POST")
                self.assertRegex(call, r"IsPublic\s*:\s*false",
                                 f"{js.name}: POST /Playlists without IsPublic: false makes the "
                                 f"playlist visible to every user (CreatePlaylistDto defaults to true)")
        self.assertGreaterEqual(found, 1, "no POST /Playlists found; update this test")

    def test_the_details_dialog_is_covered(self):
        src = (INJECT / "tentacle-details.js").read_text(encoding="utf-8")
        start = src.index("showCreatePlaylistDialog: function")
        self.assertTrue(playlist_creations(src[start:])[:1], "the New Playlist dialog no longer posts /Playlists")


if __name__ == "__main__":
    unittest.main()

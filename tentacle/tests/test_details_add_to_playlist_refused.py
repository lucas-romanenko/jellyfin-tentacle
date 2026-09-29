"""Details page "Add to Playlist": a refused add must not say "Added" (#279).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The picker lists every playlist the user can see, including another user's
public playlist that they cannot edit. Picking one sends
POST /Playlists/{id}/Items; Jellyfin answers 403, and the page showed
"Added to playlist", because fetch() resolves on any HTTP status. Same for a
401 after the session expired, a 404 for a playlist deleted meanwhile, a 500.

Runs the real click handler's request chain under node with a fake fetch.
Skipped when node is not installed.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

DETAILS_JS = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject" / "tentacle-details.js"


def _add_chain() -> str:
    """The `fetch(... '/Playlists/' + playlistId + '/Items?Ids=' ...)...;` statement
    of showPlaylistPicker's click handler."""
    src = DETAILS_JS.read_text(encoding="utf-8")
    picker = src[src.index("showPlaylistPicker: function(item)"):]
    start = picker.index("fetch(serverUrl + '/Playlists/' + playlistId + '/Items?Ids='")
    end = picker.index("});\n", picker.index(".catch(function(err)", start)) + 3
    return picker[start:end]


HARNESS = r"""
var toasts = [];
var self = { showToast: function (t) { toasts.push(t); } };
var console = { error: function () {} };
var serverUrl = 'http://jf', playlistId = 'p1', userId = 'u1', headers = {}, item = { Id: 'i1' };
function fetch() { return Promise.resolve({ ok: %OK%, status: %STATUS% }); }
%CHAIN%
setTimeout(function () { process.stdout.write(JSON.stringify(toasts)); }, 10);
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class AddToPlaylist(unittest.TestCase):
    def toasts(self, ok, status):
        script = HARNESS.replace("%CHAIN%", _add_chain()) \
                        .replace("%OK%", json.dumps(ok)).replace("%STATUS%", str(status))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise AssertionError(out.stderr)
        return json.loads(out.stdout)

    def test_a_refused_add_is_not_reported_as_added(self):
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                toasts = self.toasts(False, status)
                self.assertEqual(1, len(toasts))
                self.assertNotIn("Added", toasts[0])

    def test_a_403_says_the_playlist_cannot_be_changed(self):
        self.assertRegex(self.toasts(False, 403)[0], r"can't|cannot")

    def test_a_successful_add_still_says_added(self):
        self.assertEqual(["Added to playlist"], self.toasts(True, 204))


if __name__ == "__main__":
    unittest.main()

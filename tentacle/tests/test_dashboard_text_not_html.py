"""Names and server messages are shown as text, not read as HTML.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A few dashboard spots still put text they did not write into innerHTML as it
came: the "Failed to load users" line on the sign-in screen and in Settings >
Users (the error text of the reply), the Live TV EPG line in the last-sync
details (the server's summary), and the source and list pickers of a playlist
rule's condition row (provider source tags, list names from IMDb, Letterboxd
or Trakt). A name with `&`, `<` or quotes then showed wrong: `<Live>` vanished,
`&amp;` turned into `&`.

The node tests run the real functions, lifted out of app.js and pages.js, with
stubs; they are skipped when node is not installed. The source checks need
nothing.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "static" / "js"
APP = (JS / "app.js").read_text(encoding="utf-8")
PAGES = (JS / "pages.js").read_text(encoding="utf-8")

# Ordinary text that happens to contain every character HTML treats specially.
NAME = """Rock & Roll <Live> "Best of" O'Brien"""
AS_TEXT = "Rock &amp; Roll &lt;Live&gt;"


def _function(src: str, name: str) -> str:
    start = src.index("function %s(" % name)
    if src[max(0, start - 6):start] == "async ":
        start -= 6
    depth, i = 0, src.index("{", src.index(")", start))
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[start:i]


STUBS = r"""
const NAME = %s;
const els = {};
function el(id) { return els[id] || (els[id] = { id, innerHTML: '', value: '', style: {}, dataset: {},
  setAttribute() {}, removeAttribute() {}, querySelector() { return null; } }); }
const document = { getElementById: el, querySelector() { return null; } };
const state = { currentUser: null };
function dashTimeAgo() { return ''; }
function _fmtDuration(s) { return s + 's'; }
function loginShowManual() {}
// The reply could not be read: the error text quotes what came back.
async function fetch() { return { ok: true, status: 200, json: async () => { throw new Error('Unexpected reply: ' + NAME); } }; }
async function api() { throw new Error('Jellyfin said: ' + NAME); }
let _conditionOptions = { sources: [NAME, 'Netflix Movies'], lists: [{ name: NAME, tag: 'imdb-ls1' }, { name: 'Top 250', tag: 'imdb-top' }] };
"""


def _run(body: str) -> dict:
    script = STUBS % json.dumps(NAME) + "\n".join([
        _function(APP, "escHtml"), _function(PAGES, "escapeAttr"),
        _function(APP, "showLoginOverlay"), _function(APP, "loadUsers"),
        _function(PAGES, "_syncStepHtml"), _function(PAGES, "_buildSyncDetailHtml"),
        _function(PAGES, "_renderCondValueInput"),
    ]) + "\n(async () => { const out = {};\n" + body + "\nprocess.stdout.write(JSON.stringify(out)); })();"
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if res.returncode:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TextIsShownAsText(unittest.TestCase):
    def assertText(self, html):
        self.assertNotIn("<Live>", html)
        self.assertIn(AS_TEXT, html)

    def test_sign_in_screen_failed_to_load_users(self):
        out = _run("await showLoginOverlay(); out.html = els['login-user-grid'].innerHTML;")
        self.assertIn("Failed to load users", out["html"])
        self.assertText(out["html"])

    def test_settings_users_failed_to_load(self):
        out = _run("await loadUsers(); out.html = els['users-list'].innerHTML;")
        self.assertIn("Failed to load users", out["html"])
        self.assertText(out["html"])

    def test_last_sync_details_epg_line(self):
        out = _run("""
          out.html = _buildSyncDetailHtml({ completed_at: null, epg_synced: true, epg_details: 'EPG sync: ' + NAME });
          out.plain = _buildSyncDetailHtml({ completed_at: null, epg_synced: true, epg_details: '' });
        """)
        self.assertText(out["html"])
        self.assertIn("EPG data refreshed", out["plain"])   # the fallback still shows

    def test_rule_condition_source_and_list_pickers(self):
        out = _run("""
          const row = { querySelector() { return wrap; } }, wrap = { innerHTML: '' };
          _renderCondValueInput(row, 'source', NAME); out.source = wrap.innerHTML;
          _renderCondValueInput(row, 'list', 'imdb-ls1'); out.list = wrap.innerHTML;
        """)
        for k in ("source", "list"):
            self.assertText(out[k])
        # The stored value is still the one that is picked, and ordinary names are unchanged.
        self.assertIn('<option value="Rock &amp; Roll &lt;Live&gt; &quot;Best of&quot; O&#39;Brien" selected>', out["source"])
        self.assertIn('<option value="imdb-ls1" selected>', out["list"])
        self.assertIn(">Netflix Movies</option>", out["source"])
        self.assertIn(">Top 250</option>", out["list"])


class SourceEscapesTheText(unittest.TestCase):
    """The same spots, read from the source (runs without node)."""

    def test_no_raw_interpolation_left(self):
        spots = [
            (APP, "showLoginOverlay", r"Failed to load users: \$\{e\.message\}"),
            (APP, "loadUsers", r"Failed to load users: \$\{e\.message\}"),
            (PAGES, "_buildSyncDetailHtml", r"'ok', d\.epg_details \|\|"),
            (PAGES, "_renderCondValueInput", r">\$\{s\}</option>"),
            (PAGES, "_renderCondValueInput", r">\$\{l\.name\}</option>"),
        ]
        for src, fn, raw in spots:
            with self.subTest(fn=fn, raw=raw):
                self.assertIsNone(re.search(raw, _function(src, fn)))


if __name__ == "__main__":
    unittest.main()

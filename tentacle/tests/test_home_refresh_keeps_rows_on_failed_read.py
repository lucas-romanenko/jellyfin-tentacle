"""A failed home-config read on the live-refresh path must leave the rows alone (#257).

Run from the tentacle/ directory:  python -m unittest discover -s tests

With Home open, a version change makes the page re-read /TentacleHome/Sections.
When that one read could not get the home config from the backend (restarting,
busy past the plugin's 3 s timeout, a 5xx), the plugin answered
`{"enabled": false, "sections": []}`, the same as "home disabled", and
refreshPlaylistRows emptied the rows container. The version was already
consumed, so the rows stayed gone until the next version change.

Now the plugin answers 503 for a failed read (Sections, HeroConfig), and the
page keeps its rows on any answer that is not a successful `enabled: true`,
retrying on the next poll after a failure.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin"
HOME_JS = PLUGIN / "Inject" / "tentacle-home.js"
CONTROLLER = PLUGIN / "Api" / "HomeScreenController.cs"
NAVBAR_JS = PLUGIN / "Inject" / "tentacle-navbar.js"


def _function(src: str, name: str) -> str:
    start = src.index("function %s(" % name)
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[start:i]


def _action(src: str, route: str) -> str:
    start = src.index('[HttpGet("%s")]' % route)
    end = src.find("[Http", start + 10)
    return src[start:end if end != -1 else len(src)]


HARNESS = r"""
var answer = %ANSWER%;
var MH = { generation: 1, userId: 'u1', lastVersion: 7, activeMerge: false,
           activeSections: [{ type: 'row', playlistId: 'p1', displayText: 'A', shape: 'poster' },
                            { type: 'row', playlistId: 'p2', displayText: 'B', shape: 'poster' }] };
var container = { innerHTML: '<row A><row B>', appended: 0 };
var document = {
  getElementById: function (id) { return id === 'mh-rows-container' ? container : null; },
  querySelector: function () { return null; },
};
var console = { log: function () {}, warn: function () {} };
var loaded = 0;
function loadRow() { loaded++; }
function loadBuiltinSection() { loaded++; }
function createCard() { return {}; }
function apiGet(path) {
  if (path.indexOf('TentacleHome/Sections') === 0) {
    return answer.reject ? Promise.reject(new Error('HTTP 503')) : Promise.resolve(answer.body);
  }
  return new Promise(function () {});
}
%FUNCS%
async function main() {
  refreshPlaylistRows(1);
  for (var i = 0; i < 6; i++) await null;
  process.stdout.write(JSON.stringify({ html: container.innerHTML, loaded: loaded,
    sections: MH.activeSections.length, lastVersion: MH.lastVersion }));
}
main();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RefreshKeepsRows(unittest.TestCase):
    def run_refresh(self, answer):
        src = HOME_JS.read_text(encoding="utf-8")
        script = HARNESS.replace("%FUNCS%", _function(src, "refreshPlaylistRows")) \
                        .replace("%ANSWER%", json.dumps(answer))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise AssertionError(out.stderr)
        return json.loads(out.stdout)

    def test_a_disabled_answer_does_not_empty_the_rows(self):
        r = self.run_refresh({"body": {"enabled": False, "sections": []}})
        self.assertEqual("<row A><row B>", r["html"], "the rows container was emptied")
        self.assertEqual(2, r["sections"])

    def test_a_failed_read_keeps_the_rows_and_asks_again_on_the_next_poll(self):
        r = self.run_refresh({"reject": True})
        self.assertEqual("<row A><row B>", r["html"])
        self.assertEqual(2, r["sections"])
        self.assertNotEqual(7, r["lastVersion"],
                            "the version stays consumed, so nothing re-reads the rows "
                            "until the next change")

    def test_a_real_structure_change_still_rebuilds(self):
        r = self.run_refresh({"body": {"enabled": True, "sections": [
            {"type": "row", "playlistId": "p3", "displayText": "C", "shape": "poster"}]}})
        self.assertEqual("", r["html"])
        self.assertEqual(1, r["loaded"])
        self.assertEqual(1, r["sections"])
        self.assertEqual(7, r["lastVersion"])


class PluginTellsFailureFromDisabled(unittest.TestCase):
    def setUp(self):
        self.src = re.sub(r"//[^\n]*", "", CONTROLLER.read_text(encoding="utf-8"))

    def test_sections_answers_503_for_a_failed_read(self):
        body = _action(self.src, "Sections")
        self.assertIn("GetHomeConfigResultAsync(", body)
        self.assertRegex(body, r"if\s*\(\s*home\.Failed\s*\)\s*\{\s*return StatusCode\(503",
                         "a failed read is answered like 'home disabled'")

    def test_hero_config_answers_503_for_a_failed_read(self):
        body = _action(self.src, "HeroConfig")
        self.assertRegex(body, r"if\s*\(\s*home\.Failed\s*\)\s*\{\s*return StatusCode\(503")

    def test_toolbar_marks_its_defaults_as_a_fallback(self):
        """The toolbar keeps answering 200 with the defaults (clients show them on a
        first load), but says so, so a page that has the user's own toolbar keeps it."""
        body = _action(self.src, "Toolbar")
        self.assertRegex(body, r"fallback\s*=\s*home\.Failed")
        navbar = NAVBAR_JS.read_text(encoding="utf-8")
        fetch = navbar[navbar.index("fetchToolbarConfig: function"):navbar.index("_isButtonEnabled: function")]
        self.assertRegex(fetch, r"data\.fallback\s*&&\s*self\.toolbarConfig",
                         "a fallback answer replaces the user's own toolbar")


if __name__ == "__main__":
    unittest.main()

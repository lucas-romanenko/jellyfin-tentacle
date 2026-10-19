"""A late Sections answer from an earlier visit to Home leaves the current home alone.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Each visit to Home (web) makes its own #tentacle-home element and asks
/TentacleHome/Sections. Leaving Home removes that element and bumps the
generation. When the answer of an earlier visit arrives after the user has
come back to Home (a slow backend, or Home -> elsewhere -> Home within the
answer time), renderHomePage saw a stale generation and called
cleanupHome(), which removes whatever #tentacle-home is on the page: the
CURRENT visit's home. Its own answer then filled a detached element, so the
page showed no Tentacle rows until the next navigation. A failed stale
request did the same through the catch.

Now a stale answer (or failure) removes only its own element; a current
failure still falls back to the native home.

Runs the real renderHomePage/cleanupHome under node with a fake document.
Skipped when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HOME_JS = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject" / "tentacle-home.js"


def _function(src: str, name: str) -> str:
    start = src.index("function %s(" % name)
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[start:i]


HARNESS = r"""
var answer = %ANSWER%;
function el(id) {
  return { id: id, attached: true, innerHTML: '', children: [],
           remove: function () { this.attached = false; },
           appendChild: function (c) { this.children.push(c); return c; },
           insertBefore: function (c) { this.children.push(c); return c; } };
}
var bodyClasses = { 'tentacle-home-active': true };
var current = el('current');   // the home of the visit on screen now
var stale = el('stale');       // an earlier visit's element (the user left; already detached)
stale.attached = %STALE_ATTACHED%;
var document = {
  body: { classList: { remove: function (c) { delete bodyClasses[c]; }, add: function (c) { bodyClasses[c] = true; } } },
  getElementById: function (id) { return id === 'tentacle-home' && current.attached ? current : null; },
  createElement: function () { return el('child'); },
};
var window = { TentacleMediaBar: {} };
var console = { log: function () {}, warn: function () {}, error: function () {} };
var MH = { generation: %GEN_NOW%, userId: 'u1', heroInterval: null, versionPollTimer: null };
var rendered = 0, polling = 0;
function loadRow() { rendered++; }
function loadBuiltinSection() { rendered++; }
function loadHero() {}
function startVersionPolling() { polling++; }
function clearInterval() {}
function apiGet() {
  return answer.reject ? Promise.reject(new Error('HTTP 503')) : Promise.resolve(answer.body);
}
%FUNCS%
async function main() {
  renderHomePage(%TARGET%, %GEN_OF_CALL%);
  for (var i = 0; i < 6; i++) await null;
  process.stdout.write(JSON.stringify({ currentAttached: current.attached, staleAttached: stale.attached,
    bodyClass: !!bodyClasses['tentacle-home-active'], rendered: rendered, polling: polling }));
}
main();
"""

SECTIONS = {"enabled": True, "sections": [
    {"type": "row", "playlistId": "p1", "displayText": "A", "shape": "poster"},
    {"type": "builtin", "sectionId": "latestmedia", "displayText": "Recently Added"}]}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class LateSectionsAnswer(unittest.TestCase):
    def run_render(self, answer, gen_now, gen_of_call, target, stale_attached=False):
        src = HOME_JS.read_text(encoding="utf-8")
        funcs = "\n".join(_function(src, n) for n in ("renderHomePage", "cleanupHome", "stopVersionPolling"))
        script = (HARNESS.replace("%FUNCS%", funcs).replace("%ANSWER%", json.dumps(answer))
                  .replace("%GEN_NOW%", str(gen_now)).replace("%GEN_OF_CALL%", str(gen_of_call))
                  .replace("%TARGET%", target).replace("%STALE_ATTACHED%", "true" if stale_attached else "false"))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise AssertionError(out.stderr)
        return json.loads(out.stdout)

    def test_a_late_answer_of_an_earlier_visit_keeps_the_current_home(self):
        r = self.run_render({"body": SECTIONS}, gen_now=3, gen_of_call=1, target="stale")
        self.assertTrue(r["currentAttached"], "the home on screen was removed by an earlier visit's answer")
        self.assertTrue(r["bodyClass"], "the native home was un-hidden under the current Tentacle home")
        self.assertEqual(0, r["rendered"], "a stale answer must not render rows")

    def test_a_late_failure_of_an_earlier_visit_keeps_the_current_home(self):
        r = self.run_render({"reject": True}, gen_now=3, gen_of_call=1, target="stale")
        self.assertTrue(r["currentAttached"])
        self.assertTrue(r["bodyClass"])

    def test_a_stale_answer_still_removes_its_own_element(self):
        r = self.run_render({"body": SECTIONS}, gen_now=3, gen_of_call=1, target="stale", stale_attached=True)
        self.assertFalse(r["staleAttached"])

    def test_the_current_visit_still_renders(self):
        r = self.run_render({"body": SECTIONS}, gen_now=3, gen_of_call=3, target="current")
        self.assertTrue(r["currentAttached"])
        self.assertEqual(2, r["rendered"])
        self.assertEqual(1, r["polling"])

    def test_a_current_failure_still_falls_back_to_the_native_home(self):
        r = self.run_render({"reject": True}, gen_now=3, gen_of_call=3, target="current")
        self.assertFalse(r["currentAttached"])
        self.assertFalse(r["bodyClass"])

    def test_a_current_disabled_answer_still_falls_back_to_the_native_home(self):
        r = self.run_render({"body": {"enabled": False, "sections": []}}, gen_now=3, gen_of_call=3, target="current")
        self.assertFalse(r["currentAttached"])
        self.assertFalse(r["bodyClass"])


if __name__ == "__main__":
    unittest.main()

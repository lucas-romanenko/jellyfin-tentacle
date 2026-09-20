"""The home-screen version poller must not stack requests behind a slow backend.

Run from the tentacle/ directory:  python -m unittest discover -s tests

`startVersionPolling` fires `apiGet('TentacleHome/Version')` from a 5 s
setInterval without looking at whether the previous one has come back. The
server side now bounds each poll (L8), but the client half of the problem is
still there: while an answer is outstanding, every tick adds another request.

Runs the real function, lifted out of tentacle-home.js, under node with stubs.
Skipped when node is not installed.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HOME_JS = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject" / "tentacle-home.js"

HARNESS = r"""
var ticks = [], calls = 0, pending = [];
var MH = { versionPollTimer: null, lastVersion: -1, generation: 1 };
var document = { getElementById: function () { return {}; } };
var window = {};
var console = { log: function () {} };
var refreshed = 0;
function refreshPlaylistRows() { refreshed++; }
function setInterval(fn) { ticks.push(fn); return 1; }
function clearInterval() {}
function apiGet() {
  calls++;
  return new Promise(function (resolve, reject) { pending.push({ resolve: resolve, reject: reject }); });
}
%FUNCS%
async function main() {
  startVersionPolling(1);
  pending.shift().resolve({ version: 3 });          // the seed request
  await null; await null;
  var seedCalls = calls;
  var tick = ticks[0];
  tick(); tick(); tick();                            // three ticks, backend silent
  var whileHung = calls - seedCalls;
  pending.shift().reject(new Error('timeout'));      // the hung poll finally fails
  await null; await null; await null;
  tick();
  var afterFailure = calls - seedCalls - whileHung;
  pending.shift().resolve({ version: 4 });           // and the next one answers
  await null; await null; await null;
  tick();
  var afterSuccess = calls - seedCalls - whileHung - afterFailure;
  process.stdout.write(JSON.stringify({ whileHung: whileHung, afterFailure: afterFailure,
                                        afterSuccess: afterSuccess, refreshed: refreshed }));
}
main();
"""


def _function(src: str, name: str) -> str:
    start = src.index("function %s(" % name)
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
        if depth == 0:
            return src[start:i]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class VersionPollInFlight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = HOME_JS.read_text(encoding="utf-8")
        funcs = _function(src, "startVersionPolling") + "\n" + _function(src, "stopVersionPolling")
        out = subprocess.run(["node", "-e", HARNESS.replace("%FUNCS%", funcs)],
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise AssertionError(out.stderr)
        cls.r = json.loads(out.stdout)

    def test_ticks_during_a_hung_poll_do_not_add_requests(self):
        self.assertEqual(1, self.r["whileHung"],
                         "three ticks behind a silent backend sent %d requests" % self.r["whileHung"])

    def test_polling_resumes_after_a_failed_poll(self):
        self.assertEqual(1, self.r["afterFailure"], "a failed poll left the guard stuck")

    def test_polling_resumes_after_a_successful_poll(self):
        self.assertEqual(1, self.r["afterSuccess"], "a successful poll left the guard stuck")

    def test_a_version_change_still_refreshes_the_rows(self):
        self.assertEqual(1, self.r["refreshed"])


if __name__ == "__main__":
    unittest.main()

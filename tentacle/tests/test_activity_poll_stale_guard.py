"""The Activity pollers skip a tick while a request is out, but not forever.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The dashboard's Activity tab and Library downloads panel, and the plugin's
Activity overlay, poll /api/activity on an interval and skip a tick while the
last request is still outstanding (so a slow Radarr/Sonarr doesn't stack them
up). api() / ApiClient have no timeout, so a request that never answers would
have stopped the polling for good. After POLL_STALE_MS the poll is retried, and
the late answer of the abandoned one is dropped.

Runs the real functions, lifted out of the JS, under node with stubs. Skipped
when node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGES_JS = ROOT / "tentacle" / "static" / "js" / "pages.js"
DISCOVER_JS = ROOT / "tentacle-plugin" / "Inject" / "tentacle-discover.js"


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


def _line(src: str, prefix: str) -> str:
    """The declaration line, or '' (an older file without it: the test then fails, not errors)."""
    return next((l for l in src.splitlines() if l.strip().startswith(prefix)), "")


COMMON = r"""
var now = 1000, ticks = [], calls = 0, pending = [], rendered = [];
Date.now = function () { return now; };
function setInterval(fn) { ticks.push(fn); return 1; }
function clearInterval() {}
function hang() { calls++; return new Promise(function (res, rej) { pending.push({ res: res, rej: rej }); }); }
async function flush() { for (var i = 0; i < 5; i++) await null; }
var _dlPollBusy = false, _activityPollBusy = false;   // names used before the stale guard
"""

DASHBOARD = COMMON + r"""
var state = { currentPage: 'library' };
var _dlPollTimer = 1;
function api() { return hang(); }
function renderLibDownloads(d) { rendered.push('lib:' + d.n); }
function stopDownloadPolling() {}
async function loadActivity() { var d = await hang(); rendered.push('act:' + d.n); }
%DECLS%
%FUNCS%
async function run(poll) {
  calls = 0; pending = []; rendered = []; now = 1000;
  poll();                       // request 1: never answers
  var first = pending[0];
  now = 4000; poll(); now = 61000; poll();
  var whileOut = calls;         // still 1
  now = 92000; poll();          // stale: request 2
  var afterStale = calls;
  if (pending[1]) pending[1].res({ n: 2 }); await flush();
  first.res({ n: 1 }); await flush();   // the abandoned answer arrives late
  now = 95000; poll();                  // request 2 answered: next tick polls again
  return { whileOut: whileOut, afterStale: afterStale, next: calls, rendered: rendered };
}
(async function () {
  var lib = await run(pollLibDownloads);
  var act = await run(_pollActivity);
  process.stdout.write(JSON.stringify({ lib: lib, act: act }));
})();
"""

PLUGIN = COMMON + r"""
var window = { ApiClient: { getCurrentUserId: function () { return 'u'; } }, dispatchEvent: function () {} };
function CustomEvent() {}
var MD = { active: false, activityData: null };
function apiGet() { return hang(); }
function activityCount() { return 0; }
function renderActivityContent(d) { rendered.push(d.n); }
%DECLS%
%FUNCS%
(async function () {
  ACT.active = true;
  startActivityPolling();
  var tick = ticks[0];
  tick();                       // request 1: never answers
  var first = pending[0];
  now = 4000; tick(); now = 61000; tick();
  var whileOut = calls;
  now = 92000; tick();
  var afterStale = calls;
  if (pending[1]) pending[1].res({ n: 2 }); await flush();
  first.res({ n: 1 }); await flush();
  now = 95000; tick();
  process.stdout.write(JSON.stringify({ whileOut: whileOut, afterStale: afterStale, next: calls, rendered: rendered }));
})();
"""


def _node(script: str) -> dict:
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DashboardPollers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = PAGES_JS.read_text(encoding="utf-8")
        decls = "\n".join(_line(src, p) for p in (
            "const POLL_STALE_MS", "let _dlPollSince", "let _dlPollToken",
            "let _activityPollSince", "let _activityPollToken"))
        funcs = _function(src, "pollLibDownloads") + "\n" + _function(src, "_pollActivity")
        cls.r = _node(DASHBOARD.replace("%DECLS%", decls).replace("%FUNCS%", funcs))

    def test_no_second_request_while_one_is_out(self):
        for k in ("lib", "act"):
            self.assertEqual(1, self.r[k]["whileOut"], k)

    def test_a_request_that_never_answers_does_not_stop_the_polling(self):
        for k in ("lib", "act"):
            self.assertEqual(2, self.r[k]["afterStale"], k)
            self.assertEqual(3, self.r[k]["next"], k)

    def test_the_abandoned_answer_is_dropped(self):
        self.assertEqual(["lib:2"], self.r["lib"]["rendered"])
        # loadActivity() itself keeps an older answer from replacing a newer one;
        # here the stub renders both, so only the order of polling is checked.
        self.assertIn("act:2", self.r["act"]["rendered"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PluginActivityOverlayPoller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = DISCOVER_JS.read_text(encoding="utf-8")
        start = src.index("  var ACT = {")
        decls = src[start:src.index("};", start) + 2] + "\n" + (_line(src, "var POLL_STALE_MS") or "var POLL_STALE_MS;")
        funcs = _function(src, "startActivityPolling") + "\n" + _function(src, "stopActivityPolling")
        cls.r = _node(PLUGIN.replace("%DECLS%", decls).replace("%FUNCS%", funcs))

    def test_no_second_request_while_one_is_out(self):
        self.assertEqual(1, self.r["whileOut"])

    def test_a_request_that_never_answers_does_not_stop_the_polling(self):
        self.assertEqual(2, self.r["afterStale"])
        self.assertEqual(3, self.r["next"])

    def test_the_abandoned_answer_is_dropped(self):
        self.assertEqual([2], self.r["rendered"])


if __name__ == "__main__":
    unittest.main()

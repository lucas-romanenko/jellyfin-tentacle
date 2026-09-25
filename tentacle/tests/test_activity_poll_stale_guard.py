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
"""

DASHBOARD = r"""
var cacheable = 0, now = 1000, timers = {}, nextId = 1, calls = 0, inflight = 0, maxInflight = 0, pending = [], rendered = [];
Date.now = function () { return %FROZEN% ? 1000 : now; };
Object.defineProperty(globalThis, 'performance', { value: { now: function () { return now; } }, configurable: true });
if (%NO_ABORT%) Object.defineProperty(globalThis, 'AbortController', { value: undefined, configurable: true });
function setInterval(fn) { var id = nextId++; timers[id] = fn; return id; }
function clearInterval(id) { delete timers[id]; }
function setTimeout() { return 0; }
function clearTimeout() {}
function tickAll() { Object.keys(timers).forEach(function (k) { timers[k](); }); }
async function flush() { for (var i = 0; i < 10; i++) await null; }
// Like fetch: an aborted request rejects at once and frees its connection.
function api(url, opts) {
  calls++; inflight++; maxInflight = Math.max(maxInflight, inflight);
  if (!opts || opts.cache !== 'no-store') cacheable++;
  return new Promise(function (res, rej) {
    var done = false, p = { res: function (v) { if (!done) { done = true; inflight--; res(v); } },
                            rej: function (e) { if (!done) { done = true; inflight--; rej(e); } } };
    var sig = opts && opts.signal;
    if (sig) sig.addEventListener('abort', function () { p.rej(new Error('AbortError')); });
    pending.push(p);
  });
}
var state = { currentPage: 'library' };
var document = { getElementById: function () { return null; } };
function renderLibDownloads(d) { rendered.push('lib:' + d.n); }
var _activityData = null;
async function loadActivity(signal) { try { var d = await _fetchActivity(signal); rendered.push('act:' + d.n); } catch (e) {} }
%DECLS%
%FUNCS%
// Between scenarios every request still out finally answers (the backend comes back).
async function drain() { pending.slice().forEach(function (p) { p.res({ n: 'late', downloads: [] }); }); await flush(); }
function reset() { calls = 0; inflight = 0; maxInflight = 0; pending = []; rendered = []; }
(async function () {
  var out = {};
  // 1. give up after POLL_STALE_MS; the late answer is dropped
  reset(); now = 1000;
  _dlPollTimer = setInterval(pollLibDownloads, 5000);
  tickAll(); var first = pending[0];
  now = 4000; tickAll(); now = 61000; tickAll();
  out.whileOut = calls;
  now = 92000; tickAll();
  out.afterStale = calls; out.inflightAfterStale = inflight;
  if (pending[1]) pending[1].res({ n: 2, downloads: [1] }); await flush();
  first.res({ n: 1, downloads: [1] }); await flush();
  now = 97000; tickAll();
  out.next = calls; out.rendered = rendered.slice();
  stopDownloadPolling(); await drain();
  // 2. ten minutes of Activity polling against a backend that never answers
  reset(); now = 200000;
  startActivityPolling();
  for (var t = 0; t < 200; t++) { now += 3000; tickAll(); await flush(); }
  out.activityMaxInflight = maxInflight; out.activityInflight = inflight; out.activityCalls = calls;
  stopActivityPolling(); await flush();
  out.activityInflightAfterStop = inflight;
  await drain();
  // 3. a late answer from before a page switch doesn't overwrite the new poller
  reset(); now = 2000000;
  _dlPollTimer = setInterval(pollLibDownloads, 5000);
  tickAll(); var p1 = pending[0];
  stopDownloadPolling();                       // user leaves Library
  _dlPollTimer = setInterval(pollLibDownloads, 5000);   // and comes back
  now += 5000; tickAll(); pending[pending.length - 1].res({ n: 'fresh', downloads: [1] }); await flush();
  p1.res({ n: 'stale', downloads: [] }); await flush();
  out.renderedAfterSwitch = rendered.slice(); out.timerAfterSwitch = !!_dlPollTimer;
  stopDownloadPolling(); await drain();
  // 4. an error (backend restart) backs off and retries instead of stopping
  reset(); now = 3000000;
  _dlPollTimer = setInterval(pollLibDownloads, 5000);
  tickAll(); pending[0].rej(new Error('connection refused')); await flush();
  out.timerAfterError = !!_dlPollTimer;
  now += 3000; tickAll(); out.callsDuringBackoff = calls;      // within 5 s: skipped
  now += 3000; tickAll(); out.callsAfterBackoff = calls;       // past it: retried
  pending[1].res({ n: 'back', downloads: [1] }); await flush();
  out.renderedAfterError = rendered.slice();
  stopDownloadPolling();
  out.cacheable = cacheable;
  process.stdout.write(JSON.stringify(out));
})();
"""

DECLS = ("const POLL_STALE_MS", "const _canAbort", "const _dlPoller", "let _dlPollErrors",
         "let _dlPollRetryAt", "const _activityPoller", "let _dlPollTimer", "let _dlPollActive",
         "let _activityPollTimer")
FUNCS = ("_pollClock", "_fetchActivity", "_newPoller", "_pollStart", "_pollAbandon", "_pollEnd", "_pollStop",
         "pollLibDownloads", "stopDownloadPolling", "_pollActivity", "startActivityPolling",
         "stopActivityPolling")


def _dashboard(no_abort: bool, frozen_wall_clock: bool = False) -> dict:
    src = PAGES_JS.read_text(encoding="utf-8")
    decls = "\n".join(_line(src, p) for p in DECLS)
    funcs = "\n".join(_function(src, n) for n in FUNCS)
    return _node(DASHBOARD.replace("%NO_ABORT%", "true" if no_abort else "false")
                 .replace("%FROZEN%", "true" if frozen_wall_clock else "false")
                 .replace("%DECLS%", decls).replace("%FUNCS%", funcs))


PLUGIN = COMMON + r"""
var window = { ApiClient: { getCurrentUserId: function () { return 'u'; } }, dispatchEvent: function () {},
               performance: { now: function () { return now; } } };
Date.now = function () { return 1000; };   // frozen wall clock: timing must come from performance.now()
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
        cls.r = _dashboard(no_abort=False)

    def test_no_second_request_while_one_is_out(self):
        self.assertEqual(1, self.r["whileOut"])

    def test_a_request_that_never_answers_does_not_stop_the_polling(self):
        self.assertEqual(2, self.r["afterStale"])
        self.assertEqual(3, self.r["next"])

    def test_the_request_given_up_on_is_cancelled(self):
        self.assertEqual(1, self.r["inflightAfterStale"], "the stale request must be aborted, not left open")

    def test_the_abandoned_answer_is_dropped(self):
        self.assertEqual(["lib:2"], self.r["rendered"])

    def test_a_hung_backend_never_holds_more_than_one_connection(self):
        # 6 open requests to one host block the whole page (HTTP/1.1 limit).
        self.assertEqual(1, self.r["activityMaxInflight"])
        self.assertGreaterEqual(self.r["activityCalls"], 6, "it keeps retrying every 90 s")
        self.assertEqual(0, self.r["activityInflightAfterStop"], "stopping cancels the outstanding poll")

    def test_a_late_answer_from_before_a_page_switch_is_dropped(self):
        self.assertEqual(["lib:fresh"], self.r["renderedAfterSwitch"])
        self.assertTrue(self.r["timerAfterSwitch"], "the new poller keeps running")

    def test_polls_bypass_the_http_cache(self):
        # Chromium holds identical cacheable GETs behind one still out (cache lock).
        self.assertEqual(0, self.r["cacheable"])

    def test_an_error_backs_off_and_retries(self):
        self.assertTrue(self.r["timerAfterError"])
        self.assertEqual(1, self.r["callsDuringBackoff"])
        self.assertEqual(2, self.r["callsAfterBackoff"])
        self.assertEqual(["lib:back"], self.r["renderedAfterError"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DashboardPollersWithoutAbortController(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _dashboard(no_abort=True)

    def test_at_most_one_abandoned_request(self):
        # the current request plus at most one given up on, never more
        self.assertLessEqual(self.r["activityMaxInflight"], 2)
        self.assertLessEqual(self.r["activityInflight"], 2)

    def test_still_polls_and_drops_late_answers(self):
        self.assertEqual(1, self.r["whileOut"])
        self.assertEqual(2, self.r["afterStale"])
        self.assertEqual(["lib:2"], self.r["rendered"])
        self.assertEqual(["lib:fresh"], self.r["renderedAfterSwitch"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DashboardPollersUseAMonotonicClock(unittest.TestCase):
    """A wall-clock jump (NTP setting the time after boot) must not stall the
    pollers: the timing runs on performance.now(). Here Date.now() is frozen."""
    @classmethod
    def setUpClass(cls):
        cls.r = _dashboard(no_abort=False, frozen_wall_clock=True)

    def test_the_stale_give_up_still_happens(self):
        self.assertEqual(2, self.r["afterStale"])
        self.assertEqual(["lib:2"], self.r["rendered"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PluginActivityOverlayPoller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = DISCOVER_JS.read_text(encoding="utf-8")
        start = src.index("  var ACT = {")
        decls = src[start:src.index("};", start) + 2] + "\n" + (_line(src, "var POLL_STALE_MS") or "var POLL_STALE_MS;")
        funcs = "\n".join(_function(src, n) for n in ("pollClock", "startActivityPolling", "stopActivityPolling"))
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

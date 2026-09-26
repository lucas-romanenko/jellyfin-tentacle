"""The dashboard's Activity poll must not pile up behind a slow answer (#171).

Run from the tentacle/ directory:  python -m unittest discover -s tests

setInterval(loadActivity, 3000) did not wait for the previous answer: behind
an 8 s Sonarr one answer took 64 s and eighteen requests were in flight at
once, holding every other dashboard request behind the browser's six
connections. Runs the real loadActivity() from pages.js under node.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js"


def _loader_source():
    src = PAGES.read_text(encoding="utf-8")
    start = src.index("let _activityPromise = null;")
    end = src.index("\n}\n", src.index("function loadActivity(")) + 2
    return src[start:end]


SCRIPT = """
let _activityData = null;
const rendered = [];
const pending = [];
let requests = 0;
function api(path) { requests++; return new Promise(res => pending.push(res)); }
function renderActivity(d) { rendered.push(d.tag); }
const document = { getElementById(id) { return id === 'discover-tab-activity' ? { style: { display: '' } } : null; } };
%s
(async () => {
  const out = {};
  // Five polls while the first answer is out: one request.
  const polls = [1, 2, 3, 4, 5].map(() => loadActivity());
  out.after_polls = requests;
  out.same_promise = polls.every(p => p === polls[0]);
  // An action asks for a fresh read while the poll is still out.
  const fresh = loadActivity(true);
  out.after_fresh = requests;
  // The fresh answer lands first, then the old poll's answer arrives late.
  pending[1]({ tag: 'fresh', downloads: [] });
  await fresh;
  pending[0]({ tag: 'stale', downloads: [] });
  await polls[0];
  out.rendered = rendered;
  out.data = _activityData.tag;
  // Once nothing is out, the next poll makes a new request.
  loadActivity();
  out.after_next = requests;
  process.stdout.write(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ActivityPollSingleFlight(unittest.TestCase):
    def setUp(self):
        run = subprocess.run(["node", "-e", SCRIPT % _loader_source()],
                             capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        self.out = json.loads(run.stdout)

    def test_polls_join_the_request_already_out(self):
        self.assertEqual(1, self.out["after_polls"])
        self.assertTrue(self.out["same_promise"])

    def test_an_action_gets_a_fresh_read(self):
        self.assertEqual(2, self.out["after_fresh"])

    def test_an_older_answer_never_replaces_a_newer_one(self):
        self.assertEqual(["fresh"], self.out["rendered"])
        self.assertEqual("fresh", self.out["data"])

    def test_the_next_poll_asks_again(self):
        self.assertEqual(3, self.out["after_next"])

    def test_the_library_panel_poll_waits_too(self):
        src = PAGES.read_text(encoding="utf-8")
        body = src[src.index("async function pollLibDownloads()"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("if (_dlPollInFlight) return;", body)
        self.assertIn("finally { _dlPollInFlight = false; }", body)


if __name__ == "__main__":
    unittest.main()

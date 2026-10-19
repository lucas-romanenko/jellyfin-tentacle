"""An older Activity answer never replaces a newer one (#171).

Run from the tentacle/ directory:  python -m unittest discover -s tests

With a slow Radarr/Sonarr one /api/activity answer took a minute. The pollers
now skip a tick while their request is out and cancel one out for too long
(test_activity_poll_stale_guard.py). An action (Search again, Remove, Stop
looking) still asks for a fresh read at once, so two answers can be out
together and land in either order: the one asked for last must win. Runs the
real loadActivity() from pages.js under node.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "static" / "js" / "pages.js"


def _loader_source():
    src = PAGES.read_text(encoding="utf-8")
    start = src.index("let _activitySeq = 0;")
    end = src.index("\n}\n", src.index("async function loadActivity(")) + 2
    return src[start:end]


SCRIPT = """
let _activityData = null;
const rendered = [];
const pending = [];
function _fetchActivity(signal) { return new Promise(res => pending.push(res)); }
function renderActivity(d) { rendered.push(d.tag); }
const document = { getElementById(id) { return id === 'discover-tab-activity' ? { style: { display: '' } } : null; } };
%s
(async () => {
  const out = {};
  const poll = loadActivity();       // a poll, still out
  const action = loadActivity();     // an action's refresh, asked for after it
  out.requests = pending.length;
  pending[1]({ tag: 'fresh', downloads: [] });
  await action;
  pending[0]({ tag: 'stale', downloads: [] });
  await poll;
  out.rendered = rendered.slice();
  out.data = _activityData.tag;
  // In order, every answer is shown.
  const a = loadActivity(); pending[2]({ tag: 'next', downloads: [] }); await a;
  out.after = rendered.slice();
  process.stdout.write(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ActivityAnswersInOrder(unittest.TestCase):
    def setUp(self):
        run = subprocess.run(["node", "-e", SCRIPT % _loader_source()],
                             capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        self.out = json.loads(run.stdout)

    def test_an_action_asks_even_while_a_poll_is_out(self):
        self.assertEqual(2, self.out["requests"])

    def test_an_older_answer_never_replaces_a_newer_one(self):
        self.assertEqual(["fresh"], self.out["rendered"])
        self.assertEqual("fresh", self.out["data"])

    def test_answers_in_order_are_all_shown(self):
        self.assertEqual(["fresh", "next"], self.out["after"])


if __name__ == "__main__":
    unittest.main()

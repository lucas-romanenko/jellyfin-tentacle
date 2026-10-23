"""Sources: a provider with no channel groups is read from the provider once, not every 1.5 s.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_sources_group_sync_once.py"

_loadProviderGroups() starts a group sync (POST /api/live/sync/{id}) by
itself when the provider has no groups yet ("just switched on"), and
_pollProviderLive() calls _loadProviderGroups() again when that sync ends,
whether it ended in an error, finished, or went idle. When the provider still
has no groups afterwards (it refuses this server, the account expired, or it
lists no live categories), that started the next sync: one full group sync
(get_live_categories + the unbatched get_live_streams) every ~1.5 s for as
long as the Sources page stayed open, each with a new XtreamClient, so the
#149 guard that stops a client after three refusals never applied, plus an
error toast each round.

Runs the real srcState, _loadProviderGroups(), _updateGroupsSummary(),
provGroupsRefresh(), _pollProviderLive() and plainWords() from sources.js
under node, with the 1.5 s poll driven by hand (one tick = 1.5 s).
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "static" / "js" / "sources.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"^(async )?function %s\(" % re.escape(name), SRC, re.M)
    return SRC[m.start():SRC.index("\n}\n", m.start()) + 2]


STATE = SRC[SRC.index("const srcState = {"):SRC.index("\n};\n", SRC.index("const srcState = {")) + 4]

SCRIPT = """
%(state)s
const state = { currentPage: 'sources', sourcesTab: 'providers' };
const els = {};
const document = { getElementById: id => /^prov-groups-(sum|status)-/.test(id)
  ? (els[id] = els[id] || { id, textContent: '', style: {}, dataset: {} }) : null };
const sc = %(scenario)s;
const calls = { sync: 0, status: 0, groups: 0 }, toasts = [];
async function api(path, opts) {
  if (path.startsWith('/api/live/groups?')) { calls.groups++; return { groups: sc.groups }; }
  if (path.startsWith('/api/live/sync/') && opts && opts.method === 'POST') {
    calls.sync++; return { success: true, message: 'Group sync started' };
  }
  if (path.startsWith('/api/live/sync-status?')) { calls.status++; return sc.status; }
  throw new Error('unexpected ' + path);
}
function toast(msg, type) { toasts.push([String(msg), type || '']); }
function loadLiveTV() {}
function renderProvGroupsPanel() {}
let nextId = 1;
const timers = new Map();
function setInterval(fn) { const id = nextId++; timers.set(id, fn); return id; }
function clearInterval(id) { timers.delete(id); }
const flush = async () => { for (let k = 0; k < 10; k++) await new Promise(r => setImmediate(r)); };
%(fns)s
(async () => {
  await _loadProviderGroups(1);
  await flush();
  if (sc.press) { await provGroupsRefresh(1); await flush(); }
  for (let i = 0; i < sc.ticks; i++) {
    for (const fn of [...timers.values()]) await fn();
    await flush();
  }
  process.stdout.write(JSON.stringify({ calls, toasts, polling: timers.size }));
})();
"""

FNS = ("_loadProviderGroups", "_updateGroupsSummary", "provGroupsRefresh", "_pollProviderLive", "plainWords")
BLOCKED = {"phase": "error", "progress": 0, "message": "403 Client Error: Forbidden"}
NO_LIVE = {"phase": "complete", "progress": 100, "groups": 0,
           "message": "0 groups synced. Enable the groups you want, then sync channels."}
GROUPS = [{"id": 7, "name": "News", "enabled": True, "channel_count": 12}]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SourcesGroupSyncOnce(unittest.TestCase):
    def page(self, groups, status, ticks=20, press=False):
        """The Sources page open for ticks x 1.5 s with one Live TV provider."""
        script = SCRIPT % {"state": STATE, "fns": "\n".join(_fn(n) for n in FNS),
                           "scenario": json.dumps({"groups": groups, "status": status,
                                                   "ticks": ticks, "press": press})}
        run = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, timeout=120)
        if run.returncode:
            raise AssertionError(run.stderr)
        return json.loads(run.stdout)

    def test_a_provider_that_refuses_is_asked_once_per_page_load(self):
        out = self.page([], BLOCKED)
        self.assertLessEqual(out["calls"]["sync"], 1, f"group syncs started in 30 s: {out['calls']}")
        self.assertLessEqual(len([t for t in out["toasts"] if t[1] == "error"]), 1, out["toasts"])
        self.assertEqual(0, out["polling"], "still polling after the sync ended")

    def test_a_provider_with_no_live_groups_is_asked_once_per_page_load(self):
        out = self.page([], NO_LIVE)
        self.assertLessEqual(out["calls"]["sync"], 1, f"group syncs started in 30 s: {out['calls']}")
        self.assertEqual(0, out["polling"], "still polling after the sync ended")

    def test_a_provider_with_groups_is_not_synced_by_opening_the_page(self):
        out = self.page(GROUPS, NO_LIVE)
        self.assertEqual(0, out["calls"]["sync"])

    def test_refresh_from_provider_still_reads_the_groups(self):
        out = self.page(GROUPS, NO_LIVE, press=True)
        self.assertEqual(1, out["calls"]["sync"])
        self.assertEqual(0, out["polling"])


if __name__ == "__main__":
    unittest.main()

"""Health shows Live TV's notices (#372).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Live TV writes an activity line when a recording may be missing content
(livetv_recording_damaged), when the provider serves a placeholder instead
of the channel (livetv_placeholder) and when a guide sync fails
(epg_sync_failed). The only reader of that feed, GET /api/sync/activity,
lost its caller with the old Dashboard page, so no page showed them. The
Health page now has a "Live TV Notices" card, and the route takes
`events=` so a busy nightly feed can't push these lines out of the window.
Channel names and messages come from the provider: the card sets them as
text, never as HTML.
"""
import json
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import ActivityLog, Base
from routers import sync as sync_router
from tmp_dirs import temp_dir

STATIC = Path(__file__).resolve().parents[1] / "static"
PAGES = (STATIC / "js" / "pages.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
LIVETV_EVENTS = "livetv_recording_damaged,livetv_placeholder,epg_sync_failed"


def _fn(name):
    start = PAGES.index(f"function {name}(")
    if PAGES[start - 6:start] == "async ":
        start -= 6
    return PAGES[start:PAGES.index("\n}\n", start) + 2]


class ActivityRouteFiltersByEvent(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        t0 = datetime(2026, 10, 1, 20, 0)
        rows = [("livetv_placeholder", "Ch 1: the provider served a placeholder", 0),
                ("vod_sync", "VOD sync completed", 1),
                ("livetv_recording_damaged", "Live TV: a recording of 'Ch 3' may be missing content", 2),
                ("epg_sync_failed", "EPG sync failed: timeout", 3),
                ("livetv_sync", "Live TV channel sync", 4)]
        # The nightly sync writes a line per list, VOD provider, scan, ... after them.
        rows += [("list_fetch", f"Fetched 'List {i}'", 10 + i) for i in range(200)]
        for event, message, minute in rows:
            self.db.add(ActivityLog(event=event, message=message, created_at=t0 + timedelta(minutes=minute)))
        self.db.commit()

    def test_events_keeps_only_those_newest_first_behind_a_busy_feed(self):
        out = sync_router.get_activity(limit=50, events=LIVETV_EVENTS, db=self.db)
        self.assertEqual(["epg_sync_failed", "livetv_recording_damaged", "livetv_placeholder"],
                         [e["event"] for e in out])
        self.assertEqual({"id", "event", "message", "created_at"}, set(out[0]))

    def test_events_honours_limit(self):
        out = sync_router.get_activity(limit=2, events=LIVETV_EVENTS, db=self.db)
        self.assertEqual(["epg_sync_failed", "livetv_recording_damaged"], [e["event"] for e in out])

    def test_without_events_the_feed_is_unchanged(self):
        for kwargs in ({}, {"events": ""}, {"events": " , "}):
            with self.subTest(**kwargs):
                out = sync_router.get_activity(limit=15, db=self.db, **kwargs)
                self.assertEqual(15, len(out))
                self.assertEqual({"list_fetch"}, {e["event"] for e in out})
                self.assertEqual("Fetched 'List 199'", out[0]["message"])


class HealthPageHasTheCard(unittest.TestCase):
    def test_card_on_the_health_page_loaded_with_it(self):
        page = INDEX[INDEX.index('<div class="page" id="page-health">'):INDEX.index('<!-- ── VOD ── -->')]
        self.assertIn('id="health-livetv-notices"', page)
        self.assertIn("Live TV Notices", page)
        self.assertIn('onclick="loadHealthLiveTvNotices()"', page)
        self.assertIn("loadHealthLiveTvNotices();", _fn("loadHealthPage"))
        exposed = PAGES[PAGES.index("(function exposeGlobals()"):]
        self.assertIn("loadHealthLiveTvNotices", exposed)


# A minimal DOM: enough for the card, and assigning innerHTML anywhere is recorded.
SCRIPT = r"""
const innerHTMLWrites = [];
class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this._text = ''; this.className = '';
                     this.title = ''; this.style = { cssText: '' }; }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { for (const c of cs) this.appendChild(c); }
  get firstChild() { return this.children[0] || null; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(v) { innerHTMLWrites.push(String(v)); }
}
function tree(n) {
  const o = { tag: n.tagName };
  if (n._text) o.text = n._text;
  if (n.className) o.cls = n.className;
  if (n.title) o.title = n.title;
  if (n.children.length) o.kids = n.children.map(tree);
  return o;
}
const box = new Node('div');
const document = { createElement: t => new Node(t), getElementById: id => (id === 'health-livetv-notices' ? box : null) };
const asked = [];
let answer;
async function api(url) { asked.push(url); if (answer instanceof Error) throw answer; return answer; }
function timeAgo() { return '5m ago'; }
%s
%s
%s
%s
(async () => {
  const out = {};
  const hostile = '<img src=x onerror="alert(1)"> & \'quoted\'';
  answer = [
    { id: 3, event: 'livetv_recording_damaged', created_at: '2026-10-01T20:02:00',
      message: `Live TV: a recording of '${hostile}' may be missing content — 1 interruption(s) recovered` },
    { id: 2, event: 'livetv_placeholder', created_at: '2026-10-01T20:01:00.5Z',
      message: `${hostile}: the provider served a placeholder (black.ts) instead of the channel` },
    { id: 1, event: 'epg_sync_failed', created_at: null, message: 'EPG sync failed: no programs' },
    { id: 0, event: 'some_future_kind<b>', created_at: '2026-10-01T20:00:00', message: '' },
  ];
  await loadHealthLiveTvNotices();
  out.rows = { asked: asked.slice(), tree: tree(box), text: box.textContent };
  answer = [];
  await loadHealthLiveTvNotices();
  out.empty = tree(box);
  answer = new Error('HTTP 500');
  await loadHealthLiveTvNotices();
  out.failed = tree(box);
  out.innerHTMLWrites = innerHTMLWrites;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stderr.write(String(e.stack || e)); process.exit(1); });
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class HealthCardRendersTheNotices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start = PAGES.index("const _LIVETV_NOTICE_KINDS = {")
        kinds = PAGES[start:PAGES.index("\n};\n", start) + 3]
        script = SCRIPT % (kinds, _fn("_healthEl"), _fn("_healthDate"), _fn("loadHealthLiveTvNotices"))
        run = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.out = json.loads(run.stdout)

    @staticmethod
    def cells(table):
        tbody = [k for k in table["kids"] if k["tag"] == "TBODY"][0]
        return [[td for td in tr["kids"]] for tr in tbody["kids"]]

    def test_asks_for_live_tv_notices_only(self):
        self.assertEqual([f"/api/sync/activity?limit=50&events={LIVETV_EVENTS}"], self.out["rows"]["asked"])

    def test_one_row_per_notice_with_its_kind_and_text(self):
        wrap = self.out["rows"]["tree"]["kids"][0]
        table = wrap["kids"][0]
        self.assertEqual("TABLE", table["tag"])
        head = [th["text"] for th in table["kids"][0]["kids"][0]["kids"]]
        self.assertEqual(["When", "What", "Detail"], head)
        rows = self.cells(table)
        self.assertEqual(4, len(rows))
        badges = [r[1]["kids"][0] for r in rows]
        self.assertEqual(["Recording", "Placeholder", "Guide", "some_future_kind<b>"], [b["text"] for b in badges])
        self.assertEqual(["badge badge-red", "badge badge-amber", "badge badge-amber", "badge badge-accent"],
                         [b["cls"] for b in badges])
        hostile = '<img src=x onerror="alert(1)"> & \'quoted\''
        self.assertEqual(f"Live TV: a recording of '{hostile}' may be missing content — 1 interruption(s) recovered",
                         rows[0][2]["text"])
        self.assertEqual(f"{hostile}: the provider served a placeholder (black.ts) instead of the channel",
                         rows[1][2]["text"])
        self.assertEqual(["5m ago", "5m ago", "—", "5m ago"], [r[0].get("text") for r in rows])
        self.assertTrue(rows[0][0].get("title"), "the exact time is in the tooltip")
        self.assertNotIn("text", rows[3][2], "an empty message stays empty")

    def test_no_value_goes_through_innerhtml(self):
        self.assertEqual([], self.out["innerHTMLWrites"])

    def test_empty_and_failed_states(self):
        self.assertEqual("No Live TV problems recorded", self.out["empty"]["kids"][0]["kids"][0]["text"])
        self.assertEqual("empty-state", self.out["empty"]["kids"][0]["cls"])
        self.assertEqual("Failed to load Live TV notices", self.out["failed"]["kids"][0]["kids"][0]["text"])


if __name__ == "__main__":
    unittest.main()

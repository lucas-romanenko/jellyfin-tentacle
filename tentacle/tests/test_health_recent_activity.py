"""The Activity feed is shown on Health (#372).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Live TV writes "a recording of 'X' may be missing content" and "the provider
served a placeholder" to the Activity feed (ActivityLog), and so do the EPG,
stream-health and VOD sweeps. Its only reader, GET /api/sync/activity, lost its
last caller when the old Dashboard page went, so no page showed any of it.
Health's Recent Activity card reads it again; the real loadHealthActivity()
from pages.js runs here under node with a minimal DOM.
"""
import json
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir

STATIC = Path(__file__).resolve().parents[1] / "static"
PAGES = STATIC / "js" / "pages.js"
INDEX = STATIC / "index.html"

DAMAGED = ("Live TV: a recording of 'DVR Channel 03' may be missing content — 1 interruption(s) "
           "recovered, 161s waiting on the provider, 0 segment(s) skipped, 13 failed request(s)")
PLACEHOLDER = ("DVR Channel 01: the provider served a placeholder (black.ts) instead of the channel, "
               "so it is unavailable right now.")


def _block(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i) + len(end)]


def _card_script(entries):
    src = PAGES.read_text(encoding="utf-8")
    parts = [
        _block(src, "function timeAgo(", "\n}\n"),
        _block(src, "function escapeHtml(", "\n}\n"),
        _block(src, "function _healthDate(", "\n}\n"),
        _block(src, "const _ACTIVITY_EVENT_META = {", "\n};\n"),
        _block(src, "function _activityMeta(", "\n}\n"),
        _block(src, "function _activityIsProblem(", "\n}\n"),
        _block(src, "async function loadHealthActivity(", "\n}\n"),
    ]
    return """
    const els = {};
    const document = { getElementById(id) { return els[id] || (els[id] = { innerHTML: '', textContent: '' }); } };
    const asked = [];
    async function api(path) { asked.push(path); return %s; }
    %s
    loadHealthActivity().then(() => process.stdout.write(JSON.stringify({
      asked, html: els['health-activity'].innerHTML, count: els['health-activity-count'].textContent,
    })));
    """ % (json.dumps(entries), "\n".join(parts))


def _render(entries):
    out = subprocess.run(["node", "-e", _card_script(entries)], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class HealthPageHasTheCard(unittest.TestCase):
    def test_health_has_a_recent_activity_card_that_loads_with_the_page(self):
        html = INDEX.read_text(encoding="utf-8")
        page = html[html.index('id="page-health"'):html.index('<!-- ── VOD ── -->')]
        self.assertIn('id="health-activity"', page)
        self.assertIn('onclick="loadHealthActivity()"', page)
        src = PAGES.read_text(encoding="utf-8")
        self.assertIn("loadHealthActivity();", _block(src, "function loadHealthPage(", "\n}\n"))
        globals_block = src[src.index("(function exposeGlobals()"):]
        self.assertRegex(globals_block, r"\bloadHealthActivity\b")

    def test_the_card_reads_the_feed_that_live_tv_writes_to(self):
        self.assertIn("api('/api/sync/activity?limit=", PAGES.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TheCardShowsTheNotices(unittest.TestCase):
    def test_live_tv_notices_are_shown_and_flagged(self):
        now = datetime.utcnow().isoformat()
        got = _render([
            {"id": 3, "event": "livetv_recording_damaged", "message": DAMAGED, "created_at": now},
            {"id": 2, "event": "livetv_placeholder", "message": PLACEHOLDER, "created_at": now},
            {"id": 1, "event": "epg_sync", "message": "EPG sync: 900 programs for 40 channels", "created_at": now},
        ])
        self.assertEqual(["/api/sync/activity?limit=100"], got["asked"])
        self.assertIn("DVR Channel 03", got["html"])
        self.assertIn("may be missing content", got["html"])
        self.assertIn("served a placeholder", got["html"])
        self.assertIn('badge badge-red" style="font-size:10px">Recording damaged', got["html"])
        self.assertIn('badge badge-amber" style="font-size:10px">Channel placeholder', got["html"])
        self.assertIn("EPG sync: 900 programs", got["html"])
        self.assertEqual("(2 to look at)", got["count"])

    def test_a_failure_of_any_kind_is_flagged_and_an_unknown_event_still_shows(self):
        now = datetime.utcnow().isoformat()
        got = _render([
            {"id": 2, "event": "radarr_scan", "message": "Radarr scan failed: timed out", "created_at": now},
            {"id": 1, "event": "brand_new_thing", "message": "<b>x</b> happened", "created_at": now},
        ])
        self.assertIn('badge badge-red" style="font-size:10px">Radarr', got["html"])
        self.assertIn(">Brand new thing<", got["html"])
        self.assertIn("&lt;b&gt;x&lt;/b&gt; happened", got["html"])
        self.assertEqual("(1 to look at)", got["count"])

    def test_an_empty_feed_says_so(self):
        got = _render([])
        self.assertIn("No activity recorded yet", got["html"])
        self.assertEqual("", got["count"])


class TheFeedRoute(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.mdb = mdb

    def test_newest_first_and_the_limit_is_bounded(self):
        from routers.sync import get_activity
        start = datetime.utcnow() - timedelta(hours=1)
        for i in range(5):
            self.db.add(self.mdb.ActivityLog(event="livetv_placeholder", message=f"n{i}",
                                             created_at=start + timedelta(minutes=i)))
        self.db.commit()
        self.assertEqual(["n4", "n3"], [e["message"] for e in get_activity(limit=2, db=self.db)])
        # SQLite reads LIMIT -1 as "no limit": a bad value gets one row, not the whole table
        self.assertEqual(["n4"], [e["message"] for e in get_activity(limit=-1, db=self.db)])
        self.assertEqual(5, len(get_activity(limit=10**9, db=self.db)))


if __name__ == "__main__":
    unittest.main()

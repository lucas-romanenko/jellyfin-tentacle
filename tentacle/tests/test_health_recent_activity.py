"""The Activity feed is shown on Health (#372), as a readable log.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Live TV writes "a recording of 'X' may be missing content" and "the provider
served a placeholder" to the Activity feed (ActivityLog), and so do the EPG,
stream-health and VOD sweeps. Its only reader, GET /api/sync/activity, lost its
last caller when the old Dashboard page went, so no page showed any of it.
Health's "What happened" card reads it again, with the deletion log under
Removals: the last 7 days by default, a page at a time, repeats folded into
one line, who did it in plain words, ids only behind Details. The real
loadHealthLog() / renderHealthLog() from pages.js run here under node with a
minimal DOM.
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
SOURCES = STATIC / "js" / "sources.js"
INDEX = STATIC / "index.html"

DAMAGED = ("Live TV: a recording of 'DVR Channel 03' may be missing content — 1 interruption(s) "
           "recovered, 161s waiting on the provider, 0 segment(s) skipped, 13 failed request(s)")
PLACEHOLDER = ("DVR Channel 01: the provider served a placeholder (black.ts) instead of the channel, "
               "so it is unavailable right now.")


def _block(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i) + len(end)]


def _card_script(entries, kind="activity", days="7", problems=False):
    src = PAGES.read_text(encoding="utf-8")
    parts = [
        _block(SOURCES.read_text(encoding="utf-8"), "function plainWords(", "\n}\n"),
        _block(src, "function timeAgo(", "\n}\n"),
        _block(src, "function escapeHtml(", "\n}\n"),
        _block(src, "function _healthDate(", "\n}\n"),
        _block(src, "const _DELETION_KIND_META = {", "\n};\n"),
        _block(src, "const _ACTIVITY_EVENT_META = {", "\n};\n"),
        _block(src, "function _activityMeta(", "\n}\n"),
        _block(src, "function _activityIsProblem(", "\n}\n"),
        _block(src, "const healthLog = {", "\n"),
        _block(src, "async function loadHealthLog(", "\n}\n"),
        _block(src, "function _logClean(", "\n}\n"),
        _block(src, "function _logActor(", "\n}\n"),
        _block(src, "function _logKey(", "\n}\n"),
        _block(src, "function _logGroups(", "\n}\n"),
        _block(src, "function renderHealthLog(", "\n}\n"),
    ]
    return """
    const els = {};
    const document = { getElementById(id) { return els[id] || (els[id] = { innerHTML: '', textContent: '', style: {},
      value: id === 'health-log-days' ? %s : '', checked: id === 'health-log-problems' ? %s : false,
      options: [{text: 'Last 7 days'}], selectedIndex: 0 }); } };
    const asked = [];
    async function api(path) { asked.push(path); return %s; }
    %s
    healthLog.kind = %s;
    loadHealthLog().then(() => process.stdout.write(JSON.stringify({
      asked, html: els['health-log'].innerHTML, count: els['health-log-count'].textContent,
      more: els['health-log-more'].style.display,
    })));
    """ % (json.dumps(days), json.dumps(problems), json.dumps(entries), "\n".join(parts), json.dumps(kind))


def _render(entries, **kw):
    out = subprocess.run(["node", "-e", _card_script(entries, **kw)], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class HealthPageHasTheCard(unittest.TestCase):
    def test_health_has_a_log_card_that_loads_with_the_page(self):
        html = INDEX.read_text(encoding="utf-8")
        page = html[html.index('id="page-health"'):html.index('id="page-settings"')]
        self.assertIn('id="health-log"', page)
        self.assertIn("setHealthLogKind('deletions',this)", page)
        self.assertIn('<option value="7" selected>Last 7 days</option>', page)
        src = PAGES.read_text(encoding="utf-8")
        self.assertIn("loadHealthLog();", _block(src, "function loadHealthPage(", "\n}\n"))
        globals_block = src[src.index("(function exposeGlobals()"):]
        for fn in ("loadHealthLog", "setHealthLogKind", "renderHealthLog"):
            self.assertRegex(globals_block, r"\b%s\b" % fn)

    def test_the_card_reads_the_feed_that_live_tv_writes_to(self):
        self.assertIn("`/api/sync/activity?limit=${n}&offset=${healthLog.offset}&days=${days}`",
                      PAGES.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TheCardShowsTheNotices(unittest.TestCase):
    def test_live_tv_notices_are_shown_and_flagged(self):
        now = datetime.utcnow().isoformat()
        got = _render([
            {"id": 3, "event": "livetv_recording_damaged", "message": DAMAGED, "created_at": now},
            {"id": 2, "event": "livetv_placeholder", "message": PLACEHOLDER, "created_at": now},
            {"id": 1, "event": "epg_sync", "message": "EPG sync: 900 programs for 40 channels", "created_at": now},
        ])
        self.assertEqual(["/api/sync/activity?limit=100&offset=0&days=7"], got["asked"])
        self.assertIn("DVR Channel 03", got["html"])
        self.assertIn("may be missing content", got["html"])
        self.assertIn("served a placeholder", got["html"])
        self.assertIn('badge badge-red">Recording damaged', got["html"])
        self.assertIn('badge badge-amber">Channel placeholder', got["html"])
        self.assertIn("TV guide sync: 900 programs", got["html"], "EPG reads as TV guide")
        self.assertIn("2 to look at", got["count"])
        self.assertEqual("none", got["more"], "fewer than a page: nothing older to load")

    def test_a_failure_of_any_kind_is_flagged_and_an_unknown_event_still_shows(self):
        now = datetime.utcnow().isoformat()
        got = _render([
            {"id": 2, "event": "radarr_scan", "message": "Radarr scan failed: timed out", "created_at": now},
            {"id": 1, "event": "brand_new_thing", "message": "<b>x</b> happened", "created_at": now},
        ])
        self.assertIn('badge badge-red">Radarr', got["html"])
        self.assertIn(">Brand new thing<", got["html"])
        self.assertIn("&lt;b&gt;x&lt;/b&gt; happened", got["html"])
        self.assertIn("1 to look at", got["count"])

    def test_problems_only(self):
        now = datetime.utcnow().isoformat()
        got = _render([
            {"id": 2, "event": "radarr_scan", "message": "Radarr scan failed: timed out", "created_at": now},
            {"id": 1, "event": "list_fetch", "message": "Fetched 'Top 250'", "created_at": now},
        ], problems=True)
        self.assertIn("timed out", got["html"])
        self.assertNotIn("Top 250", got["html"])

    def test_repeats_become_one_line(self):
        now = datetime.utcnow()
        rows = [{"id": 10 - i, "event": "sonarr_add", "message": "Sonarr downloaded 'Kate Plus 8'",
                 "created_at": (now - timedelta(minutes=i)).isoformat()} for i in range(9)]
        rows.append({"id": 1, "event": "jellyfin_push", "message": "Pushed tags", "created_at": now.isoformat()})
        got = _render(rows)
        self.assertIn("&times;9", got["html"])
        self.assertEqual(2, got["html"].count('class="health-log-row"'))

    def test_a_full_page_offers_older_entries(self):
        now = datetime.utcnow().isoformat()
        got = _render([{"id": i, "event": "e%d" % i, "message": "m%d" % i, "created_at": now} for i in range(100)])
        self.assertEqual("", got["more"])

    def test_an_empty_week_says_so(self):
        got = _render([])
        self.assertIn("Nothing happened last 7 days", got["html"])
        self.assertEqual("", got["count"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TheRemovals(unittest.TestCase):
    def test_who_in_plain_words_and_ids_behind_details(self):
        now = datetime.utcnow().isoformat()
        got = _render([
            {"id": 9, "kind": "smartlist-orphan", "name": "Old Channel", "reason": "auto", "user_name": None,
             "detail": "SmartList no longer desired for user 2 — folder removed, Jellyfin playlist "
                       "39e9030c25e2b30cad8eece3a1b2c3d4", "created_at": now},
            {"id": 8, "kind": "sync-prune", "name": "IPTV", "reason": "removed-upstream", "user_name": None,
             "detail": "2 movie(s) absent", "created_at": now},
            {"id": 7, "kind": "rematch", "name": "The Decline", "reason": "manual", "user_name": "lucas",
             "detail": "Stream 188327 re-matched: 'A' (tmdb:21137)", "created_at": now},
        ], kind="deletions")
        self.assertEqual(["/api/health/deletions?limit=100&offset=0&days=7"], got["asked"])
        html = got["html"]
        text = html.split("<pre>")
        visible = "".join(t.split("</pre>")[-1] for t in text)
        self.assertIn("Playlist no longer wanted for a user", visible)
        self.assertNotIn("39e9030c25e2", visible)
        self.assertNotIn("tmdb:21137", visible)
        self.assertNotIn("188327", visible)
        self.assertIn(">Tentacle<", visible)
        self.assertNotIn(">Gone from the provider<", visible, "the badge already says it")
        self.assertIn(">lucas<", visible)
        # ...and the ids are all there under Details
        self.assertIn("39e9030c25e2b30cad8eece3a1b2c3d4", html)
        self.assertIn("tmdb:21137", html)
        self.assertIn("Stream 188327", html)


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

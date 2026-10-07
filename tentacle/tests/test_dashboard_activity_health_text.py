"""Download, sync and history entries show titles and messages as stored.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The Library downloads card (title, episode, quality, ETA, requester,
status), the Health downloads status label, the sync summary (list names,
provider names, new titles and categories) and the sync history (category
names, provider, type, error) put Radarr/Sonarr and provider values into
the page as they came. A title containing an "&...;" sequence showed
different text, and angle brackets could hide part of a line. They are now
escaped where inserted. The Health deletions table escaped the user name
twice (once into `who`, again where the row inserts `who`); it is now
escaped once, in the row. The tests check the exact text, so a missing
escape or one applied twice would show.
"""
import json
import unittest

from dashboard_js import HAVE_NODE, functions, missing_text, render

VALUES = ["O'Brien & \"Sons\" <3", "Tom &#39; Jerry &amp; co", "Ocean’s “Eleven”",
          "Šėšta `tick` ${x} </div>", "a\\\\b\\"]
ESC = functions("pages.js", ["escapeAttr", "escapeJS", "escapeHtml"]) + functions("app.js", ["escHtml", "dashTimeAgo"])


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class ActivityAndHealthText(unittest.TestCase):
    def shown(self, values, html):
        self.assertEqual([], missing_text(values, html))

    def test_library_downloads_card(self):
        src = ESC + functions("pages.js", ["renderLibDownloads"])
        dls = [{"title": v, "episode": v + " ep", "quality": v + " q", "eta": v + " eta",
                "requested_by": v + " by", "status": "downloading", "progress": 10} for v in VALUES]
        html = render(src, "", "renderLibDownloads(%s)" % json.dumps({"downloads": dls}))
        self.shown(VALUES + [v + s for v in VALUES for s in (" ep", " q", " eta", " by")], html["lib-dl-body"])

    def test_health_downloads_status_label(self):
        src = ESC + functions("pages.js", ["loadHealthDownloads"]) + "\nconst _DL_STATUS_META = {};\n"
        dls = [{"title": v, "episode": v + " ep", "status": v + " st", "quality": "HD", "source": "radarr"} for v in VALUES]
        html = render(src, "async function _fetchActivity() { return %s; }" % json.dumps({"downloads": dls}),
                      "await loadHealthDownloads()")
        self.shown(VALUES + [v + " ep" for v in VALUES] + [v + " st" for v in VALUES], html["health-downloads"])

    def test_sync_summary_and_history(self):
        src = ESC + functions("pages.js", ["_syncStepHtml", "_fmtDuration", "_buildSyncDetailHtml", "renderHistoryRuns"])
        d = {"completed_at": None, "lists_updated": [{"name": v, "added": 1, "removed": 0} for v in VALUES],
             "providers": [{"name": v + " prov", "status": "ok", "movies_new": 1, "movie_titles": [v + " film"],
                            "series_titles": [v + " show"], "new_categories": [v + " cat"]} for v in VALUES]
             + [{"name": "Failed prov", "status": "failed", "error": v + " perr"} for v in VALUES]}
        runs = [{"provider_name": v + " prov", "sync_type": v + " type", "status": "failed",
                 "error_message": v + " err", "category_stats": {"EN - " + v: {"new": 1, "total": 3}}} for v in VALUES]
        try:
            html = render(src, "", "written.sum = [_buildSyncDetailHtml(%s)]; renderHistoryRuns(%s)"
                          % (json.dumps(d), json.dumps(runs)))
        except AssertionError as e:
            self.fail(str(e)[:500])
        self.shown([v + s for v in VALUES for s in (" prov", " type", " err")] + VALUES, html["history-runs"])
        self.shown(VALUES + [v + s for v in VALUES for s in (" prov", " film", " show", " cat", " perr")], html["sum"])


    def test_health_deletions_user_name(self):
        src = (ESC + functions("pages.js", ["timeAgo"]) + "\nconst _DELETION_KIND_META = {};\n"
               "function _healthDate(v) { return null; }\n" + functions("pages.js", ["loadHealthDeletions"]))
        rows = [{"kind": "x", "name": v + " item", "reason": "manual", "user_name": v + " user", "detail": v + " why"}
                for v in VALUES]
        html = render(src, "API['/api/health/deletions'] = %s;" % json.dumps(rows), "await loadHealthDeletions()")
        self.shown([v + s for v in VALUES for s in (" item", " user", " why")], html["health-deletions"])


if __name__ == "__main__":
    unittest.main()

"""Download, sync and history entries show titles and messages as stored.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The Library downloads card (title, episode, quality, ETA, requester,
status), the Health downloads status label, the sync summary (list names,
provider names, new titles and categories), the sync history (category
names, provider, type, error) and the provider migration preview put
Radarr/Sonarr and provider values into the page as they came. A title
containing an "&...;" sequence showed different text, and angle brackets
could hide part of a line. They are now escaped where inserted; the Health
table's own escaping is left as it was (the test checks the exact text, so
an escape applied twice would show).
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


if __name__ == "__main__":
    unittest.main()

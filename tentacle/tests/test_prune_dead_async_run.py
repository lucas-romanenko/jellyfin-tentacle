"""The dead-entry prune is a run Tentacle polls until the plugin says it is done
(#181 follow-up).

Run from the tentacle/ directory:  python -m unittest discover -s tests

With a 15-minute read timeout the hourly prune still timed out on a live
library while Jellyfin was busy (a library scan, Refresh Guide), and the
plugin kept rewriting playlists for minutes after Tentacle had released
_playlist_refresh_lock; the summary was lost. Now the plugin starts the prune
and answers 202 with a run id at once, and Tentacle polls it, holding the lock,
until the plugin reports the run finished. An old plugin (no run id) still
answers the summary directly. The hourly prune also skips an hour while
Jellyfin scans the library or refreshes the guide.
"""
import re
import unittest
from pathlib import Path
from unittest import mock

import services.jellyfin as jellyfin
import services.smartlists as sl

PLUGIN_CONTROLLER = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api" / "TentacleController.cs"


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class PollsTheRun(unittest.TestCase):
    def setUp(self):
        self.jf = jellyfin.JellyfinService("http://jf:8096", "k")
        self.posts, self.gets = [], []
        self.polls = []
        self.post_answer = _Resp(202, {"runId": "r1", "state": "running"})
        self.jf.session.post = self._post
        self.jf.session.get = self._get
        p = mock.patch.object(jellyfin.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def _post(self, url, json=None, timeout=None):
        self.posts.append((url, json, timeout))
        return self.post_answer

    def _get(self, url, params=None, timeout=None):
        self.gets.append((url, timeout))
        answer = self.polls.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def test_the_prune_is_started_as_a_run_and_polled_until_done(self):
        summary = {"checkedPlaylists": 30, "prunedPlaylists": 2, "removed": 15, "refused": False}
        self.polls = [_Resp(200, {"state": "running"}), _Resp(200, {"state": "running"}),
                      _Resp(200, {"state": "done", "result": summary})]
        self.assertEqual(summary, self.jf.prune_dead_playlist_entries(["a", "b"]))
        self.assertEqual({"Ids": ["a", "b"], "Async": True}, self.posts[0][1])
        self.assertEqual(3, len(self.gets))
        self.assertTrue(all(u.endswith("/Tentacle/Playlists/PruneDead/r1") for u, _ in self.gets))

    def test_a_few_failed_polls_do_not_give_up(self):
        summary = {"removed": 1}
        self.polls = [ConnectionError("busy"), _Resp(500, {}), _Resp(200, {"state": "done", "result": summary})]
        self.assertEqual(summary, self.jf.prune_dead_playlist_entries(["a"]))

    def test_a_run_jellyfin_forgot_ends_the_wait(self):
        """Jellyfin restarted mid-prune: the run is gone, stop waiting."""
        self.polls = [_Resp(404, None)]
        with self.assertLogs("services.jellyfin", "WARNING"):
            self.assertIsNone(self.jf.prune_dead_playlist_entries(["a"]))

    def test_a_failed_run_is_reported(self):
        self.polls = [_Resp(200, {"state": "failed", "error": "boom"})]
        with self.assertLogs("services.jellyfin", "WARNING") as logs:
            self.assertIsNone(self.jf.prune_dead_playlist_entries(["a"]))
        self.assertIn("boom", " ".join(logs.output))

    def test_polling_stops_after_too_many_failures_in_a_row(self):
        self.polls = [ConnectionError("down")] * (jellyfin.PRUNE_POLL_MAX_FAILURES + 5)
        with self.assertLogs("services.jellyfin", "WARNING"):
            self.assertIsNone(self.jf.prune_dead_playlist_entries(["a"]))
        self.assertEqual(jellyfin.PRUNE_POLL_MAX_FAILURES, len(self.gets))

    def test_an_old_plugin_still_answers_the_summary_directly(self):
        self.post_answer = _Resp(200, {"checkedPlaylists": 2, "removed": 3})
        self.assertEqual(3, self.jf.prune_dead_playlist_entries(["a"])["removed"])
        self.assertEqual([], self.gets)
        # An old plugin prunes inside the POST: its read timeout stays long.
        self.assertGreaterEqual(self.posts[0][2][1], 600)


class HoldsTheLockUntilDone(unittest.TestCase):
    def test_the_playlist_lock_is_held_while_the_run_is_polled(self):
        seen = []

        def prune(svc, ids):
            seen.append(sl._playlist_refresh_lock._is_owned())
            return {"removed": 0}

        db = mock.Mock()
        db.query.return_value.all.return_value = [mock.Mock(id=1)]
        with mock.patch.object(sl, "get_setting", side_effect=lambda d, k, default="": {"jellyfin_url": "http://jf", "jellyfin_api_key": "k"}.get(k, default)), \
             mock.patch.object(sl, "_get_smartlists_with_playlist_ids", return_value=[{"name": "A", "playlist_id": "p1"}]), \
             mock.patch.object(jellyfin.JellyfinService, "running_library_tasks", return_value=[]), \
             mock.patch.object(jellyfin.JellyfinService, "prune_dead_playlist_entries", prune):
            sl.prune_dead_entries(db)
        self.assertEqual([True], seen)


class SkipsWhileJellyfinIsBusy(unittest.TestCase):
    def run_hourly(self, running):
        calls = []
        db = mock.Mock()
        db.query.return_value.all.return_value = [mock.Mock(id=1)]
        with mock.patch.object(sl, "get_setting", side_effect=lambda d, k, default="": {"jellyfin_url": "http://jf", "jellyfin_api_key": "k"}.get(k, default)), \
             mock.patch.object(sl, "_get_smartlists_with_playlist_ids", return_value=[{"name": "A", "playlist_id": "p1"}]), \
             mock.patch.object(jellyfin.JellyfinService, "running_library_tasks", return_value=running), \
             mock.patch.object(jellyfin.JellyfinService, "prune_dead_playlist_entries",
                               lambda svc, ids: calls.append(ids) or {"removed": 2}):
            return sl.prune_dead_entries(db), calls

    def test_a_library_scan_skips_this_hours_prune(self):
        removed, calls = self.run_hourly(["Scan Media Library"])
        self.assertEqual(0, removed)
        self.assertEqual([], calls)

    def test_an_idle_jellyfin_is_pruned(self):
        removed, calls = self.run_hourly([])
        self.assertEqual(2, removed)
        self.assertEqual([["p1"]], calls)

    def test_running_library_tasks_reads_the_scheduled_tasks(self):
        jf = jellyfin.JellyfinService("http://jf:8096", "k")
        jf.session.get = lambda url, params=None, timeout=None: _Resp(200, [
            {"Key": "RefreshLibrary", "Name": "Scan Media Library", "State": "Running"},
            {"Key": "RefreshGuide", "Name": "Refresh Guide", "State": "Idle"},
            {"Key": "CleanCache", "Name": "Clean Cache", "State": "Running"},
        ])
        self.assertEqual(["Scan Media Library"], jf.running_library_tasks())

    def test_a_failed_task_check_does_not_block_the_prune(self):
        jf = jellyfin.JellyfinService("http://jf:8096", "k")

        def boom(*a, **kw):
            raise ConnectionError("down")
        jf.session.get = boom
        self.assertEqual([], jf.running_library_tasks())


class PluginSide(unittest.TestCase):
    def setUp(self):
        self.src = re.sub(r"//[^\n]*", "", PLUGIN_CONTROLLER.read_text(encoding="utf-8"))

    def test_a_run_can_be_polled(self):
        self.assertIn('[HttpGet("Playlists/PruneDead/{runId}")]', self.src)

    def test_an_async_request_is_answered_at_once_with_a_run_id(self):
        post = self.src[self.src.index('[HttpPost("Playlists/PruneDead")]'):]
        post = post[:post.index('[HttpGet(')]
        self.assertRegex(post, r"body\?\.Async\s*==\s*true")
        self.assertRegex(post, r"StatusCode\(\s*202")

    def test_one_prune_at_a_time_in_the_plugin(self):
        self.assertRegex(self.src, r"static readonly SemaphoreSlim\s+PruneGate")


if __name__ == "__main__":
    unittest.main()

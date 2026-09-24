"""Tentacle's own background work stands aside while a live stream or a
recording is being proxied.

Run from the tentacle/ directory:  python -m unittest discover -s tests

On a connection-limited Xtream account, going over the limit does not refuse
the newcomer: the provider answers the already-open stream's next request
with 509, and that stream is usually a recording. Measured 2026-09-24: the
nightly VOD sync (25 minutes of player_api calls) was running through two of
the three 509 storms that cut one NHL recording into four files. The health
sweep already stood aside for live TV; the sync, discovery, the known-bad
recheck and the "wrong movie" frame grabs did not.
"""
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _db():
    import models.database as mdb
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class WaitUntilQuiet(unittest.TestCase):
    def setUp(self):
        import services.provider_activity as pa
        self.pa = pa
        self.db = _db()
        self.addCleanup(self.db.close)
        self.live = False
        self.clock = 1000.0
        self.slept = []

        def fake_sleep(s):
            self.slept.append(s)
            self.clock += s
            if self.live_ends_after_sleeps is not None and len(self.slept) >= self.live_ends_after_sleeps:
                self.live = False

        self.live_ends_after_sleeps = None
        for p in (mock.patch.object(pa, "live_streams_active", lambda: self.live),
                  mock.patch.object(pa.time, "sleep", fake_sleep),
                  mock.patch.object(pa.time, "monotonic", lambda: self.clock)):
            p.start()
            self.addCleanup(p.stop)

    def test_quiet_provider_returns_at_once(self):
        self.assertTrue(self.pa.wait_until_quiet(self.db, "sync"))
        self.assertEqual([], self.slept)

    def test_waits_until_the_stream_ends(self):
        self.live, self.live_ends_after_sleeps = True, 3
        self.assertTrue(self.pa.wait_until_quiet(self.db, "sync"))
        self.assertEqual(3, len(self.slept))
        self.assertTrue(all(s <= self.pa.POLL_SECONDS for s in self.slept))

    def test_gives_up_after_the_configured_limit(self):
        from models.database import set_setting
        set_setting(self.db, "provider_jobs_defer_while_live_seconds", "90")
        self.db.commit()
        self.live = True
        self.assertFalse(self.pa.wait_until_quiet(self.db, "sync"), "must run eventually")
        self.assertAlmostEqual(90.0, sum(self.slept), delta=1.0)

    def test_zero_setting_never_waits(self):
        from models.database import set_setting
        set_setting(self.db, "provider_jobs_defer_while_live_seconds", "0")
        self.db.commit()
        self.live = True
        self.assertTrue(self.pa.wait_until_quiet(self.db, "sync"))
        self.assertEqual([], self.slept)

    def test_default_limit_is_four_hours(self):
        self.assertEqual(4 * 3600.0, self.pa.defer_seconds(self.db))

    def test_a_cancelled_job_stops_waiting(self):
        self.live = True
        self.assertFalse(self.pa.wait_until_quiet(self.db, "sync", cancel_check=lambda: True))
        self.assertEqual([], self.slept, "a cancelled sync must not sit in the wait loop")


class OneBudgetPerJob(WaitUntilQuiet):
    """A job pauses many times; "wait up to N" is N for the whole job."""

    def _pause(self, limit="90"):
        from models.database import set_setting
        set_setting(self.db, "provider_jobs_defer_while_live_seconds", limit)
        self.db.commit()
        return self.pa.JobPause(self.db, "sync")

    def test_the_budget_is_spent_across_pauses(self):
        pause = self._pause()
        self.live, self.live_ends_after_sleeps = True, 2
        self.assertTrue(pause())
        spent_first = sum(self.slept)
        self.live, self.live_ends_after_sleeps = True, None
        self.assertFalse(pause(), "only what is left of the budget is waited")
        self.assertAlmostEqual(90.0, sum(self.slept), delta=1.0)
        self.assertGreater(sum(self.slept), spent_first)

    def test_once_spent_later_pauses_do_not_wait_at_all(self):
        pause = self._pause()
        self.live = True
        self.assertFalse(pause())
        n = len(self.slept)
        self.assertFalse(pause())
        self.assertFalse(pause())
        self.assertEqual(n, len(self.slept))

    def test_a_quiet_provider_costs_nothing(self):
        pause = self._pause()
        self.assertTrue(pause())
        self.assertEqual(0.0, pause.spent)

    def test_would_wait_reads_the_budget_and_live_tv(self):
        pause = self._pause()
        self.assertFalse(pause.would_wait())
        self.live = True
        self.assertTrue(pause.would_wait())
        pause()                                  # spends the budget
        self.assertFalse(pause.would_wait())

    def test_a_cancelled_job_does_not_wait(self):
        from models.database import set_setting
        set_setting(self.db, "provider_jobs_defer_while_live_seconds", "90")
        self.db.commit()
        self.live = True
        self.assertFalse(self.pa.JobPause(self.db, "sync", cancel_check=lambda: True)())
        self.assertEqual([], self.slept)


class SyncPausesOnlyBetweenCategories(unittest.TestCase):
    """The sync commits a category's writes at its end. A pause taken while
    writes are pending would hold SQLite's write lock for the whole pause
    (hours, on a busy evening) and every other write in Tentacle would fail
    with "database is locked". So the pause is taken at category boundaries,
    after a commit -- never from inside the provider client."""

    def _client(self):
        from services.sync import XtreamClient
        provider = types.SimpleNamespace(server_url="http://panel.test", username="u", password="p")
        c = XtreamClient(provider)
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: []
        c.session = mock.Mock()
        c.session.get = mock.Mock(return_value=resp)
        return c

    def test_provider_calls_do_not_pause_and_carry_a_timeout(self):
        c = self._client()
        c.job_pause = mock.Mock()
        c.get_vod_streams("7")
        c.get_series_info("9")
        c.job_pause.assert_not_called()
        for call in c.session.get.call_args_list:
            self.assertEqual(30, call.kwargs.get("timeout"), "requests ignores Session.timeout; pass it per call")
        self.assertFalse(hasattr(c, "before_request"), "the per-request hook is gone")

    def test_the_category_pause_commits_first(self):
        from services.sync import _pause_between_categories
        order = []
        c = self._client()
        c.job_pause = lambda: order.append("pause")
        db = mock.Mock()
        db.commit = lambda: order.append("commit")
        _pause_between_categories(c, db)
        self.assertEqual(["commit", "pause"], order)

    def test_a_bare_client_pauses_nowhere(self):
        from services.sync import _pause_between_categories
        c = self._client()
        db = mock.Mock()
        _pause_between_categories(c, db)
        db.commit.assert_not_called()
        c.get_vod_streams("7")
        self.assertEqual(1, c.session.get.call_count)

    def test_both_category_loops_pause_and_the_run_shares_one_budget(self):
        import services.sync as sync
        import main
        src = Path(sync.__file__).read_text(encoding="utf-8")
        self.assertNotIn("before_request", src)
        self.assertIn("client.job_pause = pause if pause is not None else JobPause(", src)
        main_src = Path(main.__file__).read_text(encoding="utf-8")
        self.assertIn("pause = JobPause(db, \"the scheduled provider sync\")", main_src)
        self.assertEqual(1, main_src.count("pause()"), "before discovery; the sync waits inside sync_provider")
        self.assertIn("pause=pause)", main_src)
        self.assertIn("client.job_pause.cancel_check = cancel_check", src, "cancel works while waiting")
        self.assertEqual(3, src.count("_pause_between_categories(client, db, progress_callback"),
                         "before the first provider call, then per movie and series category")

    def test_cancelled_while_waiting_stops_the_sync_there(self):
        from services.sync import _pause_between_categories
        from services.exceptions import SyncCancelledError
        c = self._client()
        c.job_pause = mock.Mock(return_value=False, would_wait=lambda: True, cancel_check=lambda: True)
        with self.assertRaises(SyncCancelledError):
            _pause_between_categories(c, mock.Mock(), None, "movies", "Action", {})
        c.job_pause = mock.Mock(return_value=False, would_wait=lambda: True, cancel_check=lambda: False)
        _pause_between_categories(c, mock.Mock(), None, "movies", "Action", {})   # budget spent: carry on

    def test_the_nightly_sync_forwards_the_waiting_message(self):
        import main
        main_src = Path(main.__file__).read_text(encoding="utf-8")
        self.assertIn('progress["item_title"] = item_title', main_src)

    def test_the_stuck_sync_cutoff_allows_for_the_wait(self):
        from routers import sync as sync_router
        src = Path(sync_router.__file__).read_text(encoding="utf-8")
        self.assertIn("stuck_hours = 4 + defer_seconds(db) / 3600.0", src)

    def test_a_waiting_sync_says_so_on_screen(self):
        from services.sync import _pause_between_categories, WAITING_FOR_LIVE_TV
        c = self._client()
        c.job_pause = mock.Mock(would_wait=lambda: True)
        shown = []
        _pause_between_categories(c, mock.Mock(), lambda *a, **kw: shown.append((a, kw)), "movies", "Action", {"new": 1})
        self.assertEqual(1, len(shown))
        self.assertEqual(("movies", "Action", {"new": 1}), shown[0][0])
        self.assertEqual(WAITING_FOR_LIVE_TV, shown[0][1]["item_title"])
        c.job_pause.assert_called_once()
        c.job_pause = mock.Mock(would_wait=lambda: False)
        _pause_between_categories(c, mock.Mock(), lambda *a, **kw: shown.append((a, kw)), "movies", "Action", {})
        self.assertEqual(1, len(shown), "nothing to say when there is nothing to wait for")


class RecheckKnownBadIsPolite(unittest.TestCase):
    def setUp(self):
        import services.stream_health as sh
        self.sh = sh
        self.db = _db()
        self.addCleanup(self.db.close)
        d = Path(tempfile.mkdtemp())
        for i in range(3):
            f = d / f"t{i}.strm"
            f.write_text(f"http://panel.test/movie/u/p/{100 + i}.mkv")
            self.db.add(sh.StreamHealth(media_type="movie", tmdb_id=100 + i, title=f"Film {i}",
                                        strm_path=str(f), stream_url=f.read_text()))
        self.db.commit()
        self.live = False
        self.checked, self.slept = [], []
        sh._probe_state["provider_busy"] = False
        for p in (mock.patch.object(sh, "_live_streams_active", lambda: self.live),
                  mock.patch.object(sh, "check_stream", self._check),
                  mock.patch.object(sh.time, "sleep", lambda s: self.slept.append(s))):
            p.start()
            self.addCleanup(p.stop)

    def _check(self, db, media_type, kind, stream_id, url, provider):
        self.checked.append(stream_id)
        return None

    def test_rechecks_are_paced(self):
        out = self.sh.recheck_known_bad(self.db)
        self.assertEqual(3, out["rechecked"])
        self.assertEqual([self.sh.PROBE_INTERVAL_SECONDS] * 2, self.slept, "back-to-back probes")
        self.assertFalse(out["deferred"])

    def test_stands_aside_for_live_tv(self):
        self.live = True
        out = self.sh.recheck_known_bad(self.db)
        self.assertEqual(0, out["rechecked"])
        self.assertTrue(out["deferred"])
        self.assertEqual([], self.checked)

    def test_stops_when_the_provider_is_over_its_limit(self):
        self.sh._probe_state["provider_busy"] = True
        out = self.sh.recheck_known_bad(self.db)
        self.assertEqual(0, out["rechecked"])
        self.assertTrue(out["provider_busy"])

    def test_the_button_does_a_batch_and_says_how_many_are_left(self):
        """At PROBE_INTERVAL_SECONDS per probe a long list outlasts a reverse
        proxy's request timeout; the route does `limit` per press."""
        out = self.sh.recheck_known_bad(self.db, limit=2)
        self.assertEqual(2, out["rechecked"])
        self.assertEqual(1, out["remaining"])
        self.assertEqual([100, 101], self.checked)
        out = self.sh.recheck_known_bad(self.db, limit=2)
        self.assertEqual(102, self.checked[2], "the next press starts with the one not yet re-tested")
        self.assertEqual(0, self.sh.recheck_known_bad(self.db)["remaining"], "no limit: everything")


class SecretsStayMasked(unittest.TestCase):
    def test_the_vod_token_secret_is_masked_like_the_api_keys(self):
        from routers import settings as settings_router
        src = Path(settings_router.__file__).read_text(encoding="utf-8")
        self.assertIn('"vod_token_secret"', src.split("# Mask sensitive values", 1)[1].split("\n", 2)[1])

    def test_a_masked_secret_posted_back_is_not_written(self):
        """GET masks it; a client that round-trips GET -> POST must not
        replace the real secret with 'abcd...wxyz' (every .strm would 404)."""
        from routers import settings as settings_router
        src = Path(settings_router.__file__).read_text(encoding="utf-8")
        sensitive = src.split("sensitive_keys = {", 1)[1].split("}", 1)[0]
        self.assertIn('"vod_token_secret"', sensitive)


class FrameGrabsRefuseWhileLive(unittest.TestCase):
    def test_refused_with_a_reason_while_a_stream_is_running(self):
        import models.database as mdb
        import services.provider_activity as pa
        import services.wrong_match as wm
        db = _db()
        self.addCleanup(db.close)
        strm = Path(tempfile.mkdtemp()) / "m.strm"
        strm.write_text("http://panel.test/movie/u/p/5.mkv")
        db.add(mdb.Movie(tmdb_id=5, title="Film", source="provider_1", strm_path=str(strm)))
        db.commit()
        with mock.patch.object(pa, "live_streams_active", lambda: True), \
                mock.patch.object(wm.shutil, "which", lambda name: "/usr/bin/ffmpeg") if hasattr(wm, "shutil") \
                else mock.patch("shutil.which", lambda name: "/usr/bin/ffmpeg"):
            with self.assertRaises(wm.WrongMatchError) as cm:
                wm.stream_frames(db, 5)
        self.assertEqual(503, cm.exception.status)
        self.assertIn("recording", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

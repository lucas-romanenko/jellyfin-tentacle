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


class SyncClientWaitsBeforeEveryCall(unittest.TestCase):
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

    def test_hook_runs_before_each_request_and_a_timeout_is_passed(self):
        c = self._client()
        waits = []
        c.before_request = lambda: waits.append(1)
        c.get_vod_streams("7")
        c.get_series_info("9")
        self.assertEqual(2, len(waits))
        for call in c.session.get.call_args_list:
            self.assertEqual(30, call.kwargs.get("timeout"), "requests ignores Session.timeout; pass it per call")

    def test_a_bare_client_has_no_hook(self):
        c = self._client()
        c.get_vod_streams("7")     # must not fail without a hook
        self.assertEqual(1, c.session.get.call_count)

    def test_sync_provider_installs_the_pause(self):
        """sync_provider wires the hook so every provider call waits for live TV."""
        import services.sync as sync
        src = Path(sync.__file__).read_text(encoding="utf-8")
        self.assertIn("client.before_request = lambda: pause_while_live(", src)


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

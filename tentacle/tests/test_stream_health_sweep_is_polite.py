"""The daily VOD health sweep must not cost anyone a live recording.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Seen in production (2026-09-20): run_stream_health_sweep() started 20 s before a
live recording got its first HTTP 509, probed exactly 100 titles in 135 s -- 100
provider connections, back to back, on the same account the recording was
streaming from -- and the recording stalled twice while it ran. The sweep did
not pace itself, did not look at whether live TV was being proxied, and carried
on after the provider had started answering 509.
"""
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _Sweep(unittest.TestCase):
    N = 12

    def setUp(self):
        import models.database as mdb
        import services.stream_health as sh
        self.mdb, self.sh = mdb, sh
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        for i in range(self.N):
            db.add(mdb.Movie(tmdb_id=100 + i, title=f"Film {i}", source="provider_1", strm_path=f"/vod/m{i}.strm"))
        mdb.set_setting(db, "stream_health_batch_size", str(self.N * 2))
        db.commit()
        db.close()
        self.probed, self.slept = [], []
        self.live = False
        self.busy_after = None
        patches = (
            mock.patch.object(sh, "SessionLocal", self.Session),
            mock.patch.object(sh, "_check_item", self._check),
            mock.patch.object(sh, "_live_streams_active", lambda: self.live),
            mock.patch.object(sh.time, "sleep", lambda s: self.slept.append(s)),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _check(self, db, item, media_type, providers):
        self.probed.append(item.title)
        if self.busy_after is not None and len(self.probed) > self.busy_after:
            self.sh._probe_state["provider_busy"] = True
            return None
        return True

    def _cursor(self):
        db = self.Session()
        try:
            return int(self.mdb.get_setting(db, "stream_health_cursor_movie", "0"))
        finally:
            db.close()

    def _last_run(self):
        import json
        db = self.Session()
        try:
            return json.loads(self.mdb.get_setting(db, "stream_health_last_run", "{}"))
        finally:
            db.close()


class SweepStandsAsideForLiveTv(_Sweep):
    def test_nothing_is_probed_while_live_tv_is_being_proxied(self):
        self.live = True
        self.sh.run_stream_health_sweep()
        self.assertEqual([], self.probed, "a probe is a provider connection; a live stream was running")
        self.assertEqual(0, self._cursor(), "titles that were not probed must not be skipped")
        self.assertTrue(self._last_run().get("deferred"))

    def test_it_stops_as_soon_as_a_live_stream_starts(self):
        def check(db, item, media_type, providers):
            self.probed.append(item.title)
            if len(self.probed) == 4:
                self.live = True
            return True
        with mock.patch.object(self.sh, "_check_item", check):
            self.sh.run_stream_health_sweep()
        self.assertEqual(4, len(self.probed))
        self.assertEqual(4, self._cursor(), "the cursor must rest on the first title NOT probed")

    def test_an_idle_server_is_swept_in_full(self):
        self.sh.run_stream_health_sweep()
        self.assertEqual(self.N, len(self.probed))
        self.assertEqual(self.N, self._cursor())
        self.assertFalse(self._last_run().get("deferred"))


class SweepStopsWhenTheProviderIsBusy(_Sweep):
    def test_a_429_or_509_ends_the_sweep(self):
        self.busy_after = 3
        self.sh.run_stream_health_sweep()
        self.assertEqual(4, len(self.probed), "kept hammering a provider that had said it was over its limit")
        self.assertTrue(self._last_run().get("provider_busy"))

    def test_the_probe_reports_a_busy_provider(self):
        class R:
            status_code = 509

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        self.sh._probe_state["provider_busy"] = False
        with mock.patch.object(self.sh.requests, "get", return_value=R()):
            self.assertIsNone(self.sh._probe_url("http://p/x", "UA"))
        self.assertTrue(self.sh._probe_state["provider_busy"])


class SweepPacesItself(_Sweep):
    def test_there_is_a_pause_between_probes(self):
        self.sh.run_stream_health_sweep()
        self.assertGreaterEqual(len(self.slept), self.N - 1)
        self.assertTrue(all(s >= 1 for s in self.slept), self.slept)


class SweepRunsAtAQuietHour(unittest.TestCase):
    def test_it_is_scheduled_by_the_clock_not_by_container_uptime(self):
        import re
        from pathlib import Path
        main = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        job = re.search(r"add_job\(\s*run_stream_health_sweep,\s*([A-Za-z]+Trigger)\(([^)]*)\)", main)
        self.assertIsNotNone(job)
        self.assertEqual("CronTrigger", job.group(1),
                         "an interval trigger fires N hours after the last restart, whenever that was")
        hour = int(re.search(r"hour=(\d+)", job.group(2)).group(1))
        self.assertIn(hour, range(2, 7), "should fall in the small hours")


class LiveDetection(unittest.TestCase):
    """_live_streams_active() must work with either limiter in routers/livetv.py."""

    def _with(self, **attrs):
        import types
        import services.stream_health as sh
        fake = types.SimpleNamespace(**attrs)
        with mock.patch.dict("sys.modules", {"routers.livetv": fake}), \
                mock.patch("routers.livetv", fake, create=True):
            import routers
            with mock.patch.object(routers, "livetv", fake, create=True):
                return sh._live_streams_active()

    def test_the_slot_counter(self):
        import types
        self.assertTrue(self._with(_stream_slots=types.SimpleNamespace(active=1), _shared_streams={}))
        self.assertFalse(self._with(_stream_slots=types.SimpleNamespace(active=0), _shared_streams={}))

    def test_a_shared_upstream(self):
        import types
        self.assertTrue(self._with(_stream_slots=types.SimpleNamespace(active=0), _shared_streams={7: object()}))

    def test_the_original_semaphore(self):
        import types
        self.assertTrue(self._with(_stream_semaphore=types.SimpleNamespace(_value=5), _MAX_CONCURRENT_STREAMS=6))
        self.assertFalse(self._with(_stream_semaphore=types.SimpleNamespace(_value=6), _MAX_CONCURRENT_STREAMS=6))
        self.assertFalse(self._with(_stream_semaphore=None, _MAX_CONCURRENT_STREAMS=6))


if __name__ == "__main__":
    unittest.main()

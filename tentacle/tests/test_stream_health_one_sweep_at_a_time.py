"""Two stream health sweeps must not run at once (#455).

Run from the tentacle/ directory:  python -m unittest discover -s tests

"Run sweep" on the Health page started a new thread on every press, and the
04:30 job's max_instances=1 only covers the scheduler's own runs. A second
sweep read the same cursor (written back only after the batch), so it probed
the same titles alongside the first -- two provider connections at once, which
is what makes a one-connection account answer 509 -- and its start reset the
shared "provider busy" flag, so the first sweep carried on after a 509.
"""
import threading
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tmp_dirs import temp_dir


class OneSweepAtATime(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        import services.stream_health as sh
        self.sh = sh
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db",
                               connect_args={"check_same_thread": False, "timeout": 30})
        mdb.Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        for i in range(12):
            db.add(mdb.Movie(tmdb_id=100 + i, title=f"Film {i}", source="provider_1", strm_path=f"/vod/m{i}.strm"))
        mdb.set_setting(db, "stream_health_batch_size", "8")   # 4 movies per run
        db.close()
        self.lock, self.open, self.max_open, self.probes = threading.Lock(), 0, 0, []
        self.on_probe = lambda n: True
        for p in (mock.patch.object(sh, "SessionLocal", Session),
                  mock.patch.object(sh, "_check_item", self._probe),
                  mock.patch.object(sh, "_live_streams_active", lambda: False),
                  mock.patch.object(sh.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(sh._probe_state.update, provider_busy=False)

    def _probe(self, db, item, media_type, providers):   # one provider connection
        with self.lock:
            self.open += 1
            self.max_open = max(self.max_open, self.open)
            self.probes.append((threading.get_ident(), item.title))
            n = len(self.probes)
        try:
            return self.on_probe(n)
        finally:
            with self.lock:
                self.open -= 1

    def _second_sweep(self, start):
        job = threading.Thread(target=start, name="second")
        job.start()
        job.join(5)
        self.assertFalse(job.is_alive(), "the second start waited for the first sweep instead of returning")

    def test_the_nightly_job_during_a_sweep_probes_nothing(self):
        def first_probe_starts_the_0430_job(n):
            if n == 1:
                self._second_sweep(self.sh.run_stream_health_sweep)
            return True
        self.on_probe = first_probe_starts_the_0430_job
        self.sh.run_stream_health_sweep()
        titles = [t for _, t in self.probes]
        self.assertEqual(1, self.max_open, f"probes open at once: {self.max_open}; titles probed: {titles}")
        self.assertEqual(["Film 0", "Film 1", "Film 2", "Film 3"], titles)

    def test_a_509_stops_the_sweep_that_saw_it(self):
        def second_probe_is_answered_509(n):
            if n == 2:
                self.sh._note_provider_busy()
                self._second_sweep(self.sh.run_stream_health_sweep)
                return None
            return True
        self.on_probe = second_probe_is_answered_509
        self.sh.run_stream_health_sweep()
        after = [t for _, t in self.probes[2:]]
        self.assertEqual([], after, "kept probing after the provider answered 509")

    def test_run_sweep_during_a_sweep_answers_not_started(self):
        from routers.health import trigger_stream_sweep
        answers = []

        def press_the_button(n):
            if n == 1:
                answers.append(trigger_stream_sweep())
            return True
        self.on_probe = press_the_button
        self.sh.run_stream_health_sweep()
        self.assertEqual([{"started": False}], answers)
        self.assertEqual(4, len(self.probes), [t for _, t in self.probes])

    def test_run_sweep_starts_one_when_none_runs(self):
        from routers.health import trigger_stream_sweep
        done = threading.Event()
        self.on_probe = lambda n: (n == 4 and done.set()) or True
        self.assertEqual({"started": True}, trigger_stream_sweep())
        self.assertTrue(done.wait(5), "the sweep the button started probed nothing")
        # The lock is free again once that sweep ends: the next one runs.
        for _ in range(50):
            if not self.sh.stream_health_sweep_running():
                break
            threading.Event().wait(0.1)
        self.assertFalse(self.sh.stream_health_sweep_running())
        self.sh.run_stream_health_sweep()
        self.assertEqual(8, len(self.probes))


if __name__ == "__main__":
    unittest.main()

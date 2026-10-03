"""Only one stream health sweep runs at a time.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p "test_stream_health_sweep_single_run.py"

services/stream_health.py promises "one at a time with a pause": every probe
is a connection on the provider account live TV streams from, and an Xtream
account that allows one connection answers a second one by refusing the one
that was already open (HTTP 509). That held inside one run of
run_stream_health_sweep(), but nothing stopped a second run starting while
the first was still going:

- POST /api/health/streams/sweep (the Health page's "Run sweep" button)
  started a new thread on every call. The dashboard re-enables the button
  after 5 s; a default sweep (100 titles, 3 s apart) takes five minutes.
- The 04:30 job has max_instances=1, which only covers the scheduler's own
  runs, not a manual one already going.

The second run read the same cursor (the first writes it back only when its
batch is done), so it probed the very same titles again while the first
run's probe was still open: two provider connections at once. And it started
by resetting the shared "the provider answered 429/509" flag, so a first run
the provider had just told to stop carried on probing -- the hazard
recheck_known_bad() already avoids ("resetting the shared flag could un-stop
a sweep running now").

Invariants:
  I1  at most one sweep probe is open at any moment, whoever started the runs
      (the button, the 04:30 job, both, several times);
  I2  a start while a sweep runs probes nothing and resets nothing; the
      button says a sweep is already running;
  I3  a 429/509 stops the run that saw it, whatever starts after it;
  I4  the cursor rests on the first title the run did not probe (a failed
      run leaves it where it was): nothing skipped, nothing probed twice;
  I5  a run that ended -- done, stood aside for live TV, stopped by a 509,
      or failed with an exception -- never keeps the next one out.

AnyMixOfStartsAndFaults checks all five over STREAM_SWEEP_PROPERTY_SEEDS
(default 1000) random runs: 0-3 extra starts (button or 04:30 job) at random
probes and, in three runs out of four, a 509, live TV starting, or a probe
raising at a random probe. A probe that raises ends in _sweep()'s own except;
TheNextSweepStillRuns also raises before it (SessionLocal), where only the
wrapper's finally gives the lock back.
"""
import os
import random
import threading
import time
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tmp_dirs import temp_dir

_real_sleep = time.sleep   # the sweep's pause is patched out (module-wide)


class _Sweeps(unittest.TestCase):
    N = 12          # movies in the library
    BATCH = 8       # stream_health_batch_size -> 4 movies per run

    def setUp(self):
        import models.database as mdb
        import services.stream_health as sh
        self.mdb, self.sh = mdb, sh
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db",
                               connect_args={"check_same_thread": False, "timeout": 30})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        for i in range(self.N):
            db.add(mdb.Movie(tmdb_id=100 + i, title=f"Film {i}", source="provider_1",
                             strm_path=f"/vod/m{i}.strm"))
        mdb.set_setting(db, "stream_health_batch_size", str(self.BATCH))
        db.commit()
        db.close()

        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.probes = []                    # (thread ident, title)
        self.runs_started = 0
        self.runs_finished = 0
        self.live = False
        self.real_sweep = sh.run_stream_health_sweep   # what main.py's scheduler holds
        sh._probe_state["provider_busy"] = False

        def counted_sweep():
            with self.lock:
                self.runs_started += 1
            try:
                self.real_sweep()
            finally:
                with self.lock:
                    self.runs_finished += 1

        patches = (
            mock.patch.object(sh, "SessionLocal", self.Session),
            mock.patch.object(sh, "_check_item", self._check),
            mock.patch.object(sh, "_live_streams_active", lambda: self.live),
            mock.patch.object(sh.time, "sleep", lambda s: None),
            # routers/health.py imports the name at call time: the button
            # starts this wrapper, which runs the real sweep.
            mock.patch.object(sh, "run_stream_health_sweep", counted_sweep),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._wait_all_done)

    # A probe: one provider connection, open from enter to return.
    def _check(self, db, item, media_type, providers):
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.probes.append((threading.get_ident(), item.title))
        try:
            return self._probe(item)
        finally:
            with self.lock:
                self.in_flight -= 1

    def _probe(self, item):
        return True

    def _press_run_sweep(self):
        from routers import health
        return health.trigger_stream_sweep()

    def _wait(self, cond, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            _real_sleep(0.01)
        return cond()

    def _all_done(self):
        return (self.runs_finished >= self.runs_started
                and not any(t.name.startswith("nightly-") and t.is_alive() for t in threading.enumerate()))

    def _wait_all_done(self):
        self._wait(self._all_done, 15)

    def _titles(self):
        return [t for _, t in self.probes]

    def _cursor(self):
        db = self.Session()
        try:
            return int(self.mdb.get_setting(db, "stream_health_cursor_movie", "0"))
        finally:
            db.close()


class ASecondRunWhileOneIsProbing(_Sweeps):
    """The first run is in the middle of a probe (a connection is open) when
    a second run is started."""

    def setUp(self):
        super().setUp()
        self.first_open = threading.Event()
        self.release = threading.Event()
        self.held = False

    def _probe(self, item):
        with self.lock:
            hold = not self.held
            self.held = True
        if hold:                    # the first run's first probe stays open
            self.first_open.set()
            self.release.wait(10)
        return True

    def _second_run_settles(self):
        # Give a second run that did start the time to probe.
        self._wait(lambda: self.runs_finished >= 1 or self.max_in_flight >= 2, 2)

    def _finish(self):
        self.release.set()
        self.assertTrue(self._wait(self._all_done, 15), "the sweeps did not finish")

    def test_pressing_run_sweep_again_opens_no_second_connection(self):
        self.assertEqual({"started": True}, self._press_run_sweep())
        self.assertTrue(self.first_open.wait(5), "the first sweep never probed")
        second = self._press_run_sweep()           # "Run sweep" again, 5 s later
        self._second_run_settles()
        self._finish()
        self.assertEqual(1, self.max_in_flight,
                         f"two sweeps had {self.max_in_flight} provider connections open at once "
                         f"(the module allows one at a time); probes: {self._titles()}")
        dupes = sorted({t for t in self._titles() if self._titles().count(t) > 1})
        self.assertEqual([], dupes, f"the second sweep probed the same titles again: {self._titles()}")
        self.assertEqual({"started": False}, second, "the button must say a sweep is already running")
        self.assertEqual(self.BATCH // 2, self._cursor())

    def test_the_nightly_job_firing_during_a_manual_sweep_opens_no_second_connection(self):
        self._press_run_sweep()
        self.assertTrue(self.first_open.wait(5), "the first sweep never probed")
        # 04:30: APScheduler calls the function main.py scheduled; its
        # max_instances=1 knows nothing about the button's thread.
        threading.Thread(target=self.real_sweep, name="nightly-sweep", daemon=True).start()
        self._wait(lambda: self.max_in_flight >= 2, 2)
        self._finish()
        self.assertEqual(1, self.max_in_flight,
                         f"the nightly sweep probed while the manual one had a connection open; "
                         f"probes: {self._titles()}")
        dupes = sorted({t for t in self._titles() if self._titles().count(t) > 1})
        self.assertEqual([], dupes, f"the same titles were probed twice: {self._titles()}")


class ASecondRunDoesNotUnstopTheFirst(_Sweeps):
    """The first run's second probe is answered 509 (the account is over its
    limit). Before that run gets to look at the flag, the nightly job fires."""

    def setUp(self):
        super().setUp()
        self.first_thread = None
        self.first_probes = 0
        self.busy_at = None

    def _probe(self, item):
        me = threading.get_ident()
        with self.lock:
            if self.first_thread is None:
                self.first_thread = me
            mine = me == self.first_thread
            if mine:
                self.first_probes += 1
            n = self.first_probes
        if mine and n == 2:
            self.sh._note_provider_busy()           # what _probe_url does on a 509
            with self.lock:
                self.busy_at = len(self.probes)
            nightly = threading.Thread(target=self.real_sweep, name="nightly-sweep", daemon=True)
            nightly.start()
            nightly.join(2)
            return None
        return True

    def test_a_509_still_stops_the_run_that_saw_it(self):
        self._press_run_sweep()
        self.assertTrue(self._wait(lambda: self.runs_finished >= 1, 15), "the sweep did not finish")
        self._wait_all_done()
        self.assertIsNotNone(self.busy_at, "the first sweep never reached its second probe")
        after = [t for ident, t in self.probes[self.busy_at:] if ident == self.first_thread]
        self.assertEqual([], after,
                         "the provider had answered 509, and the sweep that saw it kept probing "
                         f"because a second run reset the shared flag: {after}")


class TheNextSweepStillRuns(_Sweeps):
    def setUp(self):
        super().setUp()
        self.probe_raises = False   # not self.fail: that is TestCase.fail()

    def _probe(self, item):
        if self.probe_raises:
            raise RuntimeError("the connection broke")
        return True

    def test_after_a_sweep_that_failed(self):
        # A probe that raises is caught by _sweep()'s own except ("sweep failed").
        self.probe_raises = True
        with mock.patch.object(self.sh, "logger"):   # "sweep failed" is expected here
            self.real_sweep()
        self.probe_raises = False
        self.probes.clear()
        self.assertEqual({"started": True}, self._press_run_sweep())
        self.assertTrue(self._wait(lambda: self.runs_finished >= 1, 15), "the sweep did not finish")
        self.assertEqual(self.BATCH // 2, len(self.probes),
                         f"a sweep that failed kept the next one out: {self._titles()}")

    def test_after_a_sweep_that_raised_outside_its_own_except(self):
        # db = SessionLocal() runs before _sweep()'s try: only the wrapper's
        # finally gives the lock back when that raises.
        with mock.patch.object(self.sh, "SessionLocal", side_effect=RuntimeError("database is locked")):
            with self.assertRaises(RuntimeError):
                self.real_sweep()
        self.assertEqual({"started": True}, self._press_run_sweep(),
                         "a sweep that raised kept the lock: the button says one is still running")
        self.assertTrue(self._wait(lambda: self.runs_finished >= 1, 15), "the sweep did not finish")
        self.assertEqual(self.BATCH // 2, len(self.probes),
                         f"a sweep that raised kept the next one out: {self._titles()}")


class AnyMixOfStartsAndFaults(_Sweeps):
    """I1-I5 over random runs; a failing seed is in the message."""
    N = 6
    SEEDS = int(os.environ.get("STREAM_SWEEP_PROPERTY_SEEDS", "1000"))

    def setUp(self):
        super().setUp()
        self.outer = None
        self.plan = {}
        self.outer_probes = 0
        self.pressed = []
        quiet = mock.patch.object(self.sh, "logger")   # the 509 / "sweep failed" lines
        quiet.start()
        self.addCleanup(quiet.stop)

    def _probe(self, item):
        if threading.get_ident() != self.outer:
            return True
        index = self.outer_probes
        self.outer_probes += 1
        result = True
        for event in self.plan.get(index, ()):
            if event == "button":
                before = set(threading.enumerate())
                self.pressed.append(self._press_run_sweep())
                # A run the press did start gets to probe while this one is open.
                for t in set(threading.enumerate()) - before:
                    t.join(2)
            elif event == "nightly":
                t = threading.Thread(target=self.real_sweep, name="nightly-sweep", daemon=True)
                t.start()
                t.join(2)
            elif event == "busy":
                self.sh._note_provider_busy()           # what _probe_url does on a 429/509
                result = None
            elif event == "live":
                self.live = True                        # somebody starts watching
            elif event == "raise":
                raise RuntimeError("the connection broke")
        return result

    def _one(self, seed):
        rng = random.Random(seed)
        per_type = rng.randint(1, self.N + 1)
        start = rng.randrange(self.N)
        walk = min(per_type, self.N - start)            # titles this run reaches
        plan = {}
        for _ in range(rng.randint(0, 3)):
            plan.setdefault(rng.randrange(walk), []).append(rng.choice(("button", "nightly")))
        fault = rng.choice((None, "busy", "live", "raise"))
        at = rng.randrange(walk)
        if fault == "raise":
            plan.setdefault(at, []).append(fault)       # ends the probe: last
        elif fault:
            events = plan.setdefault(at, [])
            events.insert(rng.randint(0, len(events)), fault)

        db = self.Session()
        self.mdb.set_setting(db, "stream_health_batch_size", str(2 * per_type))
        self.mdb.set_setting(db, "stream_health_cursor_movie", str(start))
        db.close()
        self.plan, self.outer, self.outer_probes = plan, threading.get_ident(), 0
        self.probes, self.pressed, self.live = [], [], False
        self.in_flight = self.max_in_flight = 0

        self.real_sweep()                               # the run everything else lands on
        self.assertTrue(self._wait(self._all_done, 15), f"seed {seed}: the sweeps did not finish")

        stop = at + 1 if fault else walk
        want = [f"Film {i}" for i in range(start, start + stop)]
        mine = [t for ident, t in self.probes if ident == self.outer]
        others = [t for ident, t in self.probes if ident != self.outer]
        why = f"seed {seed} (start {start}, {per_type} per run, plan {plan}): probes {self._titles()}"
        self.assertEqual(1, self.max_in_flight, f"I1/I5 {self.max_in_flight} probes open at once; {why}")
        self.assertEqual([], others, f"I2 a second run probed while the first was running; {why}")
        self.assertEqual(want, mine, f"I3/I5 the run probed the wrong titles; {why}")
        self.assertEqual(start if fault == "raise" else start + stop, self._cursor(), f"I4 cursor; {why}")
        self.assertTrue(all(r == {"started": False} for r in self.pressed),
                        f"I2 the button said {self.pressed} while a sweep was running; {why}")

    def test_one_sweep_at_a_time_whatever_happens(self):
        for seed in range(1, self.SEEDS + 1):
            self._one(seed)
        # I5 for the last seed too: the next run is not kept out.
        self.plan, self.probes, self.live = {}, [], False
        self.real_sweep()
        self.assertTrue(self.probes, "the last run kept the next one out")


if __name__ == "__main__":
    unittest.main()

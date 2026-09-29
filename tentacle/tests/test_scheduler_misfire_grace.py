"""#271: a daily job whose trigger passed while the process was frozen still runs.

APScheduler 3 drops a run that starts more than misfire_grace_time (default
1 s) after its trigger: a backup that froze the container for a minute, a
clock step or a stalled host skipped that night's sync with one WARNING.
These tests run APScheduler's own executor check (run_job) on the jobs as
main.py schedules them, with the trigger well in the past.
"""
import logging
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir

try:
    import main
    from apscheduler.executors.base import run_job
    from apscheduler.schedulers.background import BackgroundScheduler
except Exception:  # pragma: no cover
    main = None


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class DailyJobsSurviveAFreeze(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        # A scheduler configured like main's, started paused so jobs are real
        # (defaults applied) but nothing runs on its own.
        self.sched = BackgroundScheduler(job_defaults=main.scheduler._job_defaults,
                                         timezone=main.scheduler.timezone)
        self.sched.start(paused=True)
        self.addCleanup(self.sched.shutdown, wait=False)
        for p in (mock.patch.object(main, "SessionLocal", sessionmaker(bind=engine)),
                  mock.patch.object(main, "scheduler", self.sched)):
            p.start()
            self.addCleanup(p.stop)

    def _late_run(self, job_id, late_by):
        job = self.sched.get_job(job_id)
        run_time = datetime.now(self.sched.timezone) - late_by
        events = run_job(job, "default", [run_time], "apscheduler.executors.default")
        return [e.code for e in events]

    def test_nightly_sync_runs_after_a_90_second_freeze(self):
        fn = mock.Mock(__name__="run_scheduled_sync", __qualname__="run_scheduled_sync")
        with mock.patch.object(main, "run_scheduled_sync", fn):
            self.assertTrue(main.reschedule_main_sync("0 3 * * *"))
            self._late_run("main_sync", timedelta(seconds=90))
        fn.assert_called_once()

    def test_nightly_sync_catches_up_hours_late_but_not_a_day(self):
        fn = mock.Mock(__name__="run_scheduled_sync", __qualname__="run_scheduled_sync")
        with mock.patch.object(main, "run_scheduled_sync", fn):
            main.reschedule_main_sync("0 3 * * *")
            self._late_run("main_sync", timedelta(hours=5))
            self.assertEqual(1, fn.call_count)
            self._late_run("main_sync", timedelta(hours=20))
            self.assertEqual(1, fn.call_count)

    def test_other_jobs_get_minutes_of_grace(self):
        fn = mock.Mock(__name__="once", __qualname__="once")
        main.schedule_once(fn, 1, "catch_up_test")
        self._late_run("catch_up_test", timedelta(seconds=30))
        fn.assert_called_once()


if __name__ == "__main__":
    unittest.main()

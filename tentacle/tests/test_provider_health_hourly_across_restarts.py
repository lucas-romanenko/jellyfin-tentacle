"""Provider and backup-address checks at most once an hour, restarts included.

Every start ran the provider health check two minutes later, on top of the
hourly one: about 25 deploys in a day made 34 checks, each a real connection
on every provider and every backup address. Now a start skips that check
while the last one is under an hour old, and the hourly run comes an hour
after the last check instead of an hour after the start. "Check now" by an
admin still runs. The clock is injected, nothing waits.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p test_provider_health_hourly_across_restarts.py
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Setting
from services import provider_health as ph
from tmp_dirs import temp_dir

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone(timedelta(hours=-4)))


class _Db(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        clock = mock.patch.object(ph, "_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def state(self, **entry):
        self.db.add(Setting(key=ph.SETTING_KEY, value=json.dumps({"1": {"state": "ok", **entry}})))
        self.db.commit()


class NextDue(_Db):
    def test_a_check_under_an_hour_ago_puts_the_next_an_hour_after_it(self):
        self.state(checked_at=(NOW - timedelta(minutes=20)).isoformat())
        self.assertEqual(NOW + timedelta(minutes=40), ph.next_due(self.db))

    def test_a_check_an_hour_ago_or_more_is_due_now(self):
        self.state(checked_at=(NOW - timedelta(minutes=60)).isoformat())
        self.assertIsNone(ph.next_due(self.db))

    def test_never_checked_is_due_now(self):
        self.assertIsNone(ph.next_due(self.db))

    def test_a_run_that_checked_nothing_does_not_count(self):
        # Stood aside for live TV, or every provider busy: only skipped_at moves.
        self.state(checked_at=(NOW - timedelta(hours=3)).isoformat(),
                   skipped_at=(NOW - timedelta(minutes=5)).isoformat())
        self.assertIsNone(ph.next_due(self.db))

    def test_the_newest_provider_counts(self):
        self.db.add(Setting(key=ph.SETTING_KEY, value=json.dumps({
            "1": {"checked_at": (NOW - timedelta(hours=2)).isoformat()},
            "2": {"checked_at": (NOW - timedelta(minutes=10)).isoformat()},
            "3": {"checked_at": "not a time"},
        })))
        self.db.commit()
        self.assertEqual(NOW + timedelta(minutes=50), ph.next_due(self.db))


class StartUp(_Db):
    def _start(self):
        import main
        with mock.patch.object(main, "schedule_once") as once, \
             mock.patch.object(main, "scheduler") as scheduler, \
             mock.patch.object(main, "SessionLocal", self.Session), \
             mock.patch("models.database.SessionLocal", self.Session), \
             self.assertLogs("main", "INFO") as logs:
            main.setup_scheduler(self.db)
        jobs = [c.args[2] for c in once.call_args_list]
        hourly = next(c.args[1] for c in scheduler.add_job.call_args_list
                      if c.kwargs.get("id") == "provider_health")
        said = [line for line in logs.output if "Provider health check scheduled" in line]
        return jobs, hourly, said

    def test_a_restart_within_the_hour_checks_nothing_and_says_so(self):
        self.state(checked_at=(NOW - timedelta(minutes=20)).isoformat())
        jobs, hourly, said = self._start()
        self.assertNotIn("provider_health_first", jobs)
        self.assertEqual(NOW + timedelta(minutes=40), hourly.start_date)
        self.assertEqual(timedelta(hours=1), hourly.interval)
        self.assertEqual(1, len(said))
        self.assertIn("start-up check skipped (last check 11:40), next at 12:40", said[0])

    def test_a_restart_after_the_hour_checks_shortly_after_the_start(self):
        self.state(checked_at=(NOW - timedelta(minutes=90)).isoformat())
        jobs, hourly, said = self._start()
        self.assertIn("provider_health_first", jobs)
        self.assertEqual(timedelta(hours=1), hourly.interval)
        self.assertNotIn("skipped", said[0])

    def test_the_first_start_checks(self):
        jobs, _, _ = self._start()
        self.assertIn("provider_health_first", jobs)


class CheckNowStillRuns(_Db):
    def test_check_now_runs_with_a_check_minutes_ago(self):
        self.state(checked_at=(NOW - timedelta(minutes=5)).isoformat())
        with mock.patch.object(ph, "check_providers", return_value={"checked": 1}) as check:
            self.assertEqual({"checked": 1}, ph.check_now(self.db))
        check.assert_called_once_with(self.db, confirm_delay=None)


if __name__ == "__main__":
    unittest.main()

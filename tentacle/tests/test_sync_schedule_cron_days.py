"""A stored sync schedule runs on the days cron means (#458).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The 5-field cron went straight into APScheduler 3's CronTrigger, which counts
0 = Monday ... 6 = Sunday, refuses 7, and needs both day fields to match:
"0 4 * * 1-5" ran Tuesday to Saturday, "0 3 * * 0" on Mondays, "0 3 1 * 1"
only on a 1st that is a Tuesday, and "0 3 * * 7" or "0 24 * * *" left the
install with no nightly sync after a restart while the settings form answered
success.
"""
import logging
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import get_setting
from tmp_dirs import temp_dir

try:
    import main
    import pytz
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from services.sync_schedule import sync_trigger
except Exception:  # pragma: no cover
    main = None

# Saturday 2026-10-03 03:34, as in the report
TZ = "America/Toronto"
START = datetime(2026, 10, 3, 3, 34)
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)  # datetime.weekday()


def fires(trigger, n=21):
    """The next n fire times from START."""
    out, prev, now = [], None, pytz.timezone(TZ).localize(START)
    for _ in range(n):
        prev = trigger.get_next_fire_time(prev, now)
        out.append(prev)
        now = prev + timedelta(seconds=1)
    return out


def trigger(cron):
    t = sync_trigger(cron)
    # The scheduler's own timezone in production; a fixed one here
    for c in getattr(t, "triggers", [t]):
        c.timezone = pytz.timezone(TZ)
    return t


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class CronDays(unittest.TestCase):
    def weekdays(self, cron):
        return {f.weekday() for f in fires(trigger(cron))}

    def test_the_reported_schedules(self):
        runs = fires(trigger("0 4 * * 1-5"))
        self.assertEqual({MON, TUE, WED, THU, FRI}, {f.weekday() for f in runs})
        self.assertEqual(datetime(2026, 10, 5, 4, 0), runs[0].replace(tzinfo=None))  # not Saturday
        self.assertEqual({SUN}, self.weekdays("0 3 * * 0"))
        self.assertEqual({SUN}, self.weekdays("0 3 * * 7"))
        self.assertEqual(datetime(2026, 10, 4, 3, 0), fires(trigger("0 3 * * 7"))[0].replace(tzinfo=None))

    def test_both_day_fields_run_on_either(self):
        runs = fires(trigger("0 3 1 * 1"), 30)
        self.assertTrue(all(f.day == 1 or f.weekday() == MON for f in runs), runs)
        self.assertEqual(datetime(2026, 10, 5, 3, 0), runs[0].replace(tzinfo=None))
        self.assertIn(datetime(2026, 11, 1, 3, 0), [f.replace(tzinfo=None) for f in runs])  # a Sunday
        # A "*" day of week keeps the day of month alone, as in cron
        self.assertEqual({1}, {f.day for f in fires(trigger("0 3 1 * *"), 5)})
        self.assertEqual({1}, {f.day for f in fires(trigger("0 3 1 * */1"), 5)})

    def test_day_of_week_forms(self):
        cases = {
            "1-5": {MON, TUE, WED, THU, FRI},
            "mon-fri": {MON, TUE, WED, THU, FRI},
            "MON-FRI": {MON, TUE, WED, THU, FRI},
            "sat-sun": {SAT, SUN},       # ran through Sunday before, still does
            "mon-sun": set(range(7)),
            "sun-sat": set(range(7)),
            "5-7": {FRI, SAT, SUN},
            "0-7": set(range(7)),
            "6,0": {SAT, SUN},
            "sun,wed": {SUN, WED},
            "1,3,5": {MON, WED, FRI},
            "1-5/2": {MON, WED, FRI},
            "*/2": {SUN, TUE, THU, SAT},
            "2/2": {TUE, THU, SAT},
            "6": {SAT},
            "sun": {SUN},
        }
        for dow, days in cases.items():
            with self.subTest(dow=dow):
                self.assertEqual(days, self.weekdays(f"0 3 * * {dow}"))

    def test_a_daily_schedule_is_the_same_trigger_as_before(self):
        for cron in ("0 3 * * *", "30 4 * * *", "0 */6 * * *", "0 2 1 * *", "*/30 * * * *"):
            with self.subTest(cron=cron):
                p = cron.split()
                before = CronTrigger(minute=p[0], hour=p[1], day=p[2], month=p[3], day_of_week=p[4])
                self.assertEqual(str(before), str(sync_trigger(cron)))

    def test_what_is_no_schedule_raises(self):
        for cron in ("0 24 * * *", "60 3 * * *", "0 3 32 * *", "0 3 * 13 *", "0 3 * * 8",
                     "0 3 * * 5-1", "0 3 * * fri-mon", "0 3 * * 1-", "0 3 * * 1,,2", "0 3 * * */0",
                     "0 3 * * funday", "0 3 * *", "every night please", "", None):
            with self.subTest(cron=cron):
                with self.assertRaises(ValueError):
                    sync_trigger(cron)


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class StoredSchedule(unittest.TestCase):
    """Startup (reschedule_main_sync with no argument) and the settings form,
    on a real paused scheduler."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.sched = BackgroundScheduler(job_defaults=main.scheduler._job_defaults, timezone=TZ)
        self.sched.start(paused=True)
        self.addCleanup(self.sched.shutdown, wait=False)
        for p in (mock.patch.object(main, "SessionLocal", self.Session),
                  mock.patch.object(main, "scheduler", self.sched)):
            p.start()
            self.addCleanup(p.stop)

    def _store(self, value):
        mdb.set_setting(self.db, "sync_schedule", value)

    def _job_trigger(self):
        job = self.sched.get_job("main_sync")
        self.assertIsNotNone(job, "no nightly sync scheduled")
        return job.trigger

    def test_startup_with_a_sunday_schedule_runs_on_sundays(self):
        self._store("0 3 * * 7")
        self.assertTrue(main.reschedule_main_sync())
        t = self._job_trigger()
        self.assertIn("day_of_week='sun'", str(t))

    def test_startup_with_an_unusable_schedule_runs_at_the_default(self):
        for value in ("0 24 * * *", "0 3 * * 8", "0 3 * * fri-mon"):
            with self.subTest(value=value):
                self.sched.remove_all_jobs()
                self._store(value)
                self.assertTrue(main.reschedule_main_sync())
                self.assertEqual(str(sync_trigger("0 3 * * *")), str(self._job_trigger()))
                info = main.get_schedule_info()
                self.assertFalse(info["usable"])
                self.assertEqual("03:00", info["time"])
                self.assertIsNotNone(info["next_run_human"])

    def test_schedule_info_for_a_usable_schedule(self):
        self._store("15 4 * * 1-5")
        main.reschedule_main_sync()
        info = main.get_schedule_info()
        self.assertTrue(info["usable"])
        self.assertEqual("04:15", info["time"])

    def _save(self, **settings):
        from routers import settings as r
        return r.update_settings(r.SettingsUpdate(settings=settings), db=self.db)

    def test_the_settings_form_refuses_an_unusable_schedule_and_keeps_the_job(self):
        self._store("0 4 * * 1-5")
        main.reschedule_main_sync()
        before = str(self.sched.get_job("main_sync").trigger)
        for value in ("0 24 * * *", "0 3 * * 8", "0 3 * *"):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as cm:
                    self._save(sync_schedule=value, recently_added_days="14")
                self.assertEqual(400, cm.exception.status_code)
                self.assertIn(value, cm.exception.detail)
                self.assertEqual("0 4 * * 1-5", get_setting(self.db, "sync_schedule"))
                self.assertEqual("", get_setting(self.db, "recently_added_days"), "nothing is stored")
                self.assertEqual(before, str(self.sched.get_job("main_sync").trigger))

    def test_the_settings_form_applies_a_weekday_schedule(self):
        self.assertEqual({"success": True}, self._save(sync_schedule="0 3 * * 0"))
        self.assertEqual("0 3 * * 0", get_setting(self.db, "sync_schedule"))
        self.assertIn("day_of_week='sun'", str(self.sched.get_job("main_sync").trigger))

    def test_a_blank_save_still_stores_the_default(self):
        self._store("0 4 * * 1-5")
        self.assertEqual({"success": True}, self._save(sync_schedule=""))
        self.assertEqual("0 3 * * *", get_setting(self.db, "sync_schedule"))


if __name__ == "__main__":
    unittest.main()

"""A stored sync schedule runs on the days cron means, and a stored value the
scheduler refuses still gets a nightly job.

Run from the tentacle/ directory:  python -m unittest discover -s tests

sync_schedule is a 5-field cron (the Settings page said "Use cron format"
until it became a time picker; POST /api/settings takes any value, and a Save
keeps a custom cron as it is, #385). reschedule_main_sync() handed field 5
straight to APScheduler 3's CronTrigger(day_of_week=...), which counts
0 = Monday and refuses 7, while cron counts 0 and 7 = Sunday, 1 = Monday:
"0 4 * * 1-5" ran Tuesday to Saturday, "0 3 * * 0" every Monday. A 5-field
value CronTrigger refused ("0 3 * * 7", "0 24 * * *") returned False with
no main_sync job at all, the case #157 closed only for a wrong field count.
APScheduler also wants both day fields at once where cron takes either.
"""
import logging
import random
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir

try:
    import main
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.util import localize
except Exception:  # pragma: no cover
    main = None

# Cron's own day numbers: 0 (and 7) = Sunday.
CRON_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
START = date(2026, 10, 5)  # a Monday; 35 days reach every day of the month
DAYS = 35


def fire_times(trigger):
    """Every (date, "HH:MM") the trigger fires on from START for DAYS days."""
    tz = trigger_tz(trigger)
    start = localize(datetime.combine(START, datetime.min.time()), tz)
    end = localize(datetime.combine(START + timedelta(days=DAYS), datetime.min.time()), tz)
    got, prev, now = set(), None, start
    while True:
        nxt = trigger.get_next_fire_time(prev, now)
        if nxt is None or nxt >= end:
            return got
        got.add((nxt.date(), nxt.strftime("%H:%M")))
        prev, now = nxt, nxt + timedelta(seconds=1)


def trigger_tz(trigger):
    return getattr(trigger, "timezone", None) or trigger.triggers[0].timezone


def cron_fires(dom, dow, at="03:00"):
    """Where cron (man 5 crontab) runs "M H dom * dow", worked out day by day
    from the fields themselves, independent of the code under test."""
    def num(token):
        return CRON_DAYS.index(token.lower()) if token.lower() in CRON_DAYS else int(token)

    def matches(field, value, low, high):
        for item in field.split(","):
            span, slash, step = item.partition("/")
            if span == "*":
                first, last = low, high
            elif "-" in span:
                a, b = span.split("-")
                first, last = num(a), num(b)
                if first and b.lower() == "sun":
                    last = 7  # "SAT-SUN" runs through Sunday, as APScheduler read it
            else:
                first = num(span)
                last = high if slash else first
            if first <= value <= last and (value - first) % int(step or 1) == 0:
                return True
        return False

    out = set()
    for i in range(DAYS):
        d = START + timedelta(days=i)
        weekday = d.isoweekday() % 7
        on_dom = matches(dom, d.day, 1, 31)
        on_dow = matches(dow, weekday, 0, 7) or (weekday == 0 and matches(dow, 7, 0, 7))
        # Either day field starting with "*" leaves the other one in charge;
        # with both set, either one is enough.
        hit = (on_dom and on_dow) if dom.startswith("*") or dow.startswith("*") else (on_dom or on_dow)
        if hit:
            out.add((d, at))
    return out


def weekdays(fires):
    return sorted({CRON_DAYS[d.isoweekday() % 7] for d, _ in fires}, key=CRON_DAYS.index)


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class StoredCronSchedule(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.jobs = []
        for p in (mock.patch.object(main, "SessionLocal", self.Session),
                  mock.patch.object(main.scheduler, "add_job",
                                    lambda fn, trigger, **kw: self.jobs.append((kw.get("id"), trigger)))):
            p.start()
            self.addCleanup(p.stop)

    def _stored(self, value):
        """Store value and schedule from settings, as at startup."""
        self.jobs.clear()
        db = self.Session()
        mdb.set_setting(db, "sync_schedule", value)
        db.close()
        return main.reschedule_main_sync()

    def test_weekday_numbers_mean_what_cron_means(self):
        for cron, want in (("0 4 * * 1-5", ["mon", "tue", "wed", "thu", "fri"]),
                           ("0 3 * * 0", ["sun"]),
                           ("0 3 * * 7", ["sun"]),
                           ("15 1 * * 0", ["sun"]),
                           ("0 3 * * 6,0", ["sun", "sat"]),
                           ("0 3 * * 1", ["mon"]),
                           ("0 3 * * 5-7", ["sun", "fri", "sat"]),
                           ("0 3 * * */2", ["sun", "tue", "thu", "sat"]),
                           # A step after one day runs from that day through
                           # Sunday (7), as CronTrigger reads "5/15" in the
                           # other fields.
                           ("0 3 * * 1/2", ["sun", "mon", "wed", "fri"]),
                           ("0 3 * * MON-FRI", ["mon", "tue", "wed", "thu", "fri"]),
                           # APScheduler read these right all along (its week
                           # ends on Sunday); they must not fall back to 03:00.
                           ("30 1 * * sat-sun", ["sun", "sat"]),
                           ("30 1 * * Mon-Sun", CRON_DAYS)):
            with self.subTest(cron=cron):
                ok = self._stored(cron)
                self.assertTrue(ok and self.jobs, f"{cron!r}: no main_sync job scheduled")
                self.assertEqual("main_sync", self.jobs[0][0])
                fires = fire_times(self.jobs[0][1])
                self.assertEqual(want, weekdays(fires), f"{cron!r} runs on the wrong days")
                self.assertEqual({cron.split()[1].zfill(2) + ":" + cron.split()[0].zfill(2)},
                                 {t for _, t in fires})

    def test_both_day_fields_set_runs_on_either_as_cron_does(self):
        self.assertTrue(self._stored("0 3 1 * 1"))
        fires = fire_times(self.jobs[0][1])
        self.assertIn((date(2026, 11, 1), "03:00"), fires, "the 1st (a Sunday) was skipped")
        self.assertEqual(cron_fires("1", "1"), fires)

    def test_the_default_and_every_time_the_page_writes_are_unchanged(self):
        for cron in ("0 3 * * *", "45 4 * * *", "0 0 * * *", "59 23 * * *"):
            with self.subTest(cron=cron):
                self.assertTrue(self._stored(cron))
                trigger = str(self.jobs[0][1])
                minute, hour = cron.split()[:2]
                self.assertEqual(f"cron[month='*', day='*', day_of_week='*', hour='{hour}', minute='{minute}']",
                                 trigger)

    def test_a_stored_five_field_value_the_scheduler_refuses_still_gets_the_nightly_job(self):
        # Same promise as a stored value with the wrong field count (#157):
        # the install is never left with no nightly sync.
        for cron in ("0 24 * * *", "61 3 * * *", "0 3 * * 8", "0 3 32 * *", "0 3 * 13 *",
                     "0 3 * * 5-1", "0 3 * * funday", "0 3 * * 1/0"):
            with self.subTest(cron=cron):
                ok = self._stored(cron)
                self.assertTrue(ok, f"{cron!r}: reschedule_main_sync() returned False")
                self.assertEqual(["main_sync"], [j[0] for j in self.jobs], f"{cron!r}: no nightly job")
                self.assertEqual("cron[month='*', day='*', day_of_week='*', hour='3', minute='0']",
                                 str(self.jobs[0][1]), f"{cron!r}: not the default time")

    def test_every_day_of_week_the_scheduler_took_before_is_still_taken(self):
        # Upgrade: a stored weekday field CronTrigger accepted before keeps
        # its own time instead of falling back to the default.
        from apscheduler.triggers.cron import CronTrigger
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        fields = names + [f"{a}-{b}" for a in names for b in names]
        fields += [str(a) for a in range(7)] + [f"{a}-{b}" for a in range(7) for b in range(7)]
        for field in fields:
            try:
                CronTrigger(day_of_week=field)
            except ValueError:
                continue
            with self.subTest(field=field):
                self.jobs.clear()
                self.assertTrue(main.reschedule_main_sync(f"30 1 * * {field}"), f"{field!r} is now refused")
                self.assertEqual({"01:30"}, {t for _, t in fire_times(self.jobs[0][1])})

    def test_a_refused_value_from_the_settings_form_leaves_the_live_job_alone(self):
        for cron in ("0 24 * * *", "0 3 * * 8"):
            with self.subTest(cron=cron):
                self.jobs.clear()
                self.assertFalse(main.reschedule_main_sync(cron))
                self.assertEqual([], self.jobs)


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class AnyCronDayFieldsRunWhereCronRunsThem(unittest.TestCase):
    """1,000 random day-of-month / day-of-week fields (numbers 0-7, names,
    ranges, steps, lists, "*"), each checked day by day against cron. A name
    range that ends in sun ("SAT-SUN") runs through Sunday, as APScheduler
    read it before; classic cron refuses it."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.jobs = []
        p = mock.patch.object(main.scheduler, "add_job",
                              lambda fn, trigger, **kw: self.jobs.append((kw.get("id"), trigger)))
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _dow(rnd):
        def day(n):
            # 7 written as a name gives "SAT-SUN" ranges too.
            return CRON_DAYS[n % 7].upper() if rnd.random() < 0.2 else str(n)
        items = []
        for _ in range(rnd.randint(1, 3)):
            kind = rnd.random()
            if kind < 0.15:
                item = "*"
            elif kind < 0.55:
                items.append(day(rnd.randint(0, 7)))
                continue
            else:
                a, b = sorted(rnd.sample(range(8), 2))
                item = f"{day(a)}-{day(b)}"
            if rnd.random() < 0.3:
                item += f"/{rnd.randint(1, 4)}"
            items.append(item)
        return ",".join(items)

    @staticmethod
    def _dom(rnd):
        kind = rnd.random()
        if kind < 0.5:
            return "*"
        if kind < 0.6:
            return f"*/{rnd.randint(2, 10)}"
        if kind < 0.8:
            return ",".join(str(rnd.randint(1, 31)) for _ in range(rnd.randint(1, 3)))
        a = rnd.randint(1, 28)
        return f"{a}-{rnd.randint(a, 31)}"

    def test_random_day_fields(self):
        for seed in range(1000):
            rnd = random.Random(seed)
            dom, dow = self._dom(rnd), self._dow(rnd)
            cron = f"0 3 {dom} * {dow}"
            self.jobs.clear()
            self.assertTrue(main.reschedule_main_sync(cron) and self.jobs,
                            f"seed {seed}: {cron!r} was refused")
            self.assertEqual(sorted(cron_fires(dom, dow)), sorted(fire_times(self.jobs[0][1])),
                             f"seed {seed}: {cron!r} does not run where cron runs it")


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class SettingsShowsTheNextRun(unittest.TestCase):
    """With a real (paused) scheduler: the next run the Settings page shows."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.sched = BackgroundScheduler(job_defaults=main.scheduler._job_defaults,
                                         timezone=main.scheduler.timezone)
        self.sched.start(paused=True)
        self.addCleanup(self.sched.shutdown, wait=False)
        for p in (mock.patch.object(main, "SessionLocal", self.Session),
                  mock.patch.object(main, "scheduler", self.sched)):
            p.start()
            self.addCleanup(p.stop)

    def _restart_with(self, value):
        db = self.Session()
        mdb.set_setting(db, "sync_schedule", value)
        db.close()
        self.sched.remove_all_jobs()
        main.reschedule_main_sync()
        return main.get_schedule_info()

    def test_sunday_as_7_has_a_next_run_on_a_sunday(self):
        info = self._restart_with("0 3 * * 7")
        self.assertIsNotNone(info["next_run_human"], "no nightly job after a restart")
        self.assertTrue(info["next_run_human"].startswith("Sun "), info["next_run_human"])

    def test_both_day_fields_set_still_has_a_next_run(self):
        self.assertIsNotNone(self._restart_with("0 3 1 * 1")["next_run_human"])

    def test_an_hour_of_24_runs_at_the_default_time(self):
        info = self._restart_with("0 24 * * *")
        self.assertIsNotNone(info["next_run_human"], "no nightly job after a restart")
        self.assertIn(" 3:00 AM", info["next_run_human"])


if __name__ == "__main__":
    unittest.main()

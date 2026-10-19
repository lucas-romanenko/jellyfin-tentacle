"""A stored sync schedule that is blank or no schedule still gets a nightly job (#157).

Run from the tentacle/ directory:  python -m unittest discover -s tests

main.reschedule_main_sync() read the setting straight from the DB: a stored
"  " was truthy, failed the 5-field check, and no nightly job was scheduled.
"""
import logging
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir

try:
    import main
except Exception:  # pragma: no cover
    main = None


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class StartupSchedule(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.jobs = []
        for p in (mock.patch.object(main, "SessionLocal", self.Session),
                  mock.patch.object(main.scheduler, "add_job",
                                    lambda fn, trigger, **kw: self.jobs.append((kw.get("id"), str(trigger))))):
            p.start()
            self.addCleanup(p.stop)

    def _store(self, value):
        db = self.Session()
        mdb.set_setting(db, "sync_schedule", value)
        db.close()

    def test_a_stored_blank_schedules_the_default(self):
        for value in ("", "   "):
            self.jobs.clear()
            self._store(value)
            self.assertTrue(main.reschedule_main_sync())
            self.assertEqual("main_sync", self.jobs[0][0])
            self.assertIn("hour='3'", self.jobs[0][1])

    def test_a_stored_value_that_is_no_schedule_falls_back(self):
        self._store("every night please")
        self.assertTrue(main.reschedule_main_sync())
        self.assertIn("hour='3'", self.jobs[0][1])

    def test_a_valid_stored_schedule_is_used(self):
        self._store("30 4 * * *")
        self.assertTrue(main.reschedule_main_sync())
        self.assertIn("hour='4'", self.jobs[0][1])

    def test_an_invalid_schedule_from_the_settings_form_is_still_refused(self):
        self.assertFalse(main.reschedule_main_sync("every night please"))
        self.assertEqual([], self.jobs)


if __name__ == "__main__":
    unittest.main()

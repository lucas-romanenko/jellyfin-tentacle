"""A Live TV sync that only found Jellyfin not answering doesn't say "Live TV isn't set up".

The hourly Live TV check (services/livetv_tuners.run_sync, check=True) ran during
Jellyfin's nightly tasks, got a 30 s read timeout and stored last_ok False with the
raw requests message. The admin alert check (services/admin_alerts._seen_livetv)
read that as "Live TV isn't set up in Jellyfin": a push to the admins, the
exception text (with the server's address) as the message, and "Live TV is
unavailable right now" for the family until the next hourly run an hour later,
while Live TV worked the whole time. Jellyfin not answering has its own alert
("Jellyfin can't be reached"); a timeout now leaves the Live TV alert as it is.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p test_livetv_setup_alert_unreachable.py
"""
import unittest
from unittest import mock

import requests


class SyncOutcome(unittest.TestCase):
    def _run(self, effect):
        from services import livetv_tuners as T
        with mock.patch.object(T, "SessionLocal"), \
                mock.patch.object(T, "sync", side_effect=effect), \
                mock.patch.object(T, "log_activity"), \
                mock.patch.object(T, "set_setting") as saved, \
                mock.patch.object(T, "tell_clients"), \
                mock.patch.dict(T.status, {}):
            T.run_sync("hourly check", check=True)
            return dict(T.status), saved

    def test_a_timeout_is_marked_unreachable_and_kept_after_a_restart(self):
        st, saved = self._run(requests.ReadTimeout("Read timed out. (read timeout=30)"))
        self.assertIs(False, st["last_ok"])
        self.assertTrue(st["last_unreachable"])
        self.assertIn('"last_unreachable": true', saved.call_args[0][2])

    def test_a_refused_connection_is_unreachable_too(self):
        st, _ = self._run(requests.ConnectionError("Connection refused"))
        self.assertTrue(st["last_unreachable"])

    def test_an_answer_jellyfin_refused_is_a_real_failure(self):
        st, _ = self._run(requests.HTTPError("500 Server Error", response=mock.Mock(status_code=500)))
        self.assertIs(False, st["last_ok"])
        self.assertFalse(st["last_unreachable"])

    def test_a_run_that_works_clears_it(self):
        from services import livetv_tuners as T
        with mock.patch.dict(T.status, {}):
            self._run(requests.ReadTimeout("x"))
            st, _ = self._run(lambda db, refresh=False: {"added": 0, "removed": 0, "favourites": 0,
                                                         "schedules": 0, "skipped": "nothing to do"})
        self.assertIs(True, st["last_ok"])
        self.assertFalse(st["last_unreachable"])


class AdminAlert(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from tmp_dirs import temp_dir
        engine = create_engine(f"sqlite:///{Path(temp_dir(self))}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        from services import admin_alerts as A
        self.A = A
        # only the Live TV check is real; check() keys its alerts by each check's name
        def quiet(name):
            def seen(db):
                return {}
            seen.__name__ = name
            return seen
        checks = tuple(A._seen_livetv if n == "_seen_livetv" else quiet(n) for n in (
            "_seen_providers", "_seen_guides", "_seen_arr", "_seen_downloads", "_seen_livetv", "_seen_jellyfin"))
        p = mock.patch.object(A, "CHECKS", checks)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(A, "_tell_admins")
        self.told = p.start(); self.addCleanup(p.stop)

    def _status(self, **st):
        from services import livetv_tuners as T
        base = {"running": False, "last_at": "2026-11-02T08:08:26Z", "last_ok": False, "last_message": "",
                "last_unreachable": False}
        base.update(st)
        return mock.patch.object(T, "last_status", lambda db: dict(base))

    def _active(self):
        return [r.key for r in self.A.active(self.db)]

    def test_a_timeout_raises_no_live_tv_alert(self):
        with self._status(last_message="HTTPConnectionPool(host='192.0.2.10', port=8096): Read timed out.",
                          last_unreachable=True):
            self.A.check(self.db)
        self.assertEqual([], self._active())
        self.told.assert_not_called()

    def test_a_real_setup_failure_still_raises_it(self):
        with self._status(last_message="Jellyfin refused the tuner"):
            self.A.check(self.db)
        self.assertEqual(["livetv:setup"], self._active())

    def test_a_timeout_after_a_real_failure_keeps_that_alert(self):
        with self._status(last_message="Jellyfin refused the tuner"):
            self.A.check(self.db)
        with self._status(last_message="Read timed out.", last_unreachable=True):
            self.A.check(self.db)
        self.assertEqual(["livetv:setup"], self._active())

    def test_a_status_saved_before_this_change_still_alerts(self):
        # no last_unreachable (a status saved by an older version): as before
        from services import livetv_tuners as T
        with mock.patch.object(T, "last_status", lambda db: {"running": False, "last_ok": False,
                                                              "last_message": "boom"}):
            self.A.check(self.db)
        self.assertEqual(["livetv:setup"], self._active())

    def test_working_again_clears_it(self):
        with self._status(last_message="Jellyfin refused the tuner"):
            self.A.check(self.db)
        with self._status(last_ok=True):
            self.A.check(self.db)
        self.assertEqual([], self._active())


if __name__ == "__main__":
    unittest.main()

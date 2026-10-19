"""Settings → Schedule's one "Sync now" (POST/GET /api/sync/run-now).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The dashboard had a sync button on four pages, each starting part of the work
(one provider's VOD sync, a Radarr scan, playlists). The redesigned Settings
has one schedule and one "Sync now", which runs the nightly sync itself, now.
Only one runs at a time, scheduled or not: the per-provider guards don't
cover the run's long Jellyfin tail, so a second one would rebuild every
playlist alongside the first.
"""
import threading
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir


class _Base(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        import routers.sync as rs
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.mdb, self.rs = mdb, rs
        saved = dict(rs._nightly_run)
        self.addCleanup(lambda: rs._nightly_run.update(saved))
        rs._nightly_run.update(running=False, trigger=None, started_at=None, finished_at=None)


class RunNow(_Base):
    def test_starts_the_nightly_sync_and_refuses_a_second(self):
        import main
        go = threading.Event()
        ran = []

        def fake_body():
            ran.append(True)
            go.wait(5)

        with mock.patch.object(main, "_run_scheduled_sync", fake_body):
            self.assertEqual(self.rs.run_now(), {"started": True})
            st = self.rs.run_now_status(self.db)
            self.assertTrue(st["running"])
            self.assertEqual(st["trigger"], "manual")
            again = self.rs.run_now()
            self.assertFalse(again["started"])
            # The scheduled run that comes meanwhile is skipped, not doubled
            main.run_scheduled_sync()
            go.set()
            for _ in range(100):
                if not self.rs._nightly_run["running"]:
                    break
                threading.Event().wait(0.05)
        self.assertEqual(ran, [True])
        st = self.rs.run_now_status(self.db)
        self.assertFalse(st["running"])
        self.assertIsNotNone(st["finished_at"])

    def test_a_failing_run_frees_the_slot(self):
        import main

        def boom():
            raise RuntimeError("no Jellyfin")

        with mock.patch.object(main, "_run_scheduled_sync", boom):
            with self.assertRaises(RuntimeError):
                main.run_scheduled_sync()
        self.assertFalse(self.rs._nightly_run["running"])
        self.assertTrue(self.rs.begin_nightly_run("schedule"))

    def test_status_names_the_provider_syncing(self):
        p = self.mdb.Provider(name="Bears", server_url="http://p.test", username="a", password="b", active=True)
        self.db.add(p)
        self.db.commit()
        self.assertTrue(self.rs.begin_nightly_run("schedule"))
        self.rs._running_syncs[p.id] = True
        self.addCleanup(self.rs._running_syncs.pop, p.id, None)
        st = self.rs.run_now_status(self.db)
        self.assertEqual(st["provider_id"], p.id)
        self.assertEqual(st["step"], "Syncing Bears")
        self.rs._after_sync["nightly"] = st
        self.addCleanup(self.rs._after_sync.pop, "nightly", None)
        st = self.rs.run_now_status(self.db)
        self.assertIsNone(st["provider_id"])
        self.assertIn("Jellyfin", st["step"])

    def test_admin_only(self):
        from fastapi.routing import APIRoute
        paths = {r.path for r in self.rs.router.routes if isinstance(r, APIRoute)}
        self.assertIn("/api/sync/run-now", paths)
        self.assertTrue(any("require_admin" in repr(d.dependency) for d in self.rs.router.dependencies))


if __name__ == "__main__":
    unittest.main()

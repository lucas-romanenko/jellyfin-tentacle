"""How a VOD sync run ends after a database error (#270).

A database error during the sync (a failed flush, "database is locked")
left the SyncRun "running" for good: the handler committed on a session that
still needed a rollback, which raised PendingRollbackError. The nightly then
skipped the provider every night and "Sync now" said a sync was running.
Runs the real sync_provider against a fake provider.
"""
import unittest

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

import services.sync as sync
from models.database import Movie, SyncRun
from nightly_harness import NightlyHarness


class DatabaseErrorEndsTheRun(NightlyHarness):
    """#270"""

    def setUp(self):
        super().setUp()
        self._check = sync._check_disk_before_sync
        self.addCleanup(setattr, sync, "_check_disk_before_sync", self._check)
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"])

    def _latest_run(self):
        fresh = sessionmaker(bind=self.db.bind)()
        try:
            return fresh.query(SyncRun).order_by(SyncRun.id.desc()).first()
        finally:
            fresh.close()

    def test_a_flush_error_marks_the_run_failed(self):
        db = self.db

        def flush_fails(path):  # stands in for "INSERT ... database is locked"
            db.add(Movie(tmdb_id=7, title="A", source="radarr"))
            db.add(Movie(tmdb_id=7, title="B", source="radarr"))
            db.flush()
        sync._check_disk_before_sync = flush_fails

        run = sync.sync_provider(self.provider, "full", db)   # must not raise

        stored = self._latest_run()
        self.assertEqual("failed", stored.status)
        self.assertEqual("failed", run.status)
        self.assertIsNotNone(stored.completed_at)
        self.assertIn("UNIQUE constraint failed", stored.error_message)
        self.assertNotIn("\n", stored.error_message)   # the first line only, not the SQL and parameters

    def test_database_locked_inside_the_movie_sync_marks_the_run_failed(self):
        from sqlalchemy.exc import OperationalError
        db = self.db
        calls = {"n": 0}
        real_commit = db.commit

        def commit():
            calls["n"] += 1
            if calls["n"] == 2:   # 1 = the run row; 2 = the first category's commit
                db.execute(text("SELECT 1"))
                raise OperationalError("INSERT INTO movies ...", {}, Exception("database is locked"))
            return real_commit()
        db.commit = commit
        try:
            sync.sync_provider(self.provider, "full", db)
        finally:
            db.commit = real_commit
        stored = self._latest_run()
        self.assertEqual("failed", stored.status)
        self.assertIn("database is locked", stored.error_message)

    def test_the_next_sync_runs_after_a_failed_one(self):
        db = self.db
        sync._check_disk_before_sync = lambda path: (db.add(Movie(tmdb_id=7, title="A", source="radarr")),
                                                     db.add(Movie(tmdb_id=7, title="B", source="radarr")), db.flush())
        sync.sync_provider(self.provider, "full", db)
        sync._check_disk_before_sync = lambda path: None
        run = sync.sync_provider(self.provider, "full", db)
        self.assertEqual("completed", run.status, run.error_message)
        self.assertIsNotNone(self.movie(1000))


if __name__ == "__main__":
    unittest.main()

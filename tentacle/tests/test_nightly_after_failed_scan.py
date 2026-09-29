"""#269: one database error in a nightly step no longer skips the rest of the night.

Every step's handler reused the job's session without rolling it back, so a
step that failed on a flush left it unusable: the handler's own log_activity
raised PendingRollbackError, the outer handler caught it, and the Sonarr
scan, tags, Jellyfin pipeline, EPG, playlists and home configs never ran.

Needs fastapi + apscheduler to import main; skipped otherwise.
"""
import logging
import shutil
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb

try:
    import main
    import routers.sync  # noqa: F401  (imported inside run_scheduled_sync)
except Exception:  # pragma: no cover
    main = None


def _flush_error(db):
    # A real flush failure, as a concurrent scan inserting the same title gives (#268).
    db.add(mdb.Movie(tmdb_id=1, title="Already here", source="radarr"))
    db.query(mdb.Movie).filter(mdb.Movie.source == "radarr").all()  # autoflush -> IntegrityError


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class NightlyAfterFailedScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        db.add(mdb.Movie(tmdb_id=1, title="Already here", source="radarr"))
        mdb.set_setting(db, "data_dir", self.tmp)
        db.commit()
        db.close()
        self.calls = []

    def _run(self, radarr, sonarr):
        with mock.patch.object(main, "SessionLocal", self.Session), \
             mock.patch("services.radarr.scan_radarr_library", radarr), \
             mock.patch("services.sonarr.scan_sonarr_library", sonarr), \
             mock.patch("services.tagger.refresh_recently_added_tags",
                        lambda db: self.calls.append("tags")), \
             mock.patch("services.jellyfin.run_full_jellyfin_pipeline",
                        lambda db, **kw: self.calls.append("pipeline") or {}), \
             self.assertLogs(level="ERROR") as logs:
            main.run_scheduled_sync()
        return logs.output

    def _activity(self):
        db = self.Session()
        try:
            return [a.message for a in db.query(mdb.ActivityLog).all()]
        finally:
            db.close()

    def test_sonarr_scan_still_runs_after_radarr_scan_db_error(self):
        def sonarr(db):
            self.calls.append("sonarr")
            return {"new": 0}
        logs = self._run(_flush_error, sonarr)
        self.assertEqual(["sonarr", "tags", "pipeline"], self.calls[:3])
        self.assertFalse([l for l in logs if "Scheduled sync failed" in l], logs)
        self.assertTrue(any("Radarr scan failed" in m for m in self._activity()))

    def test_tag_refresh_still_runs_after_sonarr_scan_db_error(self):
        logs = self._run(lambda db: {"new": 0}, _flush_error)
        self.assertEqual(["tags", "pipeline"], self.calls[:2])
        self.assertTrue(any("Sonarr scan failed" in m for m in self._activity()))


if __name__ == "__main__":
    unittest.main()

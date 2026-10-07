"""#517: a provider deleted while the nightly job syncs another one no longer
ends the whole job.

The nightly job loaded the active providers once, then synced them one after
the other (hours on a big catalogue). A provider still waiting its turn holds
no sync slot, so #450's guard lets its Delete through. Every commit expires the
loaded objects; when the loop reached the deleted one, reading it raised
ObjectDeletedError outside the per-provider try, and the outer handler ended
the job ("Scheduled sync failed"): no Radarr/Sonarr scan, tags, Jellyfin
pipeline, EPG or playlists that night. A provider switched off meanwhile was
synced anyway.

Needs fastapi + apscheduler to import main; skipped otherwise.
"""
import shutil
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir

try:
    import main
    import routers.sync  # noqa: F401  (imported inside run_scheduled_sync)
    import routers.providers as providers_router
except Exception:  # pragma: no cover
    main = None


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class ProviderGoneDuringNightly(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        db.add_all([mdb.Provider(name="First", server_url="http://192.0.2.1", username="u", password="p",
                                 active=True),
                    mdb.Provider(name="Second", server_url="http://192.0.2.2", username="u", password="p",
                                 active=True)])
        mdb.set_setting(db, "data_dir", self.tmp)
        db.commit()
        self.second_id = db.query(mdb.Provider).filter_by(name="Second").one().id
        db.close()
        self.calls = []

    def _night(self, while_first_syncs):
        """One run_scheduled_sync; ``while_first_syncs(session)`` is what the
        admin does from another request while the first provider syncs."""
        def fake_sync(provider, sync_type, db, **kw):
            self.calls.append(f"sync {provider.name}")
            if provider.name == "First":
                s = self.Session()
                try:
                    while_first_syncs(s)
                finally:
                    s.close()
            run = mdb.SyncRun(provider_id=provider.id, sync_type=sync_type, status="completed")
            db.add(run)
            db.commit()     # a real sync commits once per category: the job's objects expire
            return run

        with mock.patch.object(main, "SessionLocal", self.Session), \
                mock.patch("services.sync.sync_provider", fake_sync), \
                mock.patch("services.radarr.scan_radarr_library",
                           lambda db: self.calls.append("radarr") or {}), \
                mock.patch("services.sonarr.scan_sonarr_library",
                           lambda db: self.calls.append("sonarr") or {}), \
                mock.patch("services.tagger.refresh_recently_added_tags", lambda db: None), \
                mock.patch("services.jellyfin.run_full_jellyfin_pipeline", lambda db, **kw: {}), \
                self.assertLogs(level="INFO") as logs:
            main.run_scheduled_sync()
        return logs.output

    def test_deleted_provider_is_skipped_and_the_night_goes_on(self):
        def delete_second(s):
            with mock.patch("services.smartlists.sync_smartlists"), \
                    mock.patch("services.smartlists.refresh_smartlist_playlists"), \
                    mock.patch("services.smartlists.write_home_config"):
                providers_router.delete_provider(self.second_id, db=s)

        logs = self._night(delete_second)
        self.assertFalse([l for l in logs if "Scheduled sync failed" in l], logs)
        self.assertNotIn("sync Second", self.calls)
        self.assertIn("radarr", self.calls, f"the rest of the night never ran: {self.calls}")
        self.assertIn("sonarr", self.calls)

    def test_provider_switched_off_is_not_synced(self):
        def switch_off_second(s):
            s.query(mdb.Provider).filter_by(id=self.second_id).update({"active": False})
            s.commit()

        logs = self._night(switch_off_second)
        self.assertEqual(["sync First"], [c for c in self.calls if c.startswith("sync ")])
        self.assertFalse([l for l in logs if "Scheduled sync failed" in l], logs)
        self.assertIn("radarr", self.calls)


if __name__ == "__main__":
    unittest.main()

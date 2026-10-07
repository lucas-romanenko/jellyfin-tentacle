"""A provider deleted while the nightly job syncs another
one takes the rest of the night with it.

The nightly job loads the active providers once, then syncs them one after
the other (for an hour or more each on a big catalogue). #450 made Delete
refuse a provider whose sync holds its slot, but a provider still WAITING
its turn in the nightly loop holds none, so its delete goes through. When
the loop reaches it, the expired Provider object (every commit expires it)
is reloaded, ObjectDeletedError escapes the per-provider try, and the outer
handler ends the job: no Radarr/Sonarr scan, tags, Jellyfin pipeline, EPG,
playlists that night ("Scheduled sync failed").

Run from tentacle/:  python tests/hermetic.py discover -s tests -p test_nightly_provider_deleted_mid_job.py
"""
import logging
import shutil
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir

try:
    import main
    import routers.sync  # noqa: F401
    import routers.providers as providers_router
except Exception:  # pragma: no cover
    main = None


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class ProviderDeletedDuringNightly(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False)
        db = self.Session()
        db.add_all([mdb.Provider(name="A", server_url="http://192.0.2.1", username="u", password="p", active=True),
                    mdb.Provider(name="B", server_url="http://192.0.2.2", username="u", password="p", active=True)])
        mdb.set_setting(db, "data_dir", self.tmp)
        db.commit()
        self.b_id = db.query(mdb.Provider).filter_by(name="B").one().id
        db.close()
        self.calls = []

    def test_the_rest_of_the_night_runs(self):
        def fake_sync(provider, sync_type, db, **kw):
            self.calls.append(f"sync {provider.name}")
            if provider.name == "A":
                # The admin deletes B from the Providers page meanwhile.
                s = self.Session()
                try:
                    with mock.patch("services.smartlists.sync_smartlists"), \
                            mock.patch("services.smartlists.refresh_smartlist_playlists"), \
                            mock.patch("services.smartlists.write_home_config"):
                        providers_router.delete_provider(self.b_id, db=s)
                finally:
                    s.close()
            run = mdb.SyncRun(provider_id=provider.id, sync_type=sync_type, status="completed")
            db.add(run)
            db.commit()
            return run

        with mock.patch.object(main, "SessionLocal", self.Session), \
                mock.patch("services.sync.sync_provider", fake_sync), \
                mock.patch("services.radarr.scan_radarr_library", lambda db: self.calls.append("radarr") or {}), \
                mock.patch("services.sonarr.scan_sonarr_library", lambda db: self.calls.append("sonarr") or {}), \
                mock.patch("services.tagger.refresh_recently_added_tags", lambda db: None), \
                mock.patch("services.jellyfin.run_full_jellyfin_pipeline", lambda db, **kw: {}):
            logging.disable(logging.NOTSET)
            with self.assertLogs(level="INFO") as logs:
                main.run_scheduled_sync()
        failed = [l for l in logs.output if "Scheduled sync failed" in l]
        self.assertEqual([], failed, "the nightly job ended at the deleted provider")
        self.assertIn("radarr", self.calls, f"the Radarr scan never ran: {self.calls}")


if __name__ == "__main__":
    unittest.main()

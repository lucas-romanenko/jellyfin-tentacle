"""The nightly job must refresh the guide of a provider set up on the Live TV page (#174).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The nightly EPG step selected providers that are `live_tv_enabled AND active`,
but `active` is the VOD sync switch and the Live TV page creates its provider
with active=False (it must never get a VOD sync). No provider set up the
normal way matched, so after setup the guide ran dry within days while the log
still said "Syncing Live TV EPG data". Drives the real run_scheduled_sync().
"""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb

try:
    import main
    import routers.sync  # noqa: F401  (imported inside run_scheduled_sync)
    import routers.livetv as livetv_router
except Exception:  # pragma: no cover - depends on optional deps
    main = None


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class NightlyEpgForLiveProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        mdb.set_setting(self.db, "data_dir", str(self.tmp))

        # Set up exactly as the Live TV page does.
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[livetv_router.require_admin] = lambda: None
        r = TestClient(app).post("/api/live/provider", json={
            "name": "Live", "provider_type": "xtream", "server_url": "http://192.0.2.10",
            "username": "u", "password": "p"})
        self.assertEqual(200, r.status_code, r.text)
        self.provider = self.db.query(mdb.Provider).one()
        self.assertFalse(self.provider.active, "the Live TV page no longer creates it inactive?")
        self.pid = self.provider.id
        self.db.add(mdb.LiveChannel(provider_id=self.provider.id, name="C", stream_id="1",
                                    stream_url="http://192.0.2.10/live/1.ts",
                                    epg_channel_id="c.ca", enabled=True))
        self.db.commit()

    def test_the_nightly_job_syncs_its_guide(self):
        import services.jellyfin as jellyfin
        import services.smartlists as sl
        import services.radarr as radarr
        import services.sonarr as sonarr
        import services.tagger as tagger
        import services.discovery as discovery

        synced = []
        patches = [
            mock.patch.object(main, "SessionLocal", lambda: self.db),
            mock.patch.object(livetv_router, "_run_epg_sync_background",
                              lambda data: synced.append(data["id"]) or False),
            mock.patch.object(sl, "refresh_smartlist_playlists", lambda db, user_id=None, only_names=None: {}),
            mock.patch.object(sl, "sync_smartlists", lambda db, user_id=None: {}),
            mock.patch.object(sl, "write_home_config", lambda db, user_id=None: {}),
            mock.patch.object(sl, "migrate_global_smartlists_to_user", lambda db, uid: None),
            mock.patch.object(sl, "cleanup_orphaned_playlists", lambda db, uid: 0),
            mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db: {}),
            mock.patch.object(jellyfin, "push_tags_to_jellyfin", lambda db, log_prefix="": 0),
            mock.patch.object(jellyfin, "sweep_orphaned_downloads", lambda db: 0),
            mock.patch.object(radarr, "scan_radarr_library", lambda db: {}),
            mock.patch.object(sonarr, "scan_sonarr_library", lambda db: {}),
            mock.patch.object(tagger, "refresh_recently_added_tags", lambda db: None),
            mock.patch.object(discovery, "discover_new_provider_content",
                              lambda db: {"vod_new": [], "live_new": []}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        main.run_scheduled_sync()

        self.assertEqual([self.pid], synced)

    def test_the_helper_selects_on_live_tv_alone(self):
        self.assertEqual([self.pid], [p.id for p in livetv_router.live_tv_providers(self.db)])


if __name__ == "__main__":
    unittest.main()

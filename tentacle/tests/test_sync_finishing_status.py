"""A sync that is still updating Jellyfin says so (#159).

Run from the tentacle/ directory:  python -m unittest discover -s tests

sync_provider marks the SyncRun "completed" when the VOD part ends, but a manual
sync then runs the Jellyfin pipeline (library-scan wait, tag push, playlists),
and the nightly job runs scans, tags, EPG and per-user playlist rebuilds (1 h
43 m on a live install). All that time /history, /status and the dashboard said
"completed", and a new manual sync was refused as "already running".
"""
import logging
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.sync as sync_router
from models.database import Provider, SyncRun


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Db(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        mdb.set_setting(self.db, "data_dir", tmp)
        self.provider = Provider(name="P", server_url="http://192.0.2.10", username="u", password="p", active=True)
        self.db.add(self.provider)
        self.db.commit()
        self.pid = self.provider.id
        sync_router._after_sync.clear()
        sync_router._running_syncs.clear()
        self.addCleanup(sync_router._after_sync.clear)
        self.addCleanup(sync_router._running_syncs.clear)

    def _complete_run(self, db):
        run = SyncRun(provider_id=self.pid, sync_type="full", status="completed",
                      started_at=datetime.utcnow(), completed_at=datetime.utcnow(), duration_seconds=1)
        db.add(run)
        db.commit()
        return run

    def seen(self):
        db = self.Session()
        try:
            return {
                "history": sync_router.get_sync_history(db=db)["runs"][0]["status"],
                "status": sync_router.get_sync_status(db=db)["last_status"],
                "recent": sync_router.get_sync_status(db=db)["recent"][0]["finishing"],
                "dashboard": sync_router.get_dashboard(db=db)["status"]["vod_sync"]["status"],
            }
        finally:
            db.close()


class ManualSync(_Db):
    def test_the_jellyfin_half_reads_as_finishing_then_completed(self):
        during = {}

        def fake_sync(provider, sync_type, db, **kw):
            return self._complete_run(db)

        def fake_pipeline(db, log_prefix=""):
            during.update(self.seen())
            try:
                sync_router.trigger_sync(sync_router.SyncRequest(provider_id=self.pid, sync_type="full"), db=db)
            except HTTPException as e:
                during["trigger"] = e.detail
            return {}

        sync_router._running_syncs[self.pid] = True
        with mock.patch.object(sync_router, "sync_provider", fake_sync), \
                mock.patch("services.jellyfin.run_full_jellyfin_pipeline", fake_pipeline), \
                mock.patch("models.database.SessionLocal", self.Session), \
                mock.patch("routers.smartlists._compute_auto_playlists", lambda db: []):
            sync_router._run_sync_background(self.pid, "full")

        self.assertEqual({"history": "finishing", "status": "finishing", "recent": True,
                          "dashboard": "finishing"}, {k: during[k] for k in ("history", "status", "recent", "dashboard")})
        self.assertIn("still updating Jellyfin", during["trigger"])
        after = self.seen()
        self.assertEqual({"history": "completed", "status": "completed", "recent": False,
                          "dashboard": "completed"}, after)


class NightlySync(_Db):
    def test_the_rest_of_the_nightly_job_reads_as_finishing(self):
        import main
        import services.jellyfin as jellyfin
        import services.smartlists as sl
        import services.radarr as radarr
        import services.sonarr as sonarr
        import services.tagger as tagger
        import services.discovery as discovery
        during = {}

        def fake_sync(provider, sync_type, db, **kw):
            return self._complete_run(db)

        def radarr_scan(db):
            during.update(self.seen())
            return {}

        patches = [
            mock.patch.object(main, "SessionLocal", self.Session),
            mock.patch("services.sync.sync_provider", fake_sync),
            mock.patch.object(radarr, "scan_radarr_library", radarr_scan),
            mock.patch.object(sonarr, "scan_sonarr_library", lambda db: {}),
            mock.patch.object(sl, "refresh_smartlist_playlists", lambda db, user_id=None, only_names=None: {}),
            mock.patch.object(sl, "sync_smartlists", lambda db, user_id=None: {}),
            mock.patch.object(sl, "write_home_config", lambda db, user_id=None: {}),
            mock.patch.object(sl, "migrate_global_smartlists_to_user", lambda db, uid: None),
            mock.patch.object(sl, "cleanup_orphaned_playlists", lambda db, uid: 0),
            mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db: {}),
            mock.patch.object(jellyfin, "push_tags_to_jellyfin", lambda db, log_prefix="": 0),
            mock.patch.object(jellyfin, "sweep_orphaned_downloads", lambda db: 0),
            mock.patch.object(tagger, "refresh_recently_added_tags", lambda db: None),
            mock.patch.object(discovery, "discover_new_provider_content",
                              lambda db: {"vod_new": [], "live_new": []}),
            # A live stream another test left open must not make this wait.
            mock.patch("services.provider_activity.live_streams_active", lambda: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        main.run_scheduled_sync()
        self.assertEqual("finishing", during["history"])
        self.assertEqual("finishing", during["status"])
        self.assertEqual("completed", self.seen()["history"])


if __name__ == "__main__":
    unittest.main()

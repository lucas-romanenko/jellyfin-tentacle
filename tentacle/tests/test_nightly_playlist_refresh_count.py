"""The nightly job refreshes every playlist for every user twice.

run_scheduled_sync() calls run_full_jellyfin_pipeline(), whose step 4 is
`refresh_smartlist_playlists(db)` — all users — and then loops the users itself
and calls `refresh_smartlist_playlists(db, user_id=...)` again. Every playlist
is therefore queried, diffed and (when it rebuilds) cleared and re-added twice
per night.

Needs fastapi + apscheduler to import main/routers; skipped otherwise.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb

try:
    import main
    import routers.sync  # noqa: F401  (imported inside run_scheduled_sync)
except Exception:  # pragma: no cover - depends on optional deps
    main = None


def _session(tmp):
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class TestNightlyRefreshRunsOncePerUser(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = _session(self.tmp)
        self.db.add(mdb.TentacleUser(id=1, jellyfin_user_id="jf-1", display_name="User 1"))
        self.db.add(mdb.TentacleUser(id=2, jellyfin_user_id="jf-2", display_name="User 2"))
        self.db.commit()
        # A configured but unreachable Jellyfin: the pipeline runs its steps and
        # each HTTP call fails fast, which is all this test needs.
        mdb.set_setting(self.db, "jellyfin_url", "http://127.0.0.1:9")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        mdb.set_setting(self.db, "data_dir", str(self.tmp))

    def test_playlists_are_refreshed_once_per_user(self):
        import services.jellyfin as jellyfin
        import services.smartlists as sl
        import services.radarr as radarr
        import services.sonarr as sonarr
        import services.tagger as tagger
        import services.discovery as discovery

        calls = []

        def counting_refresh(db, user_id=None, only_names=None):
            calls.append(user_id)
            return {"processed": 0, "created": 0, "updated": 0, "errors": 0}

        patches = [
            mock.patch.object(main, "SessionLocal", lambda: self.db),
            mock.patch.object(sl, "refresh_smartlist_playlists", counting_refresh),
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
            mock.patch.object(jellyfin.JellyfinService, "wait_for_library_scan",
                              lambda self, **kw: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        main.run_scheduled_sync()

        self.assertEqual(
            calls, [1, 2],
            f"every playlist was refreshed more than once per user: {calls}",
        )


if __name__ == "__main__":
    unittest.main()

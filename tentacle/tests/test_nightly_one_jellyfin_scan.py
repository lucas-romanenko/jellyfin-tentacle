"""The nightly job's Radarr and Sonarr scans leave the Jellyfin library scan to
its pipeline (follow-up to #819).

#819 moved each scan's own library scan after its tag push. The nightly job
runs them back to back, though: the Radarr scan asked Jellyfin for a library
scan and the Sonarr scan's tag push listed every show ~20 s later, while it
ran. Live, every night: "Triggered Jellyfin library scan after NFO updates",
then "Series listing stopped early at 0 items" -> "Jellyfin's series listing
came back empty, no tags pushed" -> "3122 not in Jellyfin yet". And each POST
/Library/Refresh cancels the scan running and queues another: Jellyfin's log
shows "Attempting to cancel Scheduled Task Scan Media Library" 26 s after the
first one, then the pipeline's own scan right after.

Inside library_scan_later() neither scan posts /Library/Refresh, so both tag
pushes list an idle Jellyfin. Outside it (webhooks, Scan now) each scan still
asks for its own.
"""
import unittest
from unittest import mock

import test_arr_scan_tag_push_after_library_scan as base
from test_arr_scan_tag_push_after_library_scan import (
    FakeJellyfin, Radarr, Sonarr, jellyfin_class, jf_items)


class NightlyArrScans(base.RadarrScanTagPush):

    def fake(self, **kw):
        # Films 0 and 1 and shows 0 and 1 are in Jellyfin; title 2 of each is new.
        return FakeJellyfin(jf_items("Movie", "Film", 1000, 2000, (0, 1))
                            + [dict(it, Id="s" + it["Id"]) for it in
                               jf_items("Series", "Show", 2000, 2000, (0, 1))])

    def both(self, fake):
        import services.radarr as radarr
        import services.sonarr as sonarr
        with mock.patch.object(radarr, "RadarrService", Radarr), \
                mock.patch.object(sonarr, "SonarrService", Sonarr), \
                mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None), \
                mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None), \
                mock.patch("services.jellyfin.JellyfinService", jellyfin_class(fake)), \
                self.assertLogs("services", level="INFO") as logs:
            r = radarr.scan_radarr_library(self.db)
            s = sonarr.scan_sonarr_library(self.db)
        return r, s, "\n".join(logs.output)

    def test_one_after_the_other_the_sonarr_push_lists_jellyfin_during_the_radarr_scans_scan(self):
        # What the nightly job did: proves the fake reproduces the live failure.
        fake = self.fake()
        r, s, log = self.both(fake)
        self.assertIn("series listing came back empty", log)
        self.assertEqual({"i0", "i1"}, set(fake.writes), "only the films got their tags")

    def test_inside_library_scan_later_neither_scan_starts_one_and_both_push(self):
        from services.jellyfin import library_scan_later, library_scan_left_to_caller
        fake = self.fake()
        with library_scan_later():
            r, s, log = self.both(fake)
        self.assertFalse(fake.scanning, "no /Library/Refresh: the nightly pipeline runs the one scan")
        self.assertFalse(library_scan_left_to_caller(), "off again after the block")
        self.assertNotIn("listing came back empty", log)
        self.assertEqual({"i0", "i1", "si0", "si1"}, set(fake.writes))
        self.assertEqual((2, 0, 1), (r["jf_tags_pushed"], r["jf_tags_failed"], r["jf_tags_not_found"]))
        self.assertEqual((2, 0, 1), (s["jf_tags_pushed"], s["jf_tags_failed"], s["jf_tags_not_found"]))
        self.assertIn("left to the nightly job's scan", log)

    def test_the_switch_is_per_thread_a_webhook_scan_meanwhile_still_scans(self):
        import threading
        from services.jellyfin import library_scan_later, library_scan_left_to_caller
        seen = []
        with library_scan_later():
            t = threading.Thread(target=lambda: seen.append(library_scan_left_to_caller()))
            t.start()
            t.join()
            seen.append(library_scan_left_to_caller())
        self.assertEqual([False, True], seen)

    def test_the_switch_is_off_again_after_an_exception(self):
        from services.jellyfin import library_scan_later, library_scan_left_to_caller
        with self.assertRaises(RuntimeError):
            with library_scan_later():
                raise RuntimeError("scan failed")
        self.assertFalse(library_scan_left_to_caller())

    # The single-scanner tests of the base class run unchanged in their own module.
    test_titles_jellyfin_lists_get_their_tags_although_the_scan_started_a_library_scan = None
    test_control_jellyfin_that_answers_during_a_scan = None


try:
    import main
    import routers.sync  # noqa: F401  (imported inside run_scheduled_sync)
except Exception:  # pragma: no cover - depends on optional deps
    main = None


@unittest.skipIf(main is None, "fastapi/apscheduler not installed")
class NightlyJobRunsBothScansInside(unittest.TestCase):

    def test_both_arr_scans_run_inside_library_scan_later_and_the_pipeline_outside(self):
        from pathlib import Path
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from tmp_dirs import temp_dir
        from services import (discovery, jellyfin, radarr, sonarr, tagger,
                              smartlists as sl)
        tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        seen = {}

        def scan(name):
            def run(db):
                seen[name] = jellyfin.library_scan_left_to_caller()
                return {}
            return run

        def pipeline(self_, **kw):
            seen["pipeline"] = jellyfin.library_scan_left_to_caller()
            return False

        patches = [
            mock.patch.object(main, "SessionLocal", lambda: db),
            mock.patch.object(sl, "refresh_smartlist_playlists", lambda db, user_id=None, only_names=None: {}),
            mock.patch.object(sl, "sync_smartlists", lambda db, user_id=None: {}),
            mock.patch.object(sl, "write_home_config", lambda db, user_id=None: {}),
            mock.patch.object(sl, "migrate_global_smartlists_to_user", lambda db, uid: None),
            mock.patch.object(sl, "cleanup_orphaned_playlists", lambda db, uid: 0),
            mock.patch.object(sl, "_notify_jellyfin_plugin", lambda db, user=None: {}),
            mock.patch.object(jellyfin, "push_tags_to_jellyfin", lambda db, log_prefix="": 0),
            mock.patch.object(jellyfin, "sweep_orphaned_downloads", lambda db: 0),
            mock.patch.object(radarr, "scan_radarr_library", scan("radarr")),
            mock.patch.object(sonarr, "scan_sonarr_library", scan("sonarr")),
            mock.patch.object(tagger, "refresh_recently_added_tags", lambda db: None),
            mock.patch.object(discovery, "discover_new_provider_content",
                              lambda db, **kw: {"vod_new": [], "live_new": []}),
            mock.patch("services.provider_activity.live_streams_active", lambda: False),
            mock.patch.object(jellyfin.JellyfinService, "wait_for_library_scan", pipeline),
        ]
        for k, v in (("jellyfin_url", "http://jellyfin.test"), ("jellyfin_api_key", "k")):
            mdb.set_setting(db, k, v)
        db.commit()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        main.run_scheduled_sync()
        self.assertEqual({"radarr": True, "sonarr": True, "pipeline": False}, seen)


if __name__ == "__main__":
    unittest.main()

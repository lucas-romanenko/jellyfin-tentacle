"""#333's saved watched state lives on the Duplicate row, and
Radarr's own delete webhooks (MovieFileDelete / MovieDelete), which Keep VOD
itself causes, delete every Duplicate row of the film.

Keep VOD on a film whose two copies share a folder, with the download as the
Jellyfin item's main version: the users' watched state is saved on the
duplicate (pending_user_data) for the new item Jellyfin makes for the .strm.
Keep VOD deletes the download through Radarr; Radarr then posts
MovieFileDelete (reason "manual") and MovieDelete to Tentacle's webhook, and
_remove_downloaded_movie "clears the duplicate tombstones" -- the row with the
saved state too. The poller (first look after 30 s) finds nothing.

Run from tentacle/:  python tests/hermetic.py discover -s tests -p test_duplicate_keep_vod_arr_delete_webhook.py
"""
import unittest
from unittest import mock

from sqlalchemy.orm import sessionmaker

from models.database import Duplicate
from routers import duplicates
from services import duplicates as dup_service
from tests.test_duplicate_keeps_user_data import FakeJellyfin, Film
from tests.test_duplicate_keep_vod_merged_folder import _FakeArr
from tests.test_duplicate_merged_folder_user_data import MergedFilm, JF_MKV, JF_STRM
from tests.test_duplicate_twin_rows import TwinRowsOneFilm, FakeRadarr as TwinFakeRadarr


class PendingStateSurvivesRadarrDeleteWebhook(MergedFilm):
    def setUp(self):
        super().setUp()
        import routers.radarr as radarr_router
        self.radarr_router = radarr_router
        mock.patch("routers.library._cleanup_playlists_all_users").start()
        mock.patch.object(radarr_router, "emit_library_event").start()
        # The app's sessions don't autoflush (models.database.SessionLocal)
        self.db.close()
        self.Session = sessionmaker(bind=self.db.bind, autoflush=False)
        self.db = self.Session()
        self.dup = self.db.query(Duplicate).one()

    def radarr_webhook_delete(self):
        """What Tentacle does with the MovieFileDelete Radarr posts after Keep VOD."""
        s = self.Session()
        try:
            self.radarr_router._remove_downloaded_movie(s, 949, "Heat", str(self.mkv.parent))
        finally:
            s.close()

    def test_webhook_after_the_resolution_keeps_the_saved_state(self):
        self.jellyfin_film(JF_MKV, JF_STRM)          # the download is the item's main version
        FakeJellyfin.data = {("A", "film"): {"Played": True, "PlayCount": 1}}
        duplicates.resolve_duplicate(self.dup.id, duplicates.ResolveRequest(resolution="keep_vod"), db=self.db)
        self.db.expire_all()
        self.assertIsNotNone(self.db.query(Duplicate).one().pending_user_data, "saved before the delete")

        self.radarr_webhook_delete()                 # Radarr's MovieFileDelete, seconds later

        self.rescan(JF_STRM)                         # Jellyfin makes the .strm's new item
        self.db.expire_all()
        dup_service.apply_pending_user_data(self.db)
        self.assertEqual({"Played": True, "PlayCount": 1}, FakeJellyfin.data.get(("A", "new")),
                         "user A's watched state never reached the kept copy: the delete webhook "
                         "removed the duplicate that held it")

    def test_webhook_during_the_resolution(self):
        """Radarr posts MovieFileDelete as soon as it deleted the file, while
        the resolve request is still running (it then removes the title)."""
        self.jellyfin_film(JF_MKV, JF_STRM)
        FakeJellyfin.data = {("A", "film"): {"Played": True, "PlayCount": 1}}
        real = _FakeArr._delete_title

        def delete_title(arr, arr_id, delete_files):
            self.radarr_webhook_delete()
            return real(arr, arr_id, delete_files)
        with mock.patch.object(_FakeArr, "_delete_title", delete_title):
            try:
                duplicates.resolve_duplicate(self.dup.id, duplicates.ResolveRequest(resolution="keep_vod"),
                                             db=self.db)
            except Exception as e:  # noqa: BLE001
                self.fail(f"Keep VOD failed after Radarr had deleted the download: {type(e).__name__}: {e}")
        self.assertFalse(self.mkv.exists())
        self.rescan(JF_STRM)
        self.db.expire_all()
        dup_service.apply_pending_user_data(self.db)
        self.assertEqual({"Played": True, "PlayCount": 1}, FakeJellyfin.data.get(("A", "new")))


class KeepVodWebhookDuringResolve(Film):
    """Separate folders, nobody watched it: what a live install logged on
    v1.9.0 -- Radarr's MovieFileDelete and MovieDelete reached Tentacle while
    Keep VOD's delete call ran, then the request failed with
    "Instance <Duplicate> has been deleted" (500), the download already gone."""
    def setUp(self):
        super().setUp()
        import routers.radarr as radarr_router
        self.radarr_router = radarr_router
        mock.patch("routers.library._cleanup_playlists_all_users").start()
        mock.patch.object(radarr_router, "emit_library_event").start()
        self.db.close()
        self.Session = sessionmaker(bind=self.db.bind, autoflush=False)
        self.db = self.Session()
        self.dup = self.db.query(Duplicate).one()

    def _resolve_with_webhooks(self):
        real = _FakeArr._delete_title

        def delete_title(arr, arr_id, delete_files):
            s = self.Session()
            try:
                self.radarr_router._remove_downloaded_movie(s, 101, "Film", str(self.mkv.parent))
            finally:
                s.close()
            return real(arr, arr_id, delete_files)
        with mock.patch.object(_FakeArr, "_delete_title", delete_title):
            return duplicates.resolve_duplicate(self.dup.id, duplicates.ResolveRequest(resolution="keep_vod"),
                                                db=self.db)

    def test_the_resolution_is_recorded(self):
        from models.database import Movie
        self.db.query(Movie).one().radarr_path = str(self.mkv)   # one row: the VOD title with its download
        self.db.commit()
        self.assertEqual({"success": True}, self._resolve_with_webhooks())
        self.assertFalse(self.mkv.exists())
        self.assertTrue(self.strm.exists())
        self.db.expire_all()
        self.assertEqual("keep_vod", self.db.query(Duplicate).one().resolution)
        row = self.db.query(Movie).one()
        self.assertEqual(("provider_1", None), (row.source, row.radarr_path))

    def test_a_downloaded_only_row_the_webhook_removed(self):
        from models.database import Movie
        row = self.db.query(Movie).one()
        row.source, row.radarr_path = "radarr", str(self.mkv)
        self.db.commit()
        self.assertEqual({"success": True}, self._resolve_with_webhooks())
        self.assertTrue(self.strm.exists())
        self.db.expire_all()
        self.assertEqual("keep_vod", self.db.query(Duplicate).one().resolution)


class TwinRowsAndTheDeleteWebhook(TwinRowsOneFilm):
    """#504's twin rows meet #515's webhook: Keep VOD on one of a film's two
    pending rows, Radarr's delete webhook dropping both while the call runs.
    The resolution is recorded on the row resolved, the twin the webhook
    dropped stays gone (never back as pending: Keep Downloaded on it would
    find no download), and the twins' merged sources are what is saved."""
    def setUp(self):
        super().setUp()
        import routers.radarr as radarr_router
        self.radarr_router = radarr_router
        for p in (mock.patch("routers.library._cleanup_playlists_all_users"),
                  mock.patch.object(radarr_router, "emit_library_event")):
            p.start()
            self.addCleanup(p.stop)
        self.db.close()
        self.Session = sessionmaker(bind=self.db.bind, autoflush=False)   # as the app's sessions
        self.db = self.Session()
        self.addCleanup(self.db.close)

    def with_webhook(self):
        real = TwinFakeRadarr.delete_movie_by_id

        def delete_movie_by_id(radarr, movie_id, delete_files=False):
            s = self.Session()
            try:
                self.radarr_router._remove_downloaded_movie(s, 949, "Heat", str(self.mkv.parent))
            finally:
                s.close()
            return real(radarr, movie_id, delete_files)
        return mock.patch.object(TwinFakeRadarr, "delete_movie_by_id", delete_movie_by_id)

    def test_keep_vod_with_the_webhook_resolves_the_title_once(self):
        with self.with_webhook():
            self.assertEqual({"success": True}, self.resolve(self.a, "keep_vod"))
        self.assertFalse(self.mkv.exists())
        self.assertTrue(self.strm.exists())
        self.db.expire_all()
        rows = self.db.query(Duplicate).all()
        self.assertEqual([(self.a, "keep_vod")], [(d.id, d.resolution) for d in rows],
                         "the twin the webhook dropped came back, or the resolution was not recorded")
        self.assertEqual(0, duplicates.get_duplicates(db=self.db)["pending"])
        duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_radarr"), db=self.db)
        self.assert_a_copy_left()

    def test_resolve_all_keep_vod_with_the_webhook(self):
        with self.with_webhook():
            r = duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_vod"), db=self.db)
        self.assertEqual((1, 0, 1), (r["count"], r["failed"], r["skipped"]))
        self.assertTrue(self.strm.exists())
        self.db.expire_all()
        self.assertEqual(["keep_vod"], [d.resolution for d in self.db.query(Duplicate).all()])

    def merged_sources_case(self):
        other = self.root / "vod2" / "Heat (1995)" / "Heat (1995).strm"
        other.parent.mkdir(parents=True)
        other.write_text("http://q/movie/1.mp4")
        twin = self.db.get(Duplicate, self.b)
        twin.sources = [{"source": "radarr", "path": str(self.mkv)}, {"source": "provider_2", "path": str(other)}]
        self.db.commit()
        return other

    def test_keep_vod_saves_the_merged_sources(self):
        """No webhook (Radarr's Connect not set up): re-reading the
        duplicate after the arr call kept the merged sources."""
        other = self.merged_sources_case()
        self.resolve(self.a, "keep_vod")
        self.db.expire_all()
        self.assertIn(str(other), [s["path"] for s in self.db.get(Duplicate, self.a).sources])
        self.assertEqual("keep_vod", self.resolution(self.b))

    def test_keep_vod_with_the_webhook_saves_the_merged_sources(self):
        other = self.merged_sources_case()
        with self.with_webhook():
            self.resolve(self.a, "keep_vod")
        self.db.expire_all()
        self.assertIn(str(other), [s["path"] for s in self.db.get(Duplicate, self.a).sources])


def load_tests(loader, tests, pattern):
    """Only this file's own tests, not the ones its base classes bring."""
    suite = unittest.TestSuite()
    for cls in (PendingStateSurvivesRadarrDeleteWebhook, KeepVodWebhookDuringResolve,
                TwinRowsAndTheDeleteWebhook):
        suite.addTests(cls(name) for name in loader.getTestCaseNames(cls) if name in vars(cls))
    return suite


if __name__ == "__main__":
    unittest.main()

"""Keep VOD of a *series* duplicate: Sonarr posts SeriesDelete to Tentacle's
webhook inside the delete call Keep VOD makes (delete_series_by_id).

When Tentacle's row for the show is the downloaded-only one (source
"sonarr"), the webhook deletes that row and every series Duplicate of the
TMDB id, in its own session. Keep VOD then committed its stale copies: the
request failed after Sonarr had deleted the download, and no resolution was
recorded. When the row is the VOD one with a sonarr_path (hybrid), the webhook
only clears sonarr_path, and Keep VOD must still finish.

Run from tentacle/:  python tests/hermetic.py discover -s tests -p test_duplicate_keep_vod_sonarr_delete_webhook.py
"""
import unittest
from unittest import mock

from sqlalchemy.orm import sessionmaker

from models.database import Duplicate, Series
from routers import duplicates
from tests.test_duplicate_resolution_series import TestSeriesDuplicates


class KeepVodSeriesWebhookDuringResolve(TestSeriesDuplicates):
    def setUp(self):
        super().setUp()
        import routers.sonarr as sonarr_router
        self.sonarr_router = sonarr_router
        mock.patch.object(sonarr_router, "_check_webhook_auth").start()
        mock.patch.object(sonarr_router, "emit_library_event").start()
        mock.patch.object(sonarr_router, "log_activity").start()
        mock.patch("routers.library._cleanup_playlists_all_users").start()
        # The app's sessions don't autoflush (models.database.SessionLocal)
        engine = self.db.bind
        self.db.close()
        self.Session = sessionmaker(bind=engine, autoflush=False)
        self.db = self.Session()
        self.dup = self.db.query(Duplicate).one()
        self.webhooks = []

        def delete_series_by_id(*a, **kw):
            """Sonarr removes the series and posts SeriesDelete before it answers."""
            s = self.Session()
            try:
                self.webhooks.append(self.sonarr_router.sonarr_webhook(
                    {"eventType": "SeriesDelete", "deletedFiles": False,
                     "series": {"tmdbId": 1418, "title": "Show", "path": "/tv/Show (2010)"}},
                    request=mock.Mock(), db=s))
            finally:
                s.close()
            return True
        self.sonarr.return_value.delete_series_by_id.side_effect = delete_series_by_id

    def resolve(self):
        return duplicates.resolve_duplicate(self.dup.id, duplicates.ResolveRequest(resolution="keep_vod"),
                                            db=self.db)

    def test_hybrid_row_the_webhook_released(self):
        self.assertEqual({"success": True}, self.resolve())
        self.assertEqual(1, len(self.webhooks), "the SeriesDelete webhook ran inside the delete call")
        self.db.expire_all()
        self.assertEqual("keep_vod", self.db.query(Duplicate).one().resolution)
        row = self.db.query(Series).one()
        self.assertEqual(("provider_1", None), (row.source, row.sonarr_path))
        self.assertTrue(list(self.show.rglob("*.strm")), "the VOD episodes stay")

    def test_a_sonarr_only_row_the_webhook_removed(self):
        row = self.db.query(Series).one()
        row.source = "sonarr"
        self.db.commit()
        try:
            result = self.resolve()
        except Exception as e:  # noqa: BLE001
            self.fail(f"Keep VOD failed after Sonarr had deleted the download: {type(e).__name__}: {e}")
        self.assertEqual({"success": True}, result)
        self.assertEqual("deleted", self.webhooks[0]["status"])
        self.db.expire_all()
        self.assertEqual("keep_vod", self.db.query(Duplicate).one().resolution,
                         "the resolution is recorded although the webhook dropped the duplicate")
        self.assertTrue(list(self.show.rglob("*.strm")), "the VOD episodes stay")


def load_tests(loader, tests, pattern):
    """Only this file's own tests, not the ones the base class brings."""
    suite = unittest.TestSuite()
    cls = KeepVodSeriesWebhookDuringResolve
    suite.addTests(cls(name) for name in loader.getTestCaseNames(cls) if name in vars(cls))
    return suite


if __name__ == "__main__":
    unittest.main()

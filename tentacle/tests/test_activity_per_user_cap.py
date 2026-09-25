"""A non-admin's own Searching / upcoming titles are not lost behind the
library-wide first 20 (the cap was applied before the per-user filter).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from datetime import timedelta
from unittest import mock

import models.database as mdb
import routers.activity as activity
from tests import test_activity_searching as base

BACKLOG = [base._movie(9000 + k, f"Admin Backlog {k:02d}", added=base._iso(base.NOW - timedelta(minutes=10 + k)))
           for k in range(25)]
LATER = [base._movie(8000 + k, f"Admin Upcoming {k:02d}", digital=base._iso(base.NOW + timedelta(days=1 + k)))
         for k in range(25)]


class TestPerUserCap(base._Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(base, "RADARR_MOVIES", base.RADARR_MOVIES + BACKLOG + LATER)
        p.start(); self.addCleanup(p.stop)
        # The kid asked for the oldest searching movie and the latest upcoming one.
        self.db.add_all([mdb.DownloadRequest(tmdb_id=1, media_type="movie", user_id=self.kid.id),
                         mdb.DownloadRequest(tmdb_id=3, media_type="movie", user_id=self.kid.id)])
        self.db.commit()

    def test_the_kid_still_sees_their_titles(self):
        out = self.activity(user=self.kid)
        self.assertEqual(["Searching Movie"], [x["title"] for x in out["searching"]])
        self.assertEqual(["Coming Soon"], [x["title"] for x in out["unreleased"]])
        self.assertTrue(out["searching"][0].get("poster_path"), "poster enriched for the per-user pick")

    def test_the_admin_list_is_unchanged(self):
        out = self.activity()
        self.assertEqual(activity.SEARCHING_LIMIT, len(out["searching"]))
        self.assertNotIn("Searching Movie", [x["title"] for x in out["searching"]])
        self.assertEqual(20, len(out["unreleased"]))
        self.assertNotIn("_searching_all", out)

    def test_a_download_in_the_queue_is_not_also_searching_for_the_kid(self):
        queue = [{"id": 1, "movieId": 1, "movie": {"tmdbId": 1, "title": "Searching Movie"}, "title": "x",
                  "size": 10, "sizeleft": 5, "status": "downloading", "trackedDownloadState": "downloading"}]
        out = self.activity(user=self.kid, queue=queue)
        self.assertEqual([], [x["title"] for x in out["searching"]])


if __name__ == "__main__":
    unittest.main()

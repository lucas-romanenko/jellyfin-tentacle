"""A requester's Activity actions reach only the title they requested.

_can_manage checks DownloadRequest(tmdb_id, media_type, user) -- the tmdb id
only -- but _find_arr_record then fell back to the tvdb_id in the request
body when that tmdb id matched nothing in Sonarr. With a request that is not
in Sonarr under its tmdb id (a TVDB-only add records tmdb_id = -tvdb_id),
the search, stop-missing, release check and remove actions acted on
whichever show had that tvdb id. For non-admins the fallback is now taken
only when the request itself is keyed by that TVDB id; admins keep it.

/arr/grab passed any guid on, and Radarr/Sonarr accept any release in their
cache (and, with the title's ids added, file even one they could not map
under this title). A non-admin now grabs only a release listed by their own
title's current check; anything else is 409 "check again".

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import time
import unittest
from unittest import mock

from fastapi import HTTPException

import models.database as mdb
import routers.activity as activity
from services import arr_insight

from test_activity_arr_actions import _Base


class TestTvdbFallbackBoundToRequest(_Base):
    def setUp(self):
        super().setUp()
        # The user's own request, keyed the way a TVDB-only add records it; it
        # is not in Sonarr under that key. tvdb 2000 is a show nobody requested.
        self.db.add(mdb.DownloadRequest(tmdb_id=-4000, media_type="series", user_id=self.kid.id))
        self.db.commit()

    def _mismatch(self):
        return dict(media_type="series", tmdb_id=-4000, tvdb_id=2000)

    def test_remove_cannot_reach_another_show(self):
        with self.assertRaises(HTTPException) as e:
            self.remove(user=self.kid, delete_downloaded=True, **self._mismatch())
        self.assertEqual(404, e.exception.status_code)
        self.assertEqual([], self.sonarr.deleted, "a show the user never requested was removed")

    def test_search_cannot_reach_another_show(self):
        with self.assertRaises(HTTPException):
            self.search(user=self.kid, **self._mismatch())
        self.assertEqual([], self.sonarr.searched_eps)
        self.assertEqual([], self.sonarr.searched_series)

    def test_stop_missing_cannot_reach_another_show(self):
        with self.assertRaises(HTTPException):
            activity.stop_missing(activity.ArrTitle(**self._mismatch()), db=self.db, user=self.kid)
        self.assertEqual([], self.sonarr.monitoring)

    def test_check_cannot_reach_another_show(self):
        with mock.patch.object(arr_insight, "_get") as get:
            with self.assertRaises(HTTPException):
                activity.check_releases(activity.ArrTitle(**self._mismatch()), db=self.db, user=self.kid)
        get.assert_not_called()

    def test_tvdb_keyed_request_still_works(self):
        # The request for tvdb 3000 is keyed -3000: the fallback is theirs.
        self.db.add(mdb.DownloadRequest(tmdb_id=-3000, media_type="series", user_id=self.kid.id))
        self.db.commit()
        r = self.search(user=self.kid, media_type="series", tmdb_id=-3000, tvdb_id=3000)
        self.assertTrue(r["ok"])
        self.assertEqual([[1]], self.sonarr.searched_eps)

    def test_own_tmdb_request_still_works(self):
        self.db.add(mdb.DownloadRequest(tmdb_id=200, media_type="series", user_id=self.kid.id))
        self.db.commit()
        r = self.search(user=self.kid, media_type="series", tmdb_id=200, tvdb_id=2000)
        self.assertTrue(r["ok"])

    def test_admin_keeps_the_fallback(self):
        self.search(user=self.admin, media_type="series", tvdb_id=3000)
        self.assertEqual([[1]], self.sonarr.searched_eps)


class TestGrabOnlyListedReleases(_Base):
    def setUp(self):
        super().setUp()
        self.db.add(mdb.DownloadRequest(tmdb_id=100, media_type="movie", user_id=self.kid.id))
        self.db.commit()
        k = arr_insight.title_key("movie", 100, 0)
        with arr_insight._checks_lock:
            arr_insight._checks[k] = {"at": time.time(), "data": {
                "releases": [{"guid": "listed-guid", "indexer_id": 7}]}}
        self.addCleanup(arr_insight.forget, "movie", 100, 0)
        self.post = mock.patch("services.arr_insight.requests.post",
                               return_value=mock.Mock(status_code=200)).start()
        self.addCleanup(mock.patch.stopall)

    def _grab(self, user, guid, indexer_id=7):
        return activity.grab_release(activity.ArrTitle(media_type="movie", tmdb_id=100, guid=guid,
                                                       indexer_id=indexer_id), db=self.db, user=user)

    def test_non_admin_cannot_grab_an_unlisted_guid(self):
        with self.assertRaises(HTTPException) as e:
            self._grab(self.kid, "unlisted-guid")
        self.assertEqual(409, e.exception.status_code)
        self.post.assert_not_called()

    def test_a_release_from_another_titles_check_is_refused(self):
        """Listed in title B's check, grabbed as title A: refused before Radarr is asked."""
        k = arr_insight.title_key("movie", 555, 0)
        with arr_insight._checks_lock:
            arr_insight._checks[k] = {"at": time.time(), "data": {
                "releases": [{"guid": "other-title-guid", "indexer_id": 7}]}, "ids": {"movieId": 55}}
        self.addCleanup(arr_insight.forget, "movie", 555, 0)
        with self.assertRaises(HTTPException) as e:
            self._grab(self.kid, "other-title-guid")
        self.assertEqual(409, e.exception.status_code)
        self.post.assert_not_called()

    def test_a_listed_release_is_sent_with_this_titles_ids(self):
        k = arr_insight.title_key("movie", 100, 0)
        with arr_insight._checks_lock:
            arr_insight._checks[k]["ids"] = {"movieId": 10}
        self._grab(self.kid, "listed-guid")
        self.assertEqual({"guid": "listed-guid", "indexerId": 7, "movieId": 10}, self.post.call_args.kwargs["json"])

    def test_after_a_restart_a_non_admin_is_asked_to_check_again(self):
        arr_insight.forget("movie", 100, 0)
        with self.assertRaises(HTTPException) as e:
            self._grab(self.kid, "listed-guid")
        self.assertEqual(409, e.exception.status_code)
        self.post.assert_not_called()

    def test_non_admin_grabs_a_listed_release(self):
        self.assertTrue(self._grab(self.kid, "listed-guid")["ok"])
        self.post.assert_called_once()

    def test_admin_is_not_restricted(self):
        self.assertTrue(self._grab(self.admin, "anything", indexer_id=1)["ok"])


if __name__ == "__main__":
    unittest.main()

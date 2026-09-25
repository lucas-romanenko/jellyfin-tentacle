"""A TMDB hiccup during an IMDb list refresh must not drop titles from the list.

IMDb items carry only an IMDb id; enrich_items_with_tmdb() maps each through
TMDBService.find_by_imdb_id(). That method treated ANY empty answer as "not
found" — a 429 or 5xx included, and also a /find hit whose details request
then failed — and gave the caller no way to tell. The item went unmatched,
store_list_items() dropped it, and apply_list_tags_to_library() stripped the
list's tag from the library title (and so from the list's playlist) until a
later refresh happened to reach TMDB.

Uses the real TMDBService with only its HTTP session faked (as
test_tmdb_failure_prune.py does).
Run from tentacle/:  python -m unittest discover -s tests -p "test_imdb_tmdb_failure.py"
"""
import shutil
import tempfile
import unittest
from unittest import mock

import requests

from test_imdb_partial_list import _ListCase, _Resp, _gql_page


class _TMDBResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self._payload


def _tmdb(cache_dir, answer):
    from services.tmdb import TMDBService
    svc = TMDBService(bearer_token="t", cache_dir=cache_dir)
    svc.session = mock.Mock()
    svc.session.get.side_effect = answer
    return svc


class TestFindByImdbId(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_a_rate_limited_find_is_not_cached_as_not_found(self):
        svc = _tmdb(self.cache, lambda *a, **k: _TMDBResp(429))
        self.assertIsNone(svc.find_by_imdb_id("tt0133093"))
        self.assertTrue(svc.lookup_failed())
        ok = {"find/tt0133093": _TMDBResp(200, {"movie_results": [{"id": 603}]}),
              "movie/603": _TMDBResp(200, {"id": 603, "title": "The Matrix",
                                           "release_date": "1999-03-31"})}
        svc.session.get.side_effect = lambda url, **k: ok[url.split("/3/", 1)[1]]
        self.assertEqual((svc.find_by_imdb_id("tt0133093") or {}).get("tmdb_id"), 603)
        self.assertFalse(svc.lookup_failed())

    def test_a_failed_details_request_is_not_cached_either(self):
        answers = {"find/tt0133093": _TMDBResp(200, {"movie_results": [{"id": 603}]}),
                   "movie/603": _TMDBResp(503)}
        svc = _tmdb(self.cache, lambda url, **k: answers[url.split("/3/", 1)[1]])
        self.assertIsNone(svc.find_by_imdb_id("tt0133093"))
        answers["movie/603"] = _TMDBResp(200, {"id": 603, "title": "The Matrix"})
        self.assertEqual((svc.find_by_imdb_id("tt0133093") or {}).get("tmdb_id"), 603)

    def test_a_real_not_found_is_not_a_failure(self):
        svc = _tmdb(self.cache, lambda *a, **k: _TMDBResp(200, {"movie_results": [], "tv_results": []}))
        self.assertIsNone(svc.find_by_imdb_id("tt9999999"))
        self.assertFalse(svc.lookup_failed())

    def test_a_404_is_not_a_failure(self):
        svc = _tmdb(self.cache, lambda *a, **k: _TMDBResp(404))
        self.assertIsNone(svc.find_by_imdb_id("tt9999999"))
        self.assertFalse(svc.lookup_failed())


class TestListRefreshDuringATMDBOutage(_ListCase):
    def test_stored_items_and_tags_survive(self):
        from models.database import Series
        from routers import lists
        page = _Resp(200, _gql_page([("tt0000101", "Movie One", "movie"),
                                     ("tt0000201", "Show One", "tvSeries")]))
        cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cache, True)
        tmdb = _tmdb(cache, lambda *a, **k: _TMDBResp(429))
        with mock.patch.object(lists.requests, "post", return_value=page), \
             mock.patch.object(lists, "_get_tmdb_service", return_value=tmdb), \
             mock.patch("services.smartlists._notify_jellyfin_plugin"):
            lists.fetch_list(self.lst.id, db=self.db, user=self.user)
        self.assertEqual(self._stored(),
                         {(101, "movie"), (102, "movie"), (201, "series"), (202, "series")})
        self.assertTrue(self._tagged(Series, 201))
        self.db.refresh(self.lst)
        self.assertIn("TMDB did not answer for 2 item(s)", self.lst.last_fetch_note or "")


if __name__ == "__main__":
    unittest.main()

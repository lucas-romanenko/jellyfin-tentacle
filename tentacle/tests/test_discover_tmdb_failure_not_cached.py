"""Genres and New on Streaming must not cache a TMDB failure as an empty row.

A 429/5xx from TMDB made get_by_genre / get_new_on_provider store [] (or the
pages read before the failure) for 12 hours, so a one-off rate limit emptied a
Discover row for half a day. Run from tentacle/:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from unittest import mock

from services.tmdb import TMDBService


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            e = requests.HTTPError(f"{self.status_code}")
            e.response = self
            raise e

    def json(self):
        return self._data


def _page(ids):
    return {"results": [{"id": i, "title": f"T{i}", "name": f"T{i}", "release_date": "2026-01-01",
                         "first_air_date": "2026-01-01", "poster_path": f"/{i}.jpg", "vote_average": 7}
                        for i in ids]}


class TestFailureNotCached(unittest.TestCase):
    def setUp(self):
        self.t = TMDBService("tok", tempfile.mkdtemp())

    def _run(self, call):
        # First call: TMDB rate-limits. Second call: TMDB is fine again.
        with mock.patch.object(self.t.session, "get", return_value=_R(429)):
            self.assertEqual([], call())
        with mock.patch.object(self.t.session, "get", side_effect=lambda *a, **k: _R(200, _page([1, 2]))) as g:
            items = call()
        self.assertTrue(g.called, "the failure was cached: TMDB not asked again")
        self.assertEqual({1, 2}, {i["tmdb_id"] for i in items})

    def test_genre_top_rated(self):
        self._run(lambda: self.t.get_by_genre("movie", 18, mode="top_rated"))

    def test_genre_new(self):
        self._run(lambda: self.t.get_by_genre("series", 35, mode="new"))

    def test_new_on_provider(self):
        self._run(lambda: self.t.get_new_on_provider("movie", 8, region="CA"))

    def test_a_failed_later_page_is_not_cached_as_the_whole_row(self):
        pages = iter([_R(200, _page([1, 2])), _R(503)])
        with mock.patch.object(self.t.session, "get", side_effect=lambda *a, **k: next(pages)):
            self.assertEqual(2, len(self.t.get_new_on_provider("movie", 8, pages=3)))
        with mock.patch.object(self.t.session, "get", side_effect=lambda *a, **k: _R(200, _page([1, 2, 3]))) as g:
            self.t.get_new_on_provider("movie", 8, pages=3)
        self.assertTrue(g.called)

    def test_success_is_still_cached(self):
        with mock.patch.object(self.t.session, "get", side_effect=lambda *a, **k: _R(200, _page([1]))):
            self.t.get_by_genre("movie", 18)
        with mock.patch.object(self.t.session, "get", side_effect=AssertionError("asked again")):
            self.assertEqual(1, len(self.t.get_by_genre("movie", 18)))


if __name__ == "__main__":
    unittest.main()

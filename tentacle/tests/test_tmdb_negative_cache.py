"""A TMDB "no such title" is remembered for a while (#182).

Run from the tentacle/ directory:  python -m unittest discover -s tests

- A details 404 was never cached, so every scan in a burst asked TMDB again
  for the same missing series id (the #182 log showed one id 404 five times).
- A search that found nothing was written to the cache as None, but the read
  side could not tell a stored None from a miss, so it was never used.
- A 429 or 5xx still says nothing about the title and must not be cached.
"""
import logging
import unittest
from unittest import mock

import requests

from services.tmdb import TMDBService
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _response(status, body=None):
    resp = mock.Mock(status_code=status)
    resp.json.return_value = body or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status}", response=resp)
    return resp


class NegativeCache(unittest.TestCase):
    def setUp(self):
        self.tmdb = TMDBService("token", temp_dir(self))
        self.tmdb.session = mock.Mock()

    def test_a_series_404_is_asked_once(self):
        self.tmdb.session.get.return_value = _response(404)
        self.assertIsNone(self.tmdb.get_series_details(1399))
        self.assertIsNone(self.tmdb.get_series_details(1399))
        self.assertEqual(1, self.tmdb.session.get.call_count)
        self.assertFalse(self.tmdb.lookup_failed())

    def test_a_movie_404_is_asked_once(self):
        self.tmdb.session.get.return_value = _response(404)
        self.assertIsNone(self.tmdb.get_movie_details(348))
        self.assertIsNone(self.tmdb.get_movie_details(348))
        self.assertEqual(1, self.tmdb.session.get.call_count)

    def test_a_rate_limit_is_not_remembered(self):
        self.tmdb.session.get.return_value = _response(429)
        self.assertIsNone(self.tmdb.get_series_details(1399))
        self.assertTrue(self.tmdb.lookup_failed())
        self.tmdb.session.get.return_value = _response(200, {"id": 1399, "name": "Friends", "seasons": []})
        self.assertEqual(1399, self.tmdb.get_series_details(1399)["tmdb_id"])

    def test_a_404_expires(self):
        self.tmdb.session.get.return_value = _response(404)
        self.tmdb.get_movie_details(348)
        self.tmdb._cache_set("movie_details:348", None, ttl_seconds=-1)  # as if 6 h had passed
        self.tmdb.get_movie_details(348)
        self.assertEqual(2, self.tmdb.session.get.call_count)

    def test_a_search_with_no_match_is_not_repeated(self):
        self.tmdb.session.get.return_value = _response(200, {"results": []})
        self.assertIsNone(self.tmdb.search_movie("Nothing Like This", "2020"))
        asked = self.tmdb.session.get.call_count
        self.assertIsNone(self.tmdb.search_movie("Nothing Like This", "2020"))
        self.assertEqual(asked, self.tmdb.session.get.call_count)

    def test_a_failed_search_is_repeated(self):
        self.tmdb.session.get.return_value = _response(503)
        self.assertIsNone(self.tmdb.search_series("Friends", "1994"))
        asked = self.tmdb.session.get.call_count
        self.tmdb.search_series("Friends", "1994")
        self.assertGreater(self.tmdb.session.get.call_count, asked)

    def test_a_cached_match_is_still_returned(self):
        full = {"tmdb_id": 348, "title": "Alien"}
        self.tmdb._cache_set("movie_search:alien:1979", full)
        self.assertEqual(full, self.tmdb.search_movie("Alien", "1979"))
        self.tmdb.session.get.assert_not_called()

    def test_an_imdb_negative_from_before_the_upgrade_is_not_trusted(self):
        """The old code cached a failed /find (a 429) as None as well (#163)."""
        self.tmdb._cache_set("find_imdb:tt0133093", None)
        self.tmdb.session.get.return_value = _response(200, {"movie_results": [], "tv_results": []})
        self.tmdb.find_by_imdb_id("tt0133093")
        self.assertEqual(1, self.tmdb.session.get.call_count)


if __name__ == "__main__":
    unittest.main()

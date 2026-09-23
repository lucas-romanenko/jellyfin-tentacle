"""#120 (6): TMDB is never asked about a missing id.

Rows without a TMDB id were requested as tv/None and movie/None — a 422 from
TMDB, and because only a 404 is treated as "no such title", each one also
flagged the lookup as a TMDB failure.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from unittest import mock

from services.tmdb import TMDBService


class MissingIds(unittest.TestCase):
    def setUp(self):
        self.tmdb = TMDBService("token", tempfile.mkdtemp())
        self.tmdb.session = mock.Mock()

    def test_no_request_for_missing_or_invalid_ids(self):
        for bad in (None, 0, -12, "None", "", "abc"):
            self.assertIsNone(self.tmdb.get_movie_details(bad), bad)
            self.assertIsNone(self.tmdb.get_series_details(bad), bad)
            self.assertIsNone(self.tmdb.get_season_episodes(bad, 1), bad)
            self.assertIsNone(self.tmdb.get_tvdb_id(bad) if bad not in ("None", "", "abc") else None)
        self.tmdb.session.get.assert_not_called()
        self.assertFalse(self.tmdb._lookup_failed(), "a skipped id is not a TMDB failure")

    def test_a_real_id_is_still_requested(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"id": 348, "title": "Alien", "genres": []}
        self.tmdb.session.get.return_value = resp
        self.assertEqual(348, self.tmdb.get_movie_details(348)["tmdb_id"])
        self.assertIn("movie/348", self.tmdb.session.get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

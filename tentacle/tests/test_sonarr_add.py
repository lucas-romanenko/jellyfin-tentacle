"""Tests for SonarrService.add_series outcomes.

Regression cover for "already been added" being reported as a generic failure,
for a slow add being reported as a failure, and for failures reaching the
caller with no reason attached.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

import requests

from services.sonarr import SonarrService

ALREADY_ADDED = '[{"propertyName":"TvdbId","errorMessage":"This series has already been added","errorCode":"SeriesExistsValidator"}]'
LOOKUP = {"title": "Lanterns", "tvdbId": 424242}


def _response(status, text="", json_body=None):
    r = mock.Mock()
    r.status_code = status
    r.text = text
    r.json.return_value = json_body or {}
    return r


class TestAddSeries(unittest.TestCase):
    def setUp(self):
        self.sonarr = SonarrService("http://sonarr", "key")

    def test_already_added_returns_the_sentinel(self):
        with mock.patch.object(self.sonarr, "lookup_by_tvdb", return_value=dict(LOOKUP)), \
             mock.patch.object(self.sonarr.session, "post",
                               return_value=_response(400, ALREADY_ADDED)):
            result = self.sonarr.add_series(tvdb_id=424242)
        self.assertEqual(result, {"alreadyExists": True})

    def test_missing_lookup_records_a_reason(self):
        with mock.patch.object(self.sonarr, "lookup_by_tvdb", return_value=None), \
             mock.patch.object(self.sonarr, "lookup_by_tmdb", return_value=None):
            self.assertIsNone(self.sonarr.add_series(tmdb_id=999999999))
        self.assertIn("TVDB", self.sonarr.last_error)

    def test_rejection_records_a_reason(self):
        body = '[{"propertyName":"Path","errorMessage":"Folder is in use","errorCode":"SeriesPathValidator"}]'
        with mock.patch.object(self.sonarr, "lookup_by_tvdb", return_value=dict(LOOKUP)), \
             mock.patch.object(self.sonarr.session, "post", return_value=_response(400, body)):
            self.assertIsNone(self.sonarr.add_series(tvdb_id=424242))
        self.assertIn("folder", self.sonarr.last_error.lower())

    def test_timeout_is_verified_before_reporting_failure(self):
        landed = {"id": 5, "title": "Lanterns", "path": "/tv/Lanterns"}
        with mock.patch.object(self.sonarr, "lookup_by_tvdb", return_value=dict(LOOKUP)), \
             mock.patch.object(self.sonarr.session, "post",
                               side_effect=requests.exceptions.Timeout), \
             mock.patch.object(self.sonarr, "get_series_by_tvdb", return_value=landed):
            self.assertEqual(self.sonarr.add_series(tvdb_id=424242), landed)

    def test_timeout_that_did_not_land_records_a_reason(self):
        with mock.patch.object(self.sonarr, "lookup_by_tvdb", return_value=dict(LOOKUP)), \
             mock.patch.object(self.sonarr.session, "post",
                               side_effect=requests.exceptions.Timeout), \
             mock.patch.object(self.sonarr, "get_series_by_tvdb", return_value=None):
            self.assertIsNone(self.sonarr.add_series(tvdb_id=424242))
        self.assertIn("retry", self.sonarr.last_error.lower())


class TestRootFolders(unittest.TestCase):
    def test_required_read_failure_raises_instead_of_returning_empty(self):
        sonarr = SonarrService("http://sonarr", "key")
        with mock.patch.object(sonarr.session, "get",
                               side_effect=requests.exceptions.Timeout):
            self.assertEqual(sonarr.get_root_folders(), [])
            with self.assertRaises(RuntimeError):
                sonarr.get_root_folders(required=True)


if __name__ == "__main__":
    unittest.main()

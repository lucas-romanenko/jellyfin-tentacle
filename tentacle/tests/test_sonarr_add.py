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

    def test_timeout_is_polled_until_the_series_appears(self):
        # Sonarr answered the add after we stopped waiting; the series shows up
        # on a later probe, not the first one.
        landed = {"id": 5, "title": "Lanterns", "path": "/tv/Lanterns", "tvdbId": 424242}
        attempts = {"n": 0}

        def _get(url, **kwargs):
            attempts["n"] += 1
            r = mock.Mock()
            r.raise_for_status.return_value = None
            r.json.return_value = [landed] if attempts["n"] >= 3 else []
            return r

        with mock.patch("time.sleep"), \
             mock.patch.object(self.sonarr, "lookup_by_tvdb", return_value=dict(LOOKUP)), \
             mock.patch.object(self.sonarr.session, "post",
                               side_effect=requests.exceptions.Timeout), \
             mock.patch.object(self.sonarr.session, "get", side_effect=_get):
            self.assertEqual(self.sonarr.add_series(tvdb_id=424242), landed)
        self.assertGreaterEqual(attempts["n"], 3, "gave up after a single sample")

    def test_timeout_applies_episode_selection_to_the_recovered_series(self):
        # The 2xx path monitors + searches the chosen episodes. Recovering the
        # series after a timeout used to skip that, so the user was told
        # "downloading N episodes" with nothing monitored.
        landed = {"id": 5, "title": "Lanterns", "tvdbId": 424242}
        r = mock.Mock()
        r.raise_for_status.return_value = None
        r.json.return_value = [landed]
        with mock.patch("time.sleep"), \
             mock.patch.object(self.sonarr, "lookup_by_tvdb", return_value=dict(LOOKUP)), \
             mock.patch.object(self.sonarr.session, "post",
                               side_effect=requests.exceptions.Timeout), \
             mock.patch.object(self.sonarr.session, "get", return_value=r), \
             mock.patch.object(self.sonarr, "_monitor_selected_episodes") as monitor_eps, \
             mock.patch.object(self.sonarr, "_unmonitor_series"):
            self.sonarr.add_series(tvdb_id=424242,
                                   selected_episodes=[{"season": 1, "episode": 2}])
        monitor_eps.assert_called_once()

    def test_timeout_that_did_not_land_records_a_reason(self):
        r = mock.Mock()
        r.raise_for_status.return_value = None
        r.json.return_value = []
        with mock.patch("time.sleep"), \
             mock.patch.object(self.sonarr, "lookup_by_tvdb", return_value=dict(LOOKUP)), \
             mock.patch.object(self.sonarr.session, "post",
                               side_effect=requests.exceptions.Timeout), \
             mock.patch.object(self.sonarr.session, "get", return_value=r):
            self.assertIsNone(self.sonarr.add_series(tvdb_id=424242))
        self.assertIn("retry", self.sonarr.last_error.lower())

    def test_non_json_success_is_a_failure_with_a_reason_not_a_crash(self):
        r = mock.Mock()
        r.status_code = 200
        r.text = "<html>proxy</html>"
        r.json.side_effect = ValueError("not json")
        with mock.patch.object(self.sonarr, "lookup_by_tvdb", return_value=dict(LOOKUP)), \
             mock.patch.object(self.sonarr.session, "post", return_value=r):
            self.assertIsNone(self.sonarr.add_series(tvdb_id=424242))
        self.assertIn("could not read", self.sonarr.last_error.lower())

    def test_sonarr_being_down_is_not_reported_as_missing_from_tvdb(self):
        with mock.patch.object(self.sonarr.session, "get",
                               side_effect=requests.exceptions.ConnectionError("refused")):
            self.assertIsNone(self.sonarr.add_series(tvdb_id=424242))
        self.assertIn("could not reach sonarr", self.sonarr.last_error.lower())
        self.assertNotIn("thetvdb", self.sonarr.last_error.lower())


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

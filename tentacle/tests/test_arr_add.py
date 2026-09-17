"""Tests for services.arr_add — Radarr/Sonarr add outcomes and failure reasons.

Regression cover for adds that reported failure when they had actually
succeeded (slow *arr instance) or when the title was simply already present,
and for failures that reached the user as a bare "Failed to add".

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

import requests

from services.arr_add import (
    ADDED, EXISTS, FAILED, AddReport, add_movie_to_radarr,
    already_exists_in_body, explain_arr_error, radarr_root_folder,
)

MOVIE_EXISTS = '[{"propertyName":"TmdbId","errorMessage":"This movie has already been added","errorCode":"MovieExistsValidator"}]'
SERIES_EXISTS = '[{"propertyName":"TvdbId","errorMessage":"This series has already been added","errorCode":"SeriesExistsValidator"}]'
NOT_FOUND = '[{"propertyName":"TmdbId","errorMessage":"A movie with this ID was not found","errorCode":"MovieNotFound"}]'


def _response(status, text=""):
    r = requests.Response()
    r.status_code = status
    r._content = text.encode()
    return r


class TestErrorTranslation(unittest.TestCase):
    def test_validator_codes_become_sentences(self):
        self.assertIn("already in Radarr", explain_arr_error(400, MOVIE_EXISTS, "Radarr"))
        self.assertIn("root folder", explain_arr_error(
            400, '[{"errorCode":"RootFolderValidator"}]', "Sonarr"))

    def test_unmapped_validator_falls_back_to_its_message(self):
        self.assertIn("A movie with this ID was not found",
                      explain_arr_error(400, NOT_FOUND, "Radarr"))

    def test_markup_is_stripped_from_upstream_text(self):
        reason = explain_arr_error(500, "<script>alert(1)</script>", "Radarr")
        self.assertNotIn("<", reason)

    def test_already_exists_detection(self):
        self.assertTrue(already_exists_in_body(MOVIE_EXISTS))
        self.assertTrue(already_exists_in_body(SERIES_EXISTS))
        self.assertFalse(already_exists_in_body(NOT_FOUND))

    def test_already_exists_does_not_match_unrelated_messages(self):
        # A bare "already exists" substring matched failures that have nothing
        # to do with the title being present, turning them into false successes.
        self.assertFalse(already_exists_in_body(
            '[{"errorMessage":"A folder with that name already exists","errorCode":"PathValidator"}]'))
        self.assertFalse(already_exists_in_body(
            '[{"errorMessage":"Root folder already exists","errorCode":"RootFolderValidator"}]'))


class TestAddReport(unittest.TestCase):
    def test_identical_reasons_are_reported_once(self):
        report = AddReport()
        for _ in range(10):
            report.record(FAILED, "Radarr refused it: nope.")
        self.assertEqual(report.as_response()["failed"], 10)
        self.assertEqual(report.as_response()["detail"], "Radarr refused it: nope.")

    def test_success_carries_no_detail(self):
        report = AddReport()
        report.record(ADDED)
        report.record(EXISTS)
        self.assertNotIn("detail", report.as_response())


class TestAddMovieToRadarr(unittest.TestCase):
    def test_already_added_is_not_a_failure(self):
        with mock.patch("requests.post", return_value=_response(400, MOVIE_EXISTS)):
            outcome, reason = add_movie_to_radarr("http://r", "k", 1, 1, "/movies")
        self.assertEqual(outcome, EXISTS)
        self.assertIsNone(reason)

    def test_timeout_is_verified_and_counted_as_added_when_it_landed(self):
        with mock.patch("requests.post", side_effect=requests.exceptions.Timeout), \
             mock.patch("services.arr_add._radarr_has_movie", return_value=True):
            outcome, _ = add_movie_to_radarr("http://r", "k", 1204680, 1, "/movies")
        self.assertEqual(outcome, ADDED)

    def test_timeout_that_did_not_land_reports_a_reason(self):
        with mock.patch("requests.post", side_effect=requests.exceptions.Timeout), \
             mock.patch("services.arr_add._radarr_has_movie", return_value=False):
            outcome, reason = add_movie_to_radarr("http://r", "k", 1204680, 1, "/movies")
        self.assertEqual(outcome, FAILED)
        self.assertIn("retry", reason.lower())

    def test_verification_polls_rather_than_sampling_once(self):
        # Radarr has been measured answering an add after 127s, so a single
        # check moments after the timeout reported a success as a failure.
        attempts = {"n": 0}

        def _get(url, **kwargs):
            attempts["n"] += 1
            r = mock.Mock()
            r.raise_for_status.return_value = None
            r.json.return_value = [{"tmdbId": 1204680}] if attempts["n"] >= 4 else []
            return r

        with mock.patch("time.sleep"), \
             mock.patch("requests.post", side_effect=requests.exceptions.Timeout), \
             mock.patch("requests.get", side_effect=_get):
            outcome, _ = add_movie_to_radarr("http://r", "k", 1204680, 1, "/movies")
        self.assertEqual(outcome, ADDED)
        self.assertGreaterEqual(attempts["n"], 4)

    def test_verification_budget_outlasts_a_slow_arr(self):
        from services.arr_add import VERIFY_TOTAL_SECONDS, verify_backoff_delays
        self.assertGreaterEqual(sum(verify_backoff_delays()), 127,
                                "budget is shorter than a measured real add")
        self.assertGreaterEqual(VERIFY_TOTAL_SECONDS, 150)

    def test_rejection_reason_is_returned(self):
        with mock.patch("requests.post", return_value=_response(400, NOT_FOUND)):
            outcome, reason = add_movie_to_radarr("http://r", "k", 999999999, 1, "/movies")
        self.assertEqual(outcome, FAILED)
        self.assertIn("not found", reason.lower())


class TestRootFolder(unittest.TestCase):
    def test_vod_roots_are_never_chosen_for_downloads(self):
        folders = [{"path": "/data/vod/movies"}, {"path": "/data/media/movies"}]
        with mock.patch("requests.get", return_value=_mock_json(folders)):
            self.assertEqual(radarr_root_folder("http://r", "k"), "/data/media/movies")

    def test_unreadable_root_folders_raise_rather_than_guessing(self):
        # A guessed path is almost never a configured root, so the *arr rejects
        # the add and a transient blip becomes a guaranteed failure.
        with mock.patch("requests.get", side_effect=requests.exceptions.Timeout):
            with self.assertRaises(requests.exceptions.Timeout):
                radarr_root_folder("http://r", "k")

    def test_no_configured_roots_raises(self):
        with mock.patch("requests.get", return_value=_mock_json([])):
            with self.assertRaises(RuntimeError):
                radarr_root_folder("http://r", "k")


def _mock_json(payload):
    r = mock.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


if __name__ == "__main__":
    unittest.main()

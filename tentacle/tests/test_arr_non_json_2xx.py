"""A 2xx answer that is not the *arr's JSON must not become a 500 or a false "added".

4d49b86 fixed the Sonarr half: `add_series()` now catches the non-JSON body and
reports "Sonarr returned a response Tentacle could not read ..." instead of
raising (a 500 from the handler). These tests guard that.

Still broken at 0e1805f (Radarr): services/arr_add.py `add_movie_to_radarr`
    if r.status_code < 400:
        return ADDED, None
never looks at the body, so the same page (for example an SSO / reverse-proxy
login page: `requests` follows a 302 on POST and lands on a 200 HTML page) is
reported to the user as "Added to Radarr" although nothing was sent to Radarr.

Expected: both services treat a 2xx whose body is not JSON as a failure with a
reason; neither raises.
"""
import json
import unittest
from unittest import mock

import requests

from services.arr_add import ADDED, FAILED, add_movie_to_radarr
from services.sonarr import SonarrService

LOGIN_PAGE = "<!doctype html><html><body>Sign in to continue</body></html>"
LOOKUP = {"title": "Lanterns", "tvdbId": 424242}


def _html(status=200):
    r = requests.Response()
    r.status_code = status
    r._content = LOGIN_PAGE.encode()
    r.headers["Content-Type"] = "text/html"
    return r


def _json(status, payload):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload).encode()
    r.headers["Content-Type"] = "application/json"
    return r


class SonarrNonJson2xx(unittest.TestCase):
    def test_html_2xx_does_not_raise_and_carries_a_reason(self):
        sonarr = SonarrService("http://sonarr:8989", "key")
        with mock.patch.object(sonarr.session, "get", return_value=_json(200, [dict(LOOKUP)])), \
             mock.patch.object(sonarr.session, "post", return_value=_html(200)), \
             mock.patch("time.sleep"):
            result = sonarr.add_series(tvdb_id=424242, quality_profile_id=7, root_folder="/tv")
        self.assertIsNone(result)
        self.assertTrue(sonarr.last_error)


class RadarrNonJson2xx(unittest.TestCase):
    def _add(self, post_response):
        with mock.patch("requests.post", return_value=post_response), \
             mock.patch("requests.get", return_value=_json(200, [])), \
             mock.patch("time.sleep"):
            return add_movie_to_radarr("http://radarr:7878", "k", 11587, 7, "/movies")

    def test_html_2xx_is_not_reported_added(self):
        outcome, reason = self._add(_html(200))  # ADDED at 0e1805f
        self.assertEqual(outcome, FAILED, "a login page was reported as a successful add")
        self.assertTrue(reason)

    def test_json_201_is_added(self):
        """Regression guard: the normal answer is still a success."""
        outcome, reason = self._add(_json(201, {"id": 1, "tmdbId": 11587}))
        self.assertEqual((outcome, reason), (ADDED, None))


if __name__ == "__main__":
    unittest.main()

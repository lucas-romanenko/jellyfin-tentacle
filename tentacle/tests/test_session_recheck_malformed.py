"""#272: a Jellyfin that answers HTTP 200 with a body that is not a JSON object
(an SSO login page behind a proxy, a truncated reply while it restarts, a
list) must be treated like "Jellyfin down": the last known state stands and
the request goes through. It used to raise, so every cookie-authenticated
request answered 500 for as long as Jellyfin answered like that."""
import unittest
from unittest import mock

import requests

from routers import auth as auth_router
from tests import test_session_lifecycle as base


def _answer(json_side_effect=None, json_value=None):
    r = mock.Mock()
    r.status_code = 200
    r.ok = True
    if json_side_effect is not None:
        r.json.side_effect = json_side_effect
    else:
        r.json.return_value = json_value
    return r


class TestMalformedJellyfinAnswer(base.TestSessionLifecycle):
    def _call(self, jf):
        self.client.cookies.clear()
        self.client.cookies.set(auth_router.COOKIE_NAME, self._token())
        with mock.patch.object(auth_router.requests, "get", return_value=jf) as get:
            r = self.client.get("/api/auth/me")
        return r, get

    def test_html_login_page_keeps_the_session(self):
        page = _answer(json_side_effect=requests.exceptions.JSONDecodeError(
            "Expecting value", "<html>login</html>", 0))
        r, _ = self._call(page)
        self.assertEqual(r.status_code, 200)

    def test_truncated_json_keeps_the_session(self):
        body = _answer(json_side_effect=requests.exceptions.JSONDecodeError(
            "Unterminated string", '{"results": [ {"id": 1, "title": "broken', 33))
        r, _ = self._call(body)
        self.assertEqual(r.status_code, 200)

    def test_a_list_or_string_body_keeps_the_session(self):
        for value in (["x"], "oops", 7):
            auth_router._session_checks.clear()
            r, _ = self._call(_answer(json_value=value))
            self.assertEqual(r.status_code, 200, value)

    def test_the_admin_flag_is_not_changed_by_a_malformed_answer(self):
        self._call(_answer(json_value=["x"]))
        db = self.Session()
        try:
            from models.database import TentacleUser
            self.assertTrue(db.get(TentacleUser, self.uid).is_admin)
        finally:
            db.close()

    def test_a_malformed_answer_is_not_asked_again_on_every_request(self):
        page = _answer(json_side_effect=requests.exceptions.JSONDecodeError("x", "<html>", 0))
        _, get1 = self._call(page)
        _, get2 = self._call(page)
        self.assertEqual(get1.call_count, 1)
        self.assertEqual(get2.call_count, 0)


if __name__ == "__main__":
    unittest.main()

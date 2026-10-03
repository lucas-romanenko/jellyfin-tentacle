"""Sign-in while Jellyfin isn't ready says so, not "wrong password" (#392).

Run from the tentacle/ directory:  python -m unittest discover -s tests

`login` turned every error status from Jellyfin's AuthenticateByName into
401 "Invalid username or password". Jellyfin answers 503 for the first
seconds of its startup (for example right after the plugin install the setup
wizard asks for), so a user with the right password was told it was wrong.
Jellyfin refusing the sign-in (401, 400 for a blank name, 403 for an
account that is disabled or not allowed now, whatever the password, and 500,
which depends on the account too) keeps that answer; any other 5xx says
Jellyfin may still be starting, and anything else names the status.
"""
import unittest
from unittest import mock

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from models.database import Base, Setting, TentacleUser
from routers import auth as auth_router
from tmp_dirs import temp_dir

WRONG_PASSWORD = "Invalid username or password"


def http_response(status):
    r = mock.Mock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.json.side_effect = ValueError("not JSON")
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status} Error", response=r)
    else:
        r.raise_for_status.return_value = None
    return r


class LoginWhileJellyfinIsNotReady(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("jellyfin_url", "http://jellyfin:8096"), ("jellyfin_api_key", "k" * 32)):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()

    def attempt(self, answer):
        """Sign in with Jellyfin answering `answer` (a status, or an exception to raise)."""
        post = (mock.patch.object(auth_router.requests, "post", side_effect=answer)
                if isinstance(answer, Exception)
                else mock.patch.object(auth_router.requests, "post", return_value=http_response(answer)))
        response = Response()
        request = Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": [],
                           "query_string": b"", "scheme": "http", "server": ("tentacle", 8888)})
        with post, self.assertRaises(HTTPException) as cm:
            auth_router.login(auth_router.LoginRequest(username="qa", password="right"), response, request, self.db)
        # A refused sign-in leaves nothing behind, whatever the reason.
        self.assertEqual(0, self.db.query(TentacleUser).count())
        self.assertNotIn("set-cookie", response.headers)
        return cm.exception

    def test_401_is_still_a_wrong_password(self):
        e = self.attempt(401)
        self.assertEqual((401, WRONG_PASSWORD), (e.status_code, e.detail))

    def test_unavailable_says_jellyfin_may_still_be_starting(self):
        for status in (503, 502, 504):
            with self.subTest(status=status):
                e = self.attempt(status)
                self.assertEqual(503, e.status_code)
                self.assertNotIn(WRONG_PASSWORD, e.detail)
                self.assertIn(f"HTTP {status}", e.detail)
                self.assertIn("starting", e.detail)
                self.assertIn("try again", e.detail.lower())

    def test_refused_accounts_keep_the_same_answer(self):
        # Jellyfin answers 403 for a disabled account (or one outside its allowed
        # hours) whatever the password, 400 for a blank name, and 500 when failed
        # sign-ins at once for an enabled account clash saving the attempt count:
        # same reply as a wrong password, so it doesn't tell which names are which.
        for status in (403, 400, 500):
            with self.subTest(status=status):
                e = self.attempt(status)
                self.assertEqual((401, WRONG_PASSWORD), (e.status_code, e.detail))

    def test_other_answers_name_the_status(self):
        # A web server that isn't Jellyfin at the saved address, or a proxy's answer.
        for status in (404, 405):
            with self.subTest(status=status):
                e = self.attempt(status)
                self.assertEqual(502, e.status_code)
                self.assertNotIn(WRONG_PASSWORD, e.detail)
                self.assertIn(f"HTTP {status}", e.detail)
                self.assertIn("address", e.detail)

    def test_unreachable_jellyfin_is_unchanged(self):
        for exc in (requests.ConnectionError("refused"), requests.Timeout("slow")):
            with self.subTest(exc=type(exc).__name__):
                e = self.attempt(exc)
                self.assertEqual(502, e.status_code)
                self.assertTrue(e.detail.startswith("Could not reach Jellyfin"), e.detail)


if __name__ == "__main__":
    unittest.main()

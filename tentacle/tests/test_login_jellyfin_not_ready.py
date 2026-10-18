"""Sign-in only says "wrong password" when Jellyfin refused the password (#392).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The login turned every HTTP error from Jellyfin's AuthenticateByName into
401 "Invalid username or password". Jellyfin answers 503 for the first
seconds of its startup, right when the setup wizard has had it restart for
the plugin, so a new user with the right password was told it was wrong. A
saved address that isn't Jellyfin (404, 5xx) read the same. Now only a 401
is a wrong password; 503 says Jellyfin is starting, anything else names the
status Jellyfin answered.
"""
import unittest
from unittest import mock

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from models.database import Base, Setting
from routers import auth as auth_router
from tmp_dirs import temp_dir


def answer(status):
    """requests.post's response with this status, raising like requests does."""
    r = mock.Mock()
    r.status_code = status
    r.ok = status < 400
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status} Server Error", response=r)
    else:
        r.raise_for_status.return_value = None
    return r


class LoginWhileJellyfinIsNotReady(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add(Setting(key="jellyfin_url", value="http://jellyfin:8096"))
        self.db.commit()

    def attempt(self, status):
        request = Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": [],
                           "query_string": b"", "scheme": "http", "server": ("tentacle", 8888)})
        with mock.patch.object(auth_router.requests, "post", return_value=answer(status)):
            with self.assertRaises(HTTPException) as cm:
                auth_router.login(auth_router.LoginRequest(username="qa", password="right"),
                                  Response(), request, self.db)
        return cm.exception

    def test_a_refused_password_is_a_wrong_password(self):
        e = self.attempt(401)
        self.assertEqual((401, "Invalid username or password"), (e.status_code, e.detail))

    def test_jellyfin_starting_up_says_so(self):
        e = self.attempt(503)
        self.assertEqual(503, e.status_code)
        self.assertIn("starting up", e.detail)
        self.assertNotIn("password", e.detail)

    def test_any_other_answer_names_the_status(self):
        for status in (400, 403, 404, 500, 502):
            with self.subTest(status=status):
                e = self.attempt(status)
                self.assertEqual(502, e.status_code)
                self.assertIn(f"HTTP {status}", e.detail)
                self.assertNotIn("password", e.detail)


if __name__ == "__main__":
    unittest.main()

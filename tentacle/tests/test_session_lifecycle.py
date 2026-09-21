"""A Tentacle session must end when it should.

The session cookie was `<user id>.HMAC(user id)`: the same string for a user
forever, with no issue time, nothing server-side to revoke, and a user's admin
flag copied from Jellyfin only at login. So a copied cookie outlived logout,
outlived the 30-day cookie lifetime (the server never checked an age), and a
user whose admin rights were removed in Jellyfin — or whose account was
disabled or deleted there — kept full Tentacle access.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import time
import unittest
from unittest import mock

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Setting, TentacleUser, get_db
from routers import auth as auth_router

JF_ID = "a" * 32


def _jf_user(is_admin=True, disabled=False, status=200):
    r = mock.Mock()
    r.status_code = status
    r.ok = status == 200
    r.json.return_value = {"Id": JF_ID, "Name": "Owner",
                           "Policy": {"IsAdministrator": is_admin, "IsDisabled": disabled}}
    r.raise_for_status.side_effect = None if status == 200 else requests.HTTPError(str(status))
    return r


class TestSessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        engine = create_engine(f"sqlite:///{self.tmp.name}/t.db",
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        for k, v in (("jellyfin_url", "http://jf:8096"), ("jellyfin_api_key", "ADMINKEY"),
                     ("session_secret", "s3cret")):
            db.add(Setting(key=k, value=v))
        # A second, non-admin user so "no users yet" bootstrap mode is not in play.
        db.add(TentacleUser(jellyfin_user_id="b" * 32, display_name="Kid", is_admin=False))
        u = TentacleUser(jellyfin_user_id=JF_ID, display_name="Owner", is_admin=True)
        db.add(u); db.commit()
        self.uid = u.id
        db.close()
        app = FastAPI()
        app.include_router(auth_router.router)
        app.dependency_overrides[get_db] = self._db
        self.client = TestClient(app)
        auth_router._session_checks.clear() if hasattr(auth_router, "_session_checks") else None

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self):
        db = self.Session()
        try:
            yield db
        finally:
            db.close()

    def _token(self, at=None):
        db = self.Session()
        try:
            user = db.get(TentacleUser, self.uid)
            with mock.patch.object(auth_router.time, "time", return_value=at or time.time()):
                return auth_router._sign_session(user.id, "s3cret") if not hasattr(auth_router, "_issue_session") \
                    else auth_router._issue_session(db, user)
        finally:
            db.close()

    def _me(self, token, jf=None):
        self.client.cookies.clear()
        self.client.cookies.set(auth_router.COOKIE_NAME, token)
        with mock.patch.object(auth_router.requests, "get", return_value=jf or _jf_user()):
            return self.client.get("/api/auth/me")

    def test_a_token_still_works_normally(self):
        self.assertEqual(self._me(self._token()).status_code, 200)

    def test_logout_ends_the_session_server_side(self):
        token = self._token()
        self.assertEqual(self._me(token).status_code, 200)
        self.client.cookies.set(auth_router.COOKIE_NAME, token)
        self.client.post("/api/auth/logout")
        # A copy of the cookie taken before logout (proxy log, another tab,
        # another device) must not keep working.
        self.assertEqual(self._me(token).status_code, 401)

    def test_a_token_older_than_the_cookie_lifetime_is_refused(self):
        old = self._token(at=time.time() - auth_router.COOKIE_MAX_AGE - 3600)
        self.assertEqual(self._me(old).status_code, 401)

    def test_admin_removed_in_jellyfin_is_not_admin_in_tentacle(self):
        token = self._token()
        self.client.cookies.clear()
        self.client.cookies.set(auth_router.COOKIE_NAME, token)
        def jf(url, *a, **k):
            if url.rstrip("/").endswith("/Users"):
                lst = mock.Mock(status_code=200); lst.json.return_value = []
                lst.raise_for_status.side_effect = None
                return lst
            return _jf_user(is_admin=False)
        with mock.patch.object(auth_router.requests, "get", side_effect=jf):
            r = self.client.get("/api/auth/managed-users")
        self.assertEqual(r.status_code, 403)

    def test_a_user_disabled_in_jellyfin_loses_access(self):
        self.assertEqual(self._me(self._token(), jf=_jf_user(disabled=True)).status_code, 401)

    def test_a_user_deleted_in_jellyfin_loses_access(self):
        self.assertEqual(self._me(self._token(), jf=_jf_user(status=404)).status_code, 401)

    def test_jellyfin_being_down_does_not_lock_everyone_out(self):
        token = self._token()
        self.client.cookies.set(auth_router.COOKIE_NAME, token)
        with mock.patch.object(auth_router.requests, "get",
                               side_effect=requests.ConnectionError("down")):
            r = self.client.get("/api/auth/me")
        self.assertEqual(r.status_code, 200)



class TestTokenPathIsRecheckedToo(TestSessionLifecycle):
    """The plugin and the Android TV app authenticate with ?api_key= (the
    user's Jellyfin access token), never with the cookie. A demotion or a
    disabled account has to take effect on that path as well."""

    def _me_by_token(self, jf):
        self.client.cookies.clear()
        with mock.patch.object(auth_router, "_resolve_token_user", return_value=JF_ID), \
                mock.patch.object(auth_router.requests, "get", return_value=jf):
            # /api/auth/me is cookie-only; use a route that takes get_user_from_request.
            from fastapi import Depends, FastAPI
            app = FastAPI()

            @app.get("/probe")
            def probe(user=Depends(auth_router.get_user_from_request)):
                return {"is_admin": user.is_admin}

            from models.database import get_db
            app.dependency_overrides[get_db] = self._db
            return TestClient(app).get("/probe?api_key=tok")

    def test_admin_removed_in_jellyfin_is_not_admin_on_the_token_path(self):
        r = self._me_by_token(_jf_user(is_admin=False))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["is_admin"])

    def test_a_user_disabled_in_jellyfin_loses_access_on_the_token_path(self):
        self.assertEqual(self._me_by_token(_jf_user(disabled=True)).status_code, 401)

    def test_a_user_deleted_in_jellyfin_loses_access_on_the_token_path(self):
        self.assertEqual(self._me_by_token(_jf_user(status=404)).status_code, 401)



class TestPluginUsersAreProvisioned(TestSessionLifecycle):
    """A Jellyfin user who never opened the dashboard has a verified token but no
    TentacleUser row; every proxied route refused them (No results in search)."""

    def _probe(self, uid, profile):
        from fastapi import Depends, FastAPI
        from models.database import get_db
        app = FastAPI()

        @app.get("/probe")
        def probe(user=Depends(auth_router.get_user_from_request)):
            return {"name": user.display_name, "is_admin": user.is_admin}

        app.dependency_overrides[get_db] = self._db
        auth_router._token_profiles[uid] = profile
        with mock.patch.object(auth_router, "_resolve_token_user", return_value=uid), \
                mock.patch.object(auth_router, "_build_playlists_for_new_user", lambda uid: None), \
                mock.patch.object(auth_router.requests, "get", return_value=_jf_user(is_admin=False)):
            return TestClient(app).get("/probe?api_key=tok")

    def test_a_verified_token_owner_gets_a_row_on_first_call(self):
        r = self._probe("c" * 32, {"name": "Teen", "is_admin": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"name": "Teen", "is_admin": False})
        db = self.Session()
        self.assertEqual(db.query(TentacleUser).filter_by(jellyfin_user_id="c" * 32).count(), 1)
        db.close()

    def test_no_row_is_created_before_the_first_dashboard_login(self):
        db = self.Session()
        db.query(TentacleUser).delete(); db.commit(); db.close()
        r = self._probe("c" * 32, {"name": "Teen", "is_admin": True})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()

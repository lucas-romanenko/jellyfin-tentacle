"""POST /api/smartlists/notify calls the Jellyfin plugin's /Tentacle/Refresh
with the server's admin API key: every Tentacle-side cache in the plugin is
cleared and a LibraryChanged message goes to every connected client. It must
not be something an anonymous caller can trigger.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Setting, TentacleUser, get_db
from routers import auth as auth_router
from routers import smartlists as smartlists_router


class TestNotifyNeedsASession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        engine = create_engine(f"sqlite:///{self.tmp.name}/t.db",
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        for k, v in (("jellyfin_url", "http://jellyfin.internal:8096"),
                     ("jellyfin_api_key", "ADMINKEY"), ("session_secret", "s3cret")):
            db.add(Setting(key=k, value=v))
        user = TentacleUser(jellyfin_user_id="u1", display_name="Owner", is_admin=True)
        db.add(user); db.commit()
        self.user_id = user.id
        db.close()

        app = FastAPI()
        app.include_router(smartlists_router.router)
        app.dependency_overrides[get_db] = self._db
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self):
        db = self.Session()
        try:
            yield db
        finally:
            db.close()

    def test_anonymous_caller_is_refused_and_jellyfin_is_not_called(self):
        with mock.patch("services.smartlists.requests.post") as post:
            r = self.client.post("/api/smartlists/notify")
        self.assertEqual(r.status_code, 401)
        post.assert_not_called()

    def test_anonymous_caller_does_not_learn_the_internal_jellyfin_url(self):
        r = self.client.post("/api/smartlists/notify")
        self.assertNotIn("jellyfin.internal", r.text)

    def test_a_logged_in_user_can_still_push_the_home_config(self):
        token = auth_router._sign_session(self.user_id, "s3cret")
        self.client.cookies.set(auth_router.COOKIE_NAME, token)
        with mock.patch("services.smartlists.requests.post") as post:
            post.return_value.ok = True
            r = self.client.post("/api/smartlists/notify")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["notified"])
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["headers"]["X-Emby-Token"], "ADMINKEY")


if __name__ == "__main__":
    unittest.main()

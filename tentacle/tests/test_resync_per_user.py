""""Resync All" is per user, so its in-progress state must be too.

The state was one module-level dict. While user A's full resync ran, user B's
click answered "already_running"; B's dashboard then polled /sync-status and
reported A's run finishing as "Resync complete: …" — A's counts — while B's
own playlists were never rebuilt. /sync-status also showed B A's summary and
error text.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Setting, TentacleUser, get_db
from routers import auth as auth_router
from routers import smartlists as sl


class TestResyncIsPerUser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        engine = create_engine(f"sqlite:///{self.tmp.name}/t.db",
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        db.add(Setting(key="session_secret", value="s3cret"))
        a = TentacleUser(jellyfin_user_id="a" * 32, display_name="A", is_admin=True)
        b = TentacleUser(jellyfin_user_id="b" * 32, display_name="B", is_admin=False)
        db.add_all([a, b]); db.commit()
        self.a, self.b = a.id, b.id
        db.close()
        app = FastAPI()
        app.include_router(sl.router)
        app.dependency_overrides[get_db] = self._db
        self.client = TestClient(app)
        self.release = threading.Event()
        self.ran_for = []

        def fake_run(user_id):
            self.ran_for.append(user_id)
            if user_id == self.a:
                self.release.wait(5)
            sl._finish_resync(user_id, summary={"created": user_id}) if hasattr(sl, "_finish_resync") \
                else sl._resync_state.update(running=False, summary={"created": user_id})

        self.patch = mock.patch.object(sl, "_run_full_resync", side_effect=fake_run)
        self.patch.start()
        self.patch_jf = mock.patch.object(auth_router, "_refresh_from_jellyfin",
                                          return_value=True, create=True)
        self.patch_jf.start()

    def tearDown(self):
        self.release.set()
        self.patch.stop(); self.patch_jf.stop()
        if hasattr(sl, "_resync_states"):
            sl._resync_states.clear()
        else:
            sl._resync_state.update(running=False, started_at=None, finished_at=None,
                                    summary=None, error=None)
        self.tmp.cleanup()

    def _db(self):
        db = self.Session()
        try:
            yield db
        finally:
            db.close()

    def _as(self, uid):
        self.client.cookies.clear()
        self.client.cookies.set(auth_router.COOKIE_NAME, auth_router._sign_session(uid, "s3cret"))

    def test_a_second_users_resync_is_not_swallowed_by_the_first(self):
        self._as(self.a)
        self.assertEqual(self.client.post("/api/smartlists/sync", json={"full": True}).json()["status"], "started")
        self._as(self.b)
        r = self.client.post("/api/smartlists/sync", json={"full": True}).json()
        self.assertEqual(r["status"], "started")

    def test_sync_status_does_not_show_another_users_run(self):
        self._as(self.a)
        self.client.post("/api/smartlists/sync", json={"full": True})
        self._as(self.b)
        s = self.client.get("/api/smartlists/sync-status").json()
        self.assertFalse(s["running"])
        self.assertIsNone(s["summary"])

    def test_the_same_user_clicking_twice_is_still_already_running(self):
        self._as(self.a)
        self.client.post("/api/smartlists/sync", json={"full": True})
        r = self.client.post("/api/smartlists/sync", json={"full": True}).json()
        self.assertEqual(r["status"], "already_running")


if __name__ == "__main__":
    unittest.main()

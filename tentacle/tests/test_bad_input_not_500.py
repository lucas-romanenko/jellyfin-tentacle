"""#286: input that cannot be valid must get a 4xx, never the global 500.

1. An integer id above SQLite's 64-bit range (FastAPI accepts any int; sqlite3
   raises OverflowError when it binds it) → 404, like any id that doesn't exist.
2. JSON bodies of the wrong shape on routes that read an untyped dict → 422,
   and Radarr/Sonarr webhooks whose "movie"/"series" isn't an object → skipped.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BIG = "99999999999999999999"


class _App(unittest.TestCase):
    def setUp(self):
        import main
        import models.database as mdb
        from routers import auth
        self.tmp = tempfile.TemporaryDirectory()
        engine = create_engine(f"sqlite:///{self.tmp.name}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        user = mdb.TentacleUser(jellyfin_user_id="a" * 32, display_name="Owner", is_admin=True)
        db.add(user); db.commit(); db.refresh(user); db.expunge(user)
        db.close()

        def _db():
            s = Session()
            try:
                yield s
            finally:
                s.close()
        self.app = main.app
        self.app.dependency_overrides[mdb.get_db] = _db
        for dep in (auth.get_user_from_request, auth.get_current_user, auth.require_admin):
            self.app.dependency_overrides[dep] = lambda: user
        self.c = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.tmp.cleanup()


class TestIdOutOfRange(_App):
    def test_public_tuner_route(self):
        self.assertEqual(self.c.get(f"/api/live/stream/{BIG}").status_code, 404)

    def test_head_tuner_route(self):
        self.assertEqual(self.c.head(f"/api/live/stream/{BIG}").status_code, 404)

    def test_user_list_delete(self):
        self.assertEqual(self.c.delete(f"/api/lists/{BIG}").status_code, 404)

    def test_user_notification_dismiss(self):
        self.assertLess(self.c.post(f"/api/notifications/{BIG}/dismiss").status_code, 500)

    def test_control_small_id_is_404(self):
        self.assertEqual(self.c.delete("/api/lists/123").status_code, 404)

    def test_an_unrelated_overflow_is_still_a_server_error(self):
        import main
        req = mock.Mock(); req.method = "GET"; req.url.path = "/x"
        r = asyncio.run(main._id_out_of_range(req, OverflowError("math range error")))
        self.assertEqual(r.status_code, 500)


class TestWrongShapeBodies(_App):
    def test_preview_count_with_a_null_condition(self):
        r = self.c.post("/api/smartlists/preview-count", json={"conditions": [None]})
        self.assertEqual(r.status_code, 422)

    def test_sync_one_with_wrong_types(self):
        for body in ({"name": "T", "conditions": "x"}, {"name": ["T"], "conditions": [{"field": "genre"}]},
                     {"name": "T", "conditions": [None]}, ["x"], "x"):
            with self.subTest(body=body):
                self.assertEqual(self.c.post("/api/smartlists/sync-one", json=body).status_code, 422)

    def test_sync_with_wrong_types(self):
        for body in (["x"], "x", {"full": {"a": 1}}):
            with self.subTest(body=body):
                self.assertEqual(self.c.post("/api/smartlists/sync", json=body).status_code, 422)

    def test_sync_one_real_dashboard_body_still_reaches_the_sync(self):
        cond = [{"field": "genre", "operator": "contains", "value": "Family"}]
        with mock.patch("routers.smartlists.sync_single_custom_playlist",
                        return_value={"ok": True}) as s:
            r = self.c.post("/api/smartlists/sync-one", json={
                "name": "Kids", "output_tag": "Kids Tag", "apply_to": "movies", "conditions": cond})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(s.call_args[0][2:], ("Kids", cond, "movies", "Kids Tag"))

    def test_sync_one_without_output_tag_uses_the_name(self):
        with mock.patch("routers.smartlists.sync_single_custom_playlist",
                        return_value={"ok": True}) as s:
            self.c.post("/api/smartlists/sync-one", json={
                "name": "Kids", "conditions": [{"field": "genre", "operator": "contains", "value": "Family"}]})
        self.assertEqual(s.call_args[0][5], "Kids")

    def test_sync_without_a_body_still_works(self):
        with mock.patch("routers.smartlists.sync_smartlists", return_value={}) as s, \
             mock.patch("routers.smartlists.write_home_config"), \
             mock.patch("routers.smartlists._notify_jellyfin_plugin"), \
             mock.patch("routers.collections.sync_playlist_artwork", return_value={}):
            r = self.c.post("/api/smartlists/sync")
        self.assertEqual(r.status_code, 200)
        s.assert_called_once()

    def test_radarr_webhook_with_a_movie_that_is_not_an_object(self):
        for movie in ("x", ["x"], None, 5):
            for event in ("MovieDelete", "Download", "MovieFileDelete", "MovieAdded"):
                with self.subTest(movie=movie, event=event):
                    r = self.c.post("/api/radarr/webhook", json={"eventType": event, "movie": movie})
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(r.json()["status"], "skipped")

    def test_sonarr_webhook_with_a_series_that_is_not_an_object(self):
        for series in ("x", ["x"], None, 5):
            for event in ("SeriesDelete", "Download", "EpisodeFileDelete", "SeriesAdd"):
                with self.subTest(series=series, event=event):
                    r = self.c.post("/api/sonarr/webhook", json={"eventType": event, "series": series})
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(r.json()["status"], "skipped")

    def test_sonarr_webhook_with_episodes_that_are_not_objects(self):
        r = self.c.post("/api/sonarr/webhook", json={
            "eventType": "SeriesDelete", "series": {"title": "X", "tmdbId": 0}, "episodes": "x"})
        self.assertLess(r.status_code, 500)

    def test_webhook_with_a_non_object_body(self):
        for path in ("/api/radarr/webhook", "/api/sonarr/webhook"):
            with self.subTest(path=path):
                self.assertEqual(self.c.post(path, json=["x"]).status_code, 422)


if __name__ == "__main__":
    unittest.main()

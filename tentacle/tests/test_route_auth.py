"""Authentication coverage for routes that answer without a session.

Run from the tentacle/ directory:  python -m unittest discover -s tests

These pin the routes an unauthenticated caller must NOT be able to read. They
build a minimal app around the router under test and override get_db, so no
scheduler or real database is started.
"""
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _make_db():
    import models.database as mdb
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                           connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class LibraryRouteAuthTests(unittest.TestCase):
    """/api/library/* exposes the whole library; it must require a session.

    `GET /api/library/items` returned every movie and series — title, year,
    poster, source and the per-user playlist tags each item carries — to any
    caller that could reach the port, with no credential at all.
    """

    def setUp(self):
        import models.database as mdb
        from models.database import TentacleUser, Movie, get_db, set_setting
        from routers import library as library_router
        from routers.auth import _sign_session

        self.db = _make_db()
        self.db.add(TentacleUser(id=1, jellyfin_user_id="u1",
                                 display_name="viewer", is_admin=False))
        self.db.add(Movie(tmdb_id=603, title="A Movie", year="1999",
                          source="radarr"))
        set_setting(self.db, "session_secret", "test-secret")
        # Keep TMDBService's on-disk cache inside the test's own tmpdir.
        set_setting(self.db, "data_dir", tempfile.mkdtemp())
        self.db.commit()

        app = FastAPI()
        app.include_router(library_router.router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.cookie = {"tentacle_session": _sign_session(1, "test-secret")}

    def tearDown(self):
        self.db.close()

    def test_items_requires_a_session(self):
        r = self.client.get("/api/library/items")
        self.assertEqual(r.status_code, 401, f"library contents served anonymously: {r.text[:200]}")

    def test_item_detail_requires_a_session(self):
        # The detail carries the on-disk .strm path and every playlist tag.
        r = self.client.get("/api/library/item/movie/603")
        self.assertEqual(r.status_code, 401, r.text[:200])

    def test_item_detail_still_works_for_a_logged_in_user(self):
        r = self.client.get("/api/library/item/movie/603", cookies=self.cookie)
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.json()["title"], "A Movie")
        self.assertIn("can_delete", r.json())

    def test_items_still_works_for_a_logged_in_user(self):
        r = self.client.get("/api/library/items", cookies=self.cookie)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"][0]["tmdb_id"], 603)

    def test_event_stream_requires_a_session(self):
        """/api/library/stream must carry an auth dependency.

        Asserted structurally rather than over the wire: the SSE generator never
        completes, so an unauthenticated request would hold the test open.
        """
        from fastapi.routing import APIRoute
        from routers.auth import get_user_from_request
        from routers import library as library_router

        route = next(r for r in library_router.router.routes
                     if isinstance(r, APIRoute) and r.path.endswith("/stream"))
        deps = {d.call for d in route.dependant.dependencies}
        self.assertIn(get_user_from_request, deps,
                      "/api/library/stream streams every library change anonymously")

    def test_tmdb_proxy_requires_a_session(self):
        # Unauthenticated callers must not be able to spend the server's TMDB token.
        r = self.client.get("/api/library/tmdb/movie/603")
        self.assertEqual(r.status_code, 401)

    def test_tmdb_proxy_reachable_when_logged_in(self):
        with mock.patch("services.tmdb.TMDBService.get_movie_details",
                        return_value={"title": "A Movie"}):
            r = self.client.get("/api/library/tmdb/movie/603", cookies=self.cookie)
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()

"""delete-download must delete THE title it authorised, not any id it is handed.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The permission check is on tmdb_id ("did you request this title"); the
Jellyfin delete uses the caller-supplied jellyfin_item_id. Validating the id's
shape (#71) stops path traversal but not a well-formed id of a different item.
"""
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

MINE = "a" * 32       # the film this user requested (tmdb 603)
THEIRS = "b" * 32     # someone else's film (tmdb 999)
ITEMS = {
    MINE: {"Id": MINE, "Type": "Movie", "ProviderIds": {"Tmdb": "603"}},
    THEIRS: {"Id": THEIRS, "Type": "Movie", "ProviderIds": {"Tmdb": "999"}},
}


class _FakeJellyfin:
    deleted = []

    def __init__(self, *a, **kw):
        pass

    def get_item_by_id(self, item_id):
        return ITEMS.get(item_id)

    def search_by_tmdb_id(self, tmdb_id, media_type="Movie", **kw):
        return next((i for i in ITEMS.values() if i["ProviderIds"]["Tmdb"] == str(tmdb_id)), None)

    def delete_item(self, item_id):
        _FakeJellyfin.deleted.append(item_id)
        return True


class DeleteDownloadBindsTheItem(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        from models.database import DownloadRequest, Movie, TentacleUser, get_db, set_setting
        from routers import library
        from routers.auth import _sign_session
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(TentacleUser(id=1, jellyfin_user_id="u1", display_name="viewer", is_admin=False))
        self.db.add(Movie(tmdb_id=603, title="Mine", year="1999", source="radarr"))
        self.db.add(DownloadRequest(tmdb_id=603, media_type="movie", user_id=1))
        set_setting(self.db, "session_secret", "s")
        set_setting(self.db, "jellyfin_url", "http://jellyfin:8096")
        set_setting(self.db, "jellyfin_api_key", "admin-key")
        self.db.commit()
        app = FastAPI()
        app.include_router(library.router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.cookie = {"tentacle_session": _sign_session(1, "s")}
        _FakeJellyfin.deleted = []
        for p in (mock.patch("services.jellyfin.JellyfinService", _FakeJellyfin),
                  mock.patch.object(library, "_cleanup_playlists_all_users", lambda *a, **kw: None)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.db.close()

    def test_another_items_id_is_not_deleted(self):
        self.client.delete(f"/api/library/delete-download/603?media_type=movie&jellyfin_item_id={THEIRS}",
                           cookies=self.cookie)
        self.assertNotIn(THEIRS, _FakeJellyfin.deleted,
                         "a user who requested tmdb 603 deleted someone else's Jellyfin item")

    def test_the_right_item_is_still_deleted(self):
        r = self.client.delete(f"/api/library/delete-download/603?media_type=movie&jellyfin_item_id={MINE}",
                               cookies=self.cookie)
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([MINE], _FakeJellyfin.deleted)

    def test_a_wrong_id_falls_back_to_the_real_item(self):
        self.client.delete(f"/api/library/delete-download/603?media_type=movie&jellyfin_item_id={THEIRS}",
                           cookies=self.cookie)
        self.assertEqual([MINE], _FakeJellyfin.deleted)


if __name__ == "__main__":
    unittest.main()

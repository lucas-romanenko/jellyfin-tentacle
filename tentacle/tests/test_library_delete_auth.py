"""Only a deletion Jellyfin reported, an admin or the internal secret can drop a title (#139).

Run from the tentacle/ directory:  python -m unittest discover -s tests

DELETE /api/library/item/{type}/{tmdb_id} took no authentication: auditing
routes on a live install, one stray request removed "The Matrix" from the
catalogue and from every user's playlists, and its request history had to be
restored from a backup. The plugin has no shared secret, so the backend asks
Jellyfin: POST /Tentacle/Deletions/{type}/{id}/Confirm answers 200 once for a
deletion the plugin forwarded.
"""
import logging
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.library as library
from models.database import DownloadRequest, Movie, TentacleUser


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Resp:
    def __init__(self, status):
        self.status_code = status


class DeleteAuth(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        u = TentacleUser(jellyfin_user_id="a" * 32, display_name="A", is_admin=True)
        self.db.add(u)
        self.db.commit()
        for k, v in (("jellyfin_url", "http://jf:8096"), ("jellyfin_api_key", "server-key"),
                     ("internal_secret", "s3cret")):
            mdb.set_setting(self.db, k, v)
        self.db.add(Movie(tmdb_id=603, title="The Matrix", source="provider_1", provider_id=1))
        self.db.add(DownloadRequest(tmdb_id=603, media_type="movie", user_id=u.id))
        self.db.commit()
        app = FastAPI()
        app.include_router(library.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        self.client = TestClient(app)
        self.confirms = []
        self.jellyfin_says = 404

        def post(url, headers=None, timeout=None):
            self.confirms.append((url, headers))
            return _Resp(self.jellyfin_says)
        for p in (mock.patch.object(library.requests, "post", post),
                  mock.patch.object(library, "_cleanup_playlists_all_users", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)

    def _still_there(self):
        self.db.expire_all()
        return (self.db.query(Movie).filter_by(tmdb_id=603).count(),
                self.db.query(DownloadRequest).filter_by(tmdb_id=603).count())

    def test_a_stray_request_changes_nothing(self):
        r = self.client.delete("/api/library/item/movie/603")
        self.assertEqual(403, r.status_code)
        self.assertEqual((1, 1), self._still_there())

    def test_an_unknown_title_gets_the_same_answer(self):
        """A 403 before the catalogue is read: the answer cannot probe it."""
        self.assertEqual(403, self.client.delete("/api/library/item/movie/999999").status_code)

    def test_a_deletion_jellyfin_confirms_goes_through(self):
        self.jellyfin_says = 200
        r = self.client.delete("/api/library/item/movie/603")
        self.assertEqual(200, r.status_code)
        self.assertEqual((0, 0), self._still_there())
        url, headers = self.confirms[0]
        self.assertEqual("http://jf:8096/Tentacle/Deletions/movie/603/Confirm", url)
        self.assertEqual("server-key", headers["X-Emby-Token"])

    def test_the_internal_secret_is_accepted(self):
        r = self.client.delete("/api/library/item/movie/603", headers={"X-Tentacle-Secret": "s3cret"})
        self.assertEqual(200, r.status_code)
        self.assertEqual([], self.confirms, "no need to ask Jellyfin")

    def test_no_bootstrap_pass(self):
        """With no users yet, require_admin lets anyone in; this route must not."""
        self.db.query(DownloadRequest).delete()
        self.db.query(TentacleUser).delete()
        self.db.commit()
        self.assertEqual(403, self.client.delete("/api/library/item/movie/603").status_code)


if __name__ == "__main__":
    unittest.main()


class PluginHalf(unittest.TestCase):
    """The plugin records what it forwards, and only an admin/API key may confirm it."""

    def setUp(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2] / "tentacle-plugin"
        self.handler = (root / "Services" / "LibraryDeleteHandler.cs").read_text(encoding="utf-8")
        self.controller = (root / "Api" / "TentacleController.cs").read_text(encoding="utf-8")
        self.store = (root / "Services" / "RecentDeletions.cs").read_text(encoding="utf-8")

    def test_every_forwarded_deletion_is_recorded_before_it_is_sent(self):
        record = self.handler.index("RecentDeletions.Record(mediaType, tmdbId);")
        queue = self.handler.index("_pendingDeletes.Add((mediaType, tmdbId,")
        self.assertLess(record, queue)

    def test_the_deleted_items_own_id_and_path_are_forwarded(self):
        # #296: the backend must act on the copy that was deleted, never on
        # "the first item with this TMDB id".
        self.assertIn('_pendingDeletes.Add((mediaType, tmdbId, item.Id.ToString("N"), path', self.handler)
        self.assertIn("?item_id={Uri.EscapeDataString(entry.itemId)}&path={Uri.EscapeDataString(entry.path)}",
                      self.handler)

    def test_the_confirm_route_requires_elevation_and_is_single_use(self):
        route = self.controller.index('[HttpPost("Deletions/{mediaType}/{tmdbId}/Confirm")]')
        block = self.controller[route:route + 400]
        self.assertIn('[Authorize(Policy = "RequiresElevation")]', block)
        self.assertIn("TryConsume", block)
        self.assertIn("TimeSpan.FromMinutes(15)", self.store)

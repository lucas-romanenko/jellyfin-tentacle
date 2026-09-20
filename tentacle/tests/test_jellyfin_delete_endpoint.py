"""DELETE /api/library/item/{type}/{tmdb_id} on a title that isn't in the catalogue.

The Jellyfin plugin forwards one call per removed item, including the thousands
a library scan removes when their files are briefly unreadable. For a tmdb_id
Tentacle has no row for there is nothing to mirror, so the call must not drop
the user's download history or the duplicate ("keep the Radarr copy")
tombstones, and must not start a playlist sweep per call.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import DownloadRequest, Duplicate, Movie  # noqa: E402
import routers.library as library  # noqa: E402


class _FakeThread:
    started = 0

    def __init__(self, *a, **kw):
        pass

    def start(self):
        _FakeThread.started += 1


class _FakeThreading:
    Thread = _FakeThread


class TestDeleteLibraryItemUnknownTitle(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

        self._saved_threading = library.threading
        library.threading = _FakeThreading
        self.addCleanup(lambda: setattr(library, "threading", self._saved_threading))
        _FakeThread.started = 0

        # A download the user asked for, and a "this title is already downloaded,
        # keep the Radarr copy" tombstone — both keyed by the same tmdb_id.
        user = mdb.TentacleUser(jellyfin_user_id="jf-user-1", display_name="user 1", is_admin=True)
        self.db.add(user)
        self.db.commit()
        self.db.add(DownloadRequest(tmdb_id=777, media_type="movie", user_id=user.id))
        self.db.add(Duplicate(tmdb_id=777, media_type="movie",
                              sources=[{"source": "radarr", "path": "/movies/Kept Film"},
                                       {"source": "provider_1", "path": "/media/vod/movies/Kept Film"}],
                              resolution="keep_radarr"))
        self.db.commit()

    def _delete(self, tmdb_id=777):
        return library.delete_library_item("movie", tmdb_id, None, self.db)

    def test_unknown_title_keeps_request_and_tombstone(self):
        self.assertIsNone(self.db.query(Movie).filter(Movie.tmdb_id == 777).first())

        result = self._delete()

        self.assertEqual(result.get("deleted"), False)
        self.assertEqual(self.db.query(DownloadRequest).filter(
            DownloadRequest.tmdb_id == 777).count(), 1,
            "the download request was deleted for a title Tentacle never had")
        self.assertEqual(self.db.query(Duplicate).filter(
            Duplicate.tmdb_id == 777).count(), 1,
            "the keep_radarr tombstone was deleted for a title Tentacle never had")

    def test_unknown_title_starts_no_playlist_sweep(self):
        self._delete()
        self.assertEqual(_FakeThread.started, 0,
                         "a playlist sweep thread was started for a title that has no row")

    def test_known_title_is_still_deleted(self):
        # Must-not-change: a real row is still removed, with its request,
        # its tombstone and one playlist sweep.
        self.db.add(Movie(tmdb_id=777, title="Kept Film", source="provider_1", provider_id=1))
        self.db.commit()

        result = self._delete()

        self.assertEqual(result.get("deleted"), True)
        self.assertIsNone(self.db.query(Movie).filter(Movie.tmdb_id == 777).first())
        self.assertEqual(self.db.query(DownloadRequest).filter(
            DownloadRequest.tmdb_id == 777).count(), 0)
        self.assertEqual(self.db.query(Duplicate).filter(
            Duplicate.tmdb_id == 777).count(), 0)
        self.assertEqual(_FakeThread.started, 1)


if __name__ == "__main__":
    unittest.main()

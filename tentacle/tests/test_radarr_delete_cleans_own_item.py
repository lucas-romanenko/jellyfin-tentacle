"""#296 follow-up: Radarr's MovieFileDelete / MovieDelete webhooks clean up
playlists for the item in Radarr's folder only.

The same film can be in Jellyfin twice: the VOD .strm and the Radarr
download. The webhooks cleaned up "the first Jellyfin movie with this TMDB
id", which can be the VOD copy that stays, so it left every user's
playlists.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import Movie, set_setting  # noqa: E402
import routers.library as library  # noqa: E402
import routers.radarr as radarr  # noqa: E402
from tmp_dirs import temp_dir

VOD = {"Id": "vod", "ProviderIds": {"Tmdb": "999007"}, "Path": "/media/vod/movies/Film (2001)/Film (2001).strm"}
DL = {"Id": "dl", "ProviderIds": {"Tmdb": "999007"}, "Path": "/media/movies/Film (2001)/Film (2001) WEBDL-1080p.mkv"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        set_setting(self.db, "jellyfin_url", "http://jf")
        set_setting(self.db, "jellyfin_api_key", "k")
        self.db.add(mdb.TentacleUser(jellyfin_user_id="u1", display_name="u1", is_admin=True))
        self.db.commit()
        self.removed = []
        for target, value in (
                ("models.database.SessionLocal", self.Session),
                ("services.smartlists.remove_item_from_playlists",
                 lambda db, item_id, user_id: self.removed.append(item_id) or {"removed_from": 1}),
                ("services.smartlists.bump_playlist_version", lambda: None),
                ("services.smartlists._notify_jellyfin_plugin", lambda db: None)):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def jellyfin(self, *items):
        by_id = {i["Id"]: i for i in items}
        for name, fn in (("_fetch_all_items", lambda self_, media_type="Movie": [
                              {k: v for k, v in i.items() if k != "Path"} for i in items]),
                         ("get_item_by_id", lambda self_, item_id: by_id.get(item_id)),
                         ("search_by_tmdb_id", lambda self_, *a, **k: items[0] if items else None)):
            p = mock.patch(f"services.jellyfin.JellyfinService.{name}", fn)
            p.start()
            self.addCleanup(p.stop)


class TestCleanupByArrFolder(_Base):
    def test_only_the_download_in_radarrs_folder_leaves_the_playlists(self):
        self.jellyfin(VOD, DL)   # the VOD copy is "the first item with this TMDB id"
        library._cleanup_playlists_all_users(999007, "movie", arr_folder="/data/movies/Film (2001)")
        self.assertEqual(["dl"], self.removed)

    def test_nothing_is_removed_when_only_the_vod_copy_is_left(self):
        self.jellyfin(VOD)       # Jellyfin already dropped the deleted file
        library._cleanup_playlists_all_users(999007, "movie", arr_folder="/data/movies/Film (2001)")
        self.assertEqual([], self.removed)

    def test_windows_folders_match_too(self):
        self.jellyfin(VOD, DL)
        library._cleanup_playlists_all_users(999007, "movie", arr_folder="D:\\Movies\\Film (2001)")
        self.assertEqual(["dl"], self.removed)

    def test_without_a_folder_the_lookup_is_unchanged(self):
        self.jellyfin(DL)
        library._cleanup_playlists_all_users(999007, "movie")
        self.assertEqual(["dl"], self.removed)


class TestWebhookPassesRadarrsFolder(_Base):
    def setUp(self):
        super().setUp()
        self.threads = []
        test = self

        class _Thread:
            def __init__(self, target=None, args=(), kwargs=None, **kw):
                self.args, self.kwargs = args, kwargs or {}

            def start(self):
                test.threads.append((self.args, self.kwargs))

        p = mock.patch.object(radarr.threading, "Thread", _Thread)
        p.start()
        self.addCleanup(p.stop)
        for name in ("_check_webhook_auth", "emit_library_event", "log_activity"):
            q = mock.patch.object(radarr, name, lambda *a, **k: None)
            q.start()
            self.addCleanup(q.stop)
        self.db.add(Movie(tmdb_id=999007, title="Film", year="2001", source="radarr",
                          radarr_path="/data/movies/Film (2001)/Film (2001) WEBDL-1080p.mkv"))
        self.db.commit()

    def test_movie_file_delete(self):
        radarr.radarr_webhook({"eventType": "MovieFileDelete", "deleteReason": "manual",
                               "movie": {"tmdbId": 999007, "title": "Film", "folderPath": "/data/movies/Film (2001)"},
                               "movieFile": {"path": "/data/movies/Film (2001)/Film (2001) WEBDL-1080p.mkv"}},
                              None, self.db)
        args, kwargs = self.threads[0]
        self.assertEqual((999007, "movie"), args)
        self.assertEqual("/data/movies/Film (2001)", kwargs.get("arr_folder"))

    def test_movie_delete(self):
        radarr.radarr_webhook({"eventType": "MovieDelete",
                               "movie": {"tmdbId": 999007, "title": "Film", "folderPath": "/data/movies/Film (2001)"}},
                              None, self.db)
        _, kwargs = self.threads[0]
        self.assertEqual("/data/movies/Film (2001)", kwargs.get("arr_folder"))


if __name__ == "__main__":
    unittest.main()

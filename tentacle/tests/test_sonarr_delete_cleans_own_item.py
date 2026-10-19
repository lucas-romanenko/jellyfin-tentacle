"""#296 follow-up (Sonarr side of 86d9699): the SeriesDelete webhook cleans up
playlists for Sonarr's own copy of the show only.

A show's Jellyfin path is its folder, and the VOD copy's folder has the same
name as Sonarr's ("<Title> (<Year>)"), so the folder name alone cannot tell the
two apart: the download is the show whose episodes are not .strm files.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir
from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import Series, set_setting  # noqa: E402
import routers.library as library  # noqa: E402
import routers.sonarr as sonarr  # noqa: E402

VOD = {"Id": "vod", "ProviderIds": {"Tmdb": "999008"}, "Path": "/media/vod/shows/Show (2020)"}
DL = {"Id": "dl", "ProviderIds": {"Tmdb": "999008"}, "Path": "/media/shows/Show (2020)"}
EPISODES = {"vod": [{"Path": "/media/vod/shows/Show (2020)/Season 01/Show (2020) S01E01.strm"}],
            "dl": [{"Path": "/media/shows/Show (2020)/Season 01/Show (2020) S01E01 WEBDL-1080p.mkv"}]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
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
        self.listing_fails = False
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
        test = self

        def _get(self_, path, params=None):
            if test.listing_fails:
                raise RuntimeError("timed out")
            return {"Items": EPISODES.get((params or {}).get("ParentId"), [])}
        for name, fn in (("_fetch_all_items", lambda self_, media_type="Movie": [
                              {k: v for k, v in i.items() if k != "Path"} for i in items]),
                         ("get_item_by_id", lambda self_, item_id: by_id.get(item_id)),
                         ("_get", _get),
                         ("search_by_tmdb_id", lambda self_, *a, **k: items[0] if items else None)):
            p = mock.patch(f"services.jellyfin.JellyfinService.{name}", fn)
            p.start()
            self.addCleanup(p.stop)


class TestCleanupBySonarrFolder(_Base):
    def test_only_the_downloaded_show_leaves_the_playlists(self):
        self.jellyfin(VOD, DL)   # same folder name; the VOD show is "first with this TMDB id"
        library._cleanup_playlists_all_users(999008, "series", arr_folder="/tv/Show (2020)")
        self.assertEqual(["dl"], self.removed)

    def test_nothing_when_only_the_vod_show_is_left(self):
        self.jellyfin(VOD)
        library._cleanup_playlists_all_users(999008, "series", arr_folder="/tv/Show (2020)")
        self.assertEqual([], self.removed)

    def test_nothing_when_the_episodes_cannot_be_listed(self):
        self.jellyfin(VOD, DL)
        self.listing_fails = True
        library._cleanup_playlists_all_users(999008, "series", arr_folder="/tv/Show (2020)")
        self.assertEqual([], self.removed)


class TestWebhookPassesSonarrsFolder(_Base):
    def test_series_delete(self):
        threads = []

        class _Thread:
            def __init__(self, target=None, args=(), kwargs=None, **kw):
                self.args, self.kwargs = args, kwargs or {}

            def start(self):
                threads.append((self.args, self.kwargs))
        with mock.patch.object(sonarr.threading, "Thread", _Thread), \
                mock.patch.object(sonarr, "_check_webhook_auth", lambda *a: None), \
                mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None), \
                mock.patch.object(sonarr, "log_activity", lambda *a, **k: None):
            self.db.add(Series(tmdb_id=999008, title="Show", year="2020", source="sonarr",
                               sonarr_path="/tv/Show (2020)"))
            self.db.commit()
            sonarr.sonarr_webhook({"eventType": "SeriesDelete",
                                   "series": {"tmdbId": 999008, "title": "Show", "path": "/tv/Show (2020)"}},
                                  None, self.db)
        args, kwargs = threads[0]
        self.assertEqual((999008, "series"), args)
        self.assertEqual("/tv/Show (2020)", kwargs.get("arr_folder"))


if __name__ == "__main__":
    unittest.main()

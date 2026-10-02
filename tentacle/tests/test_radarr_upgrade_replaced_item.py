"""Radarr webhook after a quality upgrade: the pass works on the NEW file's
Jellyfin item, never on the replaced file's item that Jellyfin is removing.

Radarr replaces the file (new name), then sends Download. The pass started
Jellyfin's library scan a moment earlier (scan_radarr_library), and its own
library listing could still hold the replaced file's item, found first by
TMDB id. Then either:
- Jellyfin removed that item mid-pass: get_item_by_id() raised the 404 and
  the whole pass ended in "Background processing failed" (no playlist add,
  no notice, no activity line), or
- the pass finished on it: tags, playlist add and the stored item id went to
  an item that was gone a moment later, so the film dropped out of the
  user's list playlists until the next full refresh.

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_radarr_upgrade_replaced_item.py"
"""
import threading
import unittest
from pathlib import Path
from unittest import mock

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
import routers.radarr as radarr  # noqa: E402
import services.jellyfin as jfmod  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402

TMDB = 999321
FOLDER = "/data/movies/Film (2001)"
OLD = {"Id": "old", "Name": "Film", "ProductionYear": 2001, "Type": "Movie",
       "Path": "/media/movies/Film (2001)/Film (2001) WEBDL-1080p.mkv",
       "ProviderIds": {"Tmdb": str(TMDB)}, "Tags": ["Old"], "ImageTags": {"Primary": "x"}}
NEW = dict(OLD, Id="new", Path="/media/movies/Film (2001)/Film (2001) Bluray-1080p.mkv", Tags=[])
VOD = dict(OLD, Id="vod", Path="/media/vod/movies/Film (2001)/Film (2001).strm", Tags=["VOD"])


def _404(item_id):
    resp = requests.Response()
    resp.status_code = 404
    return requests.HTTPError(f"404 Client Error: Not Found for url: /Users/u/Items/{item_id}", response=resp)


class FakeJellyfin(jfmod.JellyfinService):
    """The real search_by_tmdb_id over a scripted library; every write recorded.

    listings: one list of items per library listing, the last one repeating.
    gone: ids Jellyfin has removed (GETs and writes on them 404).
    """
    listings = []
    gone = set()
    errors = {}        # item id -> exception its GET raises
    writes = []

    def __init__(self, *a, **k):
        self.user_id = "u"
        self._n = 0

    def _fetch_all_items(self, media_type="Movie", **kw):
        cls = type(self)
        i = min(cls._listed, len(cls.listings) - 1)
        cls._listed += 1
        return [dict(x) for x in cls.listings[i]]

    def _check(self, item_id):
        if item_id in type(self).errors:
            raise type(self).errors[item_id]
        if item_id in type(self).gone:
            raise _404(item_id)

    def get_item_by_id(self, item_id):
        self._check(item_id)
        return next(dict(x) for x in (OLD, NEW, VOD) if x["Id"] == item_id)

    def set_item_tags(self, item_id, tags):
        self._check(item_id)
        type(self).writes.append(("tags", item_id))
        return True

    def refresh_item_metadata(self, item_id, replace_all=False):
        if item_id in type(self).gone:
            return False   # the real one swallows the 404
        type(self).writes.append(("refresh", item_id))
        return True

    def wait_for_images(self, item_id, max_wait=30, poll_interval=3):
        self._check(item_id)
        return True

    def trigger_library_scan(self, library_id=None):
        return True


class _Pass(unittest.TestCase):
    radarr_path = f"{FOLDER}/Film (2001) Bluray-1080p.mkv"

    def setUp(self):
        tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        for k, v in (("jellyfin_url", "http://jf"), ("jellyfin_api_key", "k"), ("jellyfin_user_id", "u")):
            mdb.set_setting(db, k, v)
        user = mdb.TentacleUser(jellyfin_user_id="u1", display_name="requester")
        db.add(user)
        db.add(mdb.Movie(tmdb_id=TMDB, title="Film", year="2001", source="radarr",
                         radarr_path=self.radarr_path, tags=["Downloaded Movies"],
                         jellyfin_item_id="old"))
        db.commit()
        db.add(mdb.DownloadRequest(tmdb_id=TMDB, media_type="movie", user_id=user.id))
        db.commit()
        db.close()

        FakeJellyfin.listings, FakeJellyfin.gone, FakeJellyfin.errors = [], set(), {}
        FakeJellyfin.writes, FakeJellyfin._listed = [], 0
        self.playlist_adds, self.activity, self.threads = [], [], []
        real_thread = threading.Thread
        test = self

        def tracking_thread(*a, **k):
            t = real_thread(*a, **k)
            test.threads.append(t)
            return t

        def add_to_playlists(db, item_id, tags, media_type, jf_item=None):
            test.playlist_adds.append(item_id)
            return {"added_to": 1}

        for target, value in (
                ("models.database.SessionLocal", self.Session),
                ("models.database.log_activity", lambda db, kind, msg, *a, **k: test.activity.append(msg)),
                ("services.jellyfin.JellyfinService", FakeJellyfin),
                ("services.smartlists.add_item_to_matching_playlists", add_to_playlists),
                ("services.smartlists._notify_jellyfin_plugin", lambda db: None),
                ("time.sleep", lambda s: None)):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("scan_radarr_library", lambda db: {}),
                            ("_check_webhook_auth", lambda *a, **k: None),
                            ("emit_library_event", lambda *a, **k: None),
                            ("log_activity", lambda *a, **k: None)):
            p = mock.patch.object(radarr, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(radarr.threading, "Thread", tracking_thread)
        p.start()
        self.addCleanup(p.stop)

    def download(self, upgrade=True):
        db = self.Session()
        try:
            radarr.radarr_webhook({"eventType": "Download", "isUpgrade": upgrade,
                                   "movie": {"tmdbId": TMDB, "title": "Film", "folderPath": FOLDER},
                                   "movieFile": {"path": self.radarr_path}}, None, db)
        finally:
            db.close()
        for t in self.threads:
            t.join(20)

    def row(self):
        db = self.Session()
        try:
            return db.query(mdb.Movie).filter(mdb.Movie.tmdb_id == TMDB).one().jellyfin_item_id
        finally:
            db.close()

    def notices(self):
        db = self.Session()
        try:
            return [(n.message, n.jellyfin_item_id) for n in db.query(mdb.Notification)]
        finally:
            db.close()


class UpgradeUsesTheNewFile(_Pass):
    def test_replaced_item_removed_mid_pass_does_not_end_the_pass(self):
        # The listing still holds the replaced file's item; Jellyfin removes
        # it before the pass reads it. The next listing has the new file.
        FakeJellyfin.listings = [[OLD], [NEW]]
        FakeJellyfin.gone = {"old"}
        self.download()
        self.assertEqual(["new"], self.playlist_adds)
        self.assertEqual("new", self.row())
        self.assertEqual(["Radarr downloaded 'Film'"], self.activity, "the pass ended early")
        self.assertEqual(1, len(self.notices()))
        self.assertNotIn(("tags", "old"), FakeJellyfin.writes)

    def test_replaced_item_still_listed_and_alive_is_not_used(self):
        # Jellyfin's scan has not reached the folder yet: the replaced item
        # still answers. The pass must wait for the new one, not tag it.
        FakeJellyfin.listings = [[OLD], [OLD], [NEW]]
        self.download()
        self.assertEqual([], [w for w in FakeJellyfin.writes if w[1] == "old"])
        self.assertEqual(["new"], self.playlist_adds)
        self.assertEqual("new", self.row())
        self.assertEqual([("Film has completed and is ready to watch", "new")], self.notices())

    def test_both_listed_picks_the_new_file(self):
        FakeJellyfin.listings = [[OLD, NEW]]
        self.download()
        self.assertEqual(["new"], self.playlist_adds)
        self.assertEqual([("tags", "new"), ("refresh", "new")], FakeJellyfin.writes)

    def test_vod_copy_of_the_same_film_is_not_taken_for_the_download(self):
        FakeJellyfin.listings = [[VOD, NEW]]
        self.download(upgrade=False)
        self.assertEqual(["new"], self.playlist_adds)
        self.assertEqual("new", self.row())


class JellyfinErrorsDoNotSkipTheRest(_Pass):
    def test_error_reading_the_item_still_notifies_and_logs(self):
        resp = requests.Response()
        resp.status_code = 500
        FakeJellyfin.listings = [[NEW]]
        FakeJellyfin.errors = {"new": requests.HTTPError("500 Server Error", response=resp)}
        self.download(upgrade=False)
        self.assertEqual(1, len(self.notices()), "a Jellyfin error must not cost the notice")
        self.assertEqual(["Radarr downloaded 'Film'"], self.activity)


class UnchangedWhenTheFileNameCannotHelp(_Pass):
    def test_no_match_by_file_name_falls_back_to_the_tmdb_match(self):
        # Jellyfin shows the film under another file name (another layout):
        # after the retries the pass uses the TMDB match, as before.
        FakeJellyfin.listings = [[dict(NEW, Path="/movies/Film (2001)/VIDEO_TS")]]
        self.download(upgrade=False)
        self.assertEqual(["new"], self.playlist_adds)
        self.assertEqual("new", self.row())


class UnchangedWithoutAStoredPath(_Pass):
    radarr_path = None

    def test_no_radarr_path_takes_the_tmdb_match_at_once(self):
        FakeJellyfin.listings = [[NEW]]
        self.download(upgrade=False)
        self.assertEqual(1, FakeJellyfin._listed)
        self.assertEqual(["new"], self.playlist_adds)


class SearchByFileName(unittest.TestCase):
    def test_file_name_match_handles_windows_paths_and_case(self):
        svc = FakeJellyfin()
        FakeJellyfin._listed = 0
        FakeJellyfin.listings = [[OLD, dict(NEW, Path="D:\\Movies\\Film (2001)\\film (2001) bluray-1080p.MKV")]]
        item = svc.search_by_tmdb_id(TMDB, "Movie", title="Film", year="2001",
                                     file_name="Film (2001) Bluray-1080p.mkv")
        self.assertEqual("new", item["Id"])

    def test_without_file_name_first_tmdb_match_as_before(self):
        svc = FakeJellyfin()
        FakeJellyfin._listed = 0
        FakeJellyfin.listings = [[OLD, NEW]]
        self.assertEqual("old", svc.search_by_tmdb_id(TMDB, "Movie")["Id"])


if __name__ == "__main__":
    unittest.main()

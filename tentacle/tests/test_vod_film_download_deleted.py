"""A VOD film whose Radarr download is deleted goes back to VOD only (#378).

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_vod_film_download_deleted.py"

A film that was a VOD title first and was then downloaded by Radarr is ONE
row (source provider_N, strm_path + radarr_path). MovieFileDelete,
MovieDelete and the Radarr scan only touched rows with source "radarr", so
when the download went the VOD row kept radarr_path, downloaded_at, the
deleted item's Jellyfin id and "Downloaded Movies" for good: the VOD copy
stayed in every user's "Downloaded Movies" playlist although nothing was
downloaded. Now the row loses the download and keeps the VOD copy.
"""
import logging
import shutil
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

import models.database as mdb  # noqa: E402
from models.database import Movie  # noqa: E402
import routers.radarr as radarr  # noqa: E402
import services.radarr as sr  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402

TMDB = 999311
DL = "/data/movies/Film (2001)/Film (2001) WEBDL-1080p.mkv"
NFO = """<?xml version="1.0" encoding="utf-8"?>
<movie>
  <title>Film</title>
  <tag>Netflix Movies</tag>
  <tag>Downloaded Movies</tag>
  <tag>requester's Downloads</tag>
</movie>
"""


class _Thread:
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.call = (getattr(target, "__name__", str(target)), args, kwargs or {})

    def start(self):
        _Thread.started.append(self.call)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        _Thread.started = []
        p = mock.patch.object(radarr.threading, "Thread", _Thread)
        p.start()
        self.addCleanup(p.stop)
        for name in ("_check_webhook_auth", "emit_library_event", "log_activity"):
            q = mock.patch.object(radarr, name, lambda *a, **k: None)
            q.start()
            self.addCleanup(q.stop)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        radarr._missing_pending.clear()
        self.addCleanup(radarr._missing_pending.clear)
        radarr._missing_recent.clear()
        self.addCleanup(radarr._missing_recent.clear)

        folder = self.tmp / "vod" / "Film (2001)"
        folder.mkdir(parents=True)
        self.strm = folder / "Film (2001).strm"
        self.strm.write_text("http://provider/movie/1.mp4\n")
        self.vod_nfo = folder / "Film (2001).nfo"
        self.vod_nfo.write_text(NFO)
        user = mdb.TentacleUser(jellyfin_user_id="u1", display_name="requester")
        self.db.add(user)
        self.db.commit()
        # The state the Download webhook + Radarr scan leave: one row, both copies.
        self.db.add(Movie(tmdb_id=TMDB, title="Film", year="2001", source="provider_1", provider_id=1,
                          source_tag="Netflix", strm_path=str(self.strm),
                          nfo_path="/media/movies/Film (2001)/Film (2001) WEBDL-1080p.nfo",
                          radarr_path=DL, jellyfin_item_id="dl-item", downloaded_at=datetime(2026, 9, 1),
                          tags=["Netflix Movies", "Downloaded Movies", "Recently Added Movies",
                                "requester's Downloads"]))
        self.db.add(mdb.Duplicate(tmdb_id=TMDB, media_type="movie", resolution="pending",
                                  sources=[{"source": "radarr", "path": DL},
                                           {"source": "provider_1", "path": str(self.strm)}]))
        self.db.add(mdb.DownloadRequest(tmdb_id=TMDB, media_type="movie", user_id=user.id))
        self.db.commit()

    def row(self):
        self.db.expire_all()
        return self.db.query(Movie).filter(Movie.tmdb_id == TMDB).one()

    def event(self, event, reason=None, tmdb_id=TMDB, path=DL):
        payload = {"eventType": event,
                   "movie": {"tmdbId": tmdb_id, "title": "Film", "folderPath": path.rsplit("/", 1)[0]},
                   "movieFile": {"path": path}}
        if reason is not None:
            payload["deleteReason"] = reason
        return radarr.radarr_webhook(payload, None, self.db)

    def assert_vod_only(self, request_tag_gone=True):
        r = self.row()
        self.assertEqual(str(self.strm), r.strm_path, "the VOD copy must stay")
        self.assertEqual("provider_1", r.source)
        self.assertTrue(self.strm.exists())
        self.assertIsNone(r.radarr_path, "radarr_path still names the deleted download")
        self.assertIsNone(r.downloaded_at)
        self.assertIsNone(r.jellyfin_item_id, "still the deleted download's Jellyfin item")
        self.assertEqual(str(self.vod_nfo), r.nfo_path, "still the download's NFO")
        self.assertNotIn("Downloaded Movies", r.tags,
                         "VOD-only film still tagged 'Downloaded Movies' (stays in that playlist for every user)")
        self.assertIn("Netflix Movies", r.tags)
        self.assertIn("Recently Added Movies", r.tags)
        nfo = self.vod_nfo.read_text()
        self.assertNotIn("<tag>Downloaded Movies</tag>", nfo, "the .strm's NFO brings the tag back")
        self.assertIn("<tag>Netflix Movies</tag>", nfo)
        self.assertEqual(request_tag_gone, "requester's Downloads" not in r.tags)
        self.assertEqual(0, self.db.query(mdb.Duplicate).filter_by(tmdb_id=TMDB, resolution="pending").count(),
                         "a pending duplicate for a download that is gone")


class WebhookDeletes(_Base):
    def test_movie_file_delete_manual(self):
        self.assertEqual("deleted", self.event("MovieFileDelete", "manual")["status"])
        self.assert_vod_only()
        self.assertEqual(0, self.db.query(mdb.DownloadRequest).count())
        self.assertEqual(_Thread.started, [("_cleanup_playlists_all_users", (TMDB, "movie"),
                                            {"arr_folder": "/data/movies/Film (2001)"})],
                         "the download's item is not taken out of the playlists")

    def test_movie_delete(self):
        self.assertEqual("deleted", self.event("MovieDelete")["status"])
        self.assert_vod_only()
        self.assertEqual(0, self.db.query(mdb.DownloadRequest).count())
        self.assertEqual(1, len(_Thread.started))

    def test_bad_copy_replacement_keeps_the_request_and_its_tag(self):
        with mock.patch("services.bad_copy.is_replacing", return_value=True):
            self.event("MovieFileDelete", "manual")
        self.assert_vod_only(request_tag_gone=False)
        self.assertEqual(1, self.db.query(mdb.DownloadRequest).count())

    def test_upgrade_delete_is_still_ignored(self):
        self.assertEqual("ignored", self.event("MovieFileDelete", "upgrade")["status"])
        r = self.row()
        self.assertEqual(DL, r.radarr_path)
        self.assertEqual("dl-item", r.jellyfin_item_id)
        self.assertIn("Downloaded Movies", r.tags)

    def test_a_plain_vod_title_is_left_alone(self):
        self.db.add(Movie(tmdb_id=5, title="VOD", year="2001", source="provider_1", provider_id=1,
                          strm_path="/media/vod/movies/VOD (2001)/VOD (2001).strm", jellyfin_item_id="vod-item",
                          tags=["Netflix Movies"]))
        self.db.commit()
        self.event("MovieFileDelete", "manual", tmdb_id=5, path="/data/movies/VOD (2001)/VOD.mkv")
        r = self.db.query(Movie).filter_by(tmdb_id=5).one()
        self.assertEqual("vod-item", r.jellyfin_item_id)
        self.assertEqual(_Thread.started, [])

    def test_missing_from_disk_releases_it_once_judged(self):
        timers = []

        class _Timer:
            def __init__(self, delay, fn, args=()):
                self.args, self.daemon = args, False

            def start(self):
                timers.append(self)

            def cancel(self):
                pass
        for i in range(9):  # other downloads that keep their file: no outage
            self.db.add(Movie(tmdb_id=100 + i, title=f"Other {i}", source="radarr", radarr_path=f"/data/o{i}.mkv"))
        self.db.commit()
        with mock.patch.object(radarr.threading, "Timer", _Timer):
            self.assertEqual("queued", self.event("MovieFileDelete", "missingFromDisk")["status"])
        self.assertEqual(DL, self.row().radarr_path, "released before the burst was judged")
        self.assertEqual({"status": "removed", "removed": 1},
                         radarr._flush_missing_from_disk(*timers[-1].args, db=self.db))
        self.assert_vod_only()

    def test_missing_from_disk_counts_vod_downloads_in_the_outage_guard(self):
        """Three VOD titles' downloads of four downloads in all: refused."""
        for i in range(2):
            self.db.add(Movie(tmdb_id=200 + i, title=f"V{i}", source="provider_1", provider_id=1,
                              strm_path=f"/vod/V{i}.strm", radarr_path=f"/data/v{i}.mkv",
                              tags=["Downloaded Movies"]))
        self.db.add(Movie(tmdb_id=300, title="D", source="radarr", radarr_path="/data/d.mkv"))
        self.db.commit()
        for t in (TMDB, 200, 201):
            radarr._missing_pending[t] = ("x", "/data")
        self.assertEqual("refused", radarr._flush_missing_from_disk(db=self.db)["status"])
        self.assertEqual(DL, self.row().radarr_path)


class ScanAfterDownloadGone(_Base):
    """Radarr no longer has the file (the webhook was missed)."""

    def scan(self, radarr_movies, others=40):
        for i in range(others):  # other downloaded films that keep their file
            t = 990000 + i
            self.db.add(Movie(tmdb_id=t, title=f"Other {i}", year="2001", source="radarr",
                              radarr_path=f"/data/movies/Other {i}/o.mkv"))
            radarr_movies.append({"id": 100 + i, "tmdbId": t, "title": f"Other {i}", "year": 2001, "hasFile": True,
                                  "path": f"/data/movies/Other {i}",
                                  "movieFile": {"path": f"/data/movies/Other {i}/o.mkv"}})
        self.db.commit()
        svc = mock.MagicMock()
        svc.get_all_movies.return_value = radarr_movies
        mdb.set_setting(self.db, "radarr_url", "http://radarr")
        mdb.set_setting(self.db, "radarr_api_key", "k")
        with mock.patch.object(sr, "RadarrService", return_value=svc), \
                mock.patch("services.tmdb.get_tmdb_token", return_value=None):
            return sr.scan_radarr_library(self.db)

    def test_listed_without_a_file(self):
        stats = self.scan([{"id": 1, "tmdbId": TMDB, "title": "Film", "year": 2001, "hasFile": False,
                            "path": "/data/movies/Film (2001)"}])
        self.assert_vod_only()
        self.assertEqual((0, 1), (stats["removed"], stats["released"]))
        self.assertEqual(0, self.db.query(mdb.DownloadRequest).count())

    def test_no_longer_in_radarr(self):
        self.scan([])
        self.assert_vod_only()

    def test_vod_downloads_count_in_the_outage_guard(self):
        """Radarr's storage gone: every download listed without a file, VOD
        titles' too (4 of 5 downloads). Nothing is touched."""
        for i in range(3):
            self.db.add(Movie(tmdb_id=400 + i, title=f"V{i}", source="provider_1", provider_id=1,
                              strm_path=f"/vod/V{i}.strm", radarr_path=f"/data/v{i}.mkv"))
        self.db.add(Movie(tmdb_id=500, title="D", source="radarr", radarr_path="/data/d.mkv"))
        self.db.commit()
        listed = [{"id": 1, "tmdbId": t, "title": "x", "hasFile": False} for t in (TMDB, 400, 401, 402)]
        listed.append({"id": 2, "tmdbId": 500, "title": "D", "hasFile": True, "movieFile": {"path": "/data/d.mkv"}})
        stats = self.scan(listed, others=0)
        self.assertEqual((0, 0, 4), (stats["removed"], stats["released"], stats["removals_refused"]))
        self.assertEqual(DL, self.row().radarr_path)

    def test_a_download_still_there_keeps_its_claim(self):
        self.scan([{"id": 1, "tmdbId": TMDB, "title": "Film", "year": 2001, "hasFile": True,
                    "path": "/data/movies/Film (2001)", "movieFile": {"path": DL}}])
        r = self.row()
        self.assertEqual(DL, r.radarr_path)
        self.assertIn("Downloaded Movies", r.tags)


if __name__ == "__main__":
    unittest.main()

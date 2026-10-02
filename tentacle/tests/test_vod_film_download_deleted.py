"""A VOD film whose download is deleted goes back to being a VOD title (#378).

A film that was a VOD title first and was then downloaded by Radarr is ONE
row (source provider_N, strm_path + radarr_path, the download's Jellyfin item
id, tags "Downloaded Movies" and the requester's "<name>'s Downloads"). The
delete paths (MovieFileDelete, MovieDelete, the Radarr scan when Radarr no
longer has a file) only handled rows with source == "radarr", so this row
kept claiming the deleted download for good: radarr_path, the deleted item's
id, and "Downloaded Movies", which kept its VOD copy in every user's
"Downloaded Movies" playlist and home row.

Now only the claim goes; the .strm, its folder and the row's provider
ownership stay, so the VOD Jellyfin item keeps its id and every user's
watched state.

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_vod_film_download_deleted.py"
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
from models.database import DownloadRequest, Duplicate, Movie, TentacleUser  # noqa: E402
import routers.radarr as radarr  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402

TMDB = 999311
DL = "/data/movies/Film (2001)/Film (2001) WEBDL-1080p.mkv"
TAGS = ["Netflix Movies", "Downloaded Movies", "Recently Added Movies", "Bea's Downloads", "Al's Downloads",
        "My List"]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.threads = []
        test = self

        class _Thread:
            def __init__(self, target=None, args=(), kwargs=None, **kw):
                self.target, self.args, self.kwargs = target, args, kwargs or {}

            def start(self):
                test.threads.append((self.target.__name__, self.args, self.kwargs))

        for target, name, value in ((radarr.threading, "Thread", _Thread),
                                    (radarr, "_check_webhook_auth", lambda *a, **k: None),
                                    (radarr, "emit_library_event", lambda *a, **k: None),
                                    (radarr, "log_activity", lambda *a, **k: None)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        vod = self.tmp / "vod" / "movies" / "Film (2001)"
        vod.mkdir(parents=True)
        self.strm, self.nfo = vod / "Film (2001).strm", vod / "Film (2001).nfo"
        self.strm.write_text("http://provider/movie/1.mp4")
        self.nfo.write_text("<movie>\n  <title>Film</title>\n" +
                            "".join(f"  <tag>{t}</tag>\n" for t in TAGS) + "  <tag>a keyword</tag>\n</movie>\n")
        self.strm_mtime = self.strm.stat().st_mtime_ns
        bea = TentacleUser(jellyfin_user_id="b" * 32, display_name="Bea")
        al = TentacleUser(jellyfin_user_id="a" * 32, display_name="Al")
        self.db.add_all([bea, al])
        self.db.add(mdb.ListSubscription(user_id=None, name="My List", type="trakt", url="https://example.invalid",
                                         tag="My List", active=True))
        self.db.commit()
        self.db.add(DownloadRequest(tmdb_id=TMDB, media_type="movie", user_id=bea.id))
        # The state the Download webhook + Radarr scan leave (seen live on 1c95c59); the
        # scan points nfo_path at the download's NFO.
        self.db.add(Movie(tmdb_id=TMDB, title="Film", year="2001", source="provider_1", provider_id=1,
                          source_tag="Netflix", strm_path=str(self.strm), nfo_path=DL[:-4] + ".nfo",
                          radarr_path=DL, jellyfin_item_id="dl-item", tags=list(TAGS)))
        self.db.add(Duplicate(tmdb_id=TMDB, media_type="movie", resolution="pending",
                              sources=[{"source": "radarr", "path": DL},
                                       {"source": "provider_1", "path": str(self.strm)}]))
        self.db.commit()

    def row(self):
        self.db.expire_all()
        return self.db.query(Movie).filter(Movie.tmdb_id == TMDB).one()

    def assert_back_to_vod(self, requester_tag_kept=False):
        r = self.row()
        self.assertEqual(("provider_1", str(self.strm)), (r.source, r.strm_path), "the VOD copy must stay")
        self.assertIsNone(r.radarr_path, "radarr_path still names the deleted download")
        self.assertIsNone(r.downloaded_at)
        self.assertIsNone(r.jellyfin_item_id, "still the deleted download's Jellyfin item")
        want = ["Netflix Movies", "Recently Added Movies"] + (["Bea's Downloads"] if requester_tag_kept else []) \
            + ["My List"]
        self.assertEqual(want, r.tags, "VOD-only film still tagged as downloaded")
        self.assertEqual(str(self.nfo), r.nfo_path, "nfo_path must be the VOD copy's NFO again")
        nfo = self.nfo.read_text()
        self.assertNotIn("<tag>Downloaded Movies</tag>", nfo)
        self.assertNotIn("<tag>Al's Downloads</tag>", nfo)
        self.assertIn("<tag>a keyword</tag>", nfo, "a tag Tentacle didn't write must stay")
        self.assertEqual(self.strm_mtime, self.strm.stat().st_mtime_ns, "the .strm must not be rewritten")
        self.assertEqual("http://provider/movie/1.mp4", self.strm.read_text())
        self.assertEqual(0, self.db.query(Duplicate).filter(Duplicate.resolution == "pending").count())


class TestWebhooks(_Base):
    def test_movie_file_delete_manual(self):
        out = radarr.radarr_webhook({"eventType": "MovieFileDelete", "deleteReason": "manual",
                                     "movie": {"tmdbId": TMDB, "title": "Film", "folderPath": "/data/movies/Film (2001)"},
                                     "movieFile": {"path": DL}}, None, self.db)
        self.assertEqual("deleted", out["status"])
        self.assert_back_to_vod()
        self.assertEqual(0, self.db.query(DownloadRequest).count())
        names = [t[0] for t in self.threads]
        self.assertIn("_vod_row_released_background", names)
        bg = next(t for t in self.threads if t[0] == "_vod_row_released_background")
        self.assertEqual((TMDB, ["Al's Downloads", "Bea's Downloads", "Downloaded Movies"],
                          "/data/movies/Film (2001)"), bg[1])

    def test_movie_delete(self):
        radarr.radarr_webhook({"eventType": "MovieDelete",
                               "movie": {"tmdbId": TMDB, "title": "Film", "folderPath": "/data/movies/Film (2001)"}},
                              None, self.db)
        self.assert_back_to_vod()

    def test_bad_copy_replacement_keeps_the_requesters_tag(self):
        from services.bad_copy import mark_replacing
        mark_replacing(self.db, "movie", TMDB)
        radarr.radarr_webhook({"eventType": "MovieFileDelete", "deleteReason": "manual",
                               "movie": {"tmdbId": TMDB, "title": "Film"}, "movieFile": {"path": DL}}, None, self.db)
        self.assert_back_to_vod(requester_tag_kept=True)
        self.assertEqual(1, self.db.query(DownloadRequest).count())

    def test_upgrade_delete_is_still_ignored(self):
        # Control: an upgrade replaces the file; the row keeps claiming a download.
        radarr.radarr_webhook({"eventType": "MovieFileDelete", "deleteReason": "upgrade",
                               "movie": {"tmdbId": TMDB, "title": "Film"}, "movieFile": {"path": DL}},
                              None, self.db)
        self.assertEqual(DL, self.row().radarr_path)
        self.assertIn("Downloaded Movies", self.row().tags)
        self.assertEqual([], self.threads)

    def test_a_radarr_row_is_still_deleted(self):
        self.db.query(Movie).delete()
        self.db.add(Movie(tmdb_id=TMDB + 1, title="Other", source="radarr", radarr_path="/data/movies/O/o.mkv"))
        self.db.commit()
        radarr.radarr_webhook({"eventType": "MovieDelete", "movie": {"tmdbId": TMDB + 1, "title": "Other"}},
                              None, self.db)
        self.assertEqual(0, self.db.query(Movie).count())
        self.assertNotIn("_vod_row_released_background", [t[0] for t in self.threads])


class TestScanAfterDownloadGone(_Base):
    """Radarr still lists the film without a file, or not at all (the webhook was missed)."""

    def scan(self, film_entry, others_have_files=True, n_others=40):
        import services.radarr as sr
        radarr_movies = [film_entry] if film_entry else []
        for i in range(n_others):
            t = 990000 + i
            self.db.add(Movie(tmdb_id=t, title=f"Other {i}", year="2001", source="radarr",
                              radarr_path=f"/data/movies/Other {i}/o.mkv"))
            radarr_movies.append({"id": 100 + i, "tmdbId": t, "title": f"Other {i}", "year": 2001,
                                  "hasFile": others_have_files, "path": f"/data/movies/Other {i}",
                                  "movieFile": {"path": f"/data/movies/Other {i}/o.mkv"} if others_have_files else None})
        self.db.commit()
        svc = mock.MagicMock()
        svc.get_all_movies.return_value = radarr_movies
        mdb.set_setting(self.db, "radarr_url", "http://radarr")
        mdb.set_setting(self.db, "radarr_api_key", "k")
        with mock.patch.object(sr, "RadarrService", return_value=svc), \
                mock.patch("services.tmdb.get_tmdb_token", return_value=None):
            return sr.scan_radarr_library(self.db)

    def test_listed_without_a_file(self):
        stats = self.scan({"id": 1, "tmdbId": TMDB, "title": "Film", "year": 2001, "hasFile": False,
                           "path": "/data/movies/Film (2001)"})
        self.assertEqual(1, stats["released"])
        self.assert_back_to_vod()
        self.assertEqual(0, self.db.query(DownloadRequest).count())

    def test_no_longer_in_radarr(self):
        self.scan(None)
        self.assert_back_to_vod()

    def test_still_downloaded_nothing_changes(self):
        self.scan({"id": 1, "tmdbId": TMDB, "title": "Film", "year": 2001, "hasFile": True,
                   "path": "/data/movies/Film (2001)", "movieFile": {"path": DL}})
        r = self.row()
        self.assertEqual(DL, r.radarr_path)
        self.assertIn("Downloaded Movies", r.tags)

    def test_storage_outage_releases_nothing(self):
        """#106: Radarr reporting no file for most titles at once is its storage, not a clean-up."""
        stats = self.scan({"id": 1, "tmdbId": TMDB, "title": "Film", "year": 2001, "hasFile": False,
                           "path": "/data/movies/Film (2001)"}, others_have_files=False, n_others=5)
        self.assertEqual(0, stats.get("released", 0))
        self.assertEqual(DL, self.row().radarr_path)
        self.assertIn("Downloaded Movies", self.row().tags)


class FakeJellyfin:
    """Jellyfin still lists the download item next to the VOD item."""
    items = {}
    tag_writes = []

    def __init__(self, url, key, user_id=None):
        pass

    def _fetch_all_items(self, media_type="Movie"):
        return [{"Id": i, "ProviderIds": {"Tmdb": str(TMDB)}} for i in self.items]

    def get_item_by_id(self, item_id):
        return {"Id": item_id, "Path": self.items[item_id]}

    def set_item_owned_tags(self, item_id, desired, owned, add_only=False):
        self.tag_writes.append((item_id, list(desired), add_only))
        return "written"


class TestBackground(_Base):
    def test_vod_item_untagged_and_those_playlists_refreshed(self):
        radarr.radarr_webhook({"eventType": "MovieFileDelete", "deleteReason": "manual",
                               "movie": {"tmdbId": TMDB, "title": "Film"}, "movieFile": {"path": DL}}, None, self.db)
        mdb.set_setting(self.db, "jellyfin_url", "http://jf")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        FakeJellyfin.items = {"vod-item": "/vod-movies/Film (2001)/Film (2001).strm", "dl-item": "/movies/" + DL[13:]}
        FakeJellyfin.tag_writes = []
        _, args, _ = next(t for t in self.threads if t[0] == "_vod_row_released_background")
        import services.smartlists as sl
        engine = self.db.get_bind()
        with mock.patch("models.database.SessionLocal", sessionmaker(bind=engine)), \
                mock.patch("services.jellyfin.JellyfinService", FakeJellyfin), \
                mock.patch("routers.library._cleanup_playlists_all_users") as cleanup, \
                mock.patch.object(sl, "refresh_smartlist_playlists", return_value={"changed": 1}) as refresh, \
                mock.patch.object(sl, "_notify_jellyfin_plugin") as notify:
            radarr._vod_row_released_background(*args)
        cleanup.assert_called_once_with(TMDB, "movie", arr_folder="/data/movies/Film (2001)")
        self.assertEqual([("vod-item", ["Netflix Movies", "Recently Added Movies", "My List"], False)],
                         FakeJellyfin.tag_writes, "only the VOD item, with the row's tags")
        refresh.assert_called_once()
        self.assertEqual(["Al's Downloads", "Bea's Downloads", "Downloaded Movies"],
                         refresh.call_args.kwargs["only_names"])
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()

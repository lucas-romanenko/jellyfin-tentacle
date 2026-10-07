"""Keep VOD survives the delete webhooks Radarr and Sonarr send during it (#515).

Radarr and Sonarr post their Connect webhooks synchronously, inside the API
call that causes them: Keep VOD's file delete brings MovieFileDelete
(deleteReason "manual"), its title delete MovieDelete (Sonarr: SeriesDelete),
all before the call returns. Tentacle's handlers then delete every Duplicate
of the title and release (or remove) its row, in their own session, while the
resolve request still holds them. The request answered 500 after the download
was gone ("Instance <Duplicate> has been deleted", or StaleDataError on the
movie row), the resolution and its deletion-log entry were never recorded,
and for a film whose two copies share a folder the users' watched state saved
on that Duplicate for the kept copy's new Jellyfin item (#333) was deleted
with it. That also happens when the webhook comes after the resolution.

The fake arrs below post the webhooks to Tentacle's real handlers, each in a
session of its own, from inside the delete calls, as Radarr and Sonarr do.
Sessions don't autoflush, as the app's (models.database.SessionLocal).

Run from tentacle/:  python tests/hermetic.py discover -s tests -p test_duplicate_keep_vod_delete_webhook.py
"""
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, DeletionLog, Duplicate, Movie, Series, Setting
from routers import duplicates
from services import duplicates as dup_service
from tests.test_duplicate_keep_vod_merged_folder import FakeRadarr, FakeSonarr, _FakeArr
from tests.test_duplicate_keeps_user_data import FakeJellyfin, setUpModule, tearDownModule  # noqa: F401
from tests.test_duplicate_merged_folder_user_data import MergedJellyfin
from tmp_dirs import temp_dir


class WebhookRadarr(FakeRadarr):
    """Posts MovieFileDelete after each file delete and MovieDelete after the
    title delete, before answering, as Radarr does."""
    test = None

    def delete_movie_file(self, file_id):
        path = next(f["path"] for t in self.titles.values() for f in t["files"] if f["id"] == file_id)
        tmdb_id, title = next((k, t) for k, t in self.titles.items() if any(f["id"] == file_id for f in t["files"]))
        super().delete_movie_file(file_id)
        self.test.radarr_webhook({"eventType": "MovieFileDelete", "deleteReason": "manual",
                                  "movie": {"tmdbId": tmdb_id, "title": "Film", "folderPath": title["path"]},
                                  "movieFile": {"path": path}})

    def delete_movie_by_id(self, movie_id, delete_files=True):
        tmdb_id, title = next((k, t) for k, t in self.titles.items() if t["id"] == movie_id)
        ok = super().delete_movie_by_id(movie_id, delete_files)
        self.test.radarr_webhook({"eventType": "MovieDelete", "deletedFiles": delete_files,
                                  "movie": {"tmdbId": tmdb_id, "title": "Film", "folderPath": title["path"]}})
        return ok


class WebhookSonarr(FakeSonarr):
    """Posts SeriesDelete after the title delete, before answering."""
    test = None

    def delete_series_by_id(self, series_id, delete_files=True):
        tmdb_id, title = next((k, t) for k, t in self.titles.items() if t["id"] == series_id)
        ok = super().delete_series_by_id(series_id, delete_files)
        self.test.sonarr_webhook({"eventType": "SeriesDelete", "deletedFiles": delete_files,
                                  "series": {"tmdbId": tmdb_id, "title": "Show", "path": title["path"]}})
        return ok


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = Path(tmp)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.Session = sessionmaker(bind=engine, autoflush=False)
        self.db = self.Session()
        self.addCleanup(lambda: self.db.close())
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r"),
                     ("sonarr_url", "http://sonarr"), ("sonarr_api_key", "s"),
                     ("jellyfin_url", "http://jf"), ("jellyfin_api_key", "j")):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()
        _FakeArr.titles, _FakeArr.calls, _FakeArr.fail_file_delete = {}, [], False
        FakeJellyfin.items, FakeJellyfin.data, FakeJellyfin.posts = [], {}, []
        FakeJellyfin.users, FakeJellyfin.down, FakeJellyfin.on_post = ["A", "B"], False, None
        MergedJellyfin.notified = []
        WebhookRadarr.test = WebhookSonarr.test = self
        self.webhooks = []
        import routers.radarr as radarr_router
        import routers.sonarr as sonarr_router
        self.radarr_router, self.sonarr_router = radarr_router, sonarr_router
        for p in (mock.patch("services.radarr.RadarrService", WebhookRadarr),
                  mock.patch("services.sonarr.SonarrService", WebhookSonarr),
                  mock.patch("services.jellyfin.JellyfinService", MergedJellyfin),
                  mock.patch.object(dup_service, "start_pending_user_data_worker"),
                  mock.patch.object(radarr_router, "_check_webhook_auth"),
                  mock.patch.object(radarr_router, "emit_library_event"),
                  mock.patch.object(sonarr_router, "_check_webhook_auth"),
                  mock.patch.object(sonarr_router, "emit_library_event"),
                  mock.patch.object(sonarr_router, "_queue_webhook_event"),
                  mock.patch("routers.library._cleanup_playlists_all_users")):
            p.start()
        self.addCleanup(mock.patch.stopall)

    def _post(self, handler, payload):
        s = self.Session()
        try:
            self.webhooks.append((payload["eventType"], handler(payload, request=mock.Mock(), db=s)))
        finally:
            s.close()

    def radarr_webhook(self, payload):
        self._post(self.radarr_router.radarr_webhook, payload)

    def sonarr_webhook(self, payload):
        self._post(self.sonarr_router.sonarr_webhook, payload)

    def resolve(self, dup_id, resolution="keep_vod"):
        try:
            return duplicates.resolve_duplicate(dup_id, duplicates.ResolveRequest(resolution=resolution), db=self.db)
        except Exception as e:  # noqa: BLE001
            self.fail(f"Keep VOD failed after the arr had deleted the download: {type(e).__name__}: {e}")

    def fresh(self):
        """What is committed now."""
        self.db.close()
        self.db = self.Session()
        return self.db

    def assert_recorded(self, media_type="movie"):
        db = self.fresh()
        self.assertEqual(["keep_vod"], [d.resolution for d in db.query(Duplicate).filter(
            Duplicate.media_type == media_type)], "the resolution was not recorded")
        self.assertEqual(1, db.query(DeletionLog).filter(DeletionLog.kind == "duplicate-resolve").count(),
                         "the deletion log has no entry for the deleted download")


class FilmSeparateFolders(_Base):
    """A VOD .strm and a download in separate folders; nobody watched it."""
    def setUp(self):
        super().setUp()
        vod = self.root / "vod" / "movies" / "Film (2001)"
        vod.mkdir(parents=True)
        self.strm = vod / "Film (2001).strm"
        self.strm.write_text("http://p/movie/1.mp4")
        dl = self.root / "movies" / "Film (2001) [1080p]"
        dl.mkdir(parents=True)
        self.mkv = dl / "Film (2001).mkv"
        self.mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[101] = {"id": 7, "path": str(dl), "files": [{"id": 70, "path": str(self.mkv)}]}
        self.srcs = [{"source": "radarr", "path": str(self.mkv)}, {"source": "provider_1", "path": str(self.strm)}]

    def add(self, source="provider_1", twins=1):
        self.db.add(Movie(tmdb_id=101, title="Film", year="2001", source=source,
                          strm_path=str(self.strm), radarr_path=str(self.mkv)))
        dups = [Duplicate(tmdb_id=101, media_type="movie", resolution="pending", sources=list(self.srcs))
                for _ in range(twins)]
        self.db.add_all(dups)
        self.db.commit()
        return [d.id for d in dups]

    def test_keep_vod_is_recorded_and_the_row_goes_back_to_vod(self):
        dup_id, = self.add()
        self.assertEqual({"success": True}, self.resolve(dup_id))
        self.assertIn("MovieDelete", [e for e, _ in self.webhooks], "the webhooks ran inside the delete calls")
        self.assertFalse(self.mkv.exists())
        self.assertTrue(self.strm.exists())
        self.assert_recorded()
        row = self.db.query(Movie).one()
        self.assertEqual(("provider_1", None), (row.source, row.radarr_path))

    def test_a_downloaded_only_row_the_webhook_removed(self):
        dup_id, = self.add(source="radarr")
        self.assertEqual({"success": True}, self.resolve(dup_id))
        self.assertTrue(self.strm.exists())
        self.assert_recorded()

    def test_twin_rows_resolved_together(self):
        a, b = self.add(twins=2)
        self.assertEqual({"success": True}, self.resolve(a))
        self.assertTrue(self.strm.exists())
        db = self.fresh()
        self.assertEqual("keep_vod", db.get(Duplicate, a).resolution)
        self.assertEqual(0, duplicates.get_duplicates(db=db)["pending"])

    def test_resolve_all_counts_it_resolved(self):
        self.add()
        r = duplicates.resolve_all(duplicates.ResolveAllRequest(resolution="keep_vod"), db=self.db)
        self.assertEqual((1, 0), (r["count"], r["failed"]))
        self.assert_recorded()


JF_DIR = "/data/movies/Heat (1995)"
JF_STRM, JF_MKV = f"{JF_DIR}/Heat (1995).strm", f"{JF_DIR}/Heat (1995) - Bluray-1080p.mkv"


class FilmOneFolder(_Base):
    """Both copies in one folder, the download the Jellyfin item's main
    version: Keep VOD saves the users' watched state on the duplicate for the
    item Jellyfin makes for the .strm once the download is gone (#333)."""
    def setUp(self):
        super().setUp()
        folder = self.root / "movies" / "Heat (1995)"
        folder.mkdir(parents=True)
        self.strm = folder / "Heat (1995).strm"
        self.strm.write_text("http://p/movie/1.mp4")
        self.mkv = folder / "Heat (1995) - Bluray-1080p.mkv"
        self.mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[949] = {"id": 4, "path": str(folder), "files": [{"id": 40, "path": str(self.mkv)}]}
        self.db.add(Movie(tmdb_id=949, title="Heat", year="1995", source="provider_1",
                          strm_path=str(self.strm), radarr_path=str(self.mkv)))
        dup = Duplicate(tmdb_id=949, media_type="movie", resolution="pending",
                        sources=[{"source": "radarr", "path": str(self.mkv)},
                                 {"source": "provider_1", "path": str(self.strm)}])
        self.db.add(dup)
        self.db.commit()
        self.dup_id = dup.id
        FakeJellyfin.items = [
            {"Id": "film", "Type": "Movie", "Path": JF_MKV, "ProviderIds": {"Tmdb": "949"},
             "MediaSources": [{"Id": "film", "Path": JF_MKV}, {"Id": "version", "Path": JF_STRM}]},
            {"Id": "version", "Type": "Movie", "Path": JF_STRM, "ProviderIds": {"Tmdb": "949"}, "OwnerId": "film",
             "MediaSources": [{"Id": "version", "Path": JF_STRM}]},
        ]
        FakeJellyfin.data = {("A", "film"): {"Played": True, "PlayCount": 2},
                             ("B", "film"): {"PlaybackPositionTicks": 7_000_000_000}}

    def assert_state_reaches_the_new_item(self):
        # Jellyfin has seen the delete: a new item for the .strm, nobody's data on it.
        FakeJellyfin.items = [{"Id": "new", "Type": "Movie", "Path": JF_STRM, "ProviderIds": {"Tmdb": "949"},
                               "MediaSources": [{"Id": "new", "Path": JF_STRM}]}]
        dup_service.apply_pending_user_data(self.fresh())
        self.assertEqual({"Played": True, "PlayCount": 2}, FakeJellyfin.data.get(("A", "new")),
                         "the users' saved watched state never reached the kept copy")
        self.assertEqual({"PlaybackPositionTicks": 7_000_000_000}, FakeJellyfin.data.get(("B", "new")))

    def test_webhook_during_the_resolution(self):
        self.assertEqual({"success": True}, self.resolve(self.dup_id))
        self.assertFalse(self.mkv.exists())
        self.assertTrue(self.strm.exists())
        self.assert_recorded()
        self.assertEqual([([JF_MKV], "Deleted")], MergedJellyfin.notified)
        self.assert_state_reaches_the_new_item()

    def test_webhook_after_the_resolution(self):
        # Radarr's Connect posting late (or a retry of it): the resolution is
        # done, the saved state waits on the duplicate for Jellyfin's new item.
        with mock.patch.object(self, "radarr_webhook", lambda payload: self.webhooks.append(payload)):
            self.assertEqual({"success": True}, self.resolve(self.dup_id))
        late = [p for p in self.webhooks if isinstance(p, dict)]
        self.assertEqual(["MovieFileDelete", "MovieDelete"], [p["eventType"] for p in late])
        for payload in late:
            self.radarr_webhook(payload)
        self.assert_state_reaches_the_new_item()


class SeriesKeepVod(_Base):
    """Keep VOD of a show whose row is Sonarr's (source "sonarr"): SeriesDelete
    removes that row and the show's duplicates while Keep VOD runs."""
    def setUp(self):
        super().setUp()
        self.show = self.root / "vod" / "shows" / "Show (2010)"
        (self.show / "Season 01").mkdir(parents=True)
        (self.show / "Season 01" / "Show S01E01.strm").write_text("http://p/series/1.mp4")
        dl = self.root / "tv" / "Show (2010) [sonarr]"
        (dl / "Season 01").mkdir(parents=True)
        self.mkv = dl / "Season 01" / "Show - S01E02.mkv"
        self.mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[1418] = {"id": 5, "path": str(dl), "files": [{"id": 50, "path": str(self.mkv)}]}
        self.db.add(Series(tmdb_id=1418, title="Show", year="2010", source="sonarr", sonarr_path=str(dl)))
        dup = Duplicate(tmdb_id=1418, media_type="series", resolution="pending",
                        sources=[{"source": "sonarr", "path": str(dl)},
                                 {"source": "provider_1", "path": str(self.show)}])
        self.db.add(dup)
        self.db.commit()
        self.dup_id = dup.id

    def test_keep_vod_is_recorded(self):
        self.assertEqual({"success": True}, self.resolve(self.dup_id))
        self.assertEqual([("SeriesDelete", {"status": "deleted", "tmdb_id": 1418})], self.webhooks)
        self.assertFalse(self.mkv.exists())
        self.assertTrue(list(self.show.rglob("*.strm")), "the VOD episodes stay")
        self.assert_recorded("series")


if __name__ == "__main__":
    unittest.main()

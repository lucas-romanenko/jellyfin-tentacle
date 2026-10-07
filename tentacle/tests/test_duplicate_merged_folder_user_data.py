"""Resolving a duplicate whose two copies share one folder keeps users' watched state (#333).

Jellyfin 10.11 shows a .strm and a download in the same folder as ONE film
with two versions: the item's MediaSources name both, the other version is an
owned item /Items leaves out, and users' data is on the one item whatever
version they played. Keep Downloaded found no item for the download and
refused ("scan the library") whenever anyone had watched the film, however
often the library was scanned. When the removed copy is the item's own
(main) version, deleting it makes Jellyfin create a new item for the kept
copy with nobody's data on it, so the data is saved first and merged onto the
new item once Jellyfin has made it.

Run from tentacle/:  python -m unittest tests.test_duplicate_merged_folder_user_data
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy.orm import sessionmaker

from models.database import Duplicate, Movie
from routers import duplicates
from services import duplicates as dup_service
from tests.test_duplicate_keeps_user_data import FakeJellyfin, _Base, _Resp
from tests.test_duplicate_keep_vod_merged_folder import _FakeArr


class MergedJellyfin(FakeJellyfin):
    """Owned items (another version of a film) are left out of listings and
    come back only by id, as Jellyfin 10.11 does."""
    notified = []

    def get(self, url, params=None, timeout=None):
        if not self.down and url.endswith("/Items") and "IncludeItemTypes" in params and "ParentId" not in params:
            rows = [i for i in self.items if i["Type"] == params["IncludeItemTypes"] and not i.get("OwnerId")]
            page = rows[params["StartIndex"]:params["StartIndex"] + params["Limit"]]
            return _Resp(200, {"Items": [dict(r) for r in page], "TotalRecordCount": len(rows)})
        return super().get(url, params, timeout)

    def notify_media_updated(self, paths, update_type="Created"):
        MergedJellyfin.notified.append((list(paths), update_type))
        return True


JF_DIR = "/data/movies/Heat (1995)"
JF_STRM, JF_MKV = f"{JF_DIR}/Heat (1995).strm", f"{JF_DIR}/Heat (1995) - Bluray-1080p.mkv"


class MergedFilm(_Base):
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
        self.dup = Duplicate(tmdb_id=949, media_type="movie", resolution="pending",
                             sources=[{"source": "radarr", "path": str(self.mkv)},
                                      {"source": "provider_1", "path": str(self.strm)}])
        self.db.add(self.dup)
        self.db.commit()
        MergedJellyfin.notified = []
        mock.patch("services.jellyfin.JellyfinService", MergedJellyfin).start()
        self.worker = mock.patch.object(dup_service, "start_pending_user_data_worker").start()

    def jellyfin_film(self, main, other):
        """One film: `main` is the item's own path, `other` an owned version."""
        FakeJellyfin.items = [
            {"Id": "film", "Type": "Movie", "Path": main, "ProviderIds": {"Tmdb": "949"},
             "MediaSources": [{"Id": "film", "Path": main}, {"Id": "version", "Path": other}]},
            {"Id": "version", "Type": "Movie", "Path": other, "ProviderIds": {"Tmdb": "949"}, "OwnerId": "film",
             "MediaSources": [{"Id": "version", "Path": other}]},
        ]

    def rescan(self, kept_path, new_id="new"):
        """What Jellyfin does once the removed file is gone: a new item for the kept one."""
        FakeJellyfin.items = [{"Id": new_id, "Type": "Movie", "Path": kept_path, "ProviderIds": {"Tmdb": "949"},
                               "MediaSources": [{"Id": new_id, "Path": kept_path}]}]

    def test_keep_downloaded_saves_watched_state_and_moves_it_to_the_new_item(self):
        self.jellyfin_film(JF_STRM, JF_MKV)
        FakeJellyfin.data = {
            ("A", "film"): {"Played": True, "PlayCount": 2, "IsFavorite": True,
                            "LastPlayedDate": "2026-09-20T20:00:00.0000000Z"},
            ("B", "film"): {"PlaybackPositionTicks": 7_000_000_000},
        }
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())
        self.assertTrue(self.mkv.exists())
        self.assertEqual("radarr", self.db.query(Movie).one().source)
        self.assertEqual([([JF_STRM], "Deleted")], MergedJellyfin.notified)
        self.worker.assert_called_once()

        self.assertEqual(1, dup_service.apply_pending_user_data(self.db), "the new item isn't there yet")
        self.assertEqual([], FakeJellyfin.posts)

        self.rescan(JF_MKV)
        self.assertEqual(0, dup_service.apply_pending_user_data(self.db))
        a, b = FakeJellyfin.data[("A", "new")], FakeJellyfin.data[("B", "new")]
        self.assertEqual({"Played": True, "PlayCount": 2, "IsFavorite": True,
                          "LastPlayedDate": "2026-09-20T20:00:00.0000000Z"}, a)
        self.assertEqual({"PlaybackPositionTicks": 7_000_000_000}, b)
        self.db.refresh(self.dup)
        self.assertIsNone(self.dup.pending_user_data)
        self.assertEqual(0, dup_service.apply_pending_user_data(self.db))
        self.assertEqual(2, len(FakeJellyfin.posts), "applied once")

    def test_keep_vod_with_the_download_as_main_version(self):
        self.jellyfin_film(JF_MKV, JF_STRM)
        FakeJellyfin.data = {("A", "film"): {"Played": True, "PlayCount": 1}}
        duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.assertFalse(self.mkv.exists())
        self.assertTrue(self.strm.exists())
        self.rescan(JF_STRM)
        self.assertEqual(0, dup_service.apply_pending_user_data(self.db))
        self.assertEqual({"Played": True, "PlayCount": 1}, FakeJellyfin.data[("A", "new")])

    def test_kept_copy_is_the_main_version_nothing_to_carry(self):
        # Keep Downloaded with the download as the item's own version: the item
        # and its data stay, only the .strm version goes.
        self.jellyfin_film(JF_MKV, JF_STRM)
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())
        self.assertIsNone(self.dup.pending_user_data)
        self.worker.assert_not_called()
        self.assertEqual([], FakeJellyfin.posts)

    def test_nobody_watched_it_saves_nothing(self):
        self.jellyfin_film(JF_STRM, JF_MKV)
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())
        self.assertIsNone(self.dup.pending_user_data)
        self.worker.assert_not_called()

    def test_merge_onto_the_new_item_takes_nothing_away(self):
        self.jellyfin_film(JF_STRM, JF_MKV)
        FakeJellyfin.data = {("A", "film"): {"PlaybackPositionTicks": 5, "PlayCount": 1}}
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.rescan(JF_MKV)
        # Watched on the new item in the meantime.
        FakeJellyfin.data[("A", "new")] = {"Played": True, "PlayCount": 3}
        self.assertEqual(0, dup_service.apply_pending_user_data(self.db))
        self.assertEqual([], FakeJellyfin.posts)

    def test_a_download_jellyfin_has_not_scanned_still_refuses(self):
        # Only the .strm in Jellyfin, no version for the download: #297's refusal stands.
        FakeJellyfin.items = [{"Id": "film", "Type": "Movie", "Path": JF_STRM, "ProviderIds": {"Tmdb": "949"},
                               "MediaSources": [{"Id": "film", "Path": JF_STRM}]}]
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        with self.assertRaises(duplicates.HTTPException) as cm:
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertEqual(409, cm.exception.status_code)
        self.assertTrue(self.strm.exists())

    def test_a_failed_resolution_keeps_nothing_saved(self):
        self.jellyfin_film(JF_MKV, JF_STRM)
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        _FakeArr.fail_file_delete = True
        with self.assertRaises(duplicates.HTTPException):
            duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.db.refresh(self.dup)
        self.assertIsNone(self.dup.pending_user_data)
        self.worker.assert_not_called()

    # The saved state lives nowhere else once the copy is deleted, so it is
    # committed before anything is deleted: a failure after the delete must
    # not take every user's watched state with it (#506).
    STATE = {("A", "film"): {"Played": True, "PlayCount": 2, "IsFavorite": True},
             ("B", "film"): {"PlaybackPositionTicks": 7_000_000_000}}

    def restart(self):
        """A new session on the same database: only what was committed is there."""
        bind = self.db.get_bind()
        self.db.close()
        self.db = sessionmaker(bind=bind)()
        self.addCleanup(self.db.close)
        self.dup = self.db.query(Duplicate).one()

    def assert_state_reaches(self, kept_path):
        self.rescan(kept_path)
        dup_service.apply_pending_user_data(self.db)
        self.assertEqual({"Played": True, "PlayCount": 2, "IsFavorite": True}, FakeJellyfin.data.get(("A", "new")))
        self.assertEqual({"PlaybackPositionTicks": 7_000_000_000}, FakeJellyfin.data.get(("B", "new")))

    def test_saved_state_survives_a_failed_deletion_log_commit(self):
        # log_deletion never raises: when its own commit fails (database
        # locked, disk full) it rolls the whole session back.
        self.jellyfin_film(JF_STRM, JF_MKV)
        FakeJellyfin.data = {k: dict(v) for k, v in self.STATE.items()}
        with mock.patch.object(duplicates, "log_deletion", lambda db, **k: db.rollback()):
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())
        self.assert_state_reaches(JF_MKV)

    def test_saved_state_survives_a_crash_after_the_delete(self):
        self.jellyfin_film(JF_STRM, JF_MKV)
        FakeJellyfin.data = {k: dict(v) for k, v in self.STATE.items()}

        class Crash(BaseException):
            pass

        def crash(*a, **k):
            raise Crash()
        with mock.patch.object(duplicates, "convert_record_to_downloaded", crash), self.assertRaises(Crash):
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assertFalse(self.strm.exists())
        self.restart()
        self.assert_state_reaches(JF_MKV)

    def test_saved_state_survives_radarr_refusing_the_title_after_the_file_delete(self):
        self.jellyfin_film(JF_MKV, JF_STRM)
        FakeJellyfin.data = {k: dict(v) for k, v in self.STATE.items()}
        with mock.patch.object(_FakeArr, "_delete_title", lambda self_, arr_id, delete_files: False), \
                self.assertRaises(duplicates.HTTPException) as cm:
            duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.assertEqual(502, cm.exception.status_code)
        self.assertFalse(self.mkv.exists())
        self.assert_state_reaches(JF_STRM)

    def test_a_retry_after_a_failed_resolution_saves_the_state_once(self):
        self.jellyfin_film(JF_MKV, JF_STRM)
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        _FakeArr.fail_file_delete = True
        with self.assertRaises(duplicates.HTTPException):
            duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        _FakeArr.fail_file_delete = False
        duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.db.refresh(self.dup)
        self.assertEqual(1, len(self.dup.pending_user_data))

    def test_saved_state_waits_a_month_then_goes(self):
        self.jellyfin_film(JF_STRM, JF_MKV)
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        FakeJellyfin.items = []
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.dup.pending_user_data = [dict(self.dup.pending_user_data[0], saved_at=old)]
        self.db.commit()
        self.assertEqual(0, dup_service.apply_pending_user_data(self.db))
        self.db.refresh(self.dup)
        self.assertIsNone(self.dup.pending_user_data)

    def test_jellyfin_down_keeps_it_for_later(self):
        self.jellyfin_film(JF_STRM, JF_MKV)
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        FakeJellyfin.down = True
        self.assertEqual(1, dup_service.apply_pending_user_data(self.db))
        FakeJellyfin.down = False
        self.rescan(JF_MKV)
        self.assertEqual(0, dup_service.apply_pending_user_data(self.db))
        self.assertEqual({"Played": True}, FakeJellyfin.data[("A", "new")])


class Worker(unittest.TestCase):
    """One poller at a time; a start while it runs keeps it going."""
    def setUp(self):
        self.addCleanup(dup_service._worker.update, running=False, kicked=False)
        dup_service._worker.update(running=False, kicked=False)

    def test_runs_until_nothing_waits(self):
        answers = iter([2, 1, 0])
        with mock.patch.object(dup_service, "_PENDING_POLL_SECONDS", 0), \
                mock.patch.object(dup_service, "apply_pending_user_data", side_effect=lambda db: next(answers)), \
                mock.patch("models.database.SessionLocal"):
            dup_service._worker.update(running=True, kicked=True)
            dup_service._pending_worker()
        self.assertFalse(dup_service._worker["running"])
        self.assertEqual([], list(answers))

    def test_gives_up_after_its_polls(self):
        calls = []
        with mock.patch.object(dup_service, "_PENDING_POLL_SECONDS", 0), \
                mock.patch.object(dup_service, "apply_pending_user_data", side_effect=lambda db: calls.append(1) or 1), \
                mock.patch("models.database.SessionLocal"):
            dup_service._worker.update(running=True, kicked=True)
            dup_service._pending_worker()
        self.assertEqual(dup_service._PENDING_POLLS, len(calls))
        self.assertFalse(dup_service._worker["running"])

    def test_a_second_start_while_running_starts_no_second_thread(self):
        with mock.patch.object(dup_service.threading, "Thread") as thread:
            dup_service.start_pending_user_data_worker()
            dup_service.start_pending_user_data_worker()
        self.assertEqual(1, thread.call_count)
        self.assertTrue(dup_service._worker["kicked"])


if __name__ == "__main__":
    unittest.main()

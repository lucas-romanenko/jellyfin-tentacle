"""Duplicates (#333 follow-up) under faults: a same-folder film's users' watched state is
SAVED on the duplicate before the main version is deleted, and merged onto the
kept copy's new Jellyfin item later. Invariant: every user's played state,
play count, resume point and favourite survive a resolution that deleted a
copy (or nothing is deleted).

Run from tentacle/:  tests/hermetic.py discover -s tests -p "test_vf_dup_merged_user_data_faults.py"
"""
import random
import unittest
from unittest import mock

from sqlalchemy.orm import sessionmaker

from models.database import Duplicate
from routers import duplicates
from services import duplicates as dup_service
from tests.test_duplicate_keeps_user_data import FakeJellyfin, _Resp
import tests.test_duplicate_merged_folder_user_data as _m
from tests.test_duplicate_merged_folder_user_data import MergedJellyfin, JF_STRM, JF_MKV
from tests.test_duplicate_keep_vod_merged_folder import _FakeArr


class _Crash(BaseException):
    """The process dies (OOM, container restart): not an error the route handles."""


class _P(_m.MergedFilm):
    def restart(self):
        """Only what was committed survives."""
        bind = self.db.get_bind()
        self.db.close()
        self.db = sessionmaker(bind=bind)()
        self.addCleanup(self.db.close)
        self.dup = self.db.query(Duplicate).one()


for _name in [n for n in dir(_m.MergedFilm) if n.startswith("test")]:
    setattr(_P, _name, None)   # don't re-run the runner's own tests here


class SavedStateSurvivesFailures(_P):
    STATE = {("A", "film"): {"Played": True, "PlayCount": 2, "IsFavorite": True},
             ("B", "film"): {"PlaybackPositionTicks": 7_000_000_000}}

    def assert_state_reached_the_new_item(self):
        self.assertFalse(self.strm.exists(), "precondition: the .strm (main version) was deleted")
        self.rescan(JF_MKV)
        dup_service.apply_pending_user_data(self.db)
        self.assertEqual({"Played": True, "PlayCount": 2, "IsFavorite": True},
                         FakeJellyfin.data.get(("A", "new")),
                         "user A's watched state is lost: saved only in the session, never committed")
        self.assertEqual({"PlaybackPositionTicks": 7_000_000_000}, FakeJellyfin.data.get(("B", "new")))

    def test_deletion_log_commit_fails_after_the_strm_was_deleted(self):
        # models.log_deletion "never raises": when its commit fails (database
        # locked past busy_timeout, disk full) it rolls the session back --
        # which also drops dup.pending_user_data set by carry_user_data.
        self.jellyfin_film(JF_STRM, JF_MKV)
        FakeJellyfin.data = {k: dict(v) for k, v in self.STATE.items()}
        with mock.patch.object(duplicates, "log_deletion", lambda db, **k: db.rollback()):
            duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assert_state_reached_the_new_item()

    def test_crash_between_the_strm_delete_and_the_commit(self):
        self.jellyfin_film(JF_STRM, JF_MKV)
        FakeJellyfin.data = {k: dict(v) for k, v in self.STATE.items()}

        def crash(*a, **k):
            raise _Crash()
        with mock.patch.object(duplicates, "convert_record_to_downloaded", crash):
            with self.assertRaises(_Crash):
                duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.restart()
        self.assert_state_reached_the_new_item()

    def test_radarr_refuses_the_title_after_deleting_the_download(self):
        # Keep VOD, the download is the film's main version: Radarr deletes the
        # file, then refuses to remove the title -> 502 and a rollback. The
        # download (and with it the item users' state was on) is gone anyway.
        self.jellyfin_film(JF_MKV, JF_STRM)
        FakeJellyfin.data = {k: dict(v) for k, v in self.STATE.items()}
        with mock.patch.object(_FakeArr, "_delete_title", lambda self_, arr_id, delete_files: False):
            with self.assertRaises(duplicates.HTTPException) as cm:
                duplicates._apply_resolution(self.dup, "keep_vod", self.db)
        self.assertEqual(502, cm.exception.status_code)
        self.assertFalse(self.mkv.exists(), "precondition: Radarr deleted the download before it failed")
        self.rescan(JF_STRM)
        dup_service.apply_pending_user_data(self.db)
        self.assertEqual({"Played": True, "PlayCount": 2, "IsFavorite": True}, FakeJellyfin.data.get(("A", "new")))
        self.assertEqual({"PlaybackPositionTicks": 7_000_000_000}, FakeJellyfin.data.get(("B", "new")))

    def test_control_no_fault(self):
        self.jellyfin_film(JF_STRM, JF_MKV)
        FakeJellyfin.data = {k: dict(v) for k, v in self.STATE.items()}
        duplicates._apply_resolution(self.dup, "keep_radarr", self.db)
        self.assert_state_reached_the_new_item()


class MultiUserProperty(_P):
    """Random users/data, random main version, random Jellyfin GET failures
    during the carry. Invariant after resolve (+ rescan + apply): either nothing
    was deleted, or every user's data is on the item that plays the kept copy."""
    SEEDS = 600

    def test_random(self):
        fails = []
        real_get = MergedJellyfin.get
        for seed in range(self.SEEDS):
            rnd = random.Random(seed)
            self.tearDown_one()
            users = [f"U{i}" for i in range(rnd.randint(1, 6))]
            FakeJellyfin.users = users
            keep = rnd.choice(["keep_radarr", "keep_vod"])
            main_is_strm = rnd.random() < 0.5
            main, other = (JF_STRM, JF_MKV) if main_is_strm else (JF_MKV, JF_STRM)
            self.jellyfin_film(main, other)
            data = {}
            for u in users:
                d = {}
                if rnd.random() < 0.5: d["Played"] = True
                if rnd.random() < 0.4: d["PlayCount"] = rnd.randint(1, 5)
                if rnd.random() < 0.3: d["IsFavorite"] = True
                if rnd.random() < 0.4 and not d.get("Played"): d["PlaybackPositionTicks"] = rnd.randint(1, 10**10)
                if d: data[(u, "film")] = d
            FakeJellyfin.data = {k: dict(v) for k, v in data.items()}
            fail_at = rnd.randint(1, 12) if rnd.random() < 0.3 else None
            calls = [0]

            def get(self_, url, params=None, timeout=None):
                calls[0] += 1
                if fail_at is not None and calls[0] == fail_at:
                    raise ConnectionError("injected")
                return real_get(self_, url, params, timeout)
            with mock.patch.object(MergedJellyfin, "get", get):
                try:
                    duplicates._apply_resolution(self.dup, keep, self.db)
                except duplicates.HTTPException:
                    self.db.rollback()
            removed_main = (keep == "keep_radarr") == main_is_strm
            strm_gone, mkv_gone = not self.strm.exists(), not self.mkv.exists()
            if not (strm_gone or mkv_gone):
                continue   # nothing deleted: fine
            if removed_main:
                self.rescan(other)
                dup_service.apply_pending_user_data(self.db)
                final = "new"
            else:
                final = "film"
            for (u, _), d in data.items():
                now = FakeJellyfin.data.get((u, final), {})
                for k, v in d.items():
                    ok = now.get(k) and (now[k] >= v if k == "PlayCount" else True)
                    if k == "PlaybackPositionTicks" and now.get("Played"):
                        ok = True
                    if not ok:
                        fails.append(f"seed {seed}: user {u} lost {k}={v} ({keep}, main={'strm' if main_is_strm else 'mkv'}, fail_at={fail_at})")
        self.assertEqual([], fails[:5], f"{len(fails)} violations")

    def tearDown_one(self):
        # fresh files, fresh DB rows for each seed
        from models.database import Movie
        self.db.rollback()
        self.db.query(Duplicate).delete()
        self.db.query(Movie).delete()
        self.db.commit()
        folder = self.root / "movies" / "Heat (1995)"
        folder.mkdir(parents=True, exist_ok=True)
        self.strm.write_text("http://p/movie/1.mp4")
        self.mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[949] = {"id": 4, "path": str(folder), "files": [{"id": 40, "path": str(self.mkv)}]}
        _FakeArr.fail_file_delete = False
        self.db.add(Movie(tmdb_id=949, title="Heat", year="1995", source="provider_1",
                          strm_path=str(self.strm), radarr_path=str(self.mkv)))
        self.dup = Duplicate(tmdb_id=949, media_type="movie", resolution="pending",
                             sources=[{"source": "radarr", "path": str(self.mkv)},
                                      {"source": "provider_1", "path": str(self.strm)}])
        self.db.add(self.dup)
        self.db.commit()
        FakeJellyfin.posts = []


if __name__ == "__main__":
    unittest.main()

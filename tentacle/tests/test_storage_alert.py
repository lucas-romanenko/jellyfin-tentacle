"""Recordings or a library on Jellyfin's system disk raise an admin alert.

Lucas, 2026-11-09: Jellyfin recorded Live TV into its default folder
(<data>/livetv/recordings, next to its config and database), not onto the
media disk, and the system disk lost about 7 GB a day until an update failed
on "no space left on device". "This should never happen. Should absolutely be
on the media drives." services/admin_alerts._seen_storage reads Jellyfin's
/System/Info/Storage (10.11): a folder with the same size and about the same
free space as Jellyfin's own data folder is on that disk. Admins only, no line
for the family; one disk for everything says nothing.

Run from the tentacle/ directory:  python -m unittest discover -s tests -p test_storage_alert.py
"""
import unittest
from unittest import mock

import requests

GB = 10 ** 9
SYSTEM = {"FreeSpace": 52 * GB, "UsedSpace": 264 * GB}
MEDIA = {"FreeSpace": 24_730 * GB, "UsedSpace": 3_268 * GB}


def folder(path, disk, free_drift=0):
    return {"Path": path, "FreeSpace": disk["FreeSpace"] + free_drift,
            "UsedSpace": disk["UsedSpace"] - free_drift, "StorageType": "Fixed", "DeviceId": path}


def storage(*libraries):
    return {"ProgramDataFolder": folder("/config", SYSTEM),
            "Libraries": [{"Id": lid, "Name": name, "Folders": list(folders)} for lid, name, folders in libraries]}


class FakeJellyfin:
    def __init__(self, st, livetv=None, missing=False):
        self.st, self.livetv, self.missing = st, livetv or {}, missing

    def get(self, path, **params):
        if path == "/System/Info/Storage":
            if self.missing:
                raise requests.HTTPError("404", response=mock.Mock(status_code=404))
            return self.st
        if path == "/System/Configuration/livetv":
            return self.livetv
        raise AssertionError(path)


MOVIES = ("m1", "Movies", [folder("/data/media/movies", MEDIA)])
DEFAULT_REC = {"RecordingPath": "", "MediaLocationsCreated": ["/config/data/livetv/recordings"]}


class Seen(unittest.TestCase):
    def seen(self, jf):
        from services import admin_alerts as A
        from services import livetv_tuners as T
        with mock.patch.object(T, "jellyfin_for", lambda db: jf):
            return A._seen_storage(None)

    def test_recordings_in_jellyfins_default_folder(self):
        jf = FakeJellyfin(storage(MOVIES, ("r1", "Recordings", [folder("/config/data/livetv/recordings", SYSTEM, 3 * 10 ** 6)])),
                          DEFAULT_REC)
        out = self.seen(jf)
        self.assertEqual(["storage:recordings"], list(out))
        area, title, message, push = out["storage:recordings"]
        self.assertEqual("storage", area)
        self.assertIn("recordings", title.lower())
        self.assertIn("/config/data/livetv/recordings", message)
        self.assertIn("52 GB free", message)
        self.assertTrue(push)

    def test_recordings_on_the_media_disk(self):
        jf = FakeJellyfin(storage(MOVIES, ("r1", "Recordings", [folder("/data/media/recordings", MEDIA)])),
                          {"RecordingPath": "/data/media/recordings", "MediaLocationsCreated": ["/data/media/recordings"]})
        self.assertEqual({}, self.seen(jf))

    def test_a_library_on_the_system_disk(self):
        jf = FakeJellyfin(storage(MOVIES, ("s1", "Shows", [folder("/config/shows", SYSTEM)])), DEFAULT_REC)
        out = self.seen(jf)
        self.assertEqual(["storage:library:s1:/config/shows"], list(out))
        self.assertIn("Shows", out["storage:library:s1:/config/shows"][1])

    def test_one_disk_for_everything_says_nothing(self):
        jf = FakeJellyfin(storage(("m1", "Movies", [folder("/media/movies", SYSTEM)]),
                                  ("r1", "Recordings", [folder("/config/data/livetv/recordings", SYSTEM)])), DEFAULT_REC)
        self.assertEqual({}, self.seen(jf))

    def test_two_drives_of_one_size_are_not_one_disk(self):
        twin = {"FreeSpace": 200 * GB, "UsedSpace": 116 * GB}      # same 316 GB, other free space
        jf = FakeJellyfin(storage(MOVIES, ("r1", "Recordings", [folder("/recordings", twin)])),
                          {"RecordingPath": "/recordings"})
        self.assertEqual({}, self.seen(jf))

    def test_an_unreadable_folder_is_not_another_disk(self):
        # Jellyfin gives -1s for a folder it can't read (gone, no permission, share not mounted)
        gone = {"Path": "/media/old", "FreeSpace": -1, "UsedSpace": -1, "DeviceId": None}
        jf = FakeJellyfin(storage(("m1", "Movies", [folder("/media/movies", SYSTEM), gone]),
                                  ("r1", "Recordings", [folder("/config/data/livetv/recordings", SYSTEM)])), DEFAULT_REC)
        self.assertEqual({}, self.seen(jf))

    def test_the_recordings_folder_in_another_case(self):
        jf = FakeJellyfin(storage(MOVIES, ("r1", "Recordings", [folder("C:\\ProgramData\\Jellyfin\\Recordings", SYSTEM)])),
                          {"RecordingPath": "c:\\programdata\\jellyfin\\recordings\\"})
        self.assertEqual(["storage:recordings"], list(self.seen(jf)))

    def test_jellyfin_before_10_11_says_nothing(self):
        self.assertEqual({}, self.seen(FakeJellyfin({}, missing=True)))

    def test_no_jellyfin_set_up(self):
        self.assertEqual({}, self.seen(None))


class Alert(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from tmp_dirs import temp_dir
        engine = create_engine(f"sqlite:///{Path(temp_dir(self))}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        from services import admin_alerts as A
        from services import livetv_tuners as T
        self.A, self.T = A, T
        p = mock.patch.object(A, "CHECKS", (A._seen_storage,))
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(A, "_tell_admins")
        self.told = p.start(); self.addCleanup(p.stop)

    def check(self, jf):
        with mock.patch.object(self.T, "jellyfin_for", lambda db: jf):
            self.A.check(self.db)
        return [r.key for r in self.A.active(self.db)]

    def test_told_once_then_fixed(self):
        wrong = FakeJellyfin(storage(MOVIES, ("r1", "Recordings", [folder("/config/data/livetv/recordings", SYSTEM)])),
                             DEFAULT_REC)
        self.assertEqual(["storage:recordings"], self.check(wrong))
        self.assertEqual(["storage:recordings"], self.check(wrong))
        self.assertEqual(1, self.told.call_count)
        right = FakeJellyfin(storage(MOVIES, ("r1", "Recordings", [folder("/data/media/recordings", MEDIA)])),
                             {"RecordingPath": "/data/media/recordings"})
        self.assertEqual([], self.check(right))
        self.assertEqual(2, self.told.call_count)
        self.assertIn("media disk", self.told.call_args[0][2])

    def test_jellyfin_not_answering_keeps_the_alert(self):
        wrong = FakeJellyfin(storage(MOVIES, ("r1", "Recordings", [folder("/config/data/livetv/recordings", SYSTEM)])),
                             DEFAULT_REC)
        self.check(wrong)

        class Down:
            def get(self, path, **params):
                raise requests.ConnectionError("refused")
        self.assertEqual(["storage:recordings"], self.check(Down()))

    def test_the_family_sees_nothing(self):
        wrong = FakeJellyfin(storage(MOVIES, ("r1", "Recordings", [folder("/config/data/livetv/recordings", SYSTEM)])),
                             DEFAULT_REC)
        self.check(wrong)
        from services import provider_status
        with mock.patch.object(provider_status, "messages", lambda db: []):
            self.assertEqual([], self.A.family_messages(self.db))


if __name__ == "__main__":
    unittest.main()

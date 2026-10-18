"""A request for an album that already has files never re-pins it (#422).

An album Lidarr has with files but unmonitored shows as not in the library, so
anyone can request it. The request's job used to pin the original and search
straight away, so Lidarr re-matched the files (extra tracks unlinked) without
Fix library's Apply. Now the album is monitored and checked: Fix library offers
the pin, and it is searched only if it is already pinned right and locked.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from test_music_core import RG, FakeLidarr, _Base, lidarr_album, lidarr_release, mb_release  # noqa: E402


class TestRequestAlbumWithFiles(_Base):
    def setUp(self):
        super().setUp()
        self.mb[RG] = [mb_release("m1", "1974-01-01", 12)]

    def lidarr_has(self, pinned: str, have: int, locked: bool = False):
        releases = [lidarr_release("r13", 13, monitored=pinned == "r13"),
                    lidarr_release("r12", 12, monitored=pinned == "r12")]
        album = lidarr_album(7, RG, releases, monitored=False, have=have)
        album["anyReleaseOk"] = not locked
        FakeLidarr.state["albums"] = {7: album}

    def request(self):
        from models.database import MusicAlbum
        from services.media_requests import request_album
        out = request_album(self.db, RG, user_id=1, via="test")
        self.assertEqual(out["status"], "requested")
        self.run_jobs()
        self.db.expire_all()
        return self.db.query(MusicAlbum).filter_by(mbid=RG).one()

    def searched(self):
        return [c[2] for c in self.calls("POST", "/api/v1/command") if c[2].get("name") == "AlbumSearch"]

    def test_files_on_another_edition_are_left_to_fix_library(self):
        self.lidarr_has("r13", have=13)
        row = self.request()
        self.assertTrue(FakeLidarr.state["albums"][7]["monitored"])
        self.assertEqual(self.calls("PUT", "/api/v1/album/7"), [])   # not re-pinned
        self.assertEqual(self.searched(), [])
        self.assertEqual(row.category, "repin_trim")                  # offered in Fix library
        self.assertFalse(row.request_pending)

    def test_incomplete_files_on_another_edition_are_not_searched(self):
        self.lidarr_has("r13", have=5)
        row = self.request()
        self.assertEqual(self.calls("PUT", "/api/v1/album/7"), [])
        self.assertEqual(self.searched(), [])
        self.assertEqual(row.category, "repin_download")

    def test_a_right_locked_incomplete_pin_is_searched(self):
        self.lidarr_has("r12", have=5, locked=True)
        row = self.request()
        self.assertEqual(self.calls("PUT", "/api/v1/album/7"), [])
        self.assertEqual(self.searched(), [{"name": "AlbumSearch", "albumIds": [7]}])
        self.assertEqual(row.category, "right")
        self.assertFalse(row.request_pending)

    def test_a_right_pin_any_release_ok_is_not_searched(self):
        # A search could import another edition: locking it waits for Fix library.
        self.lidarr_has("r12", have=5, locked=False)
        self.request()
        self.assertEqual(self.calls("PUT", "/api/v1/album/7"), [])
        self.assertEqual(self.searched(), [])

    def test_a_complete_right_pin_is_not_searched(self):
        self.lidarr_has("r12", have=12, locked=True)
        self.request()
        self.assertEqual(self.searched(), [])


if __name__ == "__main__":
    unittest.main()

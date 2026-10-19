"""#239, the YouTube side: what Tentacle writes for a YouTube video has the
library's owner, like every VOD file and NFO.

The docs promise that with PUID/PGID set Tentacle hands everything it creates
to that user, and that without them a created path takes the owner of the
folder it is made in. The YouTube writer did neither: every channel folder,
video folder, .strm, NFO and image was root-owned, also under a library folder
owned by the media user and with PUID set. A media user (Jellyfin or a file
share running as that user) then cannot delete, rename or write beside them.

os.geteuid / os.chown are faked as in test_chown_default_owner: the suite does
not run as root.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_files_owner.py
"""
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import services.sync as sync
from services.youtube import library
from test_chown_default_owner import _Fs

JPEG = b"\xff\xd8\xff" + b"0" * 2048


def _video():
    return SimpleNamespace(video_id="abcdefghijk", title="A video", published_at=datetime(2026, 9, 20),
                           first_seen=None, description="", duration=600, thumbnail_url=None,
                           folder_path=None, strm_path=None)


CHANNEL = SimpleNamespace(title="Chan", slug="chan", extra_tags=[], rating=None)


class _YtFs(_Fs):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "youtube"
        self._media_owned(self.root)
        p = mock.patch.object(library, "_download", return_value=JPEG)
        p.start()
        self.addCleanup(p.stop)
        library._artwork_retry_at.clear()

    def _write(self):
        video = _video()
        library.write_video(video, CHANNEL, "http://192.0.2.20:8888", root=self.root)
        folder = self.root / "Chan" / "2026-09-20 A video [abcdefghijk]"
        return video, folder

    def _created(self, folder):
        return {str(self.root / "Chan"), str(folder)} | {str(p) for p in folder.iterdir()}


class WithPuid(_YtFs):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(sync, "VOD_PUID", "1000"), mock.patch.object(sync, "VOD_PGID", "1000")):
            p.start()
            self.addCleanup(p.stop)

    def test_every_folder_and_file_written_goes_to_puid(self):
        _, folder = self._write()
        self.assertEqual(self._created(folder), {p for p, _, _ in self.chowned})
        self.assertEqual({(1000, 1000)}, {(u, g) for _, u, g in self.chowned})
        self.assertEqual(7, len(self._created(folder)))    # 2 folders, .strm, NFO, 3 images
        self.assertEqual(sorted(["poster.jpg", "fanart.jpg", "landscape.jpg", "movie.nfo",
                                 "2026-09-20 A video [abcdefghijk].strm"]), sorted(p.name for p in folder.iterdir()))

    def test_a_second_write_changes_nothing(self):
        self._write()
        self.chowned.clear()
        self._write()
        self.assertEqual([], self.chowned)


class WithoutPuid(_YtFs):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(sync, "VOD_PUID", None), mock.patch.object(sync, "VOD_PGID", None)):
            p.start()
            self.addCleanup(p.stop)

    def test_everything_takes_the_library_folders_owner(self):
        _, folder = self._write()
        self.assertEqual(self._created(folder), {p for p, _, _ in self.chowned})
        self.assertEqual({(1000, 1000)}, {(u, g) for _, u, g in self.chowned})

    def test_under_a_root_owned_library_nothing_changes(self):
        self.owner[os.path.abspath(str(self.root))] = (0, 0)
        self._write()
        self.assertEqual([], self.chowned)


if __name__ == "__main__":
    unittest.main()

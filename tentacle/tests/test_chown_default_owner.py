"""#239: what Tentacle creates in a shared library is writable by Sonarr/Radarr.

Without PUID/PGID, Tentacle (root in its container) left the season folders,
.strm and NFOs it created root-owned, and Sonarr (the media user) then failed
every import into such a folder with "permission denied". Now a path created
as root takes the owner of the folder it was created in (the library roots
belong to the media user). With PUID/PGID set, the NFOs the Radarr/Sonarr
scans create were still root-owned: they are chowned like everything else.

os.geteuid / os.chown are faked: the test suite does not run as root.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import services.sync as sync
from services.nfo import refresh_arr_nfo, write_movie_nfo


class _Fs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.owner = {}          # path -> (uid, gid) as the fake filesystem sees it
        self.chowned = []
        real_stat, real_lstat = os.stat, os.lstat

        def fake_owner(real):
            def f(p, *a, **k):
                st = real(p, *a, **k)
                uid, gid = self.owner.get(os.path.abspath(str(p)), (0, 0))
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, uid, gid,
                                       st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
            return f

        def fake_chown(p, uid, gid, *a, **k):
            self.chowned.append((str(p), uid, gid))
            self.owner[os.path.abspath(str(p))] = (uid, gid)

        for p in (mock.patch.object(sync.os, "geteuid", lambda: 0, create=True),
                  mock.patch.object(sync.os, "stat", fake_owner(real_stat)),
                  mock.patch.object(sync.os, "lstat", fake_owner(real_lstat)),
                  mock.patch.object(sync.os, "chown", fake_chown)):
            p.start()
            self.addCleanup(p.stop)

    def _media_owned(self, path):
        path.mkdir(parents=True, exist_ok=True)
        self.owner[os.path.abspath(str(path))] = (1000, 1000)


class WithoutPuid(_Fs):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(sync, "VOD_PUID", None), mock.patch.object(sync, "VOD_PGID", None)):
            p.start()
            self.addCleanup(p.stop)

    def test_new_season_folder_takes_the_show_folders_owner(self):
        show = self.tmp / "tv" / "Breaking Bad (2008)"
        self._media_owned(show)
        season = show / "Season 02"
        season.mkdir()
        sync.chown_path(season)
        self.assertEqual([(str(season), 1000, 1000)], self.chowned)

    def test_under_a_root_owned_folder_nothing_changes(self):
        root_dir = self.tmp / "vod"
        root_dir.mkdir()
        sync.chown_path(root_dir / "x")  # missing path: best effort, no crash
        d = root_dir / "Show"
        d.mkdir()
        sync.chown_path(d)
        self.assertEqual([], self.chowned)

    def test_not_running_as_root_nothing_changes(self):
        show = self.tmp / "tv" / "Show"
        self._media_owned(show)
        (show / "Season 01").mkdir()
        with mock.patch.object(sync.os, "geteuid", lambda: 1000, create=True):
            sync.chown_path(show / "Season 01")
        self.assertEqual([], self.chowned)

    def test_a_new_nfo_from_an_arr_scan_takes_the_folders_owner(self):
        folder = self.tmp / "movies" / "Only Radarr (1991)"
        self._media_owned(folder)
        nfo = folder / "Only Radarr (1991).nfo"
        self.assertTrue(refresh_arr_nfo(nfo, write_movie_nfo, {"title": "Only Radarr", "tmdb_id": 1}, ["T"]))
        self.assertEqual([(str(nfo), 1000, 1000)], self.chowned)


class WithPuid(_Fs):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(sync, "VOD_PUID", "1000"), mock.patch.object(sync, "VOD_PGID", "1000")):
            p.start()
            self.addCleanup(p.stop)

    def test_a_new_nfo_from_an_arr_scan_is_chowned_to_puid(self):
        folder = self.tmp / "tv" / "Only Sonarr (1990)"
        folder.mkdir(parents=True)
        nfo = folder / "tvshow.nfo"
        self.assertTrue(refresh_arr_nfo(nfo, write_movie_nfo, {"title": "Only Sonarr", "tmdb_id": 2}, ["T"]))
        self.assertEqual([(str(nfo), 1000, 1000)], self.chowned)


if __name__ == "__main__":
    unittest.main()

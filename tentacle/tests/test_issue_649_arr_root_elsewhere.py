"""#649: Radarr/Sonarr scans found a download's folder only by replacing
"/data/movies" with "/media/movies" (Sonarr: "/data/shows" with "/media/shows").

With any other Radarr/Sonarr root folder (/data/media/movies, Sonarr's
/data/tv, ...) the mapped path doesn't exist inside Tentacle and the scan
skipped the download without a word: no NFO, no nfo_path on the row (so later
tag changes never reached the file either), "0 NFOs written" on every scan.

The scan now also finds the folder by NAME under Tentacle's mount (as the VOD
sync does: "names, not paths"), dates an NFO it creates with the download's
date (not "now", which put old downloads at the top of "Latest", #266), and
logs one warning per scan for downloads it still can't find. A download in a
VOD folder (hybrid series, merged layout) is the VOD sync's: no warning.

The mount roots are module attributes of services.media_files, read when the
scan runs, so the tests point them at a temp dir.
"""
import os
import re
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr:7878"), ("radarr_api_key", "k"),
                     ("sonarr_url", "http://sonarr:8989"), ("sonarr_api_key", "k"),
                     ("data_dir", str(self.tmp))):
            mdb.set_setting(self.db, k, v)
        self.db.commit()
        import services.tmdb as tmdb
        p = mock.patch.object(tmdb.TMDBService, "_request", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        # Tentacle's mounts (/media/movies, /media/shows) in the temp dir.
        self.movies = self.tmp / "media" / "movies"
        self.shows = self.tmp / "media" / "shows"
        self.movies.mkdir(parents=True)
        self.shows.mkdir(parents=True)
        import services.media_files as media_files
        for name, root in (("RADARR_MEDIA_ROOT", self.movies), ("SONARR_MEDIA_ROOT", self.shows)):
            p = mock.patch.object(media_files, name, root, create=True)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _dates(path):
        return re.findall(r"<dateadded>([^<]*)", path.read_text())

    @staticmethod
    def _warnings(logs, needle):
        return [line for line in logs.output if line.startswith("WARNING") and needle in line]


class RadarrRootElsewhere(_Base):
    def _scan(self, movies):
        import services.radarr as radarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_movies(self):
                return [dict(m) for m in movies]

        with mock.patch.object(radarr, "RadarrService", Fake), \
             mock.patch.object(radarr, "emit_library_event", lambda *a, **k: None), \
             self.assertLogs("services.radarr", level="INFO") as logs:
            stats = radarr.scan_radarr_library(self.db)
        return stats, logs

    @staticmethod
    def _movie(tmdb_id, title, year, root="/data/media/movies", added="2020-01-01T10:20:30Z"):
        folder = f"{root}/{title} ({year})"
        return {"tmdbId": tmdb_id, "title": title, "year": year, "hasFile": True, "path": folder,
                "movieFile": {"path": f"{folder}/{title} ({year}).mkv", "dateAdded": added}}

    def test_download_is_found_by_folder_name_under_the_mount(self):
        folder = self.movies / "Heat (1995)"
        folder.mkdir()
        (folder / "Heat (1995).mkv").write_bytes(b"x")
        stats, _ = self._scan([self._movie(949, "Heat", 1995)])
        nfo = folder / "Heat (1995).nfo"
        self.assertTrue(nfo.exists(), "no NFO beside a download Radarr keeps at /data/media/movies")
        self.assertEqual(1, stats["nfo_written"])
        self.assertIn("<tag>Downloaded Movies</tag>", nfo.read_text())
        row = self.db.query(mdb.Movie).filter_by(tmdb_id=949).one()
        self.assertEqual(str(nfo), row.nfo_path)

    def test_new_nfo_carries_the_download_date_not_now(self):
        folder = self.movies / "Heat (1995)"
        folder.mkdir()
        (folder / "Heat (1995).mkv").write_bytes(b"x")
        self._scan([self._movie(949, "Heat", 1995)])
        nfo = folder / "Heat (1995).nfo"
        self.assertTrue(nfo.exists())
        self.assertEqual(["2020-01-01 10:20:30"], self._dates(nfo))

    def test_windows_radarr_path_is_found_by_name(self):
        folder = self.movies / "Heat (1995)"
        folder.mkdir()
        (folder / "Heat (1995).mkv").write_bytes(b"x")
        # On Linux Path("D:\\Movies\\Heat (1995)\\Heat (1995).mkv").parent is
        # ".": the scan wrote the NFO into its working directory.
        cwd = self.tmp / "cwd"
        cwd.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd)
        movie = {"tmdbId": 949, "title": "Heat", "year": 1995, "hasFile": True, "path": "D:\\Movies\\Heat (1995)",
                 "movieFile": {"path": "D:\\Movies\\Heat (1995)\\Heat (1995).mkv"}}
        self._scan([movie])
        self.assertEqual([], [p.name for p in cwd.iterdir()], "the scan wrote into its working directory")
        self.assertTrue((folder / "Heat (1995).nfo").exists())

    def test_downloads_it_cannot_find_are_reported_once_per_scan(self):
        stats, logs = self._scan([self._movie(949, "Heat", 1995), self._movie(8195, "Ronin", 1998)])
        self.assertEqual(0, stats["nfo_written"])
        said = self._warnings(logs, "/data/media/movies")
        self.assertEqual(1, len(said), "want one warning naming Radarr's path, got:\n" + "\n".join(logs.output))
        self.assertIn(str(self.movies), said[0])  # and where Tentacle looked

    def test_a_download_in_a_vod_folder_is_not_reported(self):
        _, logs = self._scan([self._movie(949, "Heat", 1995, root="/data/vod/Movies")])
        self.assertEqual([], self._warnings(logs, "/data/vod/Movies"))


class SonarrRootElsewhere(_Base):
    def _scan(self, shows):
        import services.sonarr as sonarr

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self, raise_errors=False):
                return [dict(s) for s in shows]

            def __getattr__(self, name):
                return lambda *a, **k: []

        with mock.patch.object(sonarr, "SonarrService", Fake), \
             mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None), \
             self.assertLogs("services.sonarr", level="INFO") as logs:
            stats = sonarr.scan_sonarr_library(self.db)
        return stats, logs

    @staticmethod
    def _show(tmdb_id, title, year, root="/data/tv", added="2019-05-05T08:09:10Z"):
        return {"tmdbId": tmdb_id, "tvdbId": 81189, "title": title, "year": year,
                "path": f"{root}/{title} ({year})", "added": added, "monitorNewItems": "none",
                "statistics": {"episodeFileCount": 3}}

    def test_download_is_found_by_folder_name_under_the_mount(self):
        folder = self.shows / "Breaking Bad (2008)"
        folder.mkdir()
        stats, _ = self._scan([self._show(1396, "Breaking Bad", 2008)])
        nfo = folder / "tvshow.nfo"
        self.assertTrue(nfo.exists(), "no tvshow.nfo for a show Sonarr keeps at /data/tv")
        self.assertEqual(1, stats["nfo_written"])
        self.assertEqual(["2019-05-05 08:09:10"], self._dates(nfo))
        row = self.db.query(mdb.Series).filter_by(tmdb_id=1396).one()
        self.assertEqual(str(nfo), row.nfo_path)

    def test_downloads_it_cannot_find_are_reported_once_per_scan(self):
        shows = [self._show(1396, "Breaking Bad", 2008), dict(self._show(1399, "Game of Thrones", 2011), tvdbId=121361)]
        stats, logs = self._scan(shows)
        self.assertEqual(0, stats["nfo_written"])
        said = self._warnings(logs, "/data/tv")
        self.assertEqual(1, len(said), "want one warning naming Sonarr's path, got:\n" + "\n".join(logs.output))
        self.assertIn(str(self.shows), said[0])

    def test_a_hybrid_series_in_the_vod_folder_is_not_reported(self):
        _, logs = self._scan([self._show(1396, "Breaking Bad", 2008, root="/data/vod/Series")])
        self.assertEqual([], self._warnings(logs, "/data/vod/Series"))


if __name__ == "__main__":
    unittest.main()

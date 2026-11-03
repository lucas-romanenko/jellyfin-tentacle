"""#913: Radarr lost track of files that were still on disk.

1. With many downloads reported "no file" at once the scan rightly refused
   to drop the rows, but advised "remove the movies from Radarr". On the
   reporter's install 27 of the 33 files were still there (Radarr had lost
   track after a path change); removing them with "delete files" ticked
   would have deleted good downloads. The refusal now checks each file
   through Tentacle's mount and says which are still on disk (don't remove
   those) and which are really gone.
2. A film one level deeper than usual (<root>/<Trilogy pack>/<Film 1>/)
   was "not found" and the warning said to mount the folder, although the
   mount worked. The folder is now found with its parent folder too, and a
   download that still isn't found is named without the mount advice when
   others were found.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from tmp_dirs import temp_dir

N = 12


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
        self.movies = self.tmp / "media" / "movies"
        self.shows = self.tmp / "media" / "shows"
        self.movies.mkdir(parents=True)
        self.shows.mkdir(parents=True)
        import services.media_files as media_files
        for name, root in (("RADARR_MEDIA_ROOT", self.movies), ("SONARR_MEDIA_ROOT", self.shows)):
            p = mock.patch.object(media_files, name, root, create=True)
            p.start()
            self.addCleanup(p.stop)

    def _scan_radarr(self, movies):
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
        self.db.commit()
        return stats, logs.output


def _film(i, has_file=True, folder=None):
    folder = folder or f"/data/media/movies/Film {i} (2000)"
    return {"tmdbId": 1000 + i, "title": f"Film {i}", "year": 2000, "hasFile": has_file, "path": folder,
            "movieFile": {"path": f"{folder}/Film {i} (2000).mkv"} if has_file else {}}


class RadarrRefusal(_Base):
    def _seed(self, on_disk):
        for i in range(N):
            self.db.add(mdb.Movie(tmdb_id=1000 + i, title=f"Film {i}", year="2000", source="radarr",
                                  radarr_path=f"/data/media/movies/Film {i} (2000)/Film {i} (2000).mkv"))
            if i < on_disk:
                d = self.movies / f"Film {i} (2000)"
                d.mkdir()
                (d / f"Film {i} (2000).mkv").write_bytes(b"x")
        self.db.commit()

    def _refusal(self, logs):
        said = [line for line in logs if "REFUSING" in line]
        self.assertEqual(1, len(said), "\n".join(logs))
        return said[0]

    def test_files_still_on_disk_are_not_advised_for_removal(self):
        self._seed(on_disk=9)
        stats, logs = self._scan_radarr([_film(i, has_file=False) for i in range(N)])
        self.assertEqual(N, stats["removals_refused"])
        said = self._refusal(logs)
        self.assertIn("9 of them are still on disk", said)
        self.assertIn("Don't remove those from Radarr", said)
        self.assertIn("The other 3 really are gone", said)
        self.assertIn("Film 0", said)

    def test_none_on_disk_keeps_the_plain_advice(self):
        self._seed(on_disk=0)
        _, logs = self._scan_radarr([_film(i, has_file=False) for i in range(N)])
        said = self._refusal(logs)
        self.assertNotIn("still on disk", said)
        self.assertIn("remove the movies from Radarr", said)


class NestedFolder(_Base):
    def test_a_film_inside_a_pack_folder_is_found(self):
        d = self.movies / "Trilogy pack" / "Film 1 (2000)"
        d.mkdir(parents=True)
        (d / "Film 1 (2000).mkv").write_bytes(b"x")
        stats, logs = self._scan_radarr([_film(1, folder="/data/media/movies/Trilogy pack/Film 1 (2000)")])
        self.assertTrue((d / "Film 1 (2000).nfo").exists(), "\n".join(logs))
        self.assertEqual(1, stats["nfo_written"])
        self.assertEqual([], [line for line in logs if "not found under" in line])

    def test_not_found_names_the_film_and_skips_mount_advice_when_others_were_found(self):
        d = self.movies / "Film 2 (2000)"
        d.mkdir()
        (d / "Film 2 (2000).mkv").write_bytes(b"x")
        _, logs = self._scan_radarr([_film(1, folder="/elsewhere/Gone/Film 1 (2000)"), _film(2)])
        said = [line for line in logs if "not found under" in line]
        self.assertEqual(1, len(said), "\n".join(logs))
        self.assertIn("Film 1", said[0])
        self.assertNotIn("Mount the folder", said[0])

    def test_nothing_found_still_advises_the_mount(self):
        _, logs = self._scan_radarr([_film(1), _film(2)])
        said = [line for line in logs if "not found under" in line]
        self.assertEqual(1, len(said), "\n".join(logs))
        self.assertIn("Mount the folder", said[0])


class SonarrRefusal(_Base):
    def test_series_with_episodes_on_disk_are_not_advised_for_removal(self):
        import services.sonarr as sonarr
        for i in range(N):
            self.db.add(mdb.Series(tmdb_id=3000 + i, title=f"Show {i}", source="sonarr",
                                   sonarr_path=f"/data/tv/Show {i}"))
            if i < 5:
                d = self.shows / f"Show {i}" / "Season 01"
                d.mkdir(parents=True)
                (d / "e1.mkv").write_bytes(b"x")
        self.db.commit()
        shows = [{"tmdbId": 3000 + i, "tvdbId": i, "title": f"Show {i}", "path": f"/data/tv/Show {i}",
                  "monitorNewItems": "none", "statistics": {"episodeFileCount": 0}} for i in range(N)]
        shows.append({"tmdbId": 3099, "tvdbId": 99, "title": "Show 99", "path": "/data/tv/Show 99",
                      "monitorNewItems": "none", "statistics": {"episodeFileCount": 3}})

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_all_series(self):
                return shows

        with mock.patch.object(sonarr, "SonarrService", Fake), \
             mock.patch.object(sonarr, "emit_library_event", lambda *a, **k: None), \
             self.assertLogs("services.sonarr", level="INFO") as logs:
            sonarr.scan_sonarr_library(self.db)
        said = [line for line in logs.output if "REFUSING" in line]
        self.assertEqual(1, len(said), "\n".join(logs.output))
        self.assertIn("5 of them are still on disk", said[0])
        self.assertIn("Don't remove those from Sonarr", said[0])


class WebhookRefusal(_Base):
    def test_webhook_refusal_names_files_still_on_disk(self):
        import routers.radarr as r
        for i in range(N):
            self.db.add(mdb.Movie(tmdb_id=1000 + i, title=f"Film {i}", year="2000", source="radarr",
                                  radarr_path=f"/data/media/movies/Film {i} (2000)/Film {i} (2000).mkv"))
            if i < 4:
                d = self.movies / f"Film {i} (2000)"
                d.mkdir()
                (d / f"Film {i} (2000).mkv").write_bytes(b"x")
        self.db.commit()
        self.addCleanup(r._missing_pending.clear)
        self.addCleanup(r._missing_recent.clear)
        r._missing_pending.clear()
        r._missing_recent.clear()
        for i in range(N):
            r._missing_pending[1000 + i] = (f"Film {i}", f"/data/media/movies/Film {i} (2000)")
        with self.assertLogs(r.logger.name, level="ERROR") as logs:
            out = r._flush_missing_from_disk(db=self.db)
        self.assertEqual("refused", out["status"])
        self.assertIn("4 of them are still on disk", logs.output[0])

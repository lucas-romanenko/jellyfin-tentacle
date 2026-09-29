"""Duplicates → "Keep VOD" must remove only the downloaded copy (#254).

Radarr and Sonarr answer DELETE /movie|series/{id}?deleteFiles=true by
deleting the title's whole folder, whatever is in it (RecycleBinProvider.
DeleteFolder, after the 200). In the merged layout the docs describe, that
folder is also the VOD folder, so Keep VOD deleted the .strm/.nfo it was told
to keep: every VOD episode of a show, or a film's .strm and .nfo. Keep VOD now
deletes the downloaded files through the arr's file API (never a .strm:
Sonarr 4 lists Tentacle's .strm files as episode files), then removes the
title without deleteFiles when its folder is a VOD folder.

The fake arrs below model that behaviour: a title delete with deleteFiles
removes the folder, a file delete removes that one file.

Run from tentacle/:  python -m unittest tests.test_duplicate_keep_vod_merged_folder
"""
import logging
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Duplicate, Movie, Series, Setting
from routers import duplicates


def setUpModule(): logging.disable(logging.CRITICAL)
def tearDownModule(): logging.disable(logging.NOTSET)


class _FakeArr:
    """One title per tmdb id: {"id", "path", "files": [{"id", "path"}]}."""
    titles = {}
    calls = []
    fail_file_delete = False

    def __init__(self, url, key):
        pass

    def _by_tmdb(self, tmdb_id):
        t = self.titles.get(tmdb_id)
        return None if t is None else {"id": t["id"], "tmdbId": tmdb_id, "path": t["path"]}

    def _by_id(self, arr_id):
        return next((t for t in self.titles.values() if t["id"] == arr_id), None)

    def _files(self, arr_id):
        t = self._by_id(arr_id)
        return [dict(f) for f in (t["files"] if t else [])]

    def _delete_file(self, file_id):
        self.calls.append(("delete_file", file_id))
        if self.fail_file_delete:
            raise RuntimeError("HTTP 500: Access to the path is denied")
        for t in self.titles.values():
            for f in list(t["files"]):
                if f["id"] == file_id:
                    Path(f["path"]).unlink(missing_ok=True)
                    t["files"].remove(f)

    def _delete_title(self, arr_id, delete_files):
        self.calls.append(("delete_title", arr_id, delete_files))
        for k, t in list(self.titles.items()):
            if t["id"] == arr_id:
                if delete_files:  # Radarr/Sonarr: the whole folder, whatever is in it
                    shutil.rmtree(t["path"], ignore_errors=True)
                del self.titles[k]
                return True
        return False


class FakeRadarr(_FakeArr):
    def get_movie_by_tmdb(self, tmdb_id):
        return self._by_tmdb(tmdb_id)

    def get_movie_files(self, movie_id):
        return self._files(movie_id)

    def delete_movie_file(self, file_id):
        self._delete_file(file_id)

    def delete_movie(self, tmdb_id, delete_files=True):
        t = self._by_tmdb(tmdb_id)
        return bool(t) and self._delete_title(t["id"], delete_files)

    def delete_movie_by_id(self, movie_id, delete_files=True):
        return self._delete_title(movie_id, delete_files)


class FakeSonarr(_FakeArr):
    def get_series_by_tmdb(self, tmdb_id, raise_errors=False):
        return self._by_tmdb(tmdb_id)

    def get_episode_files(self, series_id):
        return self._files(series_id)

    def delete_episode_files(self, file_ids):
        for fid in file_ids:
            self._delete_file(fid)

    def delete_series(self, tmdb_id, delete_files=True):
        t = self._by_tmdb(tmdb_id)
        return bool(t) and self._delete_title(t["id"], delete_files)

    def delete_series_by_id(self, series_id, delete_files=True):
        return self._delete_title(series_id, delete_files)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = Path(tmp)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        for k, v in (("radarr_url", "http://radarr"), ("radarr_api_key", "r"),
                     ("sonarr_url", "http://sonarr"), ("sonarr_api_key", "s")):
            self.db.add(Setting(key=k, value=v))
        self.db.commit()
        _FakeArr.titles = {}
        _FakeArr.calls = []
        _FakeArr.fail_file_delete = False
        mock.patch("services.radarr.RadarrService", FakeRadarr).start()
        mock.patch("services.sonarr.SonarrService", FakeSonarr).start()
        self.addCleanup(mock.patch.stopall)

    def movie(self, folder_name="Parasite (2019)", arr_folder=None, tmdb_id=496243):
        vod = self.root / "vod" / "movies" / folder_name
        vod.mkdir(parents=True)
        strm, nfo = vod / f"{folder_name}.strm", vod / f"{folder_name}.nfo"
        strm.write_text("http://p/movie/1.mp4")
        nfo.write_text("<movie/>")
        arr_dir = arr_folder or vod
        arr_dir.mkdir(parents=True, exist_ok=True)
        mkv = arr_dir / f"{folder_name}.mkv"
        mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[tmdb_id] = {"id": 7, "path": str(arr_dir), "files": [{"id": 70, "path": str(mkv)}]}
        self.db.add(Movie(tmdb_id=tmdb_id, title="Parasite", year="2019", source="provider_1",
                          strm_path=str(strm), nfo_path=str(nfo)))
        dup = Duplicate(tmdb_id=tmdb_id, media_type="movie", resolution="pending",
                        sources=[{"source": "radarr", "path": str(mkv)},
                                 {"source": "provider_1", "path": str(strm)}])
        self.db.add(dup)
        self.db.commit()
        return dup, strm, nfo, mkv

    def show(self, with_download=True, tmdb_id=95396):
        show = self.root / "vod" / "shows" / "Severance (2022)"
        (show / "Season 01").mkdir(parents=True)
        (show / "Season 02").mkdir(parents=True)
        e1 = show / "Season 01" / "Severance (2022) S01E01.strm"
        e3 = show / "Season 02" / "Severance (2022) S02E01.strm"
        for p in (e1, e3):
            p.write_text("http://p/series/1.mp4")
        (show / "tvshow.nfo").write_text("<tvshow/>")
        # Sonarr 4 lists Tentacle's .strm files as the series' episode files.
        files = [{"id": 1, "path": str(e1)}, {"id": 3, "path": str(e3)}]
        mkv = show / "Season 01" / "Severance - S01E02 - Owner.mkv"
        if with_download:
            mkv.write_bytes(b"\0" * 64)
            files.append({"id": 2, "path": str(mkv)})
        _FakeArr.titles[tmdb_id] = {"id": 9, "path": str(show), "files": files}
        self.db.add(Series(tmdb_id=tmdb_id, title="Severance", year="2022", source="provider_1",
                           strm_path=str(show)))
        dup = Duplicate(tmdb_id=tmdb_id, media_type="series", resolution="pending",
                        sources=[{"source": "sonarr", "path": str(show)},
                                 {"source": "provider_1", "path": str(show)}])
        self.db.add(dup)
        self.db.commit()
        return dup, show, [e1, e3], mkv


class KeepVodMergedFolder(_Base):
    def test_movie_keeps_its_strm_and_nfo(self):
        dup, strm, nfo, mkv = self.movie()
        duplicates._apply_resolution(dup, "keep_vod", self.db)
        self.assertTrue(strm.exists(), "Keep VOD deleted the VOD .strm it was told to keep")
        self.assertTrue(nfo.exists(), "Keep VOD deleted the VOD .nfo it was told to keep")
        self.assertFalse(mkv.exists(), "the downloaded copy should be gone")
        self.assertNotIn(496243, _FakeArr.titles, "the title should be removed from Radarr")
        self.assertIn(("delete_title", 7, False), _FakeArr.calls)

    def test_show_keeps_every_vod_episode(self):
        dup, show, strms, mkv = self.show()
        duplicates._apply_resolution(dup, "keep_vod", self.db)
        for p in strms:
            self.assertTrue(p.exists(), "Keep VOD deleted a VOD episode of the show it was told to keep")
        self.assertTrue((show / "tvshow.nfo").exists())
        self.assertFalse(mkv.exists())
        self.assertNotIn(("delete_file", 1), _FakeArr.calls, "a .strm was sent to Sonarr's file delete")
        self.assertIn(("delete_title", 9, False), _FakeArr.calls)
        self.assertEqual(self.db.query(Series).one().source, "provider_1")

    def test_show_with_only_strm_episode_files_loses_nothing(self):
        dup, show, strms, _ = self.show(with_download=False)
        duplicates._apply_resolution(dup, "keep_vod", self.db)
        for p in strms:
            self.assertTrue(p.exists())
        self.assertFalse(any(c[0] == "delete_file" for c in _FakeArr.calls))
        self.assertIn(("delete_title", 9, False), _FakeArr.calls)

    def test_failed_file_delete_keeps_the_duplicate_pending(self):
        dup, strm, nfo, mkv = self.movie()
        _FakeArr.fail_file_delete = True
        with self.assertRaises(HTTPException) as cm:
            duplicates._apply_resolution(dup, "keep_vod", self.db)
        self.assertEqual(cm.exception.status_code, 502)
        self.assertTrue(mkv.exists() and strm.exists())
        self.assertIn(496243, _FakeArr.titles, "the title must stay in Radarr while its file is still on disk")
        self.assertFalse(any(c[0] == "delete_title" for c in _FakeArr.calls))


class KeepVodSeparateFolders(_Base):
    def test_download_folder_is_removed_with_its_files(self):
        arr_dir = self.root / "movies" / "Parasite (2019) [downloads]"
        dup, strm, nfo, mkv = self.movie(arr_folder=arr_dir)
        duplicates._apply_resolution(dup, "keep_vod", self.db)
        self.assertTrue(strm.exists() and nfo.exists())
        self.assertFalse(mkv.exists())
        self.assertIn(("delete_title", 7, True), _FakeArr.calls)
        self.assertFalse(arr_dir.exists())


class MergedFolderDetection(unittest.TestCase):
    """Radarr/Sonarr see the folder under their own mount: names are compared."""
    def test_same_folder_name_under_another_mount(self):
        import unicodedata
        from services.duplicates import arr_folder_is_vod_folder
        movie = Movie(strm_path="/media/vod/movies/Amélie (2001)/Amélie (2001).strm")
        nfd = "/data/films/" + unicodedata.normalize("NFD", "AMÉLIE (2001)")
        self.assertTrue(arr_folder_is_vod_folder("movie", nfd, movie))
        show = Series(strm_path="/media/vod/shows/Dark (2017)")
        self.assertTrue(arr_folder_is_vod_folder("series", "D:\\TV\\Dark (2017)\\", show))

    def test_other_folder(self):
        from services.duplicates import arr_folder_is_vod_folder
        movie = Movie(strm_path="/media/vod/movies/Heat (1995)/Heat (1995).strm")
        self.assertFalse(arr_folder_is_vod_folder("movie", "/data/movies/Heat (1995) {tmdb-949}", movie))


if __name__ == "__main__":
    unittest.main()

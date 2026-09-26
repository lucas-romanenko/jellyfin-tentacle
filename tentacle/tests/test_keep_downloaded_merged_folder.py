"""Keeping the downloaded copy never deletes its files, and never strands .strm files (#166).

Run from the tentacle/ directory:  python -m unittest discover -s tests

services.duplicates.delete_vod_files deleted <stem>.nfo unconditionally. #28
added the merged-folder guard only to media_files.delete_movie_files, so
"Keep Downloaded" (and the sync's enforcement of a keep_radarr resolution)
deleted Radarr's "Heat (1995).nfo" describing "Heat (1995).mkv", the copy the
user chose to keep. The enforcement also passed a series' show folder to that
movie helper, which ignored it: the episodes' .strm files stayed next to
Sonarr's files with nothing tracking them (#83 fixed this in the router only).
"""
import logging
import shutil
import tempfile
import unittest
from pathlib import Path

from services.duplicates import delete_vod_files
from services.media_files import delete_series_files


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class KeepDownloaded(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_merged_movie_folder_keeps_the_downloads_nfo(self):
        folder = self.tmp / "Heat (1995)"
        folder.mkdir()
        for name in ("Heat (1995).strm", "Heat (1995).mkv", "Heat (1995).nfo"):
            (folder / name).write_text("x")
        delete_vod_files(str(folder / "Heat (1995).strm"))
        self.assertFalse((folder / "Heat (1995).strm").exists())
        self.assertTrue((folder / "Heat (1995).mkv").exists())
        self.assertTrue((folder / "Heat (1995).nfo").exists(), "Radarr's NFO for the kept copy was deleted")

    def test_a_vod_only_folder_is_still_cleaned_up(self):
        folder = self.tmp / "Alien (1979)"
        folder.mkdir()
        (folder / "Alien (1979).strm").write_text("x")
        (folder / "Alien (1979).nfo").write_text("x")
        delete_vod_files(str(folder / "Alien (1979).strm"))
        self.assertFalse(folder.exists())

    def test_the_sync_enforcement_removes_a_series_strm_files(self):
        """Drives check_and_record_duplicate's keep_radarr branch."""
        from unittest import mock
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        from services import sync as vod_sync

        show = self.tmp / "Friends (1994)"
        (show / "Season 01").mkdir(parents=True)
        (show / "Season 01" / "Friends (1994) S01E01.strm").write_text("x")
        (show / "Season 01" / "Friends (1994) S01E02.mkv").write_text("x")
        (show / "Season 01" / "Friends (1994) S01E02.nfo").write_text("sonarr")

        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        provider = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p")
        db.add(provider)
        db.commit()
        db.add(mdb.Series(tmdb_id=1668, title="Friends", source=f"provider_{provider.id}", strm_path=str(show)))
        db.add(mdb.Duplicate(tmdb_id=1668, media_type="series", resolution="keep_radarr", sources=[]))
        db.commit()
        skipped = vod_sync.check_and_record_duplicate(1668, "series", f"provider_{provider.id}", str(show),
                                                      provider, db)
        self.assertTrue(skipped)
        self.assertFalse((show / "Season 01" / "Friends (1994) S01E01.strm").exists(),
                         "the provider's episodes were left with nothing tracking them")
        self.assertTrue((show / "Season 01" / "Friends (1994) S01E02.mkv").exists())
        self.assertTrue((show / "Season 01" / "Friends (1994) S01E02.nfo").exists())


if __name__ == "__main__":
    unittest.main()

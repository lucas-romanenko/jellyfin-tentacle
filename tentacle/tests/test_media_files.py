"""Tests for services.media_files — deleting only the files Tentacle wrote.

Regression cover for the merged-folder setup (VOD folder and the *arr
downloads folder are the same physical path), where a recursive delete of a
title's directory destroys downloaded media Tentacle never created.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from pathlib import Path

from services.media_files import delete_movie_files, delete_series_files


class TestDeleteSeriesFiles(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.show = self.root / "Community (2009)"
        (self.show / "Season 01").mkdir(parents=True)
        (self.show / "tvshow.nfo").write_text("x")
        (self.show / "Season 01" / "Community S01E01.strm").write_text("http://x")
        (self.show / "Season 01" / "Community S01E01.nfo").write_text("x")
        # Downloaded content Sonarr manages in the same folder
        (self.show / "Season 01" / "Community S01E02.mkv").write_text("video")
        (self.show / "Season 01" / "Community S01E02.en.srt").write_text("subs")

    def test_downloaded_content_survives(self):
        deleted = delete_series_files(self.show)
        self.assertEqual(deleted, 3)  # 2 .strm/.nfo in the season + tvshow.nfo
        self.assertTrue((self.show / "Season 01" / "Community S01E02.mkv").exists())
        self.assertTrue((self.show / "Season 01" / "Community S01E02.en.srt").exists())
        self.assertFalse((self.show / "Season 01" / "Community S01E01.strm").exists())
        self.assertFalse((self.show / "tvshow.nfo").exists())
        # Folder kept because foreign files remain
        self.assertTrue(self.show.is_dir())

    def test_empty_folders_pruned_when_nothing_foreign_remains(self):
        (self.show / "Season 01" / "Community S01E02.mkv").unlink()
        (self.show / "Season 01" / "Community S01E02.en.srt").unlink()
        delete_series_files(self.show)
        self.assertFalse(self.show.exists())

    def test_missing_path_is_a_no_op(self):
        self.assertEqual(delete_series_files(self.root / "nope"), 0)
        self.assertEqual(delete_series_files(None), 0)


class TestDeleteMovieFiles(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.folder = self.root / "Alien (1979)"
        self.folder.mkdir(parents=True)
        self.strm = self.folder / "Alien (1979).strm"
        self.strm.write_text("http://x")
        (self.folder / "Alien (1979).nfo").write_text("x")

    def test_deletes_strm_and_nfo_and_prunes_folder(self):
        self.assertEqual(delete_movie_files(self.strm), 2)
        self.assertFalse(self.folder.exists())

    def test_keeps_folder_holding_downloaded_file(self):
        (self.folder / "Alien (1979) Bluray-1080p.mkv").write_text("video")
        self.assertEqual(delete_movie_files(self.strm), 2)
        self.assertTrue((self.folder / "Alien (1979) Bluray-1080p.mkv").exists())
        self.assertTrue(self.folder.is_dir())


if __name__ == "__main__":
    unittest.main()

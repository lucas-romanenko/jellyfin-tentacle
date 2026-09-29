"""#282: a download Radarr/Sonarr can import counts as someone else's content
whatever its extension's case, and whether or not it is one of the common
container formats -- so the NFOs it shares with a VOD copy are left alone.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from services.media_files import MEDIA_SUFFIXES, delete_movie_files, delete_series_files


class TestMovieNfoNextToADownload(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.folder = self.root / "Heat (1995)"
        self.folder.mkdir()
        self.strm = self.folder / "Heat (1995).strm"
        self.strm.write_text("http://provider/movie/u/p/1.mp4")
        self.nfo = self.folder / "Heat (1995).nfo"
        self.nfo.write_text("<movie><tmdbid>949</tmdbid></movie>")   # Radarr's, for the download

    def _download(self, name):
        (self.folder / name).write_bytes(b"video")

    def test_an_upper_case_mkv_keeps_the_nfo(self):
        self._download("Heat (1995).MKV")
        delete_movie_files(self.strm)
        self.assertFalse(self.strm.exists())
        self.assertTrue(self.nfo.exists(), "the NFO describes the download")
        self.assertTrue((self.folder / "Heat (1995).MKV").exists())

    def test_an_iso_keeps_the_nfo(self):
        self._download("Heat (1995).iso")
        delete_movie_files(self.strm)
        self.assertTrue(self.nfo.exists())

    def test_without_a_download_both_files_and_the_folder_go(self):
        self.assertEqual(2, delete_movie_files(self.strm))
        self.assertFalse(self.folder.exists())


class TestShowNfoNextToAnIso(unittest.TestCase):
    def test_an_iso_download_keeps_the_shared_tvshow_nfo(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        show = root / "Sky Stories (2003)"
        (show / "Season 01").mkdir(parents=True)
        (show / "tvshow.nfo").write_text("x")
        (show / "Season 01" / "Sky Stories (2003) S01E01.strm").write_text("http://x")
        (show / "Season 01" / "Sky Stories - S01E05 - Owner Rip.iso").write_bytes(b"x")
        delete_series_files(show)
        self.assertTrue((show / "tvshow.nfo").exists())
        self.assertTrue((show / "Season 01" / "Sky Stories - S01E05 - Owner Rip.iso").exists())


class TestSuffixes(unittest.TestCase):
    def test_what_radarr_and_sonarr_import_counts_as_media(self):
        for ext in (".iso", ".img", ".vob", ".flv", ".rmvb", ".divx", ".m2ts", ".mk3d", ".ogv", ".wtv"):
            self.assertIn(ext, MEDIA_SUFFIXES)
        self.assertNotIn(".strm", MEDIA_SUFFIXES, "Tentacle's own files are not someone else's media")
        self.assertNotIn(".nfo", MEDIA_SUFFIXES)


if __name__ == "__main__":
    unittest.main()

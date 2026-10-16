"""Removing a VOD show keeps the NFO of a downloaded episode with the same name.

delete_movie_files keeps "<stem>.nfo" when a download with the .strm's stem
sits beside it: that NFO is Radarr's, describing the download (#166, #28).
delete_series_files deleted every .strm's same-stem NFO unconditionally, so in
a merged show folder where Sonarr names an episode exactly like Tentacle does
("Show (2010) S01E01"), the downloaded episode lost its metadata.
"""
import shutil
import unittest
from pathlib import Path

from services.media_files import delete_series_files
from tmp_dirs import temp_dir


class SeriesDeleteKeepsDownloadNfo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.show = self.tmp / "Show (2010)"
        self.season = self.show / "Season 01"
        self.season.mkdir(parents=True)

    def test_a_downloaded_episodes_nfo_is_kept(self):
        for name in ("Show (2010) S01E01.strm", "Show (2010) S01E01.mkv", "Show (2010) S01E01.nfo"):
            (self.season / name).write_text("x")
        delete_series_files(self.show)
        self.assertFalse((self.season / "Show (2010) S01E01.strm").exists())
        self.assertTrue((self.season / "Show (2010) S01E01.mkv").exists())
        self.assertTrue((self.season / "Show (2010) S01E01.nfo").exists(),
                        "the downloaded episode's NFO was deleted")

    def test_upper_case_and_other_formats_count_as_a_download(self):
        for name in ("Show (2010) S01E02.strm", "Show (2010) S01E02.MKV", "Show (2010) S01E02.nfo"):
            (self.season / name).write_text("x")
        delete_series_files(self.show)
        self.assertTrue((self.season / "Show (2010) S01E02.nfo").exists())

    def test_the_nfo_of_a_strm_only_episode_still_goes(self):
        for name in ("Show (2010) S01E03.strm", "Show (2010) S01E03.nfo"):
            (self.season / name).write_text("x")
        n = delete_series_files(self.show)
        self.assertEqual(2, n)
        self.assertFalse(self.show.exists(), "an all-Tentacle show folder is removed")

    def test_the_count_leaves_out_the_kept_nfo(self):
        for name in ("Show (2010) S01E01.strm", "Show (2010) S01E01.mkv", "Show (2010) S01E01.nfo",
                     "Show (2010) S01E02.strm", "Show (2010) S01E02.nfo"):
            (self.season / name).write_text("x")
        self.assertEqual(3, delete_series_files(self.show))


class StartFreshCountMatchesWhatItDeletes(unittest.TestCase):
    """The Start Fresh banner counts what the cleanup removes (#107): an NFO
    beside a same-name download is kept, so it is not counted either."""

    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_film_and_episode_nfos_beside_a_download_are_not_counted(self):
        from routers.settings import _stale_nfo_count
        film = self.tmp / "movies" / "Heat (1995)"
        film.mkdir(parents=True)
        for name in ("Heat (1995).strm", "Heat (1995).mkv", "Heat (1995).nfo"):
            (film / name).write_text("x")
        season = self.tmp / "shows" / "Show (2010)" / "Season 01"
        season.mkdir(parents=True)
        for name in ("Show (2010) S01E01.strm", "Show (2010) S01E01.mkv", "Show (2010) S01E01.nfo",
                     "Show (2010) S01E02.strm", "Show (2010) S01E02.nfo"):
            (season / name).write_text("x")
        self.assertEqual(0, _stale_nfo_count(self.tmp / "movies"))
        self.assertEqual(1, _stale_nfo_count(self.tmp / "shows"))


if __name__ == "__main__":
    unittest.main()

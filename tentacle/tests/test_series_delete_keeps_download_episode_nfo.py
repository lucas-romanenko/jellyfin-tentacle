"""services/media_files.py delete_series_files() and routers/settings.py
_stale_nfo_count() in a merged show folder (#614).

Tentacle names a VOD episode "Show (2010) S01E01.strm". Sonarr with an episode
format of "{Series Title} ({Series Year}) S{season:00}E{episode:00}" imports
"Show (2010) S01E01.mkv" beside it and writes "Show (2010) S01E01.nfo" for it.
Removing the VOD show must keep that NFO (it describes the download), exactly as
delete_movie_files() keeps a film's NFO beside a same-stem download, and the
"Delete All & Start Fresh" banner must not count NFOs the cleanup keeps.

Run from the tentacle/ directory:  python tests/hermetic.py tests/test_series_delete_keeps_download_episode_nfo.py
"""
import shutil
import unittest
from pathlib import Path

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

from services.media_files import delete_series_files  # noqa: E402
from tmp_dirs import temp_dir  # noqa: E402


def _touch(folder: Path, *names):
    for name in names:
        (folder / name).write_text("x")


class SeriesDeleteKeepsDownloadNfo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.show = self.tmp / "Show (2010)"
        self.season = self.show / "Season 01"
        self.season.mkdir(parents=True)

    def test_a_downloaded_episodes_nfo_is_kept(self):
        _touch(self.season, "Show (2010) S01E01.strm", "Show (2010) S01E01.mkv", "Show (2010) S01E01.nfo")
        self.assertEqual(1, delete_series_files(self.show))
        self.assertFalse((self.season / "Show (2010) S01E01.strm").exists())
        self.assertTrue((self.season / "Show (2010) S01E01.mkv").exists())
        self.assertTrue((self.season / "Show (2010) S01E01.nfo").exists(),
                        "the downloaded episode's NFO was deleted")

    def test_any_case_and_format_of_download_keeps_the_nfo(self):
        _touch(self.season, "Show (2010) S01E02.strm", "Show (2010) S01E02.MP4", "Show (2010) S01E02.nfo")
        delete_series_files(self.show)
        self.assertTrue((self.season / "Show (2010) S01E02.nfo").exists())

    def test_the_nfo_of_a_strm_only_episode_still_goes(self):
        _touch(self.season, "Show (2010) S01E03.strm", "Show (2010) S01E03.nfo")
        self.assertEqual(2, delete_series_files(self.show))
        self.assertFalse(self.show.exists())

    def test_mixed_season_keeps_only_the_downloads_nfo(self):
        _touch(self.show, "tvshow.nfo")
        _touch(self.season,
               "Show (2010) S01E01.strm", "Show (2010) S01E01.mkv", "Show (2010) S01E01.nfo",
               "Show (2010) S01E03.strm", "Show (2010) S01E03.nfo")
        self.assertEqual(3, delete_series_files(self.show))
        self.assertEqual(
            ["Show (2010) S01E01.mkv", "Show (2010) S01E01.nfo"],
            sorted(p.name for p in self.season.iterdir()))
        self.assertTrue((self.show / "tvshow.nfo").exists(), "tvshow.nfo goes only once no media is left")


class StartFreshCountMatchesWhatItDeletes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_film_and_episode_nfos_beside_a_download_are_not_counted(self):
        from routers.settings import _stale_nfo_count
        film = self.tmp / "movies" / "Heat (1995)"
        film.mkdir(parents=True)
        _touch(film, "Heat (1995).strm", "Heat (1995).mkv", "Heat (1995).nfo")
        season = self.tmp / "shows" / "Show (2010)" / "Season 01"
        season.mkdir(parents=True)
        _touch(season,
               "Show (2010) S01E01.strm", "Show (2010) S01E01.mkv", "Show (2010) S01E01.nfo",
               "Show (2010) S01E02.strm", "Show (2010) S01E02.nfo")
        self.assertEqual(0, _stale_nfo_count(self.tmp / "movies"))
        self.assertEqual(1, _stale_nfo_count(self.tmp / "shows"))

    def test_strm_only_nfos_are_still_counted(self):
        from routers.settings import _stale_nfo_count
        film = self.tmp / "movies" / "Alien (1979)"
        film.mkdir(parents=True)
        _touch(film, "Alien (1979).strm", "Alien (1979).nfo")
        self.assertEqual(1, _stale_nfo_count(self.tmp / "movies"))


if __name__ == "__main__":
    unittest.main()

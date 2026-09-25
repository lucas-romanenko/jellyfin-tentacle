"""VOD folder and file names must fit the filesystem's 255-BYTE name limit (A14).

vod_folder_name() capped titles at 200 characters, and 200 characters of CJK
is 600 bytes of UTF-8. ext4/XFS/btrfs/SMB cap one name at 255 bytes, so
mkdir (or the .strm write) raised ENAMETOOLONG: the title never imported and
the sync logged an ERROR for it every run. Episode files add " S01E01" to the
show folder's name and could overflow even when the folder itself fitted.

Names that already fit must not change — a different path is a new Jellyfin
item, and watched state would be lost.
Run from tentacle/:  python -m unittest discover -s tests -p "test_vod_long_names.py"
"""
import tempfile
import unittest
from pathlib import Path

from services.nfo import vod_folder_name
import services.sync as sync

LONG = "アニメ" * 30        # 90 characters, 270 bytes


def _bytes(s):
    return len(s.encode("utf-8"))


class TestFolderName(unittest.TestCase):
    def test_a_long_cjk_title_can_be_written(self):
        name = vod_folder_name(LONG, "2020")
        self.assertLessEqual(_bytes(name) + len(".strm"), 255)
        d = Path(tempfile.mkdtemp()) / name
        d.mkdir()
        (d / f"{name}.strm").write_text("x")
        (d / f"{name}.nfo").write_text("x")

    def test_the_year_is_kept_for_jellyfins_parser(self):
        self.assertTrue(vod_folder_name(LONG, "2020").endswith(" (2020)"))

    def test_stable_and_distinct(self):
        self.assertEqual(vod_folder_name(LONG, "2020"), vod_folder_name(LONG, "2020"))
        self.assertNotEqual(vod_folder_name(LONG + "A", "2020"), vod_folder_name(LONG + "B", "2020"))

    def test_names_that_fit_are_unchanged(self):
        for title, year in (("The Matrix", "1999"), ("...And Justice for All", "1979"),
                            ("アニメ" * 26, "2020"), ("x" * 200, None)):
            with self.subTest(title=title[:12]):
                old = __import__("services.nfo", fromlist=["make_folder_name"]).make_folder_name(title, year).lstrip(". ")
                if _bytes(old) + 5 <= 255:
                    self.assertEqual(vod_folder_name(title, year), old)

    def test_a_long_title_without_a_year(self):
        name = vod_folder_name(LONG, None)
        self.assertLessEqual(_bytes(name) + 5, 255)


class _Client:
    def episode_stream_url(self, ep_id, container):
        return f"http://p/series/u/p/{ep_id}.{container}"


class TestEpisodeNames(unittest.TestCase):
    def _write(self, folder_name):
        show = Path(tempfile.mkdtemp()) / folder_name
        show.mkdir()
        n = sync._write_episode_strms(_Client(), {"1": [{"id": 7, "episode_num": 3}]}, show, folder_name)
        return n, [p.name for p in (show / "Season 01").iterdir()]

    def test_an_episode_of_a_show_whose_folder_just_fits(self):
        folder = "ア" * 82 + " (2020)"      # 253 bytes: the folder fits, the episode would not
        self.assertLessEqual(_bytes(folder), 255)
        n, names = self._write(folder)
        self.assertEqual(n, 1)
        self.assertLessEqual(_bytes(names[0]), 255)
        self.assertTrue(names[0].endswith(" S01E03.strm"))

    def test_ordinary_episode_names_are_unchanged(self):
        n, names = self._write("Cheers (1982)")
        self.assertEqual(names, ["Cheers (1982) S01E03.strm"])


if __name__ == "__main__":
    unittest.main()

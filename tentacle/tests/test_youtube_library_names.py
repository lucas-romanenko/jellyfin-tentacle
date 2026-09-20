"""Folder names built from YouTube titles (abe72f4).

services/youtube/library.py truncates a title to 100 *characters*, but every
common filesystem limits a name to 255 *bytes*. A non-Latin title (Japanese,
Korean, Chinese, emoji) is 3-4 bytes per character, so the folder name for a
perfectly ordinary upload is over the limit and mkdir raises ENAMETOOLONG.

services/youtube/sync.py swallows that OSError, so the video silently never
appears in Jellyfin and the failure is retried on every run for ever.

Writes to a real temporary directory, because the limit is the filesystem's.
Needs sqlalchemy only (as CI has).
Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from services.youtube import library


class _Video:
    def __init__(self, title, vid="EXhAoxKXBcE"):
        self.video_id = vid
        self.title = title
        self.description = "Desc"
        self.published_at = datetime(2026, 9, 15)
        self.first_seen = datetime(2026, 9, 15)
        self.duration = 484
        self.live_status = None
        self.folder_path = None
        self.strm_path = None
        self.thumbnail_url = None


class _Channel:
    title = "ニュースチャンネル"
    slug = "news"
    extra_tags = []
    rating = None


# A real, unremarkable Japanese upload title: 84 characters, 252 bytes.
JP_TITLE = "【完全版】東京の下町グルメを食べ歩きながら歴史を学ぶ散歩ドキュメンタリー第三回浅草から上野まで歩いて見つけた昭和の名店と職人たちの話を聞いてきました前編" + "詳細解説付き"


class TestNonLatinTitles(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_folder_name_fits_the_filesystem_limit(self):
        video = _Video(JP_TITLE)
        folder = library.video_folder(_Channel.title, video, self.root)
        self.assertLessEqual(
            len(folder.name.encode("utf-8")), 255,
            f"folder name is {len(folder.name.encode('utf-8'))} bytes "
            f"({len(folder.name)} characters) — over the 255-byte limit",
        )

    def test_strm_name_fits_the_filesystem_limit(self):
        video = _Video(JP_TITLE)
        folder = library.video_folder(_Channel.title, video, self.root)
        strm = f"{folder.name}.strm"
        self.assertLessEqual(len(strm.encode("utf-8")), 255,
                             f"{len(strm.encode('utf-8'))} bytes")

    def test_a_japanese_upload_can_be_written(self):
        video, channel = _Video(JP_TITLE), _Channel()
        # At abe72f4: OSError [Errno 36] File name too long
        library.write_video(video, channel, "http://tentacle:8888", self.root)
        self.assertTrue(Path(video.strm_path).exists())

    def test_shortened_names_still_carry_the_video_id_and_the_date(self):
        """Whatever the truncation, the id is what keeps two uploads apart."""
        video = _Video(JP_TITLE)
        folder = library.video_folder(_Channel.title, video, self.root)
        self.assertIn("[EXhAoxKXBcE]", folder.name)
        self.assertTrue(folder.name.startswith("2026-09-15 "), folder.name)

    def test_an_ascii_title_is_untouched(self):
        video = _Video("A Perfectly Ordinary Upload")
        folder = library.video_folder("BBC News", video, self.root)
        self.assertEqual("2026-09-15 A Perfectly Ordinary Upload [EXhAoxKXBcE]",
                         folder.name)


if __name__ == "__main__":
    unittest.main()

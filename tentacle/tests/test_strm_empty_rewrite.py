"""An empty .strm is repaired like a missing one (#283).

Path.write_text() truncates the file before writing, so a write cut short
(disk full, the container stopped mid-sync, a power loss) left a 0-byte
.strm. A missing .strm was restored and a wrong address rewritten, but an
empty one was left alone for good: Jellyfin kept an item with nothing to
play. The sync now rewrites a blank .strm, and writes .strm files through a
temp file and a rename, so a cut-short write leaves the old file whole.
"""
import os
import tempfile
import unittest
from pathlib import Path as _RealPath
from unittest import mock

import services.sync as sync
from models.database import Provider
from nightly_harness import NightlyHarness


class BlankFileNeedsRewrite(unittest.TestCase):
    def test_an_empty_strm_needs_a_rewrite(self):
        p = Provider(id=1, name="P", server_url="http://prov.example:8080", username="u", password="p")
        client = sync.XtreamClient(p)
        expected = client.movie_stream_url(42, "mkv")
        with tempfile.TemporaryDirectory() as d:
            f = _RealPath(d) / "Film (2020).strm"
            for blank in ("", "  \n"):
                f.write_text(blank, encoding="utf-8")
                self.assertTrue(sync._strm_needs_rewrite(f, expected, client), repr(blank))
            f.write_text(expected, encoding="utf-8")
            self.assertFalse(sync._strm_needs_rewrite(f, expected, client))


class SyncRepairsEmptyFiles(NightlyHarness):
    def setUp(self):
        super().setUp()
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"])
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", ["Friends"])
        self.night()

    def _episode(self):
        show = _RealPath(self.series_row(5000).strm_path)
        return next(show.rglob("*.strm"))

    def test_empty_movie_and_episode_files_get_their_address_back(self):
        movie = _RealPath(self.movie(1000).strm_path)
        episode = self._episode()
        movie.write_text("", encoding="utf-8")
        episode.write_text("", encoding="utf-8")

        self.night()

        self.assertEqual("http://provider/movie/u/p/1000.mp4", movie.read_text().strip())
        self.assertEqual("http://provider/series/u/p/50001.mp4", episode.read_text().strip())

    def test_an_opted_out_movie_is_left_alone(self):
        row = self.movie(1000)
        movie = _RealPath(row.strm_path)
        movie.write_text("", encoding="utf-8")
        row.strm_disabled = True
        self.db.commit()
        self.night()
        self.assertEqual("", movie.read_text())

    def test_a_write_cut_short_keeps_the_old_file(self):
        # The real Xtream client, so an address with an old password is rewritten
        x = sync.XtreamClient(self.provider)
        for name in ("get_vod_streams", "get_series_list", "get_series_info"):
            setattr(x, name, getattr(self.client, name))
        sync.make_provider_client = lambda p: x
        movie = _RealPath(self.movie(1000).strm_path)
        movie.write_text("http://provider/movie/u/OLD/1000.mp4", encoding="utf-8")   # a changed password
        real_replace = os.replace

        def disk_full(*a, **k):
            raise OSError(28, "No space left on device")
        with mock.patch.object(sync.os, "replace", disk_full):
            self.night()
        self.assertEqual("http://provider/movie/u/OLD/1000.mp4", movie.read_text().strip())
        self.assertEqual([], [p.name for p in movie.parent.iterdir() if p.name.endswith(".tmp")])
        self.assertIs(real_replace, os.replace)
        self.night()
        self.assertEqual("http://provider/movie/u/p/1000.mp4", movie.read_text().strip())


if __name__ == "__main__":
    unittest.main()

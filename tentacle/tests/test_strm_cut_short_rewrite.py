"""A .strm cut short mid-address is repaired, and no writer cuts one short (#629).

#283 made the sync's .strm writes atomic and repairs an EMPTY .strm. A file
holding only the start of its address ("http://provider/", a write cut short
by a full disk, the container stopped, a power loss; or written before an
install had the atomic writes) played nothing for good: _strm_needs_rewrite
only rewrote another form of the same stream. A strict prefix of the address
the sync writes today is never anybody's link, so it is rewritten too.

"Fix it" (moving a copy to the right film's folder) and the YouTube library
still wrote .strm files with a plain write_text, which truncates first: they
write through sync._write_strm (temp file + rename) now, and the YouTube
library repairs a .strm it finds cut short.
"""
import tempfile
import unittest
from pathlib import Path as _RealPath
from unittest import mock

import test_wrong_match   # first: it installs the web stubs services.* need
import services.sync as sync
from models.database import Provider
from nightly_harness import NightlyHarness
from services.youtube import library
from test_youtube import FakeChannel, FakeVideo
from tmp_dirs import temp_dir


def _cut_short(self, data, *a, **k):
    """Path.write_text as a full disk does it: the file is truncated, half the
    data lands, and the write fails."""
    with open(self, "w", encoding="utf-8") as f:
        f.write(data[: len(data) // 2])
    raise OSError(28, "No space left on device")


class CutShortNeedsRewrite(unittest.TestCase):
    def test_a_strict_prefix_of_the_address_needs_a_rewrite(self):
        p = Provider(id=1, name="P", server_url="http://prov.example:8080", username="u", password="p")
        client = sync.XtreamClient(p)
        expected = client.movie_stream_url(42, "mkv")
        with tempfile.TemporaryDirectory() as d:
            f = _RealPath(d) / "Film (2020).strm"
            for cut in ("http://prov.example:8080/", "http://prov.example:8080/movie/u/p/4",
                        expected[:-4], expected[:-1]):
                f.write_text(cut, encoding="utf-8")
                self.assertTrue(sync._strm_needs_rewrite(f, expected, client), cut)
            # Not a prefix: another stream (a title listed twice), left alone as before
            f.write_text("http://prov.example:8080/movie/u/p/4.mkv", encoding="utf-8")
            self.assertFalse(sync._strm_needs_rewrite(f, expected, client))
            f.write_text(expected, encoding="utf-8")
            self.assertFalse(sync._strm_needs_rewrite(f, expected, client))


class SyncRepairsCutShortFiles(NightlyHarness):
    def setUp(self):
        super().setUp()
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"])
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", ["Friends"])
        # The real Xtream client, so the real rewrite rules run
        x = sync.XtreamClient(self.provider)
        for name in ("get_vod_streams", "get_series_list", "get_series_info"):
            setattr(x, name, getattr(self.client, name))
        sync.make_provider_client = lambda p: x
        self.night()

    def test_cut_short_movie_and_episode_files_get_their_address_back(self):
        movie = _RealPath(self.movie(1000).strm_path)
        episode = next(_RealPath(self.series_row(5000).strm_path).rglob("*.strm"))
        movie_url, episode_url = movie.read_text().strip(), episode.read_text().strip()
        movie.write_text("http://provider/", encoding="utf-8")
        episode.write_text(episode_url[: len(episode_url) // 2], encoding="utf-8")

        self.night()

        self.assertEqual("http://provider/movie/u/p/1000.mp4", movie_url)
        self.assertEqual(movie_url, movie.read_text().strip(), "a cut-short movie .strm stays broken")
        self.assertEqual(episode_url, episode.read_text().strip(), "a cut-short episode .strm stays broken")


class FixItWritesTheMovedCopyWhole(test_wrong_match._Base):
    """TestRematch's setup, without running its tests a second time."""
    rematch = test_wrong_match.TestRematch.rematch
    _share_the_folder_with_a_download = test_wrong_match.TestRematch._share_the_folder_with_a_download

    def setUp(self):
        super().setUp()
        self.fake = test_wrong_match.FakeRealTMDB()
        p = mock.patch.object(test_wrong_match.wrong_match, "_tmdb", lambda db: self.fake)
        p.start()
        self.addCleanup(p.stop)
        test_wrong_match.FakeTMDB.get_movie_details = lambda _self, tid: self.fake.get_movie_details(tid)
        self.addCleanup(lambda: delattr(test_wrong_match.FakeTMDB, "get_movie_details"))
        self.jf.trigger_library_scan = mock.Mock(return_value=True)

    def test_a_move_cut_short_leaves_no_broken_copy(self):
        old_strm = _RealPath(self.movie(self.tmdb).strm_path)
        url = old_strm.read_text()
        self._share_the_folder_with_a_download()   # so the copy moves
        with mock.patch.object(_RealPath, "write_text", _cut_short):
            with self.assertRaises((OSError, test_wrong_match.wrong_match.WrongMatchError)):
                self.rematch()
        self.assertEqual(url, old_strm.read_text(), "the old copy is untouched")
        root = old_strm.parent.parent
        left = [p.read_text() for p in root.glob("The Decline*/*") if p.suffix in (".strm", ".tmp")]
        self.assertEqual([], left, "a cut-short .strm (or its temp file) is left in the new folder")


class YouTubeStrmWrites(unittest.TestCase):
    BASE = "http://t:8888"

    def setUp(self):
        self.root = _RealPath(temp_dir(self))
        real = library._download
        library._download = lambda url: b""
        self.addCleanup(setattr, library, "_download", real)
        self.video, self.channel = FakeVideo(), FakeChannel()
        library.write_video(self.video, self.channel, self.BASE, self.root)
        self.strm = _RealPath(self.video.strm_path)
        self.url = self.strm.read_text()

    def test_a_cut_short_strm_is_repaired_on_the_next_pass(self):
        self.strm.write_text(self.url[: len(self.url) // 2], encoding="utf-8")
        info = library.write_video(self.video, self.channel, self.BASE, self.root)
        self.assertEqual(self.url, self.strm.read_text(), "a cut-short YouTube .strm stays broken")
        self.assertTrue(info["strm_written"])

    def test_a_whole_strm_is_still_never_rewritten(self):
        self.assertFalse(library.write_video(self.video, self.channel, self.BASE, self.root)["strm_written"])

    def test_a_new_strm_cut_short_leaves_nothing_behind(self):
        other = FakeVideo(vid="zzzzzzzzzzz", title="Another")
        with mock.patch.object(_RealPath, "write_text", _cut_short):
            with self.assertRaises(OSError):
                library.write_video(other, self.channel, self.BASE, self.root)
        left = [p.name for p in self.root.rglob("*") if p.suffix in (".strm", ".tmp") and p != self.strm]
        self.assertEqual([], left, "a cut-short new .strm is left for Jellyfin to scan")

    def test_a_rewrite_cut_short_keeps_the_old_file(self):
        with mock.patch.object(_RealPath, "write_text", _cut_short):
            self.assertFalse(library.rewrite_strm(self.video, "http://192.0.2.10:8888"))
        self.assertEqual(self.url, self.strm.read_text(), "a rewrite cut short broke the .strm")
        self.assertEqual([], [p.name for p in self.strm.parent.iterdir() if p.suffix == ".tmp"])


if __name__ == "__main__":
    unittest.main()

"""A .strm cut short mid-address is repaired, and no writer can cut one short
any more (#629, follow-up to #283).

#283 repaired an EMPTY .strm and made the sync's writes atomic. A file that
holds only the start of its address ("http://provider/") played nothing and
was never rewritten, and "Fix it" (moving a copy) and the YouTube library
still wrote .strm files with a plain write_text, which empties the file first.

Run from the tentacle/ directory:  python -m unittest tests.test_strm_cut_short_repaired
"""
import errno
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import services.sync as sync
from models.database import Movie
from services.youtube import library
from test_vod_namesakes import stream
from test_vod_relisted_streams import Relisted, FILLER
from tmp_dirs import temp_dir


class CutShortStrmIsRepaired(Relisted):
    def listing(self, *streams):
        self.client.movies = {"a": list(streams) + [stream(f"{n} (2010)", sid) for n, sid in FILLER]}

    def test_a_cut_short_strm_is_rewritten_by_the_next_sync(self):
        self.listing(stream("Amelie (2001)", 103))
        self.night()
        strm = Path(self.db.query(Movie).filter(Movie.tmdb_id == 194).one().strm_path)
        full = strm.read_text(encoding="utf-8")
        for cut in (len(full) // 2, len(full) - 1):
            strm.write_text(full[:cut], encoding="utf-8")
            self.night()
            self.assertEqual(full, strm.read_text(encoding="utf-8"), cut)


class PrefixRule(unittest.TestCase):
    def setUp(self):
        self.dir = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.client = sync.XtreamClient.__new__(sync.XtreamClient)
        self.client.server, self.client.username, self.client.password = "http://provider", "u", "p"
        self.client.provider_id = 1

    def _needs(self, current, expected="http://provider/movie/u/p/103.mp4"):
        f = self.dir / "x.strm"
        f.write_text(current, encoding="utf-8")
        return sync._strm_needs_rewrite(f, expected, self.client)

    def test_the_start_of_todays_address_is_rewritten(self):
        for cut in ("http://", "http://provider/", "http://provider/movie/u/p/10", "http://provider/movie/u/p/103.mp"):
            self.assertTrue(self._needs(cut), cut)

    def test_another_stream_or_host_is_still_left_alone(self):
        for other in ("http://provider/movie/u/p/10.mp4",            # another stream id
                      "http://elsewhere/movie/x/y/103.mp4",           # another host and login
                      "http://proxy/resume?d=http%3A%2F%2Fprovider"): # somebody's proxy
            self.assertFalse(self._needs(other), other)


class WritersNeverCutAFileShort(unittest.TestCase):
    """A write that fails half-way leaves the old file whole (temp + rename)."""

    def setUp(self):
        self.dir = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _disk_full_after_half(self):
        real = Path.write_text

        def write_text(path, data, *a, **k):
            if path.suffix in (".strm", ".tmp"):
                real(path, data[: len(data) // 2], *a, **k)
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, data, *a, **k)
        return mock.patch.object(Path, "write_text", write_text)

    def test_youtube_rewrite_strm_keeps_the_old_file_when_the_write_fails(self):
        strm = self.dir / "video.strm"
        strm.write_text("http://old-base/api/youtube/play/abc", encoding="utf-8")
        video = SimpleNamespace(strm_path=str(strm), video_id="abc")
        with self._disk_full_after_half():
            self.assertFalse(library.rewrite_strm(video, "http://new-base"))
        self.assertEqual("http://old-base/api/youtube/play/abc", strm.read_text(encoding="utf-8"))
        self.assertEqual([], [p.name for p in self.dir.iterdir() if p.name.startswith(".tentacle-")])

    def test_fix_it_writes_the_moved_strm_through_the_atomic_helper(self):
        import inspect
        from services import wrong_match
        src = inspect.getsource(wrong_match)
        self.assertNotIn("new_strm.write_text(", src)
        self.assertIn("_write_strm(new_strm, stream_url)", src)


if __name__ == "__main__":
    unittest.main()

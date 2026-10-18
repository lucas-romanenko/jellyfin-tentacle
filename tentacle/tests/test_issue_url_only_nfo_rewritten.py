"""An NFO beside a download that is not Tentacle's XML counts as written on every scan.

#266 made the Radarr/Sonarr scans touch only Tentacle's <tag> lines in an
existing NFO, and write nothing when they already match. An NFO with neither
</movie> nor </tvshow> never matches: Radarr's/Sonarr's Kodi metadata with only
"Metadata URL" on (a TMDB/IMDb link and nothing else), or a release .nfo with
the video's name (the scan picks video_file.with_suffix('.nfo')). It has no
<tag>, so the wanted tags always differ; update_nfo_tags has no closing tag to
insert before, writes the same bytes back and returns True. Every scan then
counts it in nfo_written, nfo_written > 0 triggers a full Jellyfin library
scan, and every Radarr/Sonarr webhook runs a scan (#628).

No network. Run from tentacle/:
  python -m unittest discover -s tests -p "test_issue_url_only_nfo_rewritten.py"
"""
import os
import unittest
from pathlib import Path

from services.nfo import refresh_arr_nfo, update_nfo_tags, write_movie_nfo, write_series_nfo
from tmp_dirs import temp_dir

OWNED = {"Recently Added Movies", "Downloaded Movies"}
TAGS = ["Downloaded Movies"]
META = {"title": "The Matrix", "tmdb_id": 603, "year": "1999"}

FOREIGN = {
    "metadata URL NFO": "https://www.themoviedb.org/movie/603\nhttps://www.imdb.com/title/tt0133093/\n",
    "release .nfo": "The.Matrix.1999.720p-GRP\n\nVideo: x264 720p\nAudio: English AC3\n",
    # Not XML Tentacle can insert into, but with a <tag> line: stripping it
    # and inserting nothing would lose it.
    "no closing tag, a tag line": "<movie>\n  <title>The Matrix</title>\n  <tag>Watched</tag>\n",
}


class ForeignNfoIsNotWritten(unittest.TestCase):
    def setUp(self):
        self.dir = Path(temp_dir(self))

    def _nfo(self, body: str) -> Path:
        nfo = self.dir / "The Matrix (1999).nfo"
        nfo.write_text(body, encoding="utf-8")
        os.utime(nfo, ns=(1_000_000_000, 1_000_000_000))
        return nfo

    def test_a_scan_neither_writes_nor_counts_it(self):
        for name, body in FOREIGN.items():
            for write in (write_movie_nfo, write_series_nfo):
                with self.subTest(name, write=write.__name__):
                    nfo = self._nfo(body)
                    for scan in ("first", "second"):
                        written = refresh_arr_nfo(nfo, write, META, TAGS, OWNED)
                        self.assertFalse(written, f"{scan} scan counts it as written "
                                                  "(nfo_written > 0 triggers a full Jellyfin library scan)")
                    self.assertEqual(body, nfo.read_text(encoding="utf-8"))
                    self.assertEqual(1_000_000_000, os.stat(nfo).st_mtime_ns, "the file was rewritten")

    def test_update_nfo_tags_reports_no_write_when_it_cannot_insert(self):
        for name, body in FOREIGN.items():
            for owned in (OWNED, None):
                with self.subTest(name, owned=owned):
                    nfo = self._nfo(body)
                    self.assertFalse(update_nfo_tags(nfo, TAGS, owned))
                    self.assertEqual(body, nfo.read_text(encoding="utf-8"))
                    self.assertEqual(1_000_000_000, os.stat(nfo).st_mtime_ns, "the file was rewritten")

    def test_an_nfo_with_the_closing_tag_still_gets_its_tags(self):
        for root in ("movie", "tvshow"):
            with self.subTest(root):
                nfo = self._nfo(f"<?xml version=\"1.0\"?>\n<{root}>\n  <title>The Matrix</title>\n</{root}>\n")
                self.assertTrue(refresh_arr_nfo(nfo, write_movie_nfo, META, TAGS, OWNED))
                self.assertIn(f"  <tag>Downloaded Movies</tag>\n</{root}>", nfo.read_text(encoding="utf-8"))
                self.assertFalse(refresh_arr_nfo(nfo, write_movie_nfo, META, TAGS, OWNED))


if __name__ == "__main__":
    unittest.main()

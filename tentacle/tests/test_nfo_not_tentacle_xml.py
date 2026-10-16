"""An NFO beside a download that is not Tentacle's XML is never counted as
written (#628, follow-up to #266).

Radarr's/Sonarr's metadata-URL NFO holds only links, and a release .nfo kept
with the video's name is plain text: neither has </movie> or </tvshow>, so
update_nfo_tags had nowhere to put the tags, wrote the same bytes back and
returned True. Every Radarr/Sonarr scan (every webhook runs one) then counted
nfo_written > 0 and triggered a full Jellyfin library scan.

Run from the tentacle/ directory:  python -m unittest tests.test_nfo_not_tentacle_xml
"""
import os
import shutil
import unittest
from pathlib import Path

from services.nfo import refresh_arr_nfo, update_nfo_tags, write_movie_nfo, write_series_nfo
from tmp_dirs import temp_dir

OWNED = {"Recently Added Movies", "Downloaded Movies", "Downloaded TV"}
META = {"title": "The Matrix", "tmdb_id": 603, "year": "1999"}


class NotTentacleXml(unittest.TestCase):
    def setUp(self):
        self.dir = Path(temp_dir(self))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _file(self, name, body):
        p = self.dir / name
        p.write_text(body, encoding="utf-8")
        return p, os.stat(p).st_mtime_ns

    def test_a_metadata_url_nfo_is_left_alone_and_not_counted(self):
        body = "https://www.themoviedb.org/movie/603\nhttps://www.imdb.com/title/tt0133093\n"
        nfo, mtime = self._file("The Matrix (1999).nfo", body)
        for _ in range(2):   # the first scan and every one after it
            self.assertFalse(refresh_arr_nfo(nfo, write_movie_nfo, META, ["Downloaded Movies"], OWNED))
        self.assertEqual(body, nfo.read_text(encoding="utf-8"))
        self.assertEqual(mtime, os.stat(nfo).st_mtime_ns, "the file is not even rewritten")

    def test_a_release_nfo_with_the_videos_name_is_not_counted(self):
        body = "RELEASE: The.Matrix.1999.720p-GRP\n\nRUNTiME: 2h16m\n"
        nfo, _ = self._file("The.Matrix.1999.720p-GRP.nfo", body)
        self.assertFalse(update_nfo_tags(nfo, ["Downloaded Movies"], OWNED))
        self.assertEqual(body, nfo.read_text(encoding="utf-8"))

    def test_a_series_metadata_url_nfo_is_not_counted(self):
        body = "https://www.thetvdb.com/?tab=series&id=81189\n"
        nfo, _ = self._file("tvshow.nfo", body)
        self.assertFalse(refresh_arr_nfo(nfo, write_series_nfo, {"title": "Breaking Bad", "tmdb_id": 1396},
                                         ["Downloaded TV"], OWNED))
        self.assertEqual(body, nfo.read_text(encoding="utf-8"))

    def test_tentacle_xml_still_gets_its_tags(self):
        nfo, _ = self._file("Heat (1995).nfo", "<?xml version='1.0' encoding='utf-8'?>\n<movie>\n"
                                                "  <title>Heat</title>\n</movie>\n")
        self.assertTrue(update_nfo_tags(nfo, ["Downloaded Movies"], OWNED))
        self.assertIn("<tag>Downloaded Movies</tag>", nfo.read_text(encoding="utf-8"))
        self.assertFalse(update_nfo_tags(nfo, ["Downloaded Movies"], OWNED), "and a re-scan writes nothing")

    def test_a_foreign_tag_is_still_removed_from_a_file_without_owned(self):
        """Without `owned` every <tag> is replaced, as before."""
        nfo, _ = self._file("Dune (2021).nfo", "<movie>\n  <tag>old</tag>\n</movie>\n")
        self.assertTrue(update_nfo_tags(nfo, ["new"]))
        text = nfo.read_text(encoding="utf-8")
        self.assertIn("<tag>new</tag>", text)
        self.assertNotIn("<tag>old</tag>", text)


if __name__ == "__main__":
    unittest.main()

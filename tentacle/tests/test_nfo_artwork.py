"""A title kept without a TMDB match shows the provider's picture.

With "Require TMDB match" off, a film or show TMDB doesn't know is kept under
the provider's name and picture: poster_path is the provider's own image, a
full URL. The NFO writer put TMDB's image host in front of every poster, so
Jellyfin got https://image.tmdb.org/t/p/w500http://provider/... and showed no
image. The writer now uses a full URL as it is, and each sync repairs the NFOs
written before.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import services.sync as sync
from models.database import Movie, Series
from nightly_harness import FakeClient, NightlyHarness
from services.nfo import repair_artwork, write_movie_nfo, write_series_nfo
from tmp_dirs import temp_dir

ICON = "http://provider.example/images/cosmic.jpg?w=300&h=450"


def _thumbs(nfo: Path) -> list:
    return [t.text for t in ET.parse(nfo).getroot().iter("thumb")]


class TheWriter(unittest.TestCase):
    def setUp(self):
        self.dir = Path(temp_dir(self))

    def test_a_full_url_is_used_as_it_is(self):
        nfo = self.dir / "movie.nfo"
        write_movie_nfo(nfo, {"title": "Cosmic Fury", "poster_path": ICON, "backdrop_path": ICON}, [])
        self.assertEqual([ICON, ICON], _thumbs(nfo))

    def test_a_show_too(self):
        nfo = self.dir / "tvshow.nfo"
        write_series_nfo(nfo, {"title": "Cosmic Fury", "poster_path": ICON}, [])
        self.assertEqual([ICON], _thumbs(nfo))

    def test_a_tmdb_path_gets_tmdbs_image_host(self):
        nfo = self.dir / "movie.nfo"
        write_movie_nfo(nfo, {"title": "Heat", "poster_path": "/abc.jpg", "backdrop_path": "/def.jpg"}, [])
        self.assertEqual(["https://image.tmdb.org/t/p/w500/abc.jpg", "https://image.tmdb.org/t/p/w1280/def.jpg"],
                         _thumbs(nfo))


class TheRepair(unittest.TestCase):
    def setUp(self):
        self.nfo = Path(temp_dir(self)) / "movie.nfo"

    def test_a_dead_link_from_before_the_fix_gets_the_providers_picture(self):
        self.nfo.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<movie>\n  <title>Cosmic Fury</title>\n'
                            f'  <thumb aspect="poster">https://image.tmdb.org/t/p/w500{ICON}</thumb>\n'
                            f'  <fanart><thumb>https://image.tmdb.org/t/p/w1280{ICON}</thumb></fanart>\n'
                            '</movie>', encoding="utf-8")
        self.assertTrue(repair_artwork(self.nfo))
        self.assertEqual([ICON, ICON], _thumbs(self.nfo))
        self.assertIn("<title>Cosmic Fury</title>", self.nfo.read_text(encoding="utf-8"))

    def test_a_good_file_is_left_alone(self):
        write_movie_nfo(self.nfo, {"title": "Heat", "poster_path": "/abc.jpg"}, [])
        before = self.nfo.read_text(encoding="utf-8")
        self.assertFalse(repair_artwork(self.nfo))
        self.assertEqual(before, self.nfo.read_text(encoding="utf-8"))

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(repair_artwork(self.nfo))


class _IconClient(FakeClient):
    def get_vod_streams(self, cat_id):
        return [dict(s, stream_icon=ICON) for s in super().get_vod_streams(cat_id)]

    def get_series_list(self, cat_id):
        return [dict(s, cover=ICON) for s in super().get_series_list(cat_id)]


class TheSync(NightlyHarness):
    def setUp(self):
        super().setUp()
        self.provider.require_tmdb_match = False
        self.db.commit()
        self.client = _IconClient(movies={"1": [("Cosmic Fury", 11)]}, series={"s1": [("Cosmic Show", 22)]})
        sync.make_provider_client = lambda provider: self.client
        self.add_category("1")
        self.add_category("s1", type_="series")

    def _nfos(self):
        movie = self.db.query(Movie).one()
        series = self.db.query(Series).one()
        return Path(movie.nfo_path or Path(movie.strm_path).with_suffix(".nfo")), Path(series.strm_path) / "tvshow.nfo"

    def test_an_unmatched_title_gets_the_providers_picture(self):
        self.sync_only()
        for nfo in self._nfos():
            self.assertEqual([ICON], _thumbs(nfo), nfo.name)

    def test_the_next_sync_repairs_an_nfo_written_before_the_fix(self):
        self.sync_only()
        for nfo in self._nfos():
            text = nfo.read_text(encoding="utf-8")
            nfo.write_text(text.replace(f">{ICON.replace('&', '&amp;')}<",
                                        f">https://image.tmdb.org/t/p/w500{ICON}<"), encoding="utf-8")
        with mock.patch("services.sync.chown_path", lambda p: None):
            self.sync_only()
        for nfo in self._nfos():
            self.assertEqual([ICON], _thumbs(nfo), nfo.name)


if __name__ == "__main__":
    unittest.main()

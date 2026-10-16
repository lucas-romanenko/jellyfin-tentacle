"""A film is deleted when the provider relabels its stream onto the year of
another film with the same title that is also in the library (two remakes, #627).

Heat (1986) plays stream 500 and Heat (1995) plays stream 600. The provider
relabels stream 500 "Heat (1995)". Stream 500 is still listed, and Tentacle's
own .strm for Heat (1986) plays it, so by the #185/#262 rule ("a stream stays
with the film whose .strm plays it") Heat (1986) should stay. The label went
to Heat (1995) through the title map's fast path, nothing placed stream 500,
and two syncs later Heat (1986) was pruned: its .strm, its Jellyfin item and
every user's watched state on it gone.

Run from tentacle/: python -m unittest tests.test_issue_relabel_onto_own_remake
"""
import unicodedata
import unittest
from pathlib import Path

import services.sync as sync
from models.database import Movie
from test_vod_namesakes import TMDB, stream
from test_vod_relisted_streams import Relisted, sync_meta, FILLER


def _fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).casefold()


class YearAwareTMDB(TMDB):
    """TMDB search the way the real one answers: an exact year first, then +-1."""
    def search_movie(self, name, year=None, **k):
        TMDB.calls.append(("search", name, year))
        same = [(tid, t, y) for tid, (t, y) in TMDB.films.items() if _fold(t) == _fold(name)]
        exact = [f for f in same if year and f[2] == str(year)]
        near = [f for f in same if year and abs(int(f[2]) - int(year)) <= 1]
        pick = exact or near or ([] if year else same)
        return sync_meta(*pick[0]) if pick else None


class RelabelOntoOwnRemake(Relisted):
    def setUp(self):
        super().setUp()
        sync.TMDBService = YearAwareTMDB
        TMDB.films[100] = ("Heat", "1986")
        TMDB.films[101] = ("Heat", "1995")

    def listing(self, *streams):
        self.client.movies = {"a": list(streams) + [stream(f"{n} (2010)", sid) for n, sid in FILLER]}

    def test_the_film_whose_file_plays_the_stream_is_kept(self):
        self.listing(stream("Heat (1986)", 500), stream("Heat (1995)", 600))
        self.night()
        heat_1986 = self.db.query(Movie).filter(Movie.tmdb_id == 100).one()
        self.assertIn("/500.", Path(heat_1986.strm_path).read_text())

        # The provider relabels stream 500; both streams are still listed.
        self.listing(stream("Heat (1995)", 500), stream("Heat (1995)", 600))
        self.night()
        self.night()
        self.db.expire_all()
        kept = self.db.query(Movie).filter(Movie.tmdb_id == 100).first()
        self.assertIsNotNone(kept, "Heat (1986) was pruned although its own .strm plays stream 500, "
                                   "which is still listed")
        self.assertIn("/500.", Path(kept.strm_path).read_text())
        heat_1995 = self.db.query(Movie).filter(Movie.tmdb_id == 101).one()
        self.assertIn("/600.", Path(heat_1995.strm_path).read_text())


if __name__ == "__main__":
    unittest.main()

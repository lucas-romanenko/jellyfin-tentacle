"""A stream relabelled onto the year of another film of ours with the same
title (a remake) stays with the film whose .strm plays it (#627, follow-up to
#262/#313).

Heat (1986) plays stream 500, Heat (1995) plays 600. When the provider labels
500 "Heat (1995)", the title map knew one film for that label and placed 500
there without asking which film plays it: Heat (1986) went unlisted and was
pruned two syncs later, with its Jellyfin item and every user's data on it.

Run from the tentacle/ directory:  python -m unittest tests.test_vod_relabel_onto_own_remake
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


class _Remakes(Relisted):
    def setUp(self):
        super().setUp()
        sync.TMDBService = YearAwareTMDB
        TMDB.films[100] = ("Heat", "1986")
        TMDB.films[101] = ("Heat", "1995")

    def listing(self, *streams):
        self.client.movies = {"a": list(streams) + [stream(f"{n} (2010)", sid) for n, sid in FILLER]}

    def plays(self, tmdb_id):
        """The stream id the film's .strm plays, or None when the film is gone."""
        self.db.expire_all()
        row = self.db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
        if row is None:
            return None
        return int(Path(row.strm_path).read_text().rsplit("/", 1)[-1].split(".")[0])

    def start(self):
        self.listing(stream("Heat (1986)", 500), stream("Heat (1995)", 600))
        self.night()
        self.assertEqual((500, 600), (self.plays(100), self.plays(101)))

    def nights(self, *streams, n=3):
        self.listing(*streams)
        for _ in range(n):
            self.night()


class RelabelOntoOwnRemake(_Remakes):
    def test_the_film_whose_file_plays_the_stream_is_kept(self):
        self.start()
        self.nights(stream("Heat (1995)", 500), stream("Heat (1995)", 600))
        self.assertEqual((500, 600), (self.plays(100), self.plays(101)))

    def test_both_directions(self):
        self.start()
        self.nights(stream("Heat (1986)", 500), stream("Heat (1986)", 600))
        self.assertEqual((500, 600), (self.plays(100), self.plays(101)))

    def test_nothing_changes_without_a_relabel(self):
        self.start()
        self.nights(stream("Heat (1986)", 500), stream("Heat (1995)", 600))
        self.assertEqual((500, 600), (self.plays(100), self.plays(101)))

    def test_a_second_listing_of_the_labelled_film_stays_with_it(self):
        """601 is a second copy of Heat (1995); no film of ours plays it."""
        self.start()
        self.nights(stream("Heat (1986)", 500), stream("Heat (1995)", 600), stream("Heat (1995)", 601))
        self.assertEqual((500, 600), (self.plays(100), self.plays(101)))

    def test_a_provider_tmdb_id_naming_the_labelled_film_is_still_followed(self):
        """With a hint the provider says which film it is: as before."""
        self.start()
        self.nights(stream("Heat (1995)", 500, 101), stream("Heat (1995)", 600, 101))
        self.assertIsNone(self.plays(100), "the hinted label decides, as before")
        self.assertEqual(600, self.plays(101))

    def test_the_relabelled_stream_stays_with_its_film_when_the_other_one_is_delisted(self):
        """Stream 600 is gone and 500 now says "Heat (1995)": the film that plays
        500 keeps it (#185); Heat (1995), whose stream left, goes as any
        delisted film does."""
        self.start()
        self.nights(stream("Heat (1995)", 500))
        self.assertEqual(500, self.plays(100))
        self.assertIsNone(self.plays(101))


class OneFilmOfTheTitle(_Remakes):
    def setUp(self):
        super().setUp()
        del TMDB.films[100]

    def test_a_single_film_is_untouched(self):
        self.listing(stream("Heat (1995)", 600))
        self.night()
        self.nights(stream("Heat (1995)", 600), stream("Heat (1995)", 601))
        self.assertEqual(600, self.plays(101))


if __name__ == "__main__":
    unittest.main()

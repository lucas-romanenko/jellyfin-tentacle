"""A relabelled film keeps its row when the new label names a namesake, or when
it is re-listed under a new id with its own TMDB id (follow-up to 95a2b53, #262/#263).

Run from the tentacle/ directory:  python -m unittest discover -s tests

95a2b53 keeps a relabelled film when the new label finds no TMDB match. Two
cases still lost it:
- the new label's year is one where TMDB has ANOTHER film of that title: the
  lookup matches that film, it is imported playing our stream, and ours is
  pruned (a new Jellyfin item: every user's watched state goes with it);
- the film is re-uploaded under a new stream id with a label too far off for
  the lookup, but the provider's TMDB id names it: pruned, never re-imported.

Built on the #262/#263 harness (tests/test_vod_relisted_streams.py). No network.
"""
import unicodedata
import unittest
from pathlib import Path as _RealPath
from urllib.parse import quote

import services.sync as sync
from models.database import Movie
from test_vod_namesakes import TMDB, stream
from test_vod_relisted_streams import Relisted, sync_meta, FILLER


def _fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).casefold()


class NamesakeTMDB(TMDB):
    """Like TMDB's search: with a year, the film of that title released that
    year (a namesake if there is one), else one a year off (#265); without a
    year, the first of that title."""

    def search_movie(self, name, year=None, **k):
        TMDB.calls.append(("search", name, year))
        same = [(tid, t, y) for tid, (t, y) in TMDB.films.items() if _fold(t) == _fold(name)]
        exact = [f for f in same if year and f[2] == str(year)]
        near = [f for f in same if year and abs(int(f[2]) - int(year)) <= 1]
        pick = exact or near or ([] if year else same)
        return sync_meta(*pick[0]) if pick else None


class _Base(Relisted):
    def setUp(self):
        super().setUp()
        sync.TMDBService = NamesakeTMDB

    def cat(self, *streams):
        self.client.movies = {"a": list(streams) + [stream(f"{n} (2010)", sid) for n, sid in FILLER]}

    def rows(self, *ids):
        return {m.tmdb_id: m.strm_path for m in self.db.query(Movie).filter(Movie.tmdb_id.in_(ids))}


class RelabelOntoANamesakesYear(_Base):
    def test_the_film_its_file_plays_is_kept(self):
        TMDB.films[777] = ("Amelie", "2002")        # another film of that title, a year later
        self.cat(stream("Amelie (2001)", 103))
        self.night()
        path = self.row(194).strm_path
        self.cat(stream("Amelie (2002)", 103))       # the provider relabels our film's stream
        self.night(); self.night()
        self.assertEqual(self.rows(194, 777), {194: path})
        self.assertTrue(self.plays(194).endswith("/103.mp4"))

    def test_with_the_films_own_hint(self):
        TMDB.films[777] = ("Amelie", "2002")
        self.cat(stream("Amelie (2001)", 103, 194))
        self.night()
        self.cat(stream("Amelie (2002)", 103, 194))
        self.night(); self.night()
        self.assertEqual(set(self.rows(194, 777)), {194})

    def test_accents_tmdb_keeps_are_ignored(self):
        TMDB.films[194] = ("Amélie", "2001")
        TMDB.films[777] = ("Amelie", "2002")
        self.cat(stream("Amelie (2001)", 103))
        self.night()
        self.cat(stream("Amelie (2002)", 103))
        self.night(); self.night()
        self.assertEqual(set(self.rows(194, 777)), {194})

    def test_a_hand_made_wrapper_is_recognised_and_left_alone(self):
        TMDB.films[777] = ("Amelie", "2002")
        self.cat(stream("Amelie (2001)", 103))
        self.night()
        f = _RealPath(self.row(194).strm_path)
        wrapped = "http://proxy.example:8765/resume?d=" + quote(f.read_text().strip(), safe="") + "&api_password=x"
        f.write_text(wrapped)
        self.cat(stream("Amelie (2002)", 103))
        self.night(); self.night()
        self.assertEqual(set(self.rows(194, 777)), {194})
        self.assertEqual(f.read_text(), wrapped)

    def test_owns_wins_over_a_hint_naming_our_other_namesake(self):
        TMDB.films[300] = ("Psycho", "1960")
        TMDB.films[301] = ("Psycho", "1998")
        self.cat(stream("Psycho (1960)", 500), stream("Psycho (1998)", 600))
        self.night()
        self.cat(stream("Psycho (2003)", 500, 301), stream("Psycho (1998)", 600))
        self.night(); self.night()
        self.assertIn(300, self.rows(300, 301))

    def test_no_lookup_is_needed_so_a_tmdb_outage_changes_nothing(self):
        TMDB.films[777] = ("Amelie", "2002")
        self.cat(stream("Amelie (2001)", 103))
        self.night()
        self.cat(stream("Amelie (2002)", 103))
        calls_before = len(TMDB.calls)
        self.night()
        self.assertFalse([c for c in TMDB.calls[calls_before:] if c[1] == "Amelie"],
                         "the film its file plays needs no lookup")
        self.night()
        self.assertEqual(set(self.rows(194, 777)), {194})


class ReuploadWithItsOwnTmdbId(_Base):
    def test_kept_and_pointed_at_the_new_stream(self):
        self.cat(stream("Amelie (2001)", 103))
        self.night()
        path = self.row(194).strm_path
        self.cat(stream("Amelie (1999)", 1103, 194))  # new id, year two off, the provider's id is the film's
        self.night(); self.night()
        self.assertEqual(self.rows(194), {194: path})
        self.assertTrue(self.plays(194).endswith("/1103.mp4"))

    def test_a_blank_file_gets_the_new_stream(self):
        self.cat(stream("Amelie (2001)", 103))
        self.night()
        _RealPath(self.row(194).strm_path).write_text("")
        self.cat(stream("Amelie (1999)", 1103, 194))
        self.night()
        self.assertTrue(self.plays(194).endswith("/1103.mp4"))

    def test_a_hint_on_another_title_is_ignored(self):
        self.cat(stream("Amelie (2001)", 103))
        self.night()
        self.cat(stream("Delicatessen (1991)", 1103, 194))   # a wrong id on another film
        self.night(); self.night()
        self.assertEqual(self.rows(194), {})


class TheProviderNamesAnotherFilm(_Base):
    """Unchanged from before: a different film, or a corrected label, goes
    through the lookup."""

    def test_a_namesake_with_a_copied_hint_is_still_imported(self):
        TMDB.films[888] = ("Amelie", "2019")
        self.cat(stream("Amelie (2001)", 103, 194))
        self.night()
        self.cat(stream("Amelie (2001)", 103, 194), stream("Amelie (2019)", 2000, 194))
        self.night(); self.night()
        self.assertIn(888, self.rows(194, 888))
        self.assertTrue(self.plays(194).endswith("/103.mp4"))

    def test_a_copied_hint_never_repoints_our_film_to_a_namesake(self):
        TMDB.films[888] = ("Amelie", "2019")
        self.cat(stream("Amelie (2001)", 103, 194))
        self.night()
        self.cat(stream("Amelie (2019)", 2000, 194))
        self.night(); self.night()
        rows = self.rows(194, 888)
        self.assertIn(888, rows)
        self.assertFalse(194 in rows and self.plays(194).endswith("/2000.mp4"))

    def test_a_label_corrected_to_the_remake_with_its_id_imports_the_remake(self):
        TMDB.films[300] = ("Psycho", "1960")
        TMDB.films[301] = ("Psycho", "1998")
        self.cat(stream("Psycho (1960)", 500))
        self.night()
        self.cat(stream("Psycho (1998)", 500, 301))
        self.night(); self.night()
        self.assertIn(301, self.rows(300, 301))


class RemakesOfOneTitle(_Base):
    """Two films of ours share a title (remakes). The label's year names a
    TMDB namesake neither of them is: the stream stays with the one whose
    .strm plays it."""

    def setUp(self):
        super().setUp()
        TMDB.films.update({101: ("Heat", "1986"), 102: ("Heat", "1995"), 777: ("Heat", "2003")})

    def test_relabel_onto_a_namesakes_year(self):
        self.cat(stream("Heat (1986)", 500), stream("Heat (1995)", 600))
        self.night()
        paths = self.rows(101, 102)
        self.cat(stream("Heat (2003)", 500), stream("Heat (1995)", 600))
        self.night(); self.night()
        self.assertEqual(self.rows(101, 102, 777), paths)
        self.assertTrue(self.plays(101).endswith("/500.mp4"))

    def test_with_the_films_own_hint(self):
        self.cat(stream("Heat (1986)", 500, 101), stream("Heat (1995)", 600, 102))
        self.night()
        self.cat(stream("Heat (2003)", 500, 101), stream("Heat (1995)", 600, 102))
        self.night(); self.night()
        self.assertEqual(set(self.rows(101, 102, 777)), {101, 102})

    def test_the_provider_naming_the_namesake_imports_it(self):
        self.cat(stream("Heat (1986)", 500), stream("Heat (1995)", 600))
        self.night()
        self.cat(stream("Heat (2003)", 500, 777), stream("Heat (1995)", 600))
        self.night(); self.night()
        self.assertIn(777, self.rows(101, 102, 777))

    def test_accent_only_namesakes_both_ours(self):
        TMDB.films.update({201: ("Léon", "1994"), 202: ("Leon", "2011"), 778: ("Leon", "2015")})
        self.cat(stream("Léon (1994)", 510), stream("Leon (2011)", 610))
        self.night()
        self.cat(stream("Léon (1994)", 510), stream("Leon (2015)", 610))
        self.night(); self.night()
        self.assertEqual(set(self.rows(201, 202, 778)), {201, 202})


if __name__ == "__main__":
    unittest.main()

"""A re-searched stream keeps the film its .strm already plays (#310).

A stream whose label differs from its film's TMDB title or year ("Dune
(2024)" for the film TMDB calls Dune, 2021) is searched again whenever its
cached answer expires. When the search then finds another film (a matcher
change, TMDB's own answer changing), the stream used to be imported as that
film, and the film it played was pruned two nights later, with every user's
watched state and playlist entries on it. Now the existing row keeps it as
long as the label still fits it; the new answer only reaches new imports. A
stream the label no longer names (a provider reusing its number) or whose
provider id names the new film moves, as before. A label with the film's own
title (any year) whose stream its .strm plays is known without a search
(#262 follow-up); one spelt otherwise still goes through it.

On the #262 harness (real XtreamClient rules, filler so the prune can act).
No network.
"""
import unittest

import services.sync as sync
from test_vod_namesakes import TMDB, stream
from test_vod_relisted_streams import Relisted


class ResearchKeepsItsFilm(Relisted):
    def setUp(self):
        super().setUp()
        sync.TMDBService = TMDB  # name search only: the label's year doesn't filter
        TMDB.films.update({438631: ("Dune", "2021"), 693134: ("Dune: Part Two", "2024"),
                           346698: ("Barbie", "2023")})
        TMDB.search.update({"Dune": 438631, "Barbie": 346698})

    def first_night(self, label="Dune (2024)", hint=None):
        self.catalogue(stream(label, 103, hint))
        self.night()
        path = self.strm(438631)
        self.assertIsNotNone(path)
        self.assertIn("/103.mp4", path.read_text())
        return path

    def test_a_new_answer_for_its_label_leaves_the_row_alone(self):
        # A label spelt unlike its film's title: searched again every time (a
        # label with the film's own title is known by its .strm, no lookup).
        TMDB.films.update({873: ("The Color Purple", "1985"), 558915: ("The Color Purple", "2023")})
        TMDB.search["The Colour Purple"] = 873
        self.catalogue(stream("The Colour Purple (1986)", 103))
        self.night()
        path = self.strm(873)
        self.assertIn("/103.mp4", path.read_text())
        TMDB.search["The Colour Purple"] = 558915  # the search now finds the remake
        self.night()
        run = self.night()
        self.assertIn(("search", "The Colour Purple"), TMDB.calls, "the label was not searched again")
        self.assertIsNotNone(self.strm(873), "the film its .strm plays was pruned")
        self.assertEqual(path, self.strm(873))
        self.assertIn("/103.mp4", path.read_text())
        self.assertIsNone(self.row(873).provider_missing_since)
        self.assertIsNone(self.strm(558915), "the stream was imported a second time as the new answer")
        self.assertEqual(0, run.movies_new)

    def test_a_new_answer_for_the_films_own_title_needs_no_search(self):
        path = self.first_night()
        TMDB.search["Dune"] = 693134  # the search now finds Part Two
        self.night()
        run = self.night()
        self.assertEqual(path, self.strm(438631))
        self.assertIn("/103.mp4", path.read_text())
        self.assertIsNone(self.strm(693134), "the stream was imported a second time as the new answer")
        self.assertEqual(0, run.movies_new)

    def test_a_label_that_no_longer_names_it_moves(self):
        self.first_night()
        self.catalogue(stream("Barbie (2023)", 103))  # the provider reused the number
        self.night()
        self.night()
        self.assertIsNotNone(self.strm(346698))
        self.assertIsNone(self.strm(438631))

    def test_a_provider_id_naming_the_new_film_moves(self):
        self.first_night()
        TMDB.search["Dune"] = 693134
        self.catalogue(stream("Dune (2024)", 103, 693134))
        self.night()
        self.night()
        self.assertIsNotNone(self.strm(693134))

    def test_a_new_stream_gets_the_new_answer(self):
        self.first_night()
        TMDB.search["Dune"] = 693134
        self.catalogue(stream("Dune (2024)", 103), stream("Dune (2024)", 104))
        self.night()
        self.assertIsNotNone(self.strm(693134), "a stream no row plays was not imported")
        self.assertIn("/104.mp4", self.strm(693134).read_text())
        self.assertIn("/103.mp4", self.strm(438631).read_text())


if __name__ == "__main__":
    unittest.main()

"""A label's year picks the film of that year, not an older namesake (#310).

TMDB's search/movie `year` matches ANY release date of a film, re-releases
included, so the first pass (with the provider's year) also brings back an
older film exactly titled as the label: "Dune (2024)" finds Dune (2021), out
again in 2024. The scorer took that exact title over the year's own film
(Dune: Part Two). The same mechanism is what a re-release label relies on
("Alien (2019)" for Alien 1979), where the near-year films are namesakes
nobody watched. Between a far-year exact title and a near-year film, the near
one now wins only with a credible share of the far one's TMDB votes.

The answers below are TMDB's real first-pass answers (top 6), recorded.
"""
import logging
import unittest

from services.tmdb import TMDBService, label_names_film
from tmp_dirs import temp_dir


def _r(i, title, date, votes, popularity):
    return {"id": i, "title": title, "release_date": date, "vote_count": votes, "popularity": popularity}


ANSWERS = {
    ('Dune', '2024'): [
        _r(438631, 'Dune', '2021-09-15', 15667, 41.3),
        _r(693134, 'Dune: Part Two', '2024-02-27', 8691, 60.6),
        _r(915935, 'Anatomy of a Fall', '2023-08-23', 3511, 18.1),
        _r(1274770, 'The Dune', '2025-03-23', 0, 0.5),
        _r(1255476, 'Dui Dune Panch', '2024-03-06', 0, 0.6),
        _r(191720, "Jodorowsky's Dune", '2013-08-30', 782, 3.1),
    ],
    ('Top Gun', '2022'): [
        _r(361743, 'Top Gun: Maverick', '2022-05-21', 11662, 49.4),
        _r(744, 'Top Gun', '1986-05-16', 9965, 20.6),
        _r(976632, 'The Real Top Gun', '2022-05-22', 3, 0.9),
        _r(1076135, 'Top Gun Maverick : Le phénomène', '2022-12-23', 6, 0.9),
        _r(978444, 'Top Gun : les coulisses du grand retour', '2022-05-19', 6, 0.8),
        _r(984430, "James Corden's Top Gun Training with Tom Cruise", '2022-06-01', 15, 1.3),
    ],
    ('Joker', '2024'): [
        _r(475557, 'Joker', '2019-10-01', 28451, 34.8),
        _r(889737, 'Joker: Folie à Deux', '2024-10-01', 3090, 17.8),
        _r(808482, "The People's Joker", '2024-04-05', 52, 2.6),
        _r(1319582, 'The Wedding of Vera Drew & The Joker', '2024-07-24', 0, 1.3),
        _r(1225120, 'Joker Out - Live from Arena Stožice', '2024-01-01', 0, 1.5),
        _r(1013247, 'Mirza: Part 1 - Joker', '2024-04-11', 2, 0.9),
    ],
    ('Alien', '2019'): [
        _r(348, 'Alien', '1979-05-25', 16931, 42.9),
        _r(529358, 'Alien Code', '2017-04-23', 90, 1.8),
        _r(574037, 'Crazy Alien', '2019-02-05', 82, 2.0),
        _r(593035, 'Alien Warfare', '2019-04-05', 154, 2.3),
        _r(596440, 'Alien', '2019-05-21', 2, 1.1),
        _r(539498, 'Alien Siege', '2018-07-31', 27, 1.5),
    ],
    ('Titanic', '2023'): [
        _r(597, 'Titanic', '1997-12-18', 27852, 39.7),
        _r(945657, 'Titanic 666', '2022-04-15', 210, 4.4),
        _r(1124589, 'Titanic', '2023-02-07', 9, 2.7),
        _r(1181637, 'Titanic: The Musical', '2023-11-04', 5, 1.1),
        _r(1174889, 'Titanic Cobh', '2023-09-01', 0, 0.7),
        _r(1237162, 'Titanic: Stories from the Heart', '2023-12-05', 4, 1.8),
    ],
    ('Psycho', '1998'): [
        _r(539, 'Psycho', '1960-06-22', 11314, 24.6),
        _r(11252, 'Psycho', '1998-12-04', 1078, 4.4),
        _r(116641, 'Psycho Sisters', '1998-01-01', 18, 2.0),
        _r(894702, 'Psycho Musical Dementia Night', '1998-08-08', 0, 1.2),
        _r(88362, 'The Dark Angel: Psycho Kickboxer', '1998-06-28', 6, 0.9),
        _r(223323, 'Psychopath', '1998-09-22', 6, 1.3),
    ],
    ('Mulan', '2020'): [
        _r(10674, 'Mulan', '1998-06-18', 10721, 21.3),
        _r(337401, 'Mulan', '2020-09-04', 7171, 11.0),
        _r(752662, 'Hua Mulan', '2020-09-09', 18, 1.5),
        _r(664345, 'Mulan: Princess Warrior', '2020-10-03', 9, 1.6),
        _r(993998, 'Matchless Mulan', '2020-05-07', 7, 1.2),
        _r(32909, 'Mulan: Rise of a Warrior', '2009-11-26', 461, 3.1),
    ],
    ('Gladiator', '2024'): [
        _r(98, 'Gladiator', '2000-05-04', 21609, 32.7),
        _r(558449, 'Gladiator II', '2024-11-13', 4789, 26.7),
        _r(1380755, 'The Making of Gladiator II', '2024-11-18', 1, 1.7),
        _r(1297397, 'Gladiator: The Real Story', '2024-08-01', 0, 1.2),
        _r(1392821, 'Gladiators', '2024-11-14', 7, 1.5),
        _r(1715981, 'Shoot Me: The Real Story of the Italian Texas Gladiators', '2024-11-26', 1, 1.3),
    ],
    ('Jaws', '2022'): [
        _r(578, 'Jaws', '1975-06-20', 11911, 25.8),
        _r(17692, 'Jaws 3-D', '1983-07-22', 1464, 7.6),
        _r(988337, 'Counting Jaws', '2022-07-10', 4, 2.4),
        _r(1505727, 'Inside Jaws', '2022-06-19', 0, 0.5),
        _r(1005060, 'Jaws vs. Kraken', '2022-07-25', 2, 1.2),
        _r(988365, 'Jaws Invasion', '2022-07-19', 1, 0.4),
    ],
}


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class Recorded(TMDBService):
    """TMDB answering the first pass with a recorded answer (no network)."""

    def __init__(self, cache_dir):
        super().__init__("token", cache_dir)
        self.calls = []

    def _request(self, endpoint, params=None):
        params = dict(params or {})
        self.calls.append((endpoint, params))
        if endpoint == "search/movie":
            return {"results": ANSWERS.get((params["query"], params.get("year")), [])}
        return None

    def get_movie_details(self, i, **k):
        return {"tmdb_id": i}


class FirstPassYear(unittest.TestCase):
    def found(self, title, year):
        tmdb = Recorded(temp_dir(self))
        hit = tmdb.search_movie(title, year, strict=True)
        self.assertIsNotNone(hit, tmdb.calls)
        self.assertEqual(1, len(tmdb.calls), "the first pass decides")
        return hit["tmdb_id"]

    def test_a_part_under_its_base_title_is_the_film_of_that_year(self):
        self.assertEqual(693134, self.found("Dune", "2024"))      # Dune: Part Two, not Dune (2021)
        self.assertEqual(361743, self.found("Top Gun", "2022"))   # Top Gun: Maverick, not Top Gun (1986)
        self.assertEqual(889737, self.found("Joker", "2024"))     # Joker: Folie à Deux, not Joker (2019)

    def test_a_re_release_label_keeps_the_film_it_names(self):
        self.assertEqual(348, self.found("Alien", "2019"))        # not a 2-vote "Alien" of 2019
        self.assertEqual(597, self.found("Titanic", "2023"))      # not a 9-vote "Titanic" of 2023
        self.assertEqual(578, self.found("Jaws", "2022"))

    def test_a_remake_of_that_year_is_still_the_remake(self):
        self.assertEqual(11252, self.found("Psycho", "1998"))     # 9.5 % of the 1960 film's votes
        self.assertEqual(337401, self.found("Mulan", "2020"))
        self.assertEqual(558449, self.found("Gladiator", "2024"))

    def test_without_a_far_exact_title_nothing_changes(self):
        tmdb = Recorded(temp_dir(self))
        near = [r for r in ANSWERS[("Dune", "2024")] if r["id"] != 438631]
        self.assertEqual(693134, tmdb._find_best_match(near, "Dune", "2024", "title", "release_date")[0])
        self.assertEqual(438631, tmdb._find_best_match(ANSWERS[("Dune", "2024")], "Dune", None,
                                                       "title", "release_date")[0])


class LabelNamesFilm(unittest.TestCase):
    def test_the_scorers_own_test(self):
        self.assertTrue(label_names_film("Dune", "2024", "Dune", "2021"))
        self.assertTrue(label_names_film("Dune", "2024", "Dune: Part Two", "2024"))
        self.assertFalse(label_names_film("Barbie", "2023", "Dune", "2021"))
        self.assertFalse(label_names_film("Dune", "2024", None, None))


if __name__ == "__main__":
    unittest.main()

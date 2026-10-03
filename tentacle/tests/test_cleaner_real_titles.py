"""Real catalogue titles that clean_title() rejected or mangled.

clean_title() turns a provider label into the name and year searched on TMDB.
It threw a name away when the first word had no a/e/i/o/u ("Psych", "Flynn",
"Rhythm + Flow") or the name started with a lower-case letter ("iCarly",
"eXistenZ", "mother!"); step 1 took any short bracketed word for a service tag
("[REC] (2007)" -> nothing, "[REC]³ Genesis" -> "³ Genesis"); and step 6 read a
number after a colon as the year ("Space: 1999" -> "Space", 1999).

A rejected name is skipped by the VOD sync before any lookup, on every sync,
with nothing in the log but a skip count. A mangled name is searched under the
wrong words: on a live install "Space: 1999" is filed as the sitcom "Spaced"
(1999), and "[REC]³ Genesis" as a 2018 film called "Genesis".

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

from models.database import Movie, Series
from nightly_harness import NightlyHarness, FakeTMDB
from services.cleaner import clean_title


class TestRealTitlesAreNotRejected(unittest.TestCase):
    def test_y_is_a_vowel(self):
        for raw, title, year in (("Psych (2006)", "Psych", "2006"),
                                 ("Flynn (2012)", "Flynn", "2012"),
                                 ("Rhythm + Flow (2019)", "Rhythm + Flow", "2019"),
                                 ("Psych: The Movie (2017)", "Psych: The Movie", "2017")):
            with self.subTest(raw=raw):
                self.assertEqual(clean_title(raw), (title, year))

    def test_titles_that_start_lower_case(self):
        for raw, title, year in (("iCarly (2007)", "iCarly", "2007"),
                                 ("eXistenZ (1999)", "eXistenZ", "1999"),
                                 ("mother! (2017)", "mother!", "2017"),
                                 ("EN - mid90s (2018)", "mid90s", "2018"),
                                 ("[4K] tick, tick... BOOM! (2021)", "tick, tick... BOOM!", "2021")):
            with self.subTest(raw=raw):
                self.assertEqual(clean_title(raw), (title, year))

    def test_still_rejected(self):
        # A label with no title left is still no title.
        self.assertEqual(clean_title("(2011)"), (None, None))
        self.assertEqual(clean_title("EN - X (2011)"), (None, None))
        self.assertEqual(clean_title("[NF] X (2011)"), (None, None))
        # A lower-case name the ranking step cut out of a longer title.
        self.assertEqual(clean_title("12.to.Midnight.2024.1080p.WEB-DL"), (None, None))
        self.assertEqual(clean_title("Shhhhh (2011)"), (None, None))


class TestBracketedTitleWordsKept(unittest.TestCase):
    def test_rec_is_the_title_not_a_service_tag(self):
        self.assertEqual(clean_title("[REC] (2007)"), ("[REC]", "2007"))
        self.assertEqual(clean_title("[REC] 2 (2009)"), ("[REC] 2", "2009"))

    def test_rec_sequel_keeps_its_name(self):
        # A live install matches what was left ("³ Genesis") to "Genesis" (2018).
        self.assertEqual(clean_title("[REC]³ Genesis"), ("[REC]³ Genesis", None))
        self.assertEqual(clean_title("[REC]⁴ Apocalypse (2014)"), ("[REC]⁴ Apocalypse", "2014"))

    def test_service_tags_in_brackets_still_stripped(self):
        self.assertEqual(clean_title("[HBO] Game of Thrones (2011)"), ("Game of Thrones", "2011"))
        self.assertEqual(clean_title("(AMZ) The Boys (2019)"), ("The Boys", "2019"))
        self.assertEqual(clean_title("[PL] Heat (1995)"), ("Heat", "1995"))
        self.assertEqual(clean_title("[4K] 1917 (2019)"), ("1917", "2019"))

    def test_leading_number_in_brackets_unchanged(self):
        # Can't be told from a rank tag ("(25) The Godfather"), and it already
        # finds the right film: left as it was, so no existing row moves.
        self.assertEqual(clean_title("(500) Days of Summer (2009)"), ("Days of Summer", "2009"))


class TestNumberInTitleIsNotAYear(unittest.TestCase):
    def test_number_after_a_colon_is_the_title(self):
        # A live install searched "Space" in 1999 and filed the show as "Spaced".
        self.assertEqual(clean_title("Space: 1999"), ("Space: 1999", None))
        self.assertEqual(clean_title("Fear Street: 1978"), ("Fear Street: 1978", None))
        self.assertEqual(clean_title("EN - Breakdown: 1975"), ("Breakdown: 1975", None))

    def test_scene_year_still_read(self):
        self.assertEqual(clean_title("Dune Part Two 2024 2160p UHD BluRay"), ("Dune Part Two", "2024"))
        self.assertEqual(clean_title("Space: 1999 (1975)"), ("Space: 1999", "1975"))
        self.assertEqual(clean_title("Some Movie 2019"), ("Some Movie", "2019"))
        self.assertEqual(clean_title("Some Movie - 2019"), ("Some Movie", "2019"))
        self.assertEqual(clean_title("NTSF:SD:SUV:: 2011"), ("NTSF:SD:SUV", "2011"))


# ── Sync level ─────────────────────────────────────────────────────────────

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class _TMDBLike(FakeTMDB):
    """Finds a title whatever its brackets or case, like TMDB's own search, and
    answers with TMDB's title and year. ``catalog``: normalised query ->
    (tmdb id, title, year)."""
    catalog = {}

    def _meta(self, name, year):
        hit = _TMDBLike.catalog.get(_norm(name))
        if hit is None:
            return None
        tmdb_id, title, tmdb_year = hit
        return {"tmdb_id": tmdb_id, "title": title, "year": tmdb_year, "overview": "",
                "genres": [], "poster_path": None, "backdrop_path": None,
                "rating": None, "runtime": None}


class _RawNameClient:
    """Xtream client that returns the provider's labels verbatim."""

    def __init__(self, movies, series):
        self.movies, self.series = movies, series

    def get_vod_streams(self, cat_id):
        return [{"name": raw, "stream_id": sid, "container_extension": "mp4"}
                for raw, sid in self.movies.get(cat_id, [])]

    def movie_stream_url(self, stream_id, ext):
        return f"http://provider/movie/u/p/{stream_id}.{ext}"

    def get_series_list(self, cat_id):
        return [{"name": raw, "series_id": sid} for raw, sid in self.series.get(cat_id, [])]

    def get_series_info(self, series_id):
        return {"episodes": {"1": [{"id": series_id * 10 + 1, "episode_num": 1,
                                    "container_extension": "mp4"}]}}

    def episode_stream_url(self, ep_id, ext):
        return f"http://provider/series/u/p/{ep_id}.{ext}"


class _SyncHarness(NightlyHarness):
    def setUp(self):
        super().setUp()
        import services.sync as sync
        self.sync = sync
        sync.TMDBService = _TMDBLike
        sync.make_provider_client = lambda provider: self.client
        self.add_category("m1", "movie")
        self.add_category("s1", "series")


class TestSyncImportsTheseTitles(_SyncHarness):
    def setUp(self):
        super().setUp()
        self.client = _RawNameClient(
            movies={"m1": [("Flynn (2012)", 501), ("eXistenZ (1999)", 502),
                           ("mother! (2017)", 503), ("[REC] (2007)", 504),
                           ("Other Film (2001)", 505)]},
            series={"s1": [("Psych (2006)", 601), ("iCarly (2007)", 602),
                           ("Other Show (2001)", 603)]})
        _TMDBLike.catalog = {_norm(t): (i, t, y) for t, i, y in (
            ("Flynn", 1001, "2012"), ("eXistenZ", 1002, "1999"), ("mother!", 1003, "2017"),
            ("[REC]", 1004, "2007"), ("Other Film", 1005, "2001"),
            ("Psych", 2001, "2006"), ("iCarly", 2002, "2007"), ("Other Show", 2003, "2001"))}

    def test_every_listed_title_is_imported(self):
        self.night()
        self.night()
        movies = {m.tmdb_id for m in self.db.query(Movie).all()}
        series = {s.tmdb_id for s in self.db.query(Series).all()}
        self.assertIn(1005, movies, "control film missing: the harness is broken")
        self.assertIn(2003, series, "control show missing: the harness is broken")
        missing = sorted(t for i, t, _ in _TMDBLike.catalog.values() if i not in movies | series)
        self.assertEqual(missing, [], f"listed every night, never imported: {missing}")


# What main's clean_title returned for these labels (measured on 98dd24b).
_MAIN = {
    "Space: 1999": ("Space", "1999"),
    "[REC]³ Genesis": ("³ Genesis", None),
    "Psych (2006)": (None, None),
    "(500) Days of Summer (2009)": ("Days of Summer", "2009"),
    "Top Gun (1986)": ("Top Gun", "1986"),
}


class TestUpgrade(_SyncHarness):
    """An install that synced with main's cleaner, then upgrades: titles
    matched right never move (same row, same .strm, same Jellyfin item); the
    two titles main filed under the wrong film get their own, and the wrong
    rows go the way of any title the provider no longer lists (two syncs)."""

    def setUp(self):
        super().setUp()
        self.client = _RawNameClient(
            movies={"m1": [("[REC]³ Genesis", 701), ("(500) Days of Summer (2009)", 702),
                           ("Top Gun (1986)", 703)]},
            series={"s1": [("Space: 1999", 801), ("Psych (2006)", 802)]})
        _TMDBLike.catalog = {
            # TMDB's fuzzy search, as seen on the live install
            "space": (745, "Spaced", "1999"),
            "genesis": (500793, "Genesis", "2018"),
            "daysofsummer": (19913, "(500) Days of Summer", "2009"),
            # the real titles
            "space1999": (1695, "Space: 1999", "1975"),
            "recgenesis": (23963, "[REC]³ Genesis", "2012"),
            "500daysofsummer": (19913, "(500) Days of Summer", "2009"),
            "topgun": (744, "Top Gun", "1986"),
            "psych": (1447, "Psych", "2006"),
        }

    def _with_main_cleaner(self):
        real = self.sync.clean_title
        self.sync.clean_title = lambda raw: _MAIN[raw]
        self.addCleanup(setattr, self.sync, "clean_title", real)
        return real

    def _snapshot(self, model, tmdb_id):
        row = self.db.query(model).filter(model.tmdb_id == tmdb_id).one()
        strm = Path(row.strm_path)
        files = sorted(p.read_text() for p in ([strm] if strm.is_file() else strm.rglob("*.strm")))
        return row.id, row.strm_path, files

    def test_upgrade(self):
        real = self._with_main_cleaner()
        self.night()
        self.night()
        # As on the live install: the wrong films, and no Psych.
        self.assertIsNotNone(self.series_row(745))
        self.assertIsNotNone(self.movie(500793))
        self.assertIsNone(self.series_row(1447))
        right = {(Movie, 19913): self._snapshot(Movie, 19913), (Movie, 744): self._snapshot(Movie, 744)}
        wrong_dirs = [Path(self.series_row(745).strm_path), Path(self.movie(500793).strm_path).parent]

        self.sync.clean_title = real   # the upgrade
        self.night()
        for (model, tid), before in right.items():
            self.assertEqual(self._snapshot(model, tid), before, f"TMDB {tid} moved on upgrade")
        self.assertIsNotNone(self.series_row(1695), "Space: 1999 not imported")
        self.assertIsNotNone(self.movie(23963), "[REC]³ Genesis not imported")
        self.assertIsNotNone(self.series_row(1447), "Psych not imported")
        # The wrong rows are only marked on the first sync (two strikes)...
        self.assertIsNotNone(self.series_row(745).provider_missing_since)
        self.assertIsNotNone(self.movie(500793).provider_missing_since)

        self.night()
        for (model, tid), before in right.items():
            self.assertEqual(self._snapshot(model, tid), before, f"TMDB {tid} moved on upgrade")
        # ...and removed with their files on the second.
        self.assertIsNone(self.series_row(745))
        self.assertIsNone(self.movie(500793))
        for d in wrong_dirs:
            self.assertFalse(d.exists(), f"{d.name} left behind")
        self.assertIsNotNone(self.series_row(1695))
        self.assertIsNotNone(self.movie(23963))


if __name__ == "__main__":
    unittest.main()

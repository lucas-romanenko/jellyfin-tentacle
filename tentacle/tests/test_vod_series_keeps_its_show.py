"""A provider show that Tentacle already imported must keep its row and its
folder when TMDB's answer for the listing changes later.

_sync_series (services/sync.py) searches TMDB for a listing whenever the
listing's (clean name, year) differs from the stored TMDB title and year
(a label with "US"/"(UK)" or without a year, a different year), and then
trusts the answer's tmdb_id: when it is not this provider's row it imports a
NEW show (new folder, new .strm files), and the old row -- no longer listed
under its id -- is pruned with its folder. For Jellyfin that is a new series
item: every user's watched/resume state, favourites, Next Up and playlist
entries for the show are gone, the show appears again in Recently Added, and
it can flip back when the TMDB answer (cached 30 days) changes again.

Films have the guard already (`_keeps_its_film`, #310: "an existing row keeps
its film ... so a matcher change reaches new imports only"); shows don't.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from pathlib import Path

from web_stubs import _ensure_web_stubs
_ensure_web_stubs()
from nightly_harness import NightlyHarness, FakeTMDB  # noqa: E402
from services import sync  # noqa: E402


class DriftingTMDB(FakeTMDB):
    """TMDB whose answer for one listing changes between nights (a new TMDB
    entry with the same name, an edited year, popularity re-ranking)."""
    answer = {}

    def search_series(self, name, year=None, **kw):
        if name in DriftingTMDB.answer:
            tid, title, yr = DriftingTMDB.answer[name]
            return {"tmdb_id": tid, "title": title, "year": yr, "overview": "", "genres": [],
                    "poster_path": None, "backdrop_path": None, "rating": None, "status": None}
        return super().search_series(name, year, **kw)


class ShowKeepsItsRow(NightlyHarness):
    def setUp(self):
        super().setUp()
        sync.TMDBService = DriftingTMDB           # tearDown puts the original back
        DriftingTMDB.answer = {}
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(100)])
        self.client.series["s1"].append(("Ghosts US", 9999))

    def test_a_changed_tmdb_answer_does_not_move_an_imported_show(self):
        DriftingTMDB.answer = {"Ghosts US": (777, "Ghosts", "2019")}
        self.night()
        row = self.series_row(777)
        self.assertIsNotNone(row, "night 1 should import the show as tmdb 777")
        folder = Path(row.strm_path)
        episodes = sorted(p.name for p in folder.rglob("*.strm"))
        self.assertTrue(episodes)

        DriftingTMDB.answer = {"Ghosts US": (888, "Ghosts", "2021")}   # TMDB now answers another show
        for _ in range(3):
            self.night()

        self.assertIsNotNone(self.series_row(777),
                             "the show's row was pruned: Jellyfin gets a new series item and every user's "
                             "watched state, resume points and favourites for it are lost")
        self.assertTrue(folder.is_dir(), f"the show's folder {folder.name} was removed")
        self.assertEqual(episodes, sorted(p.name for p in folder.rglob("*.strm")))
        self.assertIsNone(self.series_row(888),
                          "a second copy of the same listing was imported under the new TMDB answer")

    def test_a_listing_first_seen_after_the_change_gets_the_new_answer(self):
        # Control: new imports still follow TMDB's current answer.
        DriftingTMDB.answer = {"Ghosts US": (888, "Ghosts", "2021")}
        self.night()
        self.assertIsNotNone(self.series_row(888))
        self.assertIsNone(self.series_row(777))


if __name__ == "__main__":
    unittest.main()

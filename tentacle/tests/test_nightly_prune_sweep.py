"""Multi-night simulations of the provider prune and the VOD sweep (0e1805f / v2.242.0).

Each "night" runs the real functions in the order run_scheduled_sync() in
main.py calls them (see nightly_harness.py). A "sync_only" is a manual
"Sync now" from the UI (routers/sync.py): prune, but no sweep.

Every assertion is about behaviour (rows, files, row ids), never about
internal column names, so the tests hold for any implementation.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from datetime import datetime, timedelta
from pathlib import Path as _RealPath

import services.sync as sync
from models.database import Movie
from nightly_harness import NightlyHarness, FakeTMDB


class _Clock:
    """Shift services.sync's idea of "now" forward (only sync.datetime is patched)."""

    def __init__(self, test):
        self.offset = timedelta(0)
        real = sync.datetime
        clock = self

        class _ShiftedDateTime(real):
            @classmethod
            def utcnow(cls):
                return real.utcnow() + clock.offset

        sync.datetime = _ShiftedDateTime
        test.addCleanup(setattr, sync, "datetime", real)


class TestProviderRemovalAcrossNights(NightlyHarness):

    def test_title_dropped_by_provider_is_pruned_within_three_nights(self):
        """Must-not-change (#21, fixed by cc231e0): the provider drops one of 100
        titles for good. Nightly schedule only; it is gone by the third night."""
        self.add_category("1")
        titles = [f"Movie {i}" for i in range(100)]
        self.catalogue_movies("1", titles)
        self.night()
        gone = FakeTMDB.ids["Movie 7"]
        strm = _RealPath(self.movie(gone).strm_path)
        self.assertTrue(strm.exists())

        self.client.movies["1"] = [(t, s) for t, s in self.client.movies["1"] if t != "Movie 7"]
        for _ in range(3):
            self.night()

        self.assertIsNone(self.movie(gone), "title the provider dropped is still in the DB after 3 nights")
        self.assertFalse(strm.exists(), "dead .strm still on disk after 3 nights")
        self.assertEqual(self.db.query(Movie).count(), 99)

    def test_prune_mark_plus_missing_file_is_not_a_one_night_deletion(self):
        """Must-not-change (#21, fixed by cc231e0): the provider omits a title AND its
        .strm is unreadable on the same night. One night of evidence deletes nothing."""
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.night()
        victim = FakeTMDB.ids["Movie 3"]
        strm = _RealPath(self.movie(victim).strm_path)

        self.client.movies["1"] = [(t, s) for t, s in self.client.movies["1"] if t != "Movie 3"]
        strm.rename(strm.with_name(strm.name + ".offline"))
        self.night()

        self.assertIsNotNone(self.movie(victim), "row deleted after a single night of evidence")

    def test_movie_strm_lost_from_disk_is_restored_without_dropping_the_row(self):
        """A movie's .strm disappears while the provider still lists it (failed disk,
        restore from backup, accidental delete).

        cc231e0 added _repair_movie_strm() for exactly this, but only calls it on the
        two TMDB-resolved "existing" branches of _sync_movies(). A title whose cleaned
        provider name/year equals the stored row takes the known_titles fast path,
        which `continue`s without calling it. So the .strm is never rewritten, the
        sweep deletes the row on the second night, and the third night re-imports the
        title as a NEW row (new id, date_added reset, SmartList/home-row cascade).
        """
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.night()
        tmdb = FakeTMDB.ids["Movie 42"]
        row_id = self.movie(tmdb).id
        strm = _RealPath(self.movie(tmdb).strm_path)
        strm.unlink()

        self.night()
        self.assertTrue(strm.exists(), ".strm not rewritten by the next sync although the provider lists the title")
        for _ in range(2):
            self.night()
        self.assertIsNotNone(self.movie(tmdb))
        self.assertEqual(self.movie(tmdb).id, row_id, "row was deleted and re-imported instead of repaired")

    def test_prune_mark_is_cleared_when_title_is_seen_even_if_prune_is_skipped(self):
        """Two strikes must mean two consecutive absences, not two absences ever.

        Sync 1: a category comes back short and a title is marked. Sync 2: the title is
        served again, but another category's fetch raised (fetch_ok False), so
        _prune_removed_content() is skipped, and with it the only code that clears the
        provider mark. Sync 3: the title is missing again and the stale mark from sync 1
        counts as the first strike, so it is deleted after one absence. Manual syncs
        are used so the sweep cannot interfere.
        """
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.catalogue_movies("2", [f"Other {i}" for i in range(100)], first_tmdb=3000)
        self.sync_only()
        victim = FakeTMDB.ids["Movie 5"]
        full = list(self.client.movies["1"])
        short = [(t, s) for t, s in full if t != "Movie 5"]

        self.client.movies["1"] = short        # sync 1: absent -> marked
        self.sync_only()
        self.client.movies["1"] = full         # sync 2: served, but cat 2 fails
        self.client.raise_for = {"2"}
        self.sync_only()
        self.client.raise_for = set()
        self.client.movies["1"] = short        # sync 3: absent again (first strike)
        self.sync_only()

        self.assertIsNotNone(self.movie(victim),
                             "deleted on a stale mark although the title was served in between")


class TestCategoryOutageAcrossSyncs(NightlyHarness):

    def test_category_empty_for_three_syncs_prunes_nothing(self):
        """Must-not-change (#21, fixed by cc231e0 with EMPTY_CATEGORY_STRIKES): a
        category returns [] on three consecutive syncs, then recovers. Nothing is
        deleted, and nothing is left behind that would turn a later single absence
        into a deletion."""
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", [f"Big {i}" for i in range(400)])
        self.catalogue_movies("2", [f"Small {i}" for i in range(30)], first_tmdb=9000)
        self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 430)
        small = list(self.client.movies["2"])

        self.client.movies["2"] = []
        for _ in range(3):
            self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 430,
                         "titles of a temporarily empty category were pruned")

        self.client.movies["2"] = small
        self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 430)

        victim = FakeTMDB.ids["Small 3"]            # one later, single absence
        self.client.movies["2"] = [(t, s) for t, s in small if t != "Small 3"]
        self.sync_only()
        self.assertIsNotNone(self.movie(victim), "outage left a mark that acted as a first strike")

    def test_category_that_stays_empty_is_eventually_pruned(self):
        """Must-not-change: a category that really emptied is believed after a few
        syncs and its titles (not served by any other category) are pruned."""
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", [f"Big {i}" for i in range(400)])
        self.catalogue_movies("2", [f"Small {i}" for i in range(30)], first_tmdb=9000)
        self.sync_only()
        self.client.movies["2"] = []
        for _ in range(6):
            self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 400)


class TestLargeGenuineRemoval(NightlyHarness):

    def test_blocked_removal_is_eventually_processed(self):
        """The provider really drops 10% of its catalogue and never restores it.

        The 5% cap refusing it on the first nights is right. At 0e1805f the refusal is
        permanent: the same ERROR and 'sync-prune-blocked' entry every night, forever,
        and the log's advice ("delete the titles from the Library page") has no UI for
        provider titles (the Library page only GETs /api/library/item). The 100 dead
        .strm files stay in Jellyfin. Here the absence persists, sync after sync, for
        8 days; it should then be processed gradually (at most the allowance per run).
        """
        clock = _Clock(self)
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(1000)])
        self.sync_only()
        self.client.movies["1"] = self.client.movies["1"][:900]
        self.sync_only()          # marks 100
        self.sync_only()          # refuses (100 > 50)
        self.assertEqual(self.db.query(Movie).count(), 1000)

        for day in range(1, 9):   # one sync a day for 8 more days, same answer
            clock.offset = timedelta(days=day)
            self.sync_only()
        clock.offset = timedelta(days=9)
        self.sync_only()
        self.assertEqual(self.db.query(Movie).count(), 900,
                         "a removal the provider has confirmed on every sync for 9 days is never processed")


class TestStorageOutageAcrossNights(NightlyHarness):
    """#11 follow-up: files that are unreadable for a while (mergerfs branch out,
    NFS/SMB hiccup) while the provider keeps listing the titles must not cost
    library rows. The pool root stays non-empty, so the sweep's mount probe passes;
    only the sync's repair of existing titles can keep the rows."""

    def test_movie_branch_outage_deletes_no_rows(self):
        """3 of 100 movie .strm files are unreadable for two nights. At 0e1805f the
        sync does not rewrite them (known-title fast path skips _repair_movie_strm),
        so the second sweep deletes all 3 rows (under the 50-row floor)."""
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.night()
        victims = [FakeTMDB.ids[f"Movie {i}"] for i in (1, 2, 3)]
        ids = {t: self.movie(t).id for t in victims}
        for t in victims:
            strm = _RealPath(self.movie(t).strm_path)
            strm.rename(strm.with_name(strm.name + ".offline"))

        self.night()
        self.night()

        for t in victims:
            self.assertIsNotNone(self.movie(t), f"tmdb {t} deleted during a storage outage")
            self.assertEqual(self.movie(t).id, ids[t])

    def test_series_branch_outage_deletes_no_rows(self):
        """Same for a series whose whole show folder is unreadable. At 0e1805f
        _backfill_series_episodes() returns early when the show folder does not
        exist, so nothing is rewritten and the second sweep deletes the row."""
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(30)])
        self.night()
        tmdb = FakeTMDB.ids["Show 3"]
        row_id = self.series_row(tmdb).id
        show = _RealPath(self.series_row(tmdb).strm_path)
        show.rename(show.with_name(show.name + ".offline"))

        self.night()
        self.night()

        self.assertIsNotNone(self.series_row(tmdb), "series deleted during a storage outage")
        self.assertEqual(self.series_row(tmdb).id, row_id)


if __name__ == "__main__":
    unittest.main()

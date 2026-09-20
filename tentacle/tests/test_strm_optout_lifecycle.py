"""End-to-end lifecycle of the per-title ".strm management" opt-out (#3).

The toggle is driven through the real endpoint function
(routers/library.py::set_strm_managed) and then through real nights
(sync_provider + sweep_orphaned_vod_records, see nightly_harness.py).

The CI job only installs requests + sqlalchemy, so FastAPI/pydantic are
stubbed just enough to import the router module when they are missing.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from pathlib import Path as _RealPath


from web_stubs import _ensure_web_stubs  # noqa: E402


_ensure_web_stubs()

from models.database import Movie, Series  # noqa: E402
from nightly_harness import NightlyHarness, FakeTMDB  # noqa: E402
import routers.library as library  # noqa: E402


class _Admin:
    display_name = "admin"


class TestStrmOptOut(NightlyHarness):

    def _toggle(self, media_type, tmdb_id, enabled, delete_files=False):
        body = library.StrmManagedBody(enabled=enabled, delete_files=delete_files)
        return library.set_strm_managed(media_type, tmdb_id, body, db=self.db, user=_Admin())

    def test_opted_out_movie_survives_a_night_with_a_failed_category_fetch(self):
        """Opt a movie out with "also delete files", then run nights where one fetch fails.

        Night 1: the provider lists the movie; the sweep sees its .strm gone and marks it.
        Night 2: another category's fetch raises, so fetch_ok is False and the prune (the
        only thing clearing that mark) is skipped. The sweep finds its own mark and
        deletes the row. Night 3: the sync re-imports the title as new, writes the .strm
        again, and the opt-out is gone. Happened at 97d25e1; passes since cc231e0 (regression cover).
        """
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.catalogue_movies("2", [f"Other {i}" for i in range(100)], first_tmdb=3000)
        self.night()
        tmdb = FakeTMDB.ids["Movie 9"]
        strm = _RealPath(self.movie(tmdb).strm_path)

        result = self._toggle("movie", tmdb, enabled=False, delete_files=True)
        self.assertEqual(result["files_deleted"], 2)
        self.assertFalse(strm.exists())

        self.night()                         # night 1
        self.client.raise_for = {"2"}
        self.night()                         # night 2: category 2 fetch fails
        self.client.raise_for = set()
        self.night()                         # night 3

        row = self.movie(tmdb)
        self.assertIsNotNone(row, "opted-out movie row was swept")
        self.assertTrue(row.strm_disabled, "opt-out flag lost (row re-imported as new)")
        self.assertFalse(strm.exists(), ".strm regenerated for an opted-out title")

    def test_opted_out_series_with_empty_folder_survives(self):
        """Same flow for a pure-VOD series: delete_series_files removes the now-empty
        show folder, so the sweep reads the series as orphaned."""
        self.add_category("s1", type_="series")
        self.add_category("s2", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(60)])
        self.catalogue_series("s2", [f"Other Show {i}" for i in range(60)], first_tmdb=7000)
        self.night()
        tmdb = FakeTMDB.ids["Show 4"]
        show_dir = _RealPath(self.series_row(tmdb).strm_path)

        self._toggle("series", tmdb, enabled=False, delete_files=True)
        self.assertFalse(show_dir.exists())

        self.night()
        self.client.raise_for = {"s2"}
        self.night()
        self.client.raise_for = set()
        self.night()

        row = self.series_row(tmdb)
        self.assertIsNotNone(row, "opted-out series row was swept")
        self.assertTrue(row.strm_disabled)
        self.assertFalse(show_dir.exists(), "series .strm regenerated for an opted-out title")

    def test_reenabling_a_movie_writes_its_strm_back_on_the_next_sync(self):
        """Opt out with delete, then switch management back on.

        The UI toast says "Tentacle will keep .strm files for this title up to date".
        cc231e0 added _repair_movie_strm(), but the known-title fast path in
        _sync_movies() never calls it, so at 0e1805f the .strm does not come back.
        """
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.night()
        tmdb = FakeTMDB.ids["Movie 11"]
        strm = _RealPath(self.movie(tmdb).strm_path)
        self._toggle("movie", tmdb, enabled=False, delete_files=True)
        self.night()
        self._toggle("movie", tmdb, enabled=True)
        self.night()

        self.assertTrue(strm.exists(), "re-enabled movie never got its .strm back")
        self.assertIn("provider/movie", strm.read_text())
        self.assertFalse(self.movie(tmdb).strm_disabled)

    def test_reenabling_a_series_whose_folder_was_removed_writes_episodes_back(self):
        """Opt out a pure-VOD series with delete (its folder is removed), then re-enable.

        _backfill_series_episodes() returns early when the show folder does not exist,
        so at 0e1805f nothing is written back (the sweep then deletes the row
        and a later sync re-imports it as new).
        """
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(60)])
        self.night()
        tmdb = FakeTMDB.ids["Show 8"]
        show_dir = _RealPath(self.series_row(tmdb).strm_path)
        self._toggle("series", tmdb, enabled=False, delete_files=True)
        self.night()
        self._toggle("series", tmdb, enabled=True)
        self.night()

        self.assertTrue(show_dir.is_dir(), "re-enabled series folder never recreated")
        self.assertTrue(list(show_dir.rglob("*.strm")), "no episode .strm written back")

    def test_reenabled_title_is_not_swept_on_a_stale_file_mark(self):
        """A missing-file mark set before an opt-out must not survive it.

        Night A: the .strm is missing and the provider fetch fails, so nothing repairs
        it and the sweep marks the row. The user then opts the title out; the sweep
        skips opted-out rows, so the mark is never cleared. Weeks later the user
        re-enables it on a night the provider fetch fails again: at 0e1805f the first
        sweep after re-enabling reads the weeks-old mark as its first strike and
        deletes the row after one night of evidence.
        """
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(100)])
        self.night()
        tmdb = FakeTMDB.ids["Movie 13"]
        _RealPath(self.movie(tmdb).strm_path).unlink()

        self.client.raise_for = {"1"}
        self.night()                                   # sweep marks the row
        self._toggle("movie", tmdb, enabled=False)
        self.client.raise_for = set()
        for _ in range(2):
            self.night()                               # opted out: sweep skips it
        self._toggle("movie", tmdb, enabled=True)
        self.client.raise_for = {"1"}
        self.night()                                   # first sweep after re-enabling

        self.assertIsNotNone(self.movie(tmdb), "re-enabled title deleted after one sweep on a stale mark")

    def test_opt_out_does_not_touch_downloaded_episode_or_its_metadata(self):
        """Hybrid show (merged folder): Tentacle .strm for S01E01, a Sonarr download
        for S01E02 with its own .nfo and subtitles. Opting out with delete removes the
        Tentacle .strm only; the download, its .nfo and tvshow.nfo stay (Jellyfin still
        needs them for the downloaded episode)."""
        self.add_category("s1", type_="series")
        self.catalogue_series("s1", [f"Show {i}" for i in range(10)])
        self.night()
        tmdb = FakeTMDB.ids["Show 2"]
        show_dir = _RealPath(self.series_row(tmdb).strm_path)
        season = show_dir / "Season 01"
        mkv = season / "Show 2 - S01E02 - Pilot Part 2.mkv"
        ep_nfo = season / "Show 2 - S01E02 - Pilot Part 2.nfo"
        srt = season / "Show 2 - S01E02 - Pilot Part 2.en.srt"
        mkv.write_text("video")
        ep_nfo.write_text("<episodedetails><title>Pilot Part 2</title></episodedetails>")
        srt.write_text("subs")

        self._toggle("series", tmdb, enabled=False, delete_files=True)

        self.assertFalse(list(show_dir.rglob("*.strm")))
        for keep in (mkv, ep_nfo, srt, show_dir / "tvshow.nfo"):
            self.assertTrue(keep.exists(), f"{keep.name} was deleted")


if __name__ == "__main__":
    unittest.main()

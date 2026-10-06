"""A new title imported while the VOD folder is unmounted must not let the
nightly sweep delete the titles nothing wrote back that night (#440).

The share behind /media/vod/movies (or shows) is not mounted, so the container
sees the bare, empty mount point. One new title imported there made the root
non-empty: the sweep's "missing or empty root" check passed, the restores
wrote every listed title back, and the rows of a category the provider could
not read that night (#267) were deleted. Now the sync writes nothing of a type
whose root is missing or empty while titles of that type should be there.

Runs the real sync_provider + sweep_orphaned_vod_records (tests/nightly_harness.py).
"""
import shutil
import unittest

import services.sync as sync
from models.database import DeletionLog, SyncRun
from nightly_harness import NightlyHarness


def _files(root):
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class ImportOntoUnmountedRoot(NightlyHarness):

    def _unmount(self, name, keep_mount_point=True):
        shutil.rmtree(self.vod / name)        # the share is not mounted:
        if keep_mount_point:
            (self.vod / name).mkdir()         # the bare, empty mount point

    def _last_run(self):
        return self.db.query(SyncRun).order_by(SyncRun.id.desc()).first()

    def _films(self):
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", ["Heat"], first_tmdb=1000)
        self.catalogue_movies("2", ["Ronin"], first_tmdb=2000)
        self.night()                          # night 1: a library, synced

    def _shows(self):
        self.add_category("11", type_="series")
        self.add_category("12", type_="series")
        self.catalogue_series("11", ["Heat Show"], first_tmdb=5000)
        self.catalogue_series("12", ["Ronin Show"], first_tmdb=6000)
        self.night()

    def test_shows(self):
        self._shows()
        self._unmount("shows")
        self.client.raise_for = {"12"}        # category 12 can't be read (#267)
        self.catalogue_series("11", ["Heat Show", "New Show"], first_tmdb=5000)  # one new show
        self.night()
        self.assertEqual([], _files(self.vod / "shows"), "written onto the unmounted shows root")
        self.night()
        self.assertIsNotNone(self.series_row(6000), "Ronin Show deleted while the shows folder was unmounted")
        self.assertEqual([], [d.kind for d in self.db.query(DeletionLog).all()])

    def test_films(self):
        self._films()
        self._unmount("movies")
        self.client.raise_for = {"2"}
        self.catalogue_movies("1", ["Heat", "Collateral"], first_tmdb=1000)     # one new film
        self.night()
        self.assertEqual([], _files(self.vod / "movies"), "written onto the unmounted movies root")
        self.assertIsNone(self.movie(1001), "a new film was imported onto the unmounted root")
        self.night()
        self.assertIsNotNone(self.movie(2000), "Ronin deleted while the movies folder was unmounted")
        self.assertEqual([], [d.kind for d in self.db.query(DeletionLog).all()])

    def test_a_missing_root_is_not_created_again(self):
        self._films()
        self._unmount("movies", keep_mount_point=False)
        self.catalogue_movies("1", ["Heat", "Collateral"], first_tmdb=1000)
        self.night()
        self.night()
        self.assertFalse((self.vod / "movies").exists())
        self.assertIsNotNone(self.movie(2000))

    def test_the_run_says_why(self):
        """Only films are held back: the run completes, with the reason."""
        self._films()
        self._shows()
        self._unmount("movies")
        self.night()
        run = self._last_run()
        self.assertEqual("completed", run.status)
        self.assertIn("storage looks unavailable, no films were synced", run.error_message)
        self.assertNotIn("show", run.error_message)

    def test_nothing_synced_is_a_failed_run(self):
        self._films()
        self._unmount("movies")
        run = sync.sync_provider(self.provider, "movies", self.db)
        self.assertEqual("failed", run.status)
        self.assertIn("is missing or empty but 2 film(s) should be there", run.error_message)

    def test_the_other_type_still_syncs(self):
        self._films()
        self._shows()
        self._unmount("movies")
        self.catalogue_series("11", ["Heat Show", "New Show"], first_tmdb=5000)
        self.night()
        self.assertIsNotNone(self.series_row(5001), "the shows root is mounted: New Show is imported")

    def test_once_the_share_is_back_everything_syncs(self):
        self._films()
        saved = self.vod.parent / "share"
        shutil.copytree(self.vod / "movies", saved)
        self._unmount("movies")
        self.client.raise_for = {"2"}
        self.catalogue_movies("1", ["Heat", "Collateral"], first_tmdb=1000)
        self.night()
        self.night()
        shutil.rmtree(self.vod / "movies")    # the share is mounted again
        shutil.copytree(saved, self.vod / "movies")
        self.client.raise_for = set()
        before = {t: self.movie(t).date_added for t in (1000, 2000)}
        self.night()
        self.assertIsNotNone(self.movie(1001), "the new film is imported once the share is back")
        for t, added in before.items():
            self.assertEqual(added, self.movie(t).date_added, f"tmdb {t} was re-imported")

    def test_a_fresh_install_fills_its_empty_root(self):
        """No titles yet: an empty root is a new library, not an outage."""
        self.add_category("1")
        self.catalogue_movies("1", ["Heat"], first_tmdb=1000)
        self.night()
        self.assertIsNotNone(self.movie(1000))
        self.assertTrue((self.vod / "movies" / "Heat (2010)" / "Heat (2010).strm").exists())


if __name__ == "__main__":
    unittest.main()

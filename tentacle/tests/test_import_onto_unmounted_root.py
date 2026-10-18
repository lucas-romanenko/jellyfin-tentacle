"""A new title imported while the VOD folder is unmounted must not let the
nightly sweep delete the titles nothing wrote back that night (#440).

The share behind /media/vod/movies (or shows) is not mounted, so the container
sees the bare, empty mount point. One new title imported there made the root
non-empty: the sweep's "missing or empty root" check passed, and the rows of a
category the provider could not read that night (#267) were deleted. The sync
refuses a missing or empty root while titles are recorded there (#439); a
stale mount that raises OSError when read counts as unmounted too.

Runs the real sync_provider + sweep_orphaned_vod_records (tests/nightly_harness.py).
"""
import pathlib
import shutil
import unittest
from unittest import mock

import services.sync as sync
from models.database import DeletionLog
from nightly_harness import NightlyHarness


def _files(root):
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class ImportOntoUnmountedRoot(NightlyHarness):

    def _unmount(self, name):
        shutil.rmtree(self.vod / name)        # the share is not mounted:
        (self.vod / name).mkdir()             # the bare, empty mount point

    def _unmounted_night(self, sync_type):
        run = sync.sync_provider(self.provider, sync_type, self.db)
        self.assertEqual("failed", run.status)
        self.assertIn("looks unmounted", run.error_message)
        sync.sweep_orphaned_vod_records(self.db)
        self.db.expire_all()

    def test_shows(self):
        self.add_category("11", type_="series")
        self.add_category("12", type_="series")
        self.catalogue_series("11", ["Heat Show"], first_tmdb=5000)
        self.catalogue_series("12", ["Ronin Show"], first_tmdb=6000)
        self.night()
        self._unmount("shows")
        self.client.raise_for = {"12"}        # category 12 can't be read (#267)
        self.catalogue_series("11", ["Heat Show", "New Show"], first_tmdb=5000)  # one new show
        self._unmounted_night("series")
        self.assertEqual([], _files(self.vod / "shows"), "written onto the unmounted shows root")
        self._unmounted_night("series")
        self.assertIsNotNone(self.series_row(6000), "Ronin Show deleted while the shows folder was unmounted")
        self.assertIsNone(self.series_row(5001), "a new show was imported onto the unmounted root")
        self.assertEqual([], [d.kind for d in self.db.query(DeletionLog).all()])

    def test_films(self):
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", ["Heat"], first_tmdb=1000)
        self.catalogue_movies("2", ["Ronin"], first_tmdb=2000)
        self.night()
        self._unmount("movies")
        self.client.raise_for = {"2"}
        self.catalogue_movies("1", ["Heat", "Collateral"], first_tmdb=1000)     # one new film
        self._unmounted_night("full")
        self.assertEqual([], _files(self.vod / "movies"), "written onto the unmounted movies root")
        self.assertIsNone(self.movie(1001), "a new film was imported onto the unmounted root")
        self._unmounted_night("full")
        self.assertIsNotNone(self.movie(2000), "Ronin deleted while the movies folder was unmounted")
        self.assertEqual([], [d.kind for d in self.db.query(DeletionLog).all()])

    def test_a_stale_mount_counts_as_unmounted(self):
        """A stale NFS/SMB/FUSE mount raises OSError on read: no sync, no sweep."""
        self.add_category("1")
        self.catalogue_movies("1", ["Heat", "Ronin"], first_tmdb=1000)
        self.night()
        root = self.vod / "movies"
        real = pathlib.Path.iterdir

        def stale(path):
            if path == root:
                raise OSError(116, "Stale file handle")
            return real(path)

        self.catalogue_movies("1", ["Heat"], first_tmdb=1000)   # Ronin left the listing
        with mock.patch.object(pathlib.Path, "iterdir", stale):
            self._unmounted_night("full")
        self.assertIsNotNone(self.movie(1001), "Ronin deleted while the movies mount was stale")
        self.assertEqual([], [d.kind for d in self.db.query(DeletionLog).all()])


if __name__ == "__main__":
    unittest.main()

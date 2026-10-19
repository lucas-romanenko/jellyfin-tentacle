"""A sync must not write onto an unmounted (missing or empty) VOD root: the
files land on the container's own disk, make the root look mounted to the
VOD sweep, and the sweep then deletes every title that was not written back
(#439).

Runs the real sync_provider + sweep_orphaned_vod_records (tests/nightly_harness.py).
"""
import shutil
import unittest

from models.database import DeletionLog
from nightly_harness import FakeTMDB, NightlyHarness
import services.sync as sync

HEAT, RONIN, NEW_FILM = 1000, 2000, 3000
LOST = 5000


class MovieRestoreOnUnmountedRoot(NightlyHarness):

    def setUp(self):
        super().setUp()
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", ["Heat"], first_tmdb=HEAT)
        self.catalogue_movies("2", ["Ronin"], first_tmdb=RONIN)
        self.night()                          # night 1: a library, synced
        self.root = self.vod / "movies"
        shutil.rmtree(self.root)              # the share is not mounted:
        self.root.mkdir()                     # the bare, empty mount point
        self.client.raise_for = {"2"}         # and category 2 can't be read (#267)

    def unmounted_night(self):
        """The nightly while the share is out: the sync refuses, the sweep skips."""
        run = sync.sync_provider(self.provider, "full", self.db)
        self.assertEqual("failed", run.status)
        self.assertIn("looks unmounted", run.error_message)
        sync.sweep_orphaned_vod_records(self.db)
        self.db.expire_all()

    def deletions(self):
        return [(d.kind, d.detail) for d in self.db.query(DeletionLog).all()]

    def on_root(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))

    def test_nothing_is_written_onto_the_empty_mount_point(self):
        self.unmounted_night()
        self.assertEqual([], self.on_root())

    def test_a_new_film_is_not_written_there_either(self):
        self.client.movies["1"].append(("Arrival", NEW_FILM))
        FakeTMDB.ids["Arrival"] = NEW_FILM
        self.unmounted_night()
        self.assertEqual([], self.on_root())

    def test_the_film_not_restored_survives(self):
        self.unmounted_night()
        self.unmounted_night()
        self.assertIsNotNone(self.movie(RONIN), self.deletions())
        self.assertIsNotNone(self.movie(HEAT), self.deletions())

    def test_a_missing_mount_point_is_not_created(self):
        self.root.rmdir()
        self.unmounted_night()
        self.unmounted_night()
        self.assertFalse(self.root.exists())
        self.assertIsNotNone(self.movie(RONIN), self.deletions())

    def test_films_come_back_once_the_share_is_mounted(self):
        self.unmounted_night()
        (self.root / "Other (2001)").mkdir()  # the share is back: the root has files
        self.client.raise_for = set()
        self.night()
        for tmdb_id in (HEAT, RONIN):
            row = self.movie(tmdb_id)
            self.assertIsNotNone(row, self.deletions())
            self.assertTrue((self.root / row.strm_path.split("/")[-2]).is_dir())

    def test_the_restore_itself_refuses_an_empty_root(self):
        stream = {"stream_id": HEAT, "container_extension": "mp4"}
        self.assertFalse(sync._repair_movie_strm(self.client, stream, HEAT, self.provider, self.db))
        self.assertEqual([], self.on_root())

    def test_a_new_install_still_syncs_into_its_empty_folder(self):
        for row in self.db.query(sync.Movie).all():
            self.db.delete(row)
        self.db.commit()
        self.client.raise_for = set()
        self.night()
        self.assertIn("Heat (2010)", self.on_root())


class ShowsOnUnmountedRoot(NightlyHarness):

    def test_no_show_is_written_onto_the_empty_mount_point(self):
        self.add_category("9", type_="series")
        self.catalogue_series("9", ["Lost"], first_tmdb=LOST)
        self.night()
        root = self.vod / "shows"
        shutil.rmtree(root)
        root.mkdir()
        run = sync.sync_provider(self.provider, "series", self.db)
        self.assertEqual("failed", run.status)
        self.assertEqual([], list(root.iterdir()))
        sync.sweep_orphaned_vod_records(self.db)
        sync.sweep_orphaned_vod_records(self.db)
        self.db.expire_all()
        self.assertIsNotNone(self.series_row(LOST))


if __name__ == "__main__":
    unittest.main()

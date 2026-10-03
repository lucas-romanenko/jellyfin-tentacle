"""A movie .strm restore must not write onto an unmounted (empty) VOD root.

When the share behind /media/vod/movies is not mounted, the container sees
the bare mount point: an empty folder. _backfill_series_episodes refuses to
rebuild a show there ("a missing or empty mount point means storage is
unavailable"); _repair_movie_strm did not. Every film the provider still
listed was written back onto the bare mount point (the wrong disk, hidden
again once the share is mounted), which made the root non-empty, so the VOD
sweep's storage check passed and the films nothing restored that night (a
category the provider could not read, #267) were marked and deleted on the
next night.

Runs the real sync_provider + sweep_orphaned_vod_records (NightlyHarness).
TENTACLE_UNMOUNT_SEEDS (default 12) and TENTACLE_UNMOUNT_FIRST (default 0)
pick the property test's seeds; the PR ran 1,000.
"""
import os
import random
import shutil
import unittest

from models.database import DeletionLog, Movie, Series
from nightly_harness import NightlyHarness

HEAT, RONIN = 1000, 2000


def _files(root):
    """Everything under root, relative to it (nothing when root is missing)."""
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class MovieRestoreOnUnmountedRoot(NightlyHarness):

    def setUp(self):
        super().setUp()
        self.add_category("1")
        self.add_category("2")
        self.catalogue_movies("1", ["Heat"], first_tmdb=HEAT)
        self.catalogue_movies("2", ["Ronin"], first_tmdb=RONIN)
        self.night()                                   # a library, synced
        self.root = self.vod / "movies"
        # The id alone is no proof: SQLite hands a re-imported row the id of
        # the row just deleted. date_added is reset by a re-import.
        self.ids = {t: (self.movie(t).id, self.movie(t).date_added) for t in (HEAT, RONIN)}
        self.saved = self.vod.parent / "share"         # what the real share holds
        shutil.copytree(self.root, self.saved)

    def _unmount(self, keep_mount_point=True):
        shutil.rmtree(self.root)
        if keep_mount_point:
            self.root.mkdir()

    def _mount(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.copytree(self.saved, self.root)

    def _assert_rows_kept(self):
        sweeps = [(d.kind, d.detail) for d in self.db.query(DeletionLog).all()]
        self.assertEqual([], sweeps, "rows were deleted while the root was unmounted")
        for tmdb, identity in self.ids.items():
            row = self.movie(tmdb)
            self.assertIsNotNone(row, f"tmdb {tmdb} deleted while the root was unmounted")
            self.assertEqual(identity, (row.id, row.date_added), f"tmdb {tmdb} was re-imported")

    def test_nothing_is_written_onto_an_empty_mount_point(self):
        self._unmount()
        self.client.raise_for = {"2"}                  # the provider can't read category 2 (#267)
        self.night()
        self.assertEqual([], _files(self.root), "the restore wrote onto the unmounted root")

    def test_rows_not_restored_survive_an_empty_mount_point(self):
        self._unmount()
        self.client.raise_for = {"2"}
        for _ in range(3):
            self.night()
        self._assert_rows_kept()

    def test_rows_not_restored_survive_a_missing_mount_point(self):
        self._unmount(keep_mount_point=False)
        self.client.raise_for = {"2"}
        for _ in range(3):
            self.night()
        self.assertEqual([], _files(self.root))
        self._assert_rows_kept()

    def test_files_are_where_they_were_once_the_share_is_back(self):
        self._unmount()
        self.client.raise_for = {"2"}
        self.night()
        self.night()
        self._mount()
        self.client.raise_for = set()
        self.night()
        self._assert_rows_kept()
        self.assertEqual(_files(self.saved), _files(self.root))
        for tmdb in self.ids:
            self.assertTrue(os.path.exists(self.movie(tmdb).strm_path))

    def test_a_film_folder_lost_from_a_mounted_root_is_still_restored(self):
        """Control (#24): the root holds other films, so the folder comes back."""
        heat_dir = os.path.dirname(self.movie(HEAT).strm_path)
        shutil.rmtree(heat_dir)
        self.night()
        self.assertTrue(os.path.exists(self.movie(HEAT).strm_path))
        self.assertTrue(os.path.exists(os.path.splitext(self.movie(HEAT).strm_path)[0] + ".nfo"))
        self._assert_rows_kept()

    def test_control_nothing_listed_keeps_the_root_empty(self):
        """Control (passes without the fix): with nothing restored, the sweep's
        own check sees the empty root and skips."""
        self._unmount()
        self.client.raise_for = {"1", "2"}
        self.night()
        self.night()
        self.assertEqual([], _files(self.root))
        self._assert_rows_kept()


class UnmountedRootProperty(NightlyHarness):
    """Random libraries (films and shows over a few categories), then 1-4
    nights with the movies root, the shows root or both unmounted (empty or
    missing) and a random set of categories unreadable each night, then the
    share back. Invariants, checked after every night:
      U1  nothing is written under an unmounted root
      U2  no row is deleted and no row id changes
      U3  once the share is back, every row's .strm is where it was
    """

    def _seeds(self):
        n = int(os.environ.get("TENTACLE_UNMOUNT_SEEDS", "12"))
        first = int(os.environ.get("TENTACLE_UNMOUNT_FIRST", "0"))
        return range(first, first + n)

    def _world(self, rng):
        cats = []
        for c in range(rng.randint(1, 3)):
            cid = f"m{c}"
            self.add_category(cid)
            self.catalogue_movies(cid, [f"Film {c}-{i}" for i in range(rng.randint(1, 4))],
                                  first_tmdb=1000 + 100 * c)
            cats.append(cid)
        for c in range(rng.randint(0, 2)):
            cid = f"s{c}"
            self.add_category(cid, type_="series")
            self.catalogue_series(cid, [f"Show {c}-{i}" for i in range(rng.randint(1, 3))],
                                  first_tmdb=5000 + 100 * c)
            cats.append(cid)
        return cats

    def _run(self, seed):
        rng = random.Random(seed)
        cats = self._world(rng)
        self.night()
        rows = {("m", m.tmdb_id): ((m.id, m.date_added), m.strm_path) for m in self.db.query(Movie).all()}
        rows.update({("s", s.tmdb_id): ((s.id, s.date_added), s.strm_path) for s in self.db.query(Series).all()})
        down = rng.choice([["movies"], ["shows"], ["movies", "shows"]])
        keep_mount_point = rng.random() < 0.7
        saved = {}
        for name in down:
            root = self.vod / name
            saved[name] = self.vod.parent / f"share-{name}"
            shutil.copytree(root, saved[name])
            shutil.rmtree(root)
            if keep_mount_point:
                root.mkdir()

        def check(when):
            for (kind, tmdb), (identity, _) in rows.items():
                row = self.movie(tmdb) if kind == "m" else self.series_row(tmdb)
                self.assertIsNotNone(row, f"seed {seed} {when}: {kind} {tmdb} deleted")
                self.assertEqual(identity, (row.id, row.date_added),
                                 f"seed {seed} {when}: {kind} {tmdb} re-imported")
            self.assertEqual([], [d.kind for d in self.db.query(DeletionLog).all()],
                             f"seed {seed} {when}: rows deleted")

        for night in range(rng.randint(1, 4)):
            self.client.raise_for = {c for c in cats if rng.random() < 0.5}
            self.night()
            for name in down:
                self.assertEqual([], _files(self.vod / name),
                                 f"seed {seed} night {night}: written onto unmounted {name}")
            check(f"outage night {night}")

        for name in down:
            shutil.rmtree(self.vod / name, ignore_errors=True)
            shutil.copytree(saved[name], self.vod / name)
        self.client.raise_for = set()
        self.night()
        check("share back")
        for (kind, tmdb), (_, path) in rows.items():
            self.assertTrue(os.path.exists(path), f"seed {seed}: {kind} {tmdb} file missing after the share came back")

    def test_invariants_hold_for_random_outages(self):
        for seed in self._seeds():
            with self.subTest(seed=seed):
                self.tearDown()
                self.setUp()
                self._run(seed)


if __name__ == "__main__":
    unittest.main()

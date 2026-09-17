"""Tests for the destructive-operation guards in services.sync.

Regression cover for two incidents where a transient upstream failure was read
as "the content is gone" and deleted thousands of records plus their files:
a provider category returning an empty list, and a VOD mount going away.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Provider, ProviderCategory
import services.sync as sync


def _session():
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestPruneRemovedContent(unittest.TestCase):
    def setUp(self):
        self.db = _session()
        self.provider = Provider(name="P", server_url="", username="", password="")
        self.db.add(self.provider)
        self.db.commit()
        for i in range(200):
            self.db.add(Movie(tmdb_id=1000 + i, title=f"M{i}",
                              source=f"provider_{self.provider.id}",
                              provider_id=self.provider.id))
        self.db.commit()

    def _prune(self, seen):
        return sync._prune_removed_content(self.db, self.provider, "movie", seen)

    def test_first_absence_only_marks(self):
        removed = self._prune(set(range(1000, 1197)))
        self.assertEqual(removed, 0)
        self.assertEqual(
            self.db.query(Movie).filter(Movie.provider_missing_since.isnot(None)).count(), 3)

    def test_second_absence_deletes(self):
        seen = set(range(1000, 1197))
        self._prune(seen)
        self.assertEqual(self._prune(seen), 3)
        self.assertEqual(self.db.query(Movie).count(), 197)

    def test_reappearing_title_clears_its_mark(self):
        self._prune(set(range(1000, 1197)))
        self._prune(set(range(1000, 1200)))
        self.assertEqual(
            self.db.query(Movie).filter(Movie.provider_missing_since.isnot(None)).count(), 0)
        self.assertEqual(self.db.query(Movie).count(), 200)

    def test_mass_disappearance_is_refused(self):
        # 190 of 200 gone on two consecutive runs — past the 5% safety limit,
        # so nothing is deleted even though both runs agree.
        seen = set(range(1000, 1010))
        self._prune(seen)
        self.assertEqual(self._prune(seen), 0)
        self.assertEqual(self.db.query(Movie).count(), 200)


class TestCategoryWentEmpty(unittest.TestCase):
    def setUp(self):
        self.db = _session()
        p = Provider(name="P", server_url="", username="", password="")
        self.db.add(p)
        self.db.commit()
        self.cat = ProviderCategory(provider_id=p.id, category_id="1",
                                    category_name="EN - NEW RELEASE", type="movie",
                                    title_count=6427)
        self.db.add(self.cat)
        self.db.commit()

    def test_empty_response_is_treated_as_a_failed_fetch(self):
        self.assertTrue(sync._category_went_empty(self.db, self.cat, 0))
        # The known count is kept, so the signal doesn't erase itself — zeroing
        # it meant a two-night outage was accepted as a real empty category.
        self.assertEqual(self.cat.title_count, 6427)
        self.assertEqual(self.cat.consecutive_empty_syncs, 1)

    def test_outage_lasting_several_syncs_is_still_not_believed(self):
        for _ in range(sync.EMPTY_CATEGORY_STRIKES - 1):
            self.assertTrue(sync._category_went_empty(self.db, self.cat, 0))
        self.assertEqual(self.cat.title_count, 6427)

    def test_category_that_really_emptied_is_believed_eventually(self):
        for _ in range(sync.EMPTY_CATEGORY_STRIKES - 1):
            sync._category_went_empty(self.db, self.cat, 0)
        # Nth consecutive empty response — accepted, pruning resumes
        self.assertFalse(sync._category_went_empty(self.db, self.cat, 0))

    def test_content_returning_resets_the_streak(self):
        sync._category_went_empty(self.db, self.cat, 0)
        sync._category_went_empty(self.db, self.cat, 0)
        self.assertFalse(sync._category_went_empty(self.db, self.cat, 5))
        self.assertEqual(self.cat.consecutive_empty_syncs, 0)
        # ...and the count of strikes starts over, so the guard is full strength again
        self.assertTrue(sync._category_went_empty(self.db, self.cat, 0))

    def test_non_empty_response_is_never_suspect(self):
        self.assertFalse(sync._category_went_empty(self.db, self.cat, 5))
        self.assertEqual(self.cat.title_count, 6427)

    def test_category_that_never_held_anything_is_not_protected(self):
        self.cat.title_count = 0
        self.assertFalse(sync._category_went_empty(self.db, self.cat, 0))


class TestVodSweep(unittest.TestCase):
    def setUp(self):
        self.db = _session()
        p = Provider(name="P", server_url="", username="", password="")
        self.db.add(p)
        self.db.commit()
        self.root = Path(tempfile.mkdtemp())
        self.paths = []
        for i in range(100):
            folder = self.root / f"M{i}"
            folder.mkdir()
            strm = folder / f"M{i}.strm"
            strm.write_text("http://x")
            self.paths.append(strm)
            self.db.add(Movie(tmdb_id=2000 + i, title=f"M{i}", source=f"provider_{p.id}",
                              provider_id=p.id, strm_path=str(strm)))
        self.db.commit()

    def _sweep(self):
        return sync._sweep_one_type(self.db, Movie, "movie", self.root, datetime.utcnow())

    def test_unavailable_mount_deletes_nothing(self):
        # An empty root means the storage is gone, not that every title was deleted.
        empty = Path(tempfile.mkdtemp())
        count, _ = sync._sweep_one_type(self.db, Movie, "movie", empty, datetime.utcnow())
        self.assertEqual(count, 0)
        self.assertEqual(self.db.query(Movie).count(), 100)

    def test_single_missing_file_needs_two_sweeps(self):
        self.paths[0].unlink()
        self.assertEqual(self._sweep()[0], 0)
        self.db.commit()
        self.assertEqual(self._sweep()[0], 1)

    def test_mass_disappearance_is_refused(self):
        for strm in self.paths[:80]:
            strm.unlink()
        self._sweep()
        self.db.commit()
        count, _ = self._sweep()
        self.assertEqual(count, 0)
        self.assertEqual(self.db.query(Movie).count(), 100)


if __name__ == "__main__":
    unittest.main()


class TestGuardsAreIndependent(unittest.TestCase):
    """The sync prune and the VOD sweep must not read each other's marks.

    They shared one `missing_since` column, which broke both directions: the
    sweep cleared the prune's mark every night (so a title the provider really
    dropped was never pruned), and either guard's mark satisfied the other's
    second strike (so a title could be deleted after a single night).
    """

    def setUp(self):
        self.db = _session()
        self.provider = Provider(name="P", server_url="", username="", password="")
        self.db.add(self.provider)
        self.db.commit()
        self.root = Path(tempfile.mkdtemp())
        for i in range(100):
            folder = self.root / f"M{i}"
            folder.mkdir()
            strm = folder / f"M{i}.strm"
            strm.write_text("http://x")
            self.db.add(Movie(tmdb_id=3000 + i, title=f"M{i}",
                              source=f"provider_{self.provider.id}",
                              provider_id=self.provider.id, strm_path=str(strm)))
        self.db.commit()
        self.all_seen = set(range(3000, 3100))

    def _prune(self, seen):
        return sync._prune_removed_content(self.db, self.provider, "movie", seen)

    def _sweep(self):
        count, _ = sync._sweep_one_type(self.db, Movie, "movie", self.root,
                                        datetime.utcnow())
        self.db.commit()
        return count

    def test_sweep_does_not_clear_the_prunes_mark(self):
        # Provider drops one title, but its .strm is still on disk — which is
        # exactly the normal nightly ordering (prune, then sweep).
        seen = self.all_seen - {3000}
        self._prune(seen)
        self._sweep()
        row = self.db.query(Movie).filter(Movie.tmdb_id == 3000).first()
        self.assertIsNotNone(row.provider_missing_since,
                             "the sweep erased the prune's first strike")
        # Second night: the provider still doesn't list it, so it is removed.
        self.assertEqual(self._prune(seen), 1)

    def test_prunes_mark_is_not_a_first_strike_for_the_sweep(self):
        seen = self.all_seen - {3001}
        self._prune(seen)                     # marks provider_missing_since
        (self.root / "M1" / "M1.strm").unlink()  # file briefly missing
        self.assertEqual(self._sweep(), 0,
                         "the sweep deleted on its first observation")
        self.assertEqual(self.db.query(Movie).count(), 100)

    def test_sweeps_mark_is_not_a_first_strike_for_the_prune(self):
        (self.root / "M2" / "M2.strm").unlink()
        self._sweep()                         # marks file_missing_since
        seen = self.all_seen - {3002}
        self.assertEqual(self._prune(seen), 0,
                         "the prune deleted on its first observation")
        self.assertEqual(self.db.query(Movie).count(), 100)

    def test_opted_out_titles_are_never_swept(self):
        # "Stop managing .strm + delete files" leaves the row with no file on
        # disk by design. Sweeping it would delete the row and the next sync
        # would re-import the title and rewrite the .strm, undoing the opt-out.
        row = self.db.query(Movie).filter(Movie.tmdb_id == 3003).first()
        row.strm_disabled = True
        self.db.commit()
        (self.root / "M3" / "M3.strm").unlink()
        self._sweep()
        self._sweep()
        self.assertIsNotNone(self.db.query(Movie).filter(Movie.tmdb_id == 3003).first())
        self.assertTrue(self.db.query(Movie).filter(Movie.tmdb_id == 3003).first().strm_disabled)

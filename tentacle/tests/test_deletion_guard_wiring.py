"""Pins the v2.241.0 deletion guards that the rest of the suite does not.

Mutation-tested against 97d25e1: every test here passes on 97d25e1 and fails
when the guard it names is reverted. The existing suite stays green under
those reverts.
Run from tentacle/:  python -m unittest discover -s tests
"""
import logging
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Series, Provider, ProviderCategory
import services.sync as sync


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _EmptyClient:
    def get_vod_streams(self, category_id):
        return []

    def get_series(self, category_id):
        return []


class DeletionGuardWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.p = Provider(name="P", server_url="http://x", username="u", password="p")
        self.db.add(self.p)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sweep_probe_protects_a_small_library_when_the_mount_is_empty(self):
        """Guard: _sweep_one_type's missing/empty-root probe.

        test_unavailable_mount_deletes_nothing keeps every .strm on disk (in another
        temp dir) and sweeps once, so it passes with the probe deleted. Here the root
        really is empty, there are fewer rows than the 50-row floor, and we sweep three
        times, so only the probe prevents deletion."""
        root = self.tmp / "vod-movies"
        root.mkdir()
        for i in range(30):
            self.db.add(Movie(tmdb_id=i + 1, title=f"M{i}", source=f"provider_{self.p.id}",
                              provider_id=self.p.id, strm_path=str(root / f"M{i}" / f"M{i}.strm")))
        self.db.commit()
        for _ in range(3):
            removed, _titles = sync._sweep_one_type(self.db, Movie, "movie", root, datetime.utcnow())
            self.db.commit()
            self.assertEqual(removed, 0)
        self.assertEqual(self.db.query(Movie).count(), 30)

    def test_prune_percentage_cap_not_just_the_50_row_floor(self):
        """Guard: PRUNE_MAX_FRACTION (5%). Existing tests only exceed the 50-row
        floor, so raising the fraction to 60% keeps them green. 2,000 rows with 150
        gone on two runs is 7.5%: above 5% and above the floor."""
        self.db.add_all(Movie(tmdb_id=10_000 + i, title=f"M{i}", source=f"provider_{self.p.id}",
                              provider_id=self.p.id) for i in range(2000))
        self.db.commit()
        seen = {10_000 + i for i in range(150, 2000)}
        sync._prune_removed_content(self.db, self.p, "movie", seen)
        self.assertEqual(sync._prune_removed_content(self.db, self.p, "movie", seen), 0)
        self.assertEqual(self.db.query(Movie).count(), 2000)

    def test_sync_movies_treats_first_empty_category_as_failed_fetch(self):
        """Guard: the call to _category_went_empty inside _sync_movies. The helper is
        tested on its own; removing the call site keeps the suite green."""
        self.db.add(ProviderCategory(provider_id=self.p.id, category_id="1", category_name="EN - NEW",
                                     type="movie", whitelisted=True, title_count=6427))
        self.db.commit()
        *_rest, cleanup = sync._sync_movies(self.p, _EmptyClient(), None, self.db, self.tmp, 7)
        self.assertIs(cleanup["fetch_ok"], False)

    def test_backfill_skips_strm_disabled_series(self):
        """Guard: _backfill_series_episodes honours strm_disabled (#3). Removing the
        check keeps the suite green and silently rewrites an opted-out series."""
        show = self.tmp / "Show (2000)"
        show.mkdir()
        self.db.add(Series(tmdb_id=77, title="Show", source=f"provider_{self.p.id}",
                           provider_id=self.p.id, strm_path=str(show), strm_disabled=True))
        self.db.commit()
        calls = []

        class Client:
            def get_series_info(self, sid):
                calls.append(sid)
                return {"episodes": {"1": [{"episode_num": 1, "id": 5, "container_extension": "mkv"}]}}

        self.assertEqual(sync._backfill_series_episodes(Client(), {"series_id": 1}, 77, self.p, self.db), 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

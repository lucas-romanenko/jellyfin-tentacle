"""One user's failing playlist sync does not stop the other users'.

Run from the tentacle/ directory:  python -m unittest discover -s tests

sync_smartlists(db) (all users), the all-user playlist refresh (after every
VOD sync's tag push and after Refresh Tags) and the YouTube publish loop ran
the users one after another with no per-user error handling, so an
exception for one user (Jellyfin failing for that user, a config file that
can't be read) skipped every user after them. The nightly loop already
isolated each user; these now do the same, roll the session back, log the
failure and go on. A SmartList config whose Name is not text (written by an
older version) is skipped instead of failing that user's every sync.
"""
import json
import random
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir

import models.database as mdb
from services import smartlists


class _Db(unittest.TestCase):
    def setUp(self):
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def users(self, n):
        us = [mdb.TentacleUser(jellyfin_user_id=f"{i:032x}", display_name=f"U{i}") for i in range(n)]
        self.db.add_all(us)
        self.db.commit()
        return [u.id for u in us]


class AllUserLoops(_Db):
    def run_loop(self, loop, target, ids, failing):
        called = []
        real_single = {"sync": smartlists.sync_smartlists, "refresh": smartlists._refresh_smartlist_playlists_inner}[loop]

        def fake(db, user_id=None, **kw):
            if user_id is None:
                return real_single(db, user_id=None, **kw)
            called.append(user_id)
            if user_id in failing:
                raise RuntimeError(f"failed for {user_id}")
            return {"created": 1, "updated": 0, "removed": 0, "total": 1, "processed": 1, "changed": 0, "errors": 0}

        with mock.patch.object(smartlists, target, side_effect=fake):
            out = getattr(smartlists, target)(self.db, user_id=None)
        return called, out

    def test_property_every_other_user_still_runs(self):
        """Property over 1,000 seeds: random users, random failing subset, both loops."""
        ids = self.users(6)
        with mock.patch("routers.collections.sync_playlist_artwork", return_value=0), \
                mock.patch.object(smartlists.logger, "warning"):
            for seed in range(1, 1001):
                rnd = random.Random(seed)
                failing = set(rnd.sample(ids, rnd.randint(0, len(ids))))
                for loop, target in (("sync", "sync_smartlists"), ("refresh", "_refresh_smartlist_playlists_inner")):
                    called, out = self.run_loop(loop, target, ids, failing)
                    self.assertEqual(ids, called, f"seed {seed} {loop}")
                    self.assertEqual(len(failing), out.get("errors", 0), f"seed {seed} {loop}")

    def test_the_session_is_usable_after_a_failed_user(self):
        ids = self.users(2)
        seen = []

        def fake(db, user_id=None, **kw):
            if user_id is None:
                return real(db, user_id=None, **kw)
            if user_id == ids[0]:
                db.add(mdb.TentacleUser(jellyfin_user_id=f"{0:032x}", display_name="dup"))  # unique clash
                db.flush()
            seen.append(db.query(mdb.TentacleUser).count())
            return {}

        real = smartlists._refresh_smartlist_playlists_inner
        with mock.patch.object(smartlists, "_refresh_smartlist_playlists_inner", side_effect=fake), \
                mock.patch.object(smartlists.logger, "warning"):
            smartlists._refresh_smartlist_playlists_inner(self.db, None)
        self.assertEqual([2], seen, "the next user ran on a session left in a failed state")


class ConfigNames(unittest.TestCase):
    def test_a_config_whose_name_is_not_text_is_skipped(self):
        root = Path(temp_dir(self))
        for folder, name in (("a", "Good"), ("b", 1), ("c", ["x"]), ("d", "")):
            (root / folder).mkdir()
            (root / folder / "config.json").write_text(json.dumps({"Name": name}), encoding="utf-8")
        self.assertEqual(["Good"], list(smartlists._scan_existing(root)))


if __name__ == "__main__":
    unittest.main()

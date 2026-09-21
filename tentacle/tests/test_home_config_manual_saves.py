"""Manual Home Screen saves must be as recoverable as generated ones.

write_home_config() copies the outgoing file into `backups/` before it writes
(the home layout is the only Tentacle state with no other snapshot). The Home
Screen page's own endpoints — reorder, add-row, remove-row, row-max-items,
hero, hero-sort, toolbar, merge-continue-watching — and the YouTube "Home row"
toggle all write through `routers/smartlists.py::_write_home_json`, which goes
straight to `_atomic_write_json`. Those are exactly the writes a user regrets.

These tests drive the real entry point (`_write_home_json`) rather than any
proposed helper, so they fail with an assertion at 1633dd1 and pass once the
backup happens inside that write.

The router/YouTube tests need fastapi; they are skipped without it.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb

try:
    import routers.smartlists as rsl
    import routers.youtube as ryt
except Exception:  # pragma: no cover - depends on optional deps
    rsl = ryt = None


def _session(tmp):
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db):
    user = mdb.TentacleUser(id=1, jellyfin_user_id="jf-1", display_name="User 1")
    db.add(user)
    db.commit()
    return user


ROWS = {
    "hero": {"enabled": False, "playlist_id": "", "display_name": ""},
    "rows": [
        {"type": "playlist", "playlist_id": "pl-a", "display_name": "Marvel Movies", "order": 1},
        {"type": "playlist", "playlist_id": "pl-b", "display_name": "HBO TV", "order": 2},
    ],
}


@unittest.skipIf(rsl is None, "fastapi not installed")
class TestWriteHomeJsonKeepsABackup(unittest.TestCase):
    """The write every Home Screen edit goes through must keep a snapshot."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home-configs"
        self.db = _session(self.tmp)
        self.user = _user(self.db)

    def _backups(self):
        return sorted((self.home / "backups").glob("jf-1-*.json"))

    def _path(self):
        return self.home / "jf-1.json"

    def test_the_replaced_config_is_backed_up(self):
        smaller = {"hero": ROWS["hero"], "rows": ROWS["rows"][:1]}
        with mock.patch.object(rsl, "HOME_CONFIG_DIR", str(self.home)):
            rsl._write_home_json(self.user, ROWS)
            rsl._write_home_json(self.user, smaller)

        backups = self._backups()
        self.assertEqual(len(backups), 1, "no backup of the config that was replaced")
        self.assertEqual(json.loads(backups[0].read_text()), ROWS)
        self.assertEqual(json.loads(self._path().read_text()), smaller)

    def test_an_identical_rewrite_does_not_consume_the_backup_history(self):
        with mock.patch.object(rsl, "HOME_CONFIG_DIR", str(self.home)):
            rsl._write_home_json(self.user, ROWS)
            rsl._write_home_json(self.user, ROWS)
        self.assertEqual(self._backups(), [],
                         "a no-op rewrite pushed real history out of the capped backup set")

    def test_a_first_write_is_fine_without_an_existing_file(self):
        with mock.patch.object(rsl, "HOME_CONFIG_DIR", str(self.home)):
            rsl._write_home_json(self.user, ROWS)
        self.assertEqual(json.loads(self._path().read_text()), ROWS)
        self.assertEqual(self._backups(), [])


@unittest.skipIf(rsl is None, "fastapi not installed")
class TestRemoveRowIsRecoverable(unittest.TestCase):

    def test_removing_a_row_leaves_a_backup_containing_it(self):
        tmp = Path(tempfile.mkdtemp())
        db = _session(tmp)
        user = _user(db)

        with mock.patch.object(rsl, "HOME_CONFIG_DIR", str(tmp / "home-configs")):
            rsl._write_home_json(user, ROWS)
            with mock.patch.object(rsl, "bump_playlist_version"), \
                 mock.patch.object(rsl, "_notify_jellyfin_plugin", lambda db: {}):
                rsl.remove_row(rsl.RemoveRowRequest(row_key="playlist:pl-b"), db=db, user=user)

            path = Path(tmp / "home-configs" / "jf-1.json")
            self.assertEqual([r["playlist_id"] for r in json.loads(path.read_text())["rows"]],
                             ["pl-a"])
            backups = list((path.parent / "backups").glob("jf-1-*.json"))
            self.assertTrue(backups, "removing a home row left no backup")
            kept = json.loads(backups[-1].read_text())
            self.assertIn("pl-b", [r["playlist_id"] for r in kept["rows"]])


if __name__ == "__main__":
    unittest.main()

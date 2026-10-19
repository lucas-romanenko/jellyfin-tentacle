"""#295: a database from before multi-user support gets working per-user tables.

Before multi-user support home_row_order and auto_playlist_toggles were keyed
by playlist_id / key alone. The generic "add missing columns" pass adds
user_id to them (it is nullable) but not id (NOT NULL, no default), and the
recreate that adds id used to take user_id as proof it had already run, so
it never did: every ORM query then failed with "no such column ... .id".

Each case runs the real startup (create_tables) in a child process, because
models.database binds its engine to DATA_DIR at import.
"""
import os
import sqlite3
import subprocess
import sys
import textwrap
import unittest
import shutil
from tmp_dirs import temp_dir

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)

OLD_HOME_ROW_ORDER = ("CREATE TABLE home_row_order (playlist_id VARCHAR NOT NULL, "
                      "display_order INTEGER NOT NULL, PRIMARY KEY (playlist_id))")
OLD_TOGGLES = ('CREATE TABLE auto_playlist_toggles ("key" VARCHAR NOT NULL, '
               'enabled BOOLEAN, PRIMARY KEY ("key"))')

PROBE = """
    import models.database as mdb
    mdb.create_tables()
    db = mdb.SessionLocal()
    out = {}
    for M in (mdb.HomeRowOrder, mdb.AutoPlaylistToggle):
        try:
            out[M.__tablename__] = sorted(
                (r.user_id, getattr(r, "playlist_id", None) or r.key,
                 getattr(r, "display_order", None) if hasattr(r, "display_order") else r.enabled)
                for r in db.query(M).all())
        except Exception as e:
            out[M.__tablename__] = "ERROR " + str(e).splitlines()[0]
            db.rollback()
    try:
        db.add(mdb.AutoPlaylistToggle(user_id=1, key="builtin:x", enabled=True))
        db.add(mdb.AutoPlaylistToggle(user_id=2, key="builtin:x", enabled=False))
        db.commit()
        out["per_user"] = "ok"
    except Exception as e:
        out["per_user"] = "ERROR " + str(e).splitlines()[0]
    print(repr(out))
"""


def _run(code, data_dir):
    env = dict(os.environ, DATA_DIR=data_dir, PYTHONPATH=APP)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env, cwd=APP,
                          capture_output=True, text=True, timeout=120)


class PreMultiUserUpgrade(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db_path = os.path.join(self.dir, "tentacle.db")

    def _old_db(self, *statements):
        c = sqlite3.connect(self.db_path)
        for s in statements:
            c.execute(s)
        c.commit()
        c.close()

    def _start(self):
        r = _run(PROBE, self.dir)
        self.assertEqual(0, r.returncode, r.stderr[-3000:])
        import ast
        return ast.literal_eval(r.stdout.strip().splitlines()[-1]), r.stderr

    def _schema(self, table):
        c = sqlite3.connect(self.db_path)
        try:
            return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
        finally:
            c.close()

    def test_upgrade_from_pre_multiuser_recreates_both_tables(self):
        self._old_db(
            OLD_HOME_ROW_ORDER, OLD_TOGGLES,
            "INSERT INTO home_row_order (playlist_id, display_order) VALUES ('pl-a', 2)",
            "INSERT INTO auto_playlist_toggles (key, enabled) VALUES ('builtin:recently_added_movies', 1)",
        )
        out, _ = self._start()
        self.assertEqual([(None, "pl-a", 2)], out["home_row_order"])
        self.assertEqual([(None, "builtin:recently_added_movies", True)], out["auto_playlist_toggles"])
        self.assertEqual("ok", out["per_user"])
        self.assertIn("id", self._schema("home_row_order"))
        self.assertIn("id", self._schema("auto_playlist_toggles"))

    def test_old_table_that_already_got_user_id_keeps_it(self):
        # What an install that already started a build with the generic pass
        # has on disk: the old primary key plus a user_id column.
        self._old_db(
            OLD_HOME_ROW_ORDER, OLD_TOGGLES,
            "ALTER TABLE home_row_order ADD COLUMN user_id INTEGER",
            "ALTER TABLE auto_playlist_toggles ADD COLUMN user_id INTEGER",
            "INSERT INTO home_row_order (playlist_id, display_order, user_id) VALUES ('pl-a', 3, 7)",
            "INSERT INTO auto_playlist_toggles (key, enabled, user_id) VALUES ('builtin:y', 0, 7)",
        )
        out, _ = self._start()
        self.assertEqual([(7, "pl-a", 3)], out["home_row_order"])
        self.assertEqual([(7, "builtin:y", False)], out["auto_playlist_toggles"])
        self.assertEqual("ok", out["per_user"])

    def test_interrupted_recreate_is_finished_from_the_old_copy(self):
        # A recreate cut off after the RENAME: only the _old tables hold data.
        self._old_db(
            OLD_HOME_ROW_ORDER.replace("home_row_order", "_home_row_order_old"),
            OLD_TOGGLES.replace("auto_playlist_toggles", "_auto_toggles_old"),
            "INSERT INTO _home_row_order_old (playlist_id, display_order) VALUES ('pl-b', 5)",
            "INSERT INTO _auto_toggles_old (key, enabled) VALUES ('builtin:z', 1)",
        )
        out, _ = self._start()
        self.assertEqual([(None, "pl-b", 5)], out["home_row_order"])
        self.assertEqual([(None, "builtin:z", True)], out["auto_playlist_toggles"])
        c = sqlite3.connect(self.db_path)
        left = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('_home_row_order_old', '_auto_toggles_old')")]
        c.close()
        self.assertEqual([], left)

    def test_fresh_and_current_databases_are_left_alone(self):
        out, _ = self._start()
        self.assertEqual([], out["home_row_order"])
        self.assertEqual("ok", out["per_user"])
        out2, _ = self._start()  # a second start is a no-op
        self.assertEqual([(1, "builtin:x", True), (2, "builtin:x", False)], out2["auto_playlist_toggles"][:2])


if __name__ == "__main__":
    unittest.main()

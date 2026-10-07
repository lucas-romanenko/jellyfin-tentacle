"""N1: an existing database gets the new columns (match_overrides.label /
moved_from, blocked_streams.label, match_suspects.reason) on start-up, and
keeps every row. The real start-up (create_tables) runs in a child process,
because models.database binds its engine to DATA_DIR at import (as in
test_upgrade_pre_multiuser_tables).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import ast
import os
import sqlite3
import subprocess
import sys
import textwrap
import unittest

from tmp_dirs import temp_dir

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)

OLD = [
    "CREATE TABLE providers (id INTEGER PRIMARY KEY, name VARCHAR)",
    "INSERT INTO providers (id, name) VALUES (1, 'P')",
    "CREATE TABLE match_overrides (id INTEGER PRIMARY KEY, provider_id INTEGER NOT NULL, media_type VARCHAR NOT NULL, "
    "stream_key VARCHAR NOT NULL, tmdb_id INTEGER NOT NULL, previous_tmdb_id INTEGER, title VARCHAR, set_by VARCHAR, "
    "created_at DATETIME, CONSTRAINT uq_match_override UNIQUE (provider_id, media_type, stream_key))",
    "INSERT INTO match_overrides (provider_id, media_type, stream_key, tmdb_id, title) VALUES (1, 'movie', '188327', 674607, 'The Decline')",
    "CREATE TABLE blocked_streams (id INTEGER PRIMARY KEY, provider_id INTEGER NOT NULL, media_type VARCHAR NOT NULL, "
    "stream_key VARCHAR NOT NULL, tmdb_id INTEGER, title VARCHAR, reason VARCHAR, blocked_by VARCHAR, created_at DATETIME)",
    "INSERT INTO blocked_streams (provider_id, media_type, stream_key, tmdb_id) VALUES (1, 'movie', '177000', 3000)",
    "CREATE TABLE match_suspects (id INTEGER PRIMARY KEY, tmdb_id INTEGER NOT NULL, media_type VARCHAR NOT NULL, "
    "title VARCHAR, expected_minutes INTEGER, actual_minutes INTEGER, jellyfin_item_id VARCHAR, dismissed BOOLEAN, "
    "detected_at DATETIME)",
    "INSERT INTO match_suspects (tmdb_id, media_type, title, expected_minutes, actual_minutes) VALUES (5, 'movie', 'F', 90, 60)",
]

PROBE = """
    import logging; logging.disable(logging.CRITICAL)
    import models.database as mdb
    mdb.create_tables()
    db = mdb.SessionLocal()
    o = db.query(mdb.MatchOverride).one(); b = db.query(mdb.BlockedStream).one(); s = db.query(mdb.MatchSuspect).one()
    print(repr({"override": (o.stream_key, o.tmdb_id, o.label, o.moved_from), "block": (b.stream_key, b.label),
                "suspect": (s.tmdb_id, s.actual_minutes, s.reason)}))
"""


class UpgradeAddsTheColumns(unittest.TestCase):
    def test_old_rows_are_kept_and_the_new_columns_are_empty(self):
        d = temp_dir(self)
        c = sqlite3.connect(os.path.join(d, "tentacle.db"))
        for sql in OLD:
            c.execute(sql)
        c.commit()
        c.close()
        env = dict(os.environ, DATA_DIR=d, PYTHONPATH=APP)
        for _ in range(2):   # a second start changes nothing
            r = subprocess.run([sys.executable, "-c", textwrap.dedent(PROBE)], env=env, cwd=APP,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(0, r.returncode, r.stderr[-2000:])
            out = ast.literal_eval(r.stdout.strip().splitlines()[-1])
            self.assertEqual({"override": ("188327", 674607, None, None), "block": ("177000", None),
                              "suspect": (5, 60, None)}, out)


if __name__ == "__main__":
    unittest.main()

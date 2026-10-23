"""The Live TV catalog's first build kept its values in a JSON `data` column
(live_networks, live_channel_catalog, live_key_aliases). On start-up every
value moves into its plain column and `data` is dropped; a second start
changes nothing; a key with no column keeps `data`. The real start-up
(create_tables) runs in a child process, because models.database binds its
engine to DATA_DIR at import (as in test_upgrade_relist_label_columns).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import ast
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import unittest

from tmp_dirs import temp_dir

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)

# The tables as Plan B (cc44e7d) created them.
OLD = [
    "CREATE TABLE providers (id INTEGER PRIMARY KEY, name VARCHAR)",
    "INSERT INTO providers (id, name) VALUES (1, 'P')",
    "CREATE TABLE live_channels (id INTEGER PRIMARY KEY, provider_id INTEGER NOT NULL, name VARCHAR NOT NULL, "
    "stream_id VARCHAR, stream_url VARCHAR NOT NULL, enabled BOOLEAN)",
    "INSERT INTO live_channels (id, provider_id, name, stream_id, stream_url, enabled) VALUES "
    "(1, 1, 'NBC A', '1', 'http://192.0.2.1/1', 1), (2, 1, 'NBC B', '2', 'http://192.0.2.1/2', 1), "
    "(3, 1, 'CNN', '3', 'http://192.0.2.1/3', 1)",
    "CREATE TABLE live_networks (id INTEGER NOT NULL, name VARCHAR NOT NULL, fold_key VARCHAR, "
    "chosen_channel_id INTEGER, merged_into INTEGER, data JSON, PRIMARY KEY (id), "
    "FOREIGN KEY(chosen_channel_id) REFERENCES live_channels (id) ON DELETE SET NULL, "
    "FOREIGN KEY(merged_into) REFERENCES live_networks (id) ON DELETE SET NULL)",
    "CREATE INDEX ix_live_networks_fold_key ON live_networks (fold_key)",
    "CREATE TABLE live_channel_catalog (channel_id INTEGER NOT NULL, network_id INTEGER, data JSON, "
    "PRIMARY KEY (channel_id), FOREIGN KEY(channel_id) REFERENCES live_channels (id) ON DELETE CASCADE, "
    "FOREIGN KEY(network_id) REFERENCES live_networks (id) ON DELETE SET NULL)",
    "CREATE INDEX ix_live_channel_catalog_network_id ON live_channel_catalog (network_id)",
    "CREATE TABLE live_key_aliases (old_key VARCHAR NOT NULL, new_key VARCHAR NOT NULL, data JSON, "
    "PRIMARY KEY (old_key))",
]
ROWS = [
    ("INSERT INTO live_networks (id, name, fold_key, chosen_channel_id, data) VALUES (?, ?, ?, ?, ?)",
     (1, "NBC", "nbc", 2, json.dumps({"auto_channel_id": 1, "name_set": True, "types": ["News", "Local"]}))),
    ("INSERT INTO live_networks (id, name, fold_key, data) VALUES (?, ?, ?, ?)",
     (2, "Gone", "gone", json.dumps({"dissolved": True, "excluded": True}))),
    ("INSERT INTO live_networks (id, name, fold_key, data) VALUES (?, ?, ?, ?)", (3, "Plain", "plain", "{}")),
    ("INSERT INTO live_channel_catalog (channel_id, network_id, data) VALUES (?, ?, ?)",
     (1, 1, json.dumps({"key": "n1", "pinned": True}))),
    ("INSERT INTO live_channel_catalog (channel_id, network_id, data) VALUES (?, ?, ?)", (2, 1, json.dumps({"key": "n1"}))),
    ("INSERT INTO live_channel_catalog (channel_id, network_id, data) VALUES (?, ?, ?)",
     (3, None, json.dumps({"types": ["News"]}))),
    ("INSERT INTO live_key_aliases (old_key, new_key, data) VALUES (?, ?, ?)",
     ("c1", "n1", json.dumps({"at": "2026-10-21T18:55:01.123456"}))),
    ("INSERT INTO live_key_aliases (old_key, new_key, data) VALUES (?, ?, ?)", ("c2", "n1", None)),
]

PROBE = """
    import logging; logging.disable(logging.CRITICAL)
    import sqlite3
    import models.database as mdb
    mdb.create_tables()
    db = mdb.SessionLocal()
    nets = {n.id: (n.name, n.chosen_channel_id, n.auto_channel_id, n.name_set, n.excluded, n.dissolved, n.types)
            for n in db.query(mdb.LiveNetwork).all()}
    cats = {c.channel_id: (c.network_id, c.pinned, c.types, c.pool_key) for c in db.query(mdb.LiveChannelCatalog).all()}
    aliases = {a.old_key: (a.new_key, a.created_at.isoformat() if a.created_at else None)
               for a in db.query(mdb.LiveKeyAlias).all()}
    conn = sqlite3.connect(mdb.DATABASE_URL.replace("sqlite:///", ""))
    data = sorted(t for t in ("live_networks", "live_channel_catalog", "live_key_aliases")
                  if "data" in {r[1] for r in conn.execute(f"PRAGMA table_info({t})")})
    print(repr({"nets": nets, "cats": cats, "aliases": aliases, "data": data}))
"""

EXPECTED = {
    "nets": {1: ("NBC", 2, 1, True, False, False, "News,Local"), 2: ("Gone", None, None, False, True, True, None),
             3: ("Plain", None, None, False, False, False, None)},
    "cats": {1: (1, True, None, "n1"), 2: (1, False, None, "n1"), 3: (None, False, "News", None)},
    "aliases": {"c1": ("n1", "2026-10-21T18:55:01.123456"), "c2": ("n1", None)},
    "data": [],
}


class UpgradeMovesDataIntoColumns(unittest.TestCase):
    def make_db(self, extra=()):
        d = temp_dir(self)
        c = sqlite3.connect(os.path.join(d, "tentacle.db"))
        for sql in OLD:
            c.execute(sql)
        for sql, args in [*ROWS, *extra]:
            c.execute(sql, args)
        c.commit()
        c.close()
        return d

    def start(self, d):
        env = dict(os.environ, DATA_DIR=d, PYTHONPATH=APP)
        r = subprocess.run([sys.executable, "-c", textwrap.dedent(PROBE)], env=env, cwd=APP,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr[-2000:])
        return ast.literal_eval(r.stdout.strip().splitlines()[-1])

    def test_every_value_moves_and_data_goes(self):
        d = self.make_db()
        for _ in range(2):   # a second start changes nothing
            self.assertEqual(EXPECTED, self.start(d))

    def test_a_key_with_no_column_keeps_data(self):
        d = self.make_db([("UPDATE live_networks SET data = ? WHERE id = 3", (json.dumps({"surprise": 1}),))])
        out = self.start(d)
        self.assertEqual(["live_networks"], out["data"])
        self.assertEqual(EXPECTED["nets"], out["nets"])
        self.assertEqual(EXPECTED["cats"], out["cats"])


if __name__ == "__main__":
    unittest.main()

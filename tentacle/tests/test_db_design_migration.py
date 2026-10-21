"""The move to the data-column layout (models/db_design.py) on upgrade.

A database from before it (the layout frozen in models/legacy_schema.py) is
converted once, in one transaction, with a copy first; every value is found
again; a second start changes nothing; a failure midway leaves the database
as it was; revert() gives the old layout back identically. A fresh install
gets the same schema straight away.

The full startup (create_tables) runs in a child process, because
models.database binds its engine to DATA_DIR at import.
"""
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from tmp_dirs import temp_dir

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)

START = """
    import models.database as mdb
    mdb.create_tables()
"""

PROBE = """
    import json
    import models.database as mdb
    mdb.create_tables()
    db = mdb.SessionLocal()
    m = db.query(mdb.Movie).filter(mdb.Movie.tmdb_id == 10).one()
    d = db.query(mdb.Movie).filter(mdb.Movie.source == "radarr").one()
    s = db.query(mdb.Series).one()
    ch = db.query(mdb.LiveUserChannel).order_by(mdb.LiveUserChannel.position).all()
    y = db.query(mdb.YouTubeChannel).one()
    dup = db.query(mdb.Duplicate).one()
    print(json.dumps({
        "movie": [m.title, m.year, m.rating, m.genres, m.tags, m.source, m.strm_disabled,
                  str(m.provider_missing_since), m.overview],
        "downloaded": [d.title, d.source, d.provider_id, d.radarr_path],
        "series": [s.source, s.sonarr_monitored, s.provider_series_ids],
        "channels": [c.channel_key for c in ch],
        "youtube": [y.include_videos, y.keep_count, y.last_skips, y.extra_tags],
        "duplicate": [dup.sources, dup.pending_user_data],
        "vod_filter": db.query(mdb.Movie).filter(mdb.Movie.source.like("provider_%"),
                                                  mdb.Movie.strm_path.isnot(None)).count(),
    }))
"""


def _run(code, data_dir):
    env = dict(os.environ, DATA_DIR=data_dir, PYTHONPATH=APP)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env, cwd=APP,
                          capture_output=True, text=True, timeout=120)


def _old_database(path):
    """A database as 1.11 left it, with the cases the conversion has to keep:
    JSON lists and a JSON 'null', 0/1 booleans, dates, NULLs, a column this
    version doesn't know (missing_since), both kinds of channel key."""
    from sqlalchemy import create_engine
    from models.legacy_schema import LEGACY
    engine = create_engine(f"sqlite:///{path}")
    LEGACY.create_all(engine)
    engine.dispose()
    c = sqlite3.connect(path)
    c.executescript("""
        ALTER TABLE movies ADD COLUMN missing_since DATETIME;
        INSERT INTO settings (key, value, updated_at) VALUES ('sync_schedule', '0 3 * * *', '2026-01-02 03:04:05.000006');
        INSERT INTO settings (key, value) VALUES ('migrated_youtube_made_for_kids_reset', '1');   -- as 1.11 left it
        INSERT INTO tentacle_users (id, jellyfin_user_id, display_name, is_admin, notifications_enabled, session_version)
            VALUES (1, 'abc', 'Lucas', 1, 1, 0);
        INSERT INTO providers (id, name, provider_type, server_url, username, password, active, priority, live_tv_enabled,
                               require_tmdb_match, has_live)
            VALUES (1, 'P', 'xtream', 'http://p.test', 'u', 'pw', 1, 1, 1, 1, 0);
        INSERT INTO movies (id, tmdb_id, title, year, overview, rating, genres, source, provider_id, strm_path, tags,
                            strm_disabled, provider_missing_since, date_added, missing_since)
            VALUES (1, 10, 'Film', '1999', NULL, 7.5, '["Action", "Drama"]', 'provider_1', 1, '/vod/Film.strm',
                    '["Netflix Movies"]', 0, '2026-10-20 01:02:03.000004', '2026-01-01 00:00:00.000000',
                    '2026-02-02 00:00:00.000000');
        INSERT INTO movies (id, tmdb_id, title, source, provider_id, radarr_path, genres, tags, date_added)
            VALUES (2, 11, 'Download', 'radarr', NULL, '/movies/Download', '[]', 'null', '2026-01-01 00:00:00.000000');
        INSERT INTO series (id, tmdb_id, title, source, provider_id, sonarr_monitored, provider_series_ids)
            VALUES (1, 20, 'Show', 'provider_1', 1, 1, '["77", "78"]');
        INSERT INTO youtube_channels (id, input_url, kind, title, slug, include_videos, keep_count, last_skips, extra_tags)
            VALUES (1, 'https://youtube.test/@x', 'channel', 'X', 'x', 1, NULL, '{"short": 2}', '[]');
        INSERT INTO live_channels (id, provider_id, name, stream_id, stream_url, group_title, enabled, sort_order)
            VALUES (5, 1, 'News', '100', 'http://p.test/live/1.ts', 'NEWS', 1, 0);
        INSERT INTO live_channel_groups (id, provider_id, name, enabled) VALUES (1, 1, 'NEWS', 1);
        INSERT INTO live_user_lineups (id, jellyfin_user_id, own_list, tuner_url) VALUES (1, 'abc', 1, 'http://t.test');
        INSERT INTO live_user_channels (id, jellyfin_user_id, channel_key, position) VALUES (1, 'abc', 'c5', 0);
        INSERT INTO live_user_channels (id, jellyfin_user_id, channel_key, position) VALUES (2, 'abc', 'y1', 1);
        INSERT INTO duplicates (id, tmdb_id, media_type, sources, resolution, pending_user_data)
            VALUES (1, 10, 'movie', '[{"source": "radarr", "path": "/m"}]', 'pending', NULL);
        INSERT INTO epg_programs (id, channel_id, title, start, stop)
            VALUES (1, 'news.test', 'Late news', '2026-10-21 22:00:00.000000', '2026-10-21 23:00:00.000000');
    """)
    c.commit()
    c.close()


def _dump(path) -> str:
    """Schema and rows of every table, in name order (a rebuilt table moves to
    the end of sqlite_master, which iterdump would follow)."""
    c = sqlite3.connect(path)
    try:
        out = []
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            out.append(c.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0])
            out += sorted(r[0] for r in c.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (t,)))
            out += [repr(r) for r in c.execute(f'SELECT * FROM "{t}" ORDER BY rowid')]
        return "\n".join(out)
    finally:
        c.close()


def _schema(path) -> dict:
    c = sqlite3.connect(path)
    try:
        return {name: sql for name, sql in c.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name")}
    finally:
        c.close()


class DataColumnUpgrade(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir(self)
        self.db_path = os.path.join(self.dir, "tentacle.db")

    def _start(self, code=START):
        r = _run(code, self.dir)
        self.assertEqual(0, r.returncode, r.stderr[-3000:])
        return r

    def _fresh_schema(self):
        other = temp_dir(self)
        r = _run(START, other)
        self.assertEqual(0, r.returncode, r.stderr[-3000:])
        return _schema(os.path.join(other, "tentacle.db"))

    def test_fresh_install_gets_the_data_column_layout(self):
        self._start()
        c = sqlite3.connect(self.db_path)
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            cols = [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
            self.assertIn("data", cols, t)
        self.assertIsNone(c.execute("SELECT 1 FROM settings WHERE key='db_design_previous_schema'").fetchone())
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO movies (tmdb_id, title, data) VALUES (1, 't', 'not json')")
        c.close()
        self.assertFalse(os.path.exists(self.db_path + ".before-db-design"))

    def test_upgrade_keeps_every_value_where_the_code_reads_it(self):
        _old_database(self.db_path)
        r = self._start(PROBE)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(["Film", "1999", 7.5, ["Action", "Drama"], ["Netflix Movies"], "provider_1", False,
                          "2026-10-20 01:02:03.000004", None], out["movie"])
        self.assertEqual(["Download", "radarr", None, "/movies/Download"], out["downloaded"])
        self.assertEqual(["provider_1", True, ["77", "78"]], out["series"])
        self.assertEqual(["c5", "y1"], out["channels"])
        self.assertEqual([True, None, {"short": 2}, []], out["youtube"])   # a NULL keep_count stays NULL
        self.assertEqual([[{"source": "radarr", "path": "/m"}], None], out["duplicate"])
        self.assertEqual(1, out["vod_filter"])

        c = sqlite3.connect(self.db_path)
        data = json.loads(c.execute("SELECT data FROM movies WHERE id = 1").fetchone()[0])
        self.assertEqual("2026-02-02 00:00:00.000000", data["missing_since"])   # a column this version doesn't know
        self.assertNotIn("overview", data)                                       # NULL is no key
        self.assertNotIn("source", data)                                         # derived from provider_id
        self.assertIs(data["strm_disabled"], False)
        data2 = json.loads(c.execute("SELECT data FROM movies WHERE id = 2").fetchone()[0])
        self.assertIsNone(data2["tags"])                                          # the text 'null' is kept as a null
        self.assertEqual([(5, None), (None, 1)], c.execute(
            "SELECT live_channel_id, youtube_channel_id FROM live_user_channels ORDER BY id").fetchall())
        c.close()

        self.assertTrue(os.path.exists(self.db_path + ".before-db-design"))
        self.assertEqual(self._fresh_schema(),
                         {k: v for k, v in _schema(self.db_path).items()})

    def test_a_second_start_changes_nothing(self):
        _old_database(self.db_path)
        self._start()
        first = _dump(self.db_path)
        backup_time = os.path.getmtime(self.db_path + ".before-db-design")
        self._start()
        self.assertEqual(first, _dump(self.db_path))
        self.assertEqual(backup_time, os.path.getmtime(self.db_path + ".before-db-design"))

    def test_the_backup_is_the_old_database(self):
        _old_database(self.db_path)
        before = _dump(self.db_path)
        self._start()
        self.assertEqual(before, _dump(self.db_path + ".before-db-design"))

    def test_a_failure_midway_leaves_the_database_as_it_was(self):
        from models import db_design
        _old_database(self.db_path)
        before = _dump(self.db_path)
        real = db_design._convert_table
        calls = []

        def fail_on_the_tenth(conn, table):
            calls.append(table)
            if len(calls) == 10:
                raise OSError("disk full")
            return real(conn, table)

        with mock.patch.object(db_design, "_convert_table", fail_on_the_tenth):
            with self.assertRaises(OSError):
                db_design.convert(self.db_path)
        self.assertEqual(before, _dump(self.db_path))
        # The next start does it all
        self._start()
        self.assertEqual([], db_design.old_layout_tables(sqlite3.connect(self.db_path)))

    def test_revert_gives_the_old_database_back(self):
        from models import db_design
        _old_database(self.db_path)
        before = _dump(self.db_path)
        self._start()
        db_design.revert(self.db_path)
        self.assertEqual(before, _dump(self.db_path))

    def test_a_reverted_database_converts_again(self):
        from models import db_design
        _old_database(self.db_path)
        self._start()
        converted = _dump(self.db_path)
        db_design.revert(self.db_path)
        self._start()
        # Same values; only the record of the old layout (its date) may differ
        strip = lambda d: "\n".join(l for l in d.splitlines() if "db_design_previous_schema" not in l)
        self.assertEqual(strip(converted), strip(_dump(self.db_path)))


if __name__ == "__main__":
    unittest.main()


class ServedAsBefore(unittest.TestCase):
    """What the side-by-side run of main and this branch on the same data found."""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.mdb = mdb

    def test_the_old_layout_record_is_not_a_setting(self):
        from routers import settings
        self.db.add(self.mdb.Setting(key="db_design_previous_schema", value="2026-10-21", data={"tables": {}}))
        self.db.add(self.mdb.Setting(key="sync_schedule", value="0 3 * * *"))
        self.db.commit()
        self.assertEqual({"sync_schedule"}, set(settings.get_settings(self.db)))

    def test_source_tags_for_rule_conditions_come_from_data(self):
        from routers import tags
        m = self.mdb
        self.db.add(m.Provider(name="P", server_url="http://p.test", username="u", password="p"))
        self.db.add_all([m.Movie(tmdb_id=1, title="A", provider_id=1, source_tag="Netflix Movies"),
                         m.Movie(tmdb_id=2, title="B", provider_id=1, source_tag="Netflix Movies"),
                         m.Series(tmdb_id=3, title="C", provider_id=1, source_tag="Disney+ Series"),
                         m.Movie(tmdb_id=4, title="D", source="radarr")])
        self.db.commit()
        user = m.TentacleUser(jellyfin_user_id="abc", display_name="A", is_admin=True)
        self.db.add(user)
        self.db.commit()
        out = tags.condition_options(db=self.db, user=user)
        self.assertEqual(["Disney+ Series", "Netflix Movies"], out["sources"])

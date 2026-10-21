"""
Tentacle - The move to the db-design layout, and the way back

Until 1.11 every detail of a row had a column of its own. Now a table keeps
a few real columns (ids, foreign keys, unique keys, and what every row has
and SQL filters, sorts or joins on) and the rest in one JSON column, `data`
(models/data_fields.py; the reasons per column: Database.md in the project
page).

convert() runs once at startup on a database still in the old layout:

- A copy of the database is written first, next to it
  (tentacle.db.before-db-design), with SQLite's backup API.
- Every table is rebuilt in ONE transaction: a crash or a full disk midway
  leaves the database exactly as it was, and the next start does it again.
- Every value is kept: a real column is copied as it is, every other old
  column goes into `data` under its own name (columns this version doesn't
  know too). NULL is no key. Two old columns are derived and not kept:
  movies/series.source (provider_id says it) and
  live_user_channels.channel_key (now two foreign keys); a value that didn't
  match what they say is kept in data as source_was / channel_key_was.
- The old tables' SQL is kept in settings (key PREVIOUS_SCHEMA_KEY, in
  `data`), which revert() uses to give the old layout back exactly.

revert() is the way back (python -m models.db_design revert <db>): each
table in its old layout again, every value where it was. Run it with
Tentacle stopped, then start the previous version.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime

logger = logging.getLogger(__name__)

PREVIOUS_SCHEMA_KEY = "db_design_previous_schema"
BACKUP_SUFFIX = ".before-db-design"
_OLD = "_dbd_old_"

# Old columns that are worked out from a real column now, by table
DERIVED = {"movies": "source", "series": "source", "live_user_channels": "channel_key"}
_DOWNLOADED = {"movies": "radarr", "series": "sonarr"}


def _models():
    from models.database import Base
    return Base


def _q(name) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(conn, table) -> list:
    """[(name, declared type)] of a table, in order; [] if there is no table."""
    return [(r[1], r[2] or "") for r in conn.execute(f'PRAGMA table_info("{table}")')]


def old_layout_tables(conn) -> list:
    """The model tables that exist and have no `data` column yet."""
    out = []
    for name in _models().metadata.tables:
        cols = _columns(conn, name)
        if cols and "data" not in {c for c, _ in cols}:
            out.append(name)
    return out


def _model_class(table):
    for mapper in _models().registry.mappers:
        if mapper.local_table.name == table:
            return mapper.class_
    raise KeyError(table)


def _is_json_type(declared: str) -> bool:
    return declared.strip().upper() == "JSON"


def _is_bool_type(declared: str) -> bool:
    return declared.strip().upper() == "BOOLEAN"


def _stored(declared: str, value):
    """An old column's value as it goes into `data`."""
    if _is_json_type(declared) and isinstance(value, str):
        try:
            return json.loads(value)   # the JSON itself; the text 'null' is a null that's kept
        except ValueError:
            return value
    if _is_bool_type(declared) and value in (0, 1):
        return bool(value)
    if isinstance(value, bytes):
        raise ValueError("a binary value can't go into data")
    return value


def _derived(table, row: dict, data: dict, new: dict):
    """Fill the real columns that replace a derived old one; keep an old value
    that didn't match in data."""
    if table in ("movies", "series"):
        pid = row.get("provider_id")
        expected = f"provider_{pid}" if pid is not None else _DOWNLOADED[table]
        if "source" in row and row["source"] != expected:
            data["source_was"] = row["source"]
    elif table == "live_user_channels":
        key = str(row.get("channel_key") or "")
        kind, number = key[:1], key[1:]
        if kind in ("c", "y") and number.isdigit():
            new["live_channel_id" if kind == "c" else "youtube_channel_id"] = int(number)
        else:
            data["channel_key_was"] = row.get("channel_key")


def _convert_table(conn, table) -> int:
    from sqlalchemy.schema import CreateIndex, CreateTable
    from models.database import engine
    model = _models().metadata.tables[table]
    real = [c.name for c in model.columns if c.name != "data"]
    old = _columns(conn, table)
    old_types = dict(old)
    derived = DERIVED.get(table)

    # The old table steps aside (its indexes go with it when it's dropped) and
    # the new one is made exactly as a fresh install makes it
    _drop_indexes(conn, table)
    conn.execute(f"ALTER TABLE {_q(table)} RENAME TO {_q(_OLD + table)}")
    conn.execute(str(CreateTable(model).compile(dialect=engine.dialect)))
    missing = _missing_real(model, table, {c for c, _ in old})
    names = real + ["data"]
    insert = (f"INSERT INTO {_q(table)} ({', '.join(_q(n) for n in names)}) "
              f"VALUES ({', '.join('?' for _ in names)})")
    cur = conn.execute(f"SELECT * FROM {_q(_OLD + table)}")
    cols = [d[0] for d in cur.description]
    count = 0
    while True:
        batch = cur.fetchmany(2000)
        if not batch:
            break
        rows = []
        for values in batch:
            row = dict(zip(cols, values))
            data, new = {}, {}
            for name, value in row.items():
                if name in real or name == derived or value is None:
                    continue
                data[name] = _stored(old_types[name], value)
            for name in real:
                if name in row:
                    new[name] = row[name]
                elif name in missing:
                    new[name] = missing[name]
            if derived:
                _derived(table, row, data, new)
            rows.append([new.get(n) for n in real] + [json.dumps(data)])
        conn.executemany(insert, rows)
        count += len(rows)
    conn.execute(f"DROP TABLE {_q(_OLD + table)}")
    for index in model.indexes:
        conn.execute(str(CreateIndex(index).compile(dialect=engine.dialect)))
    return count


def _drop_indexes(conn, table):
    """A table's own indexes (not those of its constraints), so the table that
    replaces it can have indexes of the same names."""
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
                                "AND sql IS NOT NULL", (table,)).fetchall():
        conn.execute(f"DROP INDEX {_q(name)}")


def _begin(conn):
    # A table renamed aside must not take other tables' foreign keys with it
    conn.execute("PRAGMA legacy_alter_table=ON")
    conn.execute("BEGIN IMMEDIATE")


def _missing_real(model, table, old_names) -> dict:
    """{column: value} for the real columns the old table never had (a table
    from a very old build): the column's default, and a NOT NULL one without a
    default gets an empty value, so the conversion can't fail on it."""
    from sqlalchemy import Boolean, Integer, Float
    out = {}
    for col in model.columns:
        if col.name == "data" or col.name in old_names or col.primary_key:
            continue
        default = getattr(col.default, "arg", None) if col.default is not None else None
        if default is not None and not callable(default):
            out[col.name] = default
        elif not col.nullable:
            out[col.name] = 0 if isinstance(col.type, (Boolean, Integer, Float)) else ""
        else:
            continue
        logger.warning(f"[migrate] {table}.{col.name} didn't exist: rows get {out[col.name]!r}")
    return out


def _old_schema(conn, tables) -> dict:
    out = {}
    for t in tables:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
        indexes = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY name", (t,))]
        out[t] = {"sql": sql, "indexes": indexes, "columns": _columns(conn, t)}
    return out


def backup(db_path) -> str:
    """A copy of the database next to it, before anything changes. An older
    copy is kept: it was taken before the same conversion (which never
    finished, or the database would not be here again)."""
    dest = db_path + BACKUP_SUFFIX
    if os.path.exists(dest):
        return dest
    part = dest + ".part"
    src = sqlite3.connect(db_path, timeout=30)
    try:
        out = sqlite3.connect(part)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    os.replace(part, dest)
    return dest


def convert(db_path) -> dict:
    """Move a database in the old layout to the db-design layout (see above).
    Returns {table: rows} of what was converted ({} when nothing was left)."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        if not old_layout_tables(conn):
            return {}
        copy = backup(db_path)
        logger.info(f"[migrate] Moving the database to the data-column layout; a copy is in {copy}")
        _begin(conn)
        try:
            tables = old_layout_tables(conn)   # again, now that nobody else can write
            schema = _old_schema(conn, tables)
            done = {t: _convert_table(conn, t) for t in tables}
            if done and not _columns(conn, "settings"):
                from sqlalchemy.schema import CreateTable
                from models.database import engine
                conn.execute(str(CreateTable(_models().metadata.tables["settings"]).compile(dialect=engine.dialect)))
            if done:
                previous = {"converted_at": datetime.utcnow().isoformat(timespec="seconds"),
                            "backup": os.path.basename(copy), "tables": schema}
                old = conn.execute("SELECT data FROM settings WHERE key = ?", (PREVIOUS_SCHEMA_KEY,)).fetchone()
                if old:   # converted before, reverted, now again: keep the first layout's tables too
                    first = json.loads(old[0] or "{}").get("tables") or {}
                    previous["tables"] = {**first, **schema}
                conn.execute("INSERT OR REPLACE INTO settings (key, value, data) VALUES (?, ?, ?)",
                             (PREVIOUS_SCHEMA_KEY, previous["converted_at"], json.dumps(previous)))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        logger.info(f"[migrate] Data-column layout: {len(done)} table(s), "
                    f"{sum(done.values())} row(s), every value kept")
        return done
    finally:
        conn.close()


# ── The way back ─────────────────────────────────────────────────────────────

def _old_value(declared: str, value):
    if _is_json_type(declared):
        return json.dumps(value)   # a kept null is the text 'null', as it was
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _derived_old(table, row: dict, data: dict):
    if table in ("movies", "series"):
        if "source_was" in data:
            return data["source_was"]
        pid = row.get("provider_id")
        return f"provider_{pid}" if pid is not None else _DOWNLOADED[table]
    if table == "live_user_channels":
        if "channel_key_was" in data:
            return data["channel_key_was"]
        if row.get("live_channel_id") is not None:
            return f"c{row['live_channel_id']}"
        if row.get("youtube_channel_id") is not None:
            return f"y{row['youtube_channel_id']}"
    return None


def revert(db_path) -> dict:
    """Give a converted database its old layout back (see the module's text).
    One transaction; returns {table: rows}."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        row = conn.execute("SELECT data FROM settings WHERE key = ?", (PREVIOUS_SCHEMA_KEY,)).fetchone()
        if not row:
            raise RuntimeError("this database has no record of its old layout (never converted?)")
        previous = json.loads(row[0])
        _begin(conn)
        try:
            done = {}
            for table, old in previous["tables"].items():
                cols = _columns(conn, table)
                if not cols or "data" not in {c for c, _ in cols}:
                    continue   # not converted (or already back)
                done[table] = _revert_table(conn, table, old)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return done
    finally:
        conn.close()


def _revert_table(conn, table, old) -> int:
    old_cols = [c for c, _ in old["columns"]]
    old_types = dict((c, t) for c, t in old["columns"])
    _drop_indexes(conn, table)
    conn.execute(f"ALTER TABLE {_q(table)} RENAME TO {_q(_OLD + table)}")
    conn.execute(old["sql"])
    cur = conn.execute(f"SELECT * FROM {_q(_OLD + table)}")
    cols = [d[0] for d in cur.description]
    insert = (f"INSERT INTO {_q(table)} ({', '.join(_q(c) for c in old_cols)}) "
              f"VALUES ({', '.join('?' for _ in old_cols)})")
    derived = DERIVED.get(table)
    count = 0
    while True:
        batch = cur.fetchmany(2000)
        if not batch:
            break
        rows = []
        for values in batch:
            row = dict(zip(cols, values))
            data = json.loads(row.get("data") or "{}")
            out = []
            for c in old_cols:
                if c == derived:
                    out.append(_derived_old(table, row, data))
                elif c in row and c != "data":
                    out.append(row[c])
                else:
                    out.append(_old_value(old_types[c], data.get(c)) if c in data else None)
            rows.append(out)
        conn.executemany(insert, rows)
        count += len(rows)
    conn.execute(f"DROP TABLE {_q(_OLD + table)}")
    for sql in old["indexes"]:
        conn.execute(sql)
    if table == "settings":
        conn.execute("DELETE FROM settings WHERE key = ?", (PREVIOUS_SCHEMA_KEY,))
    return count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 3 or sys.argv[1] not in ("convert", "revert"):
        sys.exit("usage: python -m models.db_design convert|revert <path to tentacle.db>")
    result = (convert if sys.argv[1] == "convert" else revert)(sys.argv[2])
    print(json.dumps(result, indent=1))

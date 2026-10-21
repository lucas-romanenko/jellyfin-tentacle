"""
Tentacle - Fields kept in a row's JSON `data` column

The database follows a small set of rules (docs: Database.md in the project
page): a table has a few real columns, the ones every row has and that SQL
filters, sorts or joins on (ids, foreign keys, unique keys), and everything
else lives in one JSON text column, `data`.

`DataField` keeps the code that reads and writes those details unchanged: on
a row, `movie.overview` reads and writes `data["overview"]`; on the class,
`Movie.overview` is `json_extract(movies.data, '$.overview')`, so filters,
sorts and bulk updates keep working. Values are stored as SQLite stored the
old column (dates as SQLAlchemy's "YYYY-MM-DD HH:MM:SS.ffffff" text, so a
date in `data` compares like a date column), a None value is no key at all.

Every read of a list or dict gives a copy: change it, then set it back
(`row.tags = tags`), and the row is saved. Changing it in place saves nothing,
as with the old JSON columns.
"""

import copy
import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, event, func, literal, type_coerce
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import object_session
from sqlalchemy.sql.elements import ClauseElement

DT_FORMAT = "%Y-%m-%d %H:%M:%S.%f"   # what SQLAlchemy writes for a DateTime column in SQLite
_TOUCHED = "_data_fields_set"         # instance __dict__ key: fields set since the last flush


def _to_stored(kind, value):
    """A Python value as it goes into `data` (None: no key)."""
    if value is None:
        return None
    if kind is DateTime:
        return value.strftime(DT_FORMAT) if isinstance(value, datetime) else str(value)
    if kind is Boolean:
        return bool(value)
    if kind in (String, Text):
        # A text column stored a number as its text (SQLite's TEXT affinity)
        if isinstance(value, bool):
            return str(int(value))
        return str(value) if isinstance(value, (int, float)) else value
    if kind is Integer and isinstance(value, bool):
        return int(value)
    if kind is JSON:
        return copy.deepcopy(value)
    return value


def _from_stored(kind, value):
    """A value read from `data`, as the old column gave it."""
    if value is None:
        return None
    if kind is DateTime:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return value
        return value
    if kind is Boolean:
        return bool(value)
    if kind is Float and isinstance(value, int):
        return float(value)
    if kind is Integer and isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (list, dict)):
        return copy.deepcopy(value)
    return value


class DataField(hybrid_property):
    """One detail of a row, kept in its `data` column under the attribute's name.

    `kind` is the SQLAlchemy type the old column had (Integer, String, Text,
    Boolean, Float, DateTime, JSON); `default` and `onupdate` work as on a
    Column (a value, or a callable such as datetime.utcnow).
    """

    def __init__(self, kind, default=None, onupdate=None):
        self.kind = kind
        self.default = default
        self.onupdate = onupdate
        self.name = None
        super().__init__(self._get, self._set, expr=self._expr, update_expr=self._update)

    def __set_name__(self, owner, name):
        self.name = name
        self.__name__ = name
        fields = owner.__dict__.get("__data_fields__")
        if fields is None:
            fields = {}
            owner.__data_fields__ = fields
        fields[name] = self

    # On a row

    def _get(self, row):
        return _from_stored(self.kind, (row.data or {}).get(self.name))

    def _set(self, row, value):
        stored = _to_stored(self.kind, value)
        data = dict(row.data or {})
        if stored is None:
            data.pop(self.name, None)
        else:
            data[self.name] = stored
        row.data = data
        row.__dict__.setdefault(_TOUCHED, set()).add(self.name)

    # In SQL

    def path(self):
        return f"$.{self.name}"

    def _expr(self, cls):
        if self.kind is JSON:
            # The JSON text of the value (NULL when there is none), which the
            # JSON type turns back into Python
            return type_coerce(cls.data.op("->")(self.path()), JSON)
        return type_coerce(func.json_extract(cls.data, self.path()), self.kind())

    def _update(self, cls, value):
        if value is None:
            return [(cls.data, func.json_remove(cls.data, self.path()))]
        if isinstance(value, ClauseElement):
            new = value
        elif self.kind is Boolean:
            new = func.json("true" if value else "false")
        elif self.kind is JSON:
            new = func.json(json.dumps(value))
        else:
            new = literal(_to_stored(self.kind, value))
        return [(cls.data, func.json_set(cls.data, self.path(), new))]


def data_fields(cls) -> dict:
    """{name: DataField} of a model, its bases' included."""
    out = {}
    for klass in reversed(cls.__mro__):
        out.update(klass.__dict__.get("__data_fields__") or {})
    return out


def _call(fn):
    return fn() if callable(fn) else fn


def _touched(row) -> set:
    return row.__dict__.get(_TOUCHED) or set()


def _fill_defaults(mapper, connection, row):
    """Before an INSERT: the defaults of fields nobody set, as a Column's."""
    if row.data is None:
        row.data = {}
    touched = _touched(row)
    for name, field in data_fields(type(row)).items():
        if field.default is None or name in touched or name in row.data:
            continue
        field._set(row, _call(field.default))


def _apply_onupdate(mapper, connection, row):
    """Before an UPDATE that changes something: onupdate fields nobody set."""
    fields = [(n, f) for n, f in data_fields(type(row)).items() if f.onupdate is not None]
    if not fields:
        return
    session = object_session(row)
    if session is not None and not session.is_modified(row, include_collections=False):
        return
    touched = _touched(row)
    for name, field in fields:
        if name not in touched:
            field._set(row, _call(field.onupdate))


def _forget_touched(mapper, connection, row):
    row.__dict__.pop(_TOUCHED, None)


def register(base):
    """Give every model of `base` its DataField defaults and onupdates."""
    event.listen(base, "before_insert", _fill_defaults, propagate=True)
    event.listen(base, "before_update", _apply_onupdate, propagate=True)
    event.listen(base, "after_insert", _forget_touched, propagate=True)
    event.listen(base, "after_update", _forget_touched, propagate=True)

""""From My Lists" must not offer titles Jellyfin already has (#5).

The Popular/Trending/Search rows use _is_in_library(), which consults
Tentacle's tables and then the Jellyfin ownership map. The "From My Lists"
row (_get_missing_from_lists) only consults Tentacle's tables, so a title
that is in Jellyfin but was never recorded by Tentacle is offered as missing.

Does not need fastapi: requests + sqlalchemy only, with stand-ins.
Run from tentacle/:  python -m unittest tests.test_discover_from_my_lists
"""
import sys
import types
import unittest
from unittest import mock


def _install_framework_stubs():
    try:
        import fastapi  # noqa: F401
        return
    except ImportError:
        pass
    # The same stand-ins as the other Discover test files, so whichever file
    # the test runner imports first installs working ones for all of them.
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def __init__(self, *a, **kw):
            pass

        def _deco(self, *a, **kw):
            return lambda fn: fn

        get = post = put = delete = patch = _deco

    fastapi.HTTPException = HTTPException
    fastapi.APIRouter = APIRouter
    fastapi.Depends = lambda *a, **kw: None
    fastapi.Request = type("Request", (), {})
    fastapi.Response = type("Response", (), {})
    responses = types.ModuleType("fastapi.responses")

    class Response:
        def __init__(self, content=b"", media_type=None, **kw):
            self.body = content
            self.media_type = media_type

    responses.Response = Response
    fastapi.responses = responses
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    try:
        import pydantic  # noqa: F401
    except ImportError:
        pydantic = types.ModuleType("pydantic")
        pydantic.BaseModel = type("BaseModel", (), {})
        sys.modules["pydantic"] = pydantic
    try:
        import httpx  # noqa: F401
    except ImportError:
        httpx = types.ModuleType("httpx")
        httpx.HTTPError = type("HTTPError", (Exception,), {})
        httpx.AsyncClient = object
        sys.modules["httpx"] = httpx
    auth = types.ModuleType("routers.auth")
    auth.get_user_from_request = lambda *a, **kw: None
    auth.require_admin = lambda *a, **kw: None
    sys.modules["routers.auth"] = auth


_install_framework_stubs()

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from models.database import Base, ListItem, ListSubscription, Movie  # noqa: E402
import routers.discover as discover  # noqa: E402

# What Jellyfin owns, per media type, as _get_jellyfin_tmdb_items returns it.
JELLYFIN = {"movie": {10: "jf-item-10"}, "series": {20: "jf-item-20"}}


class TestFromMyLists(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        sub = ListSubscription(name="l", type="trakt", url="u", tag="t", active=True)
        self.db.add(sub)
        self.db.flush()
        self.db.add_all([
            ListItem(list_id=sub.id, tmdb_id=10, media_type="movie", title="In Jellyfin only", poster_path="/p.jpg"),
            ListItem(list_id=sub.id, tmdb_id=11, media_type="movie", title="Missing", poster_path="/p.jpg"),
            ListItem(list_id=sub.id, tmdb_id=12, media_type="movie", title="In Tentacle", poster_path="/p.jpg"),
            ListItem(list_id=sub.id, tmdb_id=20, media_type="series", title="Show in Jellyfin", poster_path="/p.jpg"),
            ListItem(list_id=sub.id, tmdb_id=21, media_type="series", title="Show missing", poster_path="/p.jpg"),
        ])
        self.db.add(Movie(tmdb_id=12, title="In Tentacle", source="provider_1"))
        self.db.commit()
        p = mock.patch.object(discover, "_get_jellyfin_tmdb_items",
                              lambda media_type: JELLYFIN["series" if media_type == "series" else "movie"])
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.db.close()

    def missing(self, type_filter):
        rows = discover._get_missing_from_lists(self.db, discover._known_tmdb_ids(self.db), type_filter)
        return sorted(r["tmdb_id"] for r in rows)

    def test_movies_row_skips_titles_jellyfin_already_has(self):
        self.assertEqual(self.missing("movies"), [11])

    def test_series_row_skips_titles_jellyfin_already_has(self):
        self.assertEqual(self.missing("series"), [21])

    def test_titles_in_tentacles_tables_are_still_skipped(self):
        """Must not change."""
        self.assertNotIn(12, self.missing("movies"))


if __name__ == "__main__":
    unittest.main()

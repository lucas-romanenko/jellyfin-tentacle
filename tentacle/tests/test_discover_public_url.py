""""Watch in Jellyfin" deep links must use an address the browser can reach.

jellyfin_url is the address Tentacle's server uses (docs recommend
http://jellyfin:8096 in Docker), so a link built from it is dead in the
user's browser. An optional jellyfin_public_url overrides it for links only
(the setting name is the proposal in the issue; the behaviour is what matters).

Does not need fastapi: requests + sqlalchemy only, with stand-ins.
Run from tentacle/:  python -m pytest tests/test_discover_public_url.py
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

from models.database import Base, Setting  # noqa: E402
import routers.discover as discover  # noqa: E402
import services.jellyfin as jellyfin  # noqa: E402


class TestWatchLinkAddress(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add_all([Setting(key="jellyfin_url", value="http://jellyfin:8096"),
                         Setting(key="jellyfin_api_key", value="k")])
        self.db.commit()
        self.used = []
        cache = dict(discover._jf_server_id_cache)
        cache.update({k: v for k, v in (("id", None), ("checked", False), ("retry_at", 0.0)) if k in cache})
        for p in (mock.patch.object(discover, "_jf_server_id_cache", cache),
                  mock.patch.object(jellyfin.JellyfinService, "get_server_id",
                                    lambda svc: self.used.append(svc.url) or "srv1")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.db.close()

    def test_link_uses_public_url_when_set(self):
        self.db.add(Setting(key="jellyfin_public_url", value="https://jf.example.com/"))
        self.db.commit()
        self.assertEqual(discover._jellyfin_web_url(self.db, "abc"),
                         "https://jf.example.com/web/#/details?id=abc&serverId=srv1")
        self.assertEqual(self.used, ["http://jellyfin:8096"])  # server id still fetched internally

    def test_link_falls_back_to_jellyfin_url(self):
        """Must not change: without the new setting, links look exactly as before."""
        self.assertEqual(discover._jellyfin_web_url(self.db, "abc"),
                         "http://jellyfin:8096/web/#/details?id=abc&serverId=srv1")


if __name__ == "__main__":
    unittest.main()

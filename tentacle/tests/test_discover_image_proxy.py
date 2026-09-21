"""TVDB image proxy (routers/discover.py::image_proxy).

* #2 regression: a double-encoded artworks.thetvdb.com URL is accepted, and the
  SSRF allowlist still rejects LAN / look-alike hosts after decoding;
* the disk cache must only be written under md5(url): the backend endpoint is
  unauthenticated and reachable directly (port 8888), and at 0e1805f (v2.242.0)
  any cache_key is accepted, so image B can be stored under image A's key
  (served for A to every client afterwards, with no expiry) and made-up keys
  write unlimited copies.

Does not need fastapi: runs with only requests + sqlalchemy installed (like
CI); minimal stand-ins are installed when fastapi/httpx/pydantic are missing.

Run from tentacle/:  python -m pytest tests/test_discover_image_proxy.py
"""
import asyncio
import hashlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote


# ── Stand-ins for web-framework modules CI doesn't install ────────────────
def _install_framework_stubs():
    try:
        import fastapi  # noqa: F401
        return
    except ImportError:
        pass

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

    if "pydantic" not in sys.modules:
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

import routers.discover as discover  # noqa: E402
import services.ssrf as ssrf  # noqa: E402

TVDB_A = "https://artworks.thetvdb.com/banners/posters/262638-1.jpg"
TVDB_B = "https://artworks.thetvdb.com/banners/posters/258148-1.jpg"


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class FakeAsyncClient:
    fetched = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        FakeAsyncClient.fetched.append(url)
        return types.SimpleNamespace(status_code=200, content=f"bytes-of:{url}".encode(),
                                     headers={"content-type": "image/jpeg"})


class TestImageProxy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        FakeAsyncClient.fetched = []
        for p in (
            mock.patch.object(discover, "TVDB_PROXY_CACHE", Path(self.tmp.name)),
            mock.patch.object(discover.httpx, "AsyncClient", FakeAsyncClient),
            mock.patch.object(ssrf, "host_is_public", lambda host: True),  # no DNS in tests
        ):
            p.start()
            self.addCleanup(p.stop)

    def call(self, key, url):
        return asyncio.run(discover.image_proxy(key, url=url))

    def files(self):
        return sorted(os.listdir(self.tmp.name))

    def test_double_encoded_tvdb_url_is_accepted(self):
        """#2 regression: a client that re-encodes the query value still gets the image."""
        double = quote(TVDB_A, safe="")
        self.assertEqual(discover._normalize_proxy_url(double), TVDB_A)
        self.assertEqual(discover._normalize_proxy_url(quote(double, safe="")), TVDB_A)
        resp = self.call(md5(TVDB_A), double)
        self.assertEqual(resp.body, f"bytes-of:{TVDB_A}".encode())

    def test_lan_and_lookalike_hosts_are_rejected(self):
        """#2 must not change: the SSRF allowlist still holds after decoding."""
        for bad in ("http://192.0.2.10/x.jpg", quote("http://192.0.2.10/x.jpg", safe=""),
                    "https://thetvdb.com.evil.test/x.jpg", "http://169.254.169.254/?x=thetvdb.com"):
            with self.assertRaises(discover.HTTPException) as cm:
                self.call(md5(bad), bad)
            self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(FakeAsyncClient.fetched, [])

    def test_minted_url_is_fetched_once_then_served_from_cache(self):
        """Must not change: the URLs _rewrite_tvdb_url mints keep working and caching."""
        path = discover._rewrite_tvdb_url(TVDB_A)
        key = path.split("/image-proxy/")[1].split("?")[0]
        self.call(key, TVDB_A)
        self.call(key, TVDB_A)
        self.assertEqual(FakeAsyncClient.fetched, [TVDB_A])
        self.assertEqual(self.files(), [f"{md5(TVDB_A)}.jpg"])

    def test_cache_key_must_match_the_url(self):
        """Made-up keys must not write files (unbounded copies of one image)."""
        with self.assertRaises(discover.HTTPException) as cm:
            self.call("0" * 32, TVDB_A)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.files(), [])

    def test_image_cannot_be_stored_under_another_images_key(self):
        """Cache poisoning: B fetched under A's key would be served for A forever."""
        try:
            self.call(md5(TVDB_A), TVDB_B)
        except discover.HTTPException:
            pass
        resp = self.call(md5(TVDB_A), TVDB_A)
        self.assertEqual(resp.body, f"bytes-of:{TVDB_A}".encode())


if __name__ == "__main__":
    unittest.main()

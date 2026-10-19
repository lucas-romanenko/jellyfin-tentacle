"""#464: a DNS lookup that hangs must not freeze the server.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The SSRF checks resolve the host with a blocking getaddrinfo. #371 moved the
Live TV stream open's check off the event loop; the VOD request (two lookups),
every redirect hop of a VOD or Live TV stream, the HLS variant and playlist-line
checks and the TVDB image proxy still ran it on the loop. Tentacle runs one
uvicorn worker, so while the resolver hung, every running stream, recording and
API request waited with it.

Each test runs a ticker next to the request and measures the longest the loop
went without running it: a blocked loop stalls for the whole hang. What is
accepted and refused is unchanged; artwork already in the cache is served
without a lookup.
"""
import asyncio
import hashlib
import ipaddress
import socket
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import HTTPException

from services import ssrf
from tmp_dirs import temp_dir

HANG = 0.4
# A free loop runs the 10 ms ticker every ~10 ms; a blocked one not for HANG.
MAX_STALL = HANG / 2
EAI_AGAIN = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
DNS = {
    "provider.test": ["93.184.216.34"],
    "cdn.example": ["93.184.216.35"],
    "artworks.thetvdb.com": ["151.101.1.1"],
}


def _gai_hanging(*hanging):
    """getaddrinfo from DNS; the names in `hanging` time out as in an outage."""
    def gai(host, *a, **kw):
        if host in hanging:
            time.sleep(HANG)
            raise EAI_AGAIN
        answer = DNS.get(host)
        if answer is None:
            try:
                ipaddress.ip_address(host)
                answer = [host]
            except ValueError:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answer]
    return gai


async def _longest_stall(make_coro):
    """Run make_coro() next to a ticker; (its result or exception, longest stall)."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    stall = 0.0

    async def ticker():
        nonlocal stall
        last = loop.time()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = loop.time()
            stall = max(stall, now - last)
            last = now

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        result = await make_coro()
    except Exception as e:
        result = e
    stop.set()
    await t
    return result, stall


def _redirect(url, location):
    return httpx.Response(302, headers={"location": location}, request=httpx.Request("GET", url))


class _Client:
    """httpx.AsyncClient stand-in: every request answers from `route(url)`."""

    def __init__(self, route, **kw):
        self.route, self.requested = route, []

    async def aclose(self):
        pass

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        self.requested.append(str(request.url))
        return self.route(str(request.url))


class VodRequest(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        import routers.livetv as livetv
        import routers.vod as vod
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models.database import Provider, set_setting
        from services import vod_tokens
        self.vod = vod
        livetv._stream_slots = livetv._StreamSlots()
        self.addCleanup(lambda: setattr(livetv, "_stream_slots", livetv._StreamSlots()))
        self.addCleanup(vod._playbacks.clear)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        prov = Provider(name="P", server_url="http://provider.test", username="u", password="p",
                        provider_type="xtream")
        self.db.add(prov)
        set_setting(self.db, "vod_token_secret", "s" * 64)
        self.db.commit()
        url = vod_tokens.url("http://tentacle:8888", "s" * 64, prov.id, "movie", 42, "mkv")
        self.token_file = url.rsplit("/", 1)[1]
        self.upstream = "http://provider.test/movie/u/p/42.mkv"

    def _head(self, route, hanging):
        client = _Client(route)
        with mock.patch.object(ssrf.socket, "getaddrinfo", _gai_hanging(*hanging)), \
                mock.patch("httpx.AsyncClient", lambda **kw: client):
            result, stall = asyncio.run(_longest_stall(
                lambda: self.vod.vod_head("movie", self.token_file, None, self.db)))
        return result, stall, client.requested

    def test_a_provider_name_that_hangs_does_not_stall_the_loop(self):
        result, stall, requested = self._head(lambda u: httpx.Response(200), {"provider.test"})
        self.assertIsInstance(result, HTTPException)
        self.assertEqual(502, result.status_code)
        self.assertEqual([], requested)
        self.assertLess(stall, MAX_STALL, "the event loop was blocked while the resolver hung")

    def test_a_redirect_target_that_hangs_does_not_stall_the_loop(self):
        def route(url):
            return _redirect(url, "http://cdn.example/movie/42.mkv")
        with self.assertLogs("routers.vod", level="WARNING"):
            result, stall, requested = self._head(route, {"cdn.example"})
        self.assertEqual(502, getattr(result, "status_code", None))
        self.assertEqual([self.upstream], requested, "the refused redirect was fetched")
        self.assertLess(stall, MAX_STALL, "the event loop was blocked while the resolver hung")

    def test_a_public_provider_still_plays(self):
        result, stall, requested = self._head(
            lambda u: httpx.Response(200, headers={"content-length": "7"}), set())
        self.assertEqual(200, result.status_code)
        self.assertEqual([self.upstream], requested)

    def test_a_redirect_to_a_private_host_is_still_refused(self):
        def route(url):
            return _redirect(url, "http://10.0.0.5:8096/x")
        with self.assertLogs("routers.vod", level="WARNING"):
            result, _, requested = self._head(route, set())
        self.assertEqual(502, getattr(result, "status_code", None))
        self.assertEqual([self.upstream], requested)


class LiveTvRedirect(unittest.TestCase):
    def test_a_redirect_target_that_hangs_does_not_stall_the_loop(self):
        import routers.livetv as livetv
        client = _Client(lambda u: _redirect(u, "http://cdn.example/live/1.ts"))
        with mock.patch.object(ssrf.socket, "getaddrinfo", _gai_hanging("cdn.example")), \
                self.assertLogs("routers.livetv", level="WARNING"):
            result, stall = asyncio.run(_longest_stall(
                lambda: livetv._send_checked(client, "http://provider.test/live/u/p/1.ts", {})))
        self.assertEqual(502, getattr(result, "status_code", None))
        self.assertEqual(["http://provider.test/live/u/p/1.ts"], client.requested)
        self.assertLess(stall, MAX_STALL, "the event loop was blocked while the resolver hung")


class HlsChecks(unittest.TestCase):
    """The variant check at open and the worker's playlist-line check."""

    def test_a_slow_check_does_not_stall_the_loop(self):
        import routers.livetv as livetv
        from test_livetv_hls_master_playlist import _FakeClient, _collect
        _FakeClient.requested = []
        checked = []

        def slow_guard(url):
            checked.append(url)
            time.sleep(HANG)
            return True

        with mock.patch.object(httpx, "AsyncClient", _FakeClient), \
                mock.patch.object(livetv, "is_safe_url", slow_guard):
            chunks, stall = asyncio.run(_longest_stall(lambda: _collect()))
        self.assertTrue(chunks and chunks[0][:1] == b"\x47", "the stream produced no video")
        self.assertTrue(checked)
        self.assertLess(stall, MAX_STALL, "the event loop was blocked while a check ran")


TVDB = "https://artworks.thetvdb.com/banners/posters/262638-1.jpg"


class ImageProxy(unittest.TestCase):
    def setUp(self):
        import routers.discover as discover
        self.discover = discover
        self.cache = Path(temp_dir(self))
        self.fetched = []
        fetched = self.fetched

        class _Fetch:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                fetched.append(url)
                return httpx.Response(200, content=b"fetched", headers={"content-type": "image/jpeg"})

        for p in (mock.patch.object(discover, "TVDB_PROXY_CACHE", self.cache),
                  mock.patch.object(discover.httpx, "AsyncClient", _Fetch)):
            p.start()
            self.addCleanup(p.stop)

    def _get(self, url, gai):
        key = hashlib.md5(url.encode()).hexdigest()
        with mock.patch.object(ssrf.socket, "getaddrinfo", gai):
            return asyncio.run(_longest_stall(lambda: self.discover.image_proxy(key, url=url)))

    def test_cached_artwork_is_served_without_a_lookup(self):
        (self.cache / f"{hashlib.md5(TVDB.encode()).hexdigest()}.jpg").write_bytes(b"cached")

        def no_lookup(*a, **kw):
            raise AssertionError("a cached image was resolved")

        resp, _ = self._get(TVDB, no_lookup)
        self.assertEqual(b"cached", resp.body)
        self.assertEqual([], self.fetched)

    def test_a_host_off_the_allowlist_is_refused_even_when_cached(self):
        # Older versions cached any URL that merely contained "thetvdb.com".
        bad = "http://169.254.169.254/latest/x.jpg?thetvdb.com"
        (self.cache / f"{hashlib.md5(bad.encode()).hexdigest()}.jpg").write_bytes(b"old")
        result, _ = self._get(bad, _gai_hanging())
        self.assertEqual(400, getattr(result, "status_code", None))

    def test_uncached_artwork_whose_host_hangs_does_not_stall_the_loop(self):
        result, stall = self._get(TVDB, _gai_hanging("artworks.thetvdb.com"))
        self.assertEqual(400, getattr(result, "status_code", None))
        self.assertEqual([], self.fetched)
        self.assertEqual([], list(self.cache.iterdir()))
        self.assertLess(stall, MAX_STALL, "the event loop was blocked while the resolver hung")

    def test_uncached_public_artwork_is_fetched_and_cached(self):
        resp, _ = self._get(TVDB, _gai_hanging())
        self.assertEqual(b"fetched", resp.body)
        self.assertEqual([TVDB], self.fetched)
        self.assertEqual(1, len(list(self.cache.iterdir())))

    def test_a_private_tvdb_name_is_still_refused(self):
        with mock.patch.dict(DNS, {"artworks.thetvdb.com": ["10.0.0.5"]}):
            result, _ = self._get(TVDB, _gai_hanging())
        self.assertEqual(400, getattr(result, "status_code", None))
        self.assertEqual([], self.fetched)


if __name__ == "__main__":
    unittest.main()

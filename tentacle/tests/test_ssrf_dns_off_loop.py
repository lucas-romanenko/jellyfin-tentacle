"""The SSRF guard's DNS lookup must not hold up the event loop -- on every
async path that runs it, not only the Live TV stream open fixed in #370/#371.

Run from the tentacle/ directory:  python -m unittest discover -s tests

services.ssrf resolves a URL's host with a blocking socket.getaddrinfo. #371
moved the Live TV stream open's check into asyncio.to_thread, but these still
called the guard directly inside `async def`, on the event loop:

- routers/vod.py `_resolve()` (lan_origin_guard(server_url), then guard(url))
  from `vod_head` / `vod_stream`: two lookups per VOD request, and every seek
  is a new request;
- routers/discover.py `image_proxy`: is_safe_url() before the disk cache was
  even looked at, so artwork already on disk was refused while DNS was down;
- every redirect hop: routers/vod.py `_open` and routers/livetv.py
  `_send_checked` (stream open, raw-TS re-dial, HLS re-resolve);
- Live TV HLS: the variant check at open and the worker's per-origin
  `line_guard`.

Uvicorn runs one worker, so while the resolver hangs (16 s per lookup in the
outage behind #370) every running stream, recording and API call waits.

No stream URL is accepted or refused differently, and no lookups are added:
the property test compares every decision, and the number of lookups, with
the guard called directly. The one exception is artwork already on disk,
which is now served with no lookup. A file an older version left in the cache
for a host other than TVDB is still refused, before the cache is read.
"""
import asyncio
import hashlib
import ipaddress
import logging
import os
import random
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from services import ssrf
from tmp_dirs import temp_dir

HANG = 0.5
MIN_TICKS = 10        # ~25 expected per lookup off the loop; a blocked loop manages 0 or 1

EAI_AGAIN = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")


def _answer(answer):
    if isinstance(answer, BaseException):
        raise answer
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answer]


class _HangingResolver:
    """getaddrinfo that hangs, then fails, for the given names (a resolver
    timing out in a DNS outage). Other names go to whatever was installed
    before (tests/hermetic.py's offline resolver)."""

    def __init__(self, hosts, hang=HANG):
        self.hosts = set(hosts)
        self.hang = hang
        self.calls = []
        self.real = socket.getaddrinfo

    def __call__(self, host, *a, **kw):
        if host in self.hosts:
            self.calls.append(host)
            if self.hang:
                time.sleep(self.hang)
            raise EAI_AGAIN
        return self.real(host, *a, **kw)


async def _with_ticker(make_call):
    """(result or HTTPException, ticks, seconds) for one awaited call."""
    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    started = time.monotonic()
    try:
        result = await make_call()
    except HTTPException as e:
        result = e
    elapsed = time.monotonic() - started
    stop.set()
    await t
    return result, ticks, elapsed


class _NoClient:
    """httpx.AsyncClient stand-in that must not be used: no network."""

    def __init__(self, *a, **kw):
        raise AssertionError("the test reached for the network")


class _RedirectClient:
    """Answers the first request with a redirect to `location`, the next ones 200."""

    def __init__(self, location):
        self.location = location
        self.sent = []

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        self.sent.append(str(request.url))
        if len(self.sent) == 1:
            return httpx.Response(302, headers={"location": self.location}, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    async def aclose(self):
        pass


def _request(method="GET"):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": method, "headers": [(b"host", b"tentacle")], "path": "/",
                    "query_string": b"", "scheme": "http", "server": ("tentacle", 8888),
                    "client": ("10.0.0.5", 1)}, receive)


def _vod_db(test, server_urls):
    """A database with one Xtream provider per server URL, and a signed VOD
    token file for each: [(provider, token_file)]."""
    import models.database as mdb
    from models.database import Provider, set_setting
    from services import vod_tokens
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{temp_dir(test)}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    test.addCleanup(db.close)
    rows = []
    for server_url in server_urls:
        prov = Provider(name="P", server_url=server_url, username="u", password="p",
                        provider_type="xtream", user_agent="UA/1")
        db.add(prov)
        db.commit()
        rows.append(prov)
    set_setting(db, "vod_token_secret", "s" * 64)
    db.commit()
    out = []
    for prov in rows:
        url = vod_tokens.url("http://tentacle:8888", "s" * 64, prov.id, "movie", 2141622, "mkv")
        out.append((prov, url.rsplit("/", 1)[1]))
    return db, out


# ---------------------------------------------------------------------------
# Discover: TVDB image proxy
# ---------------------------------------------------------------------------

class ImageProxyLookup(unittest.TestCase):
    URL = "https://artworks.thetvdb.com/banners/v4/movie/1/posters/x.jpg"

    def setUp(self):
        import routers.discover as discover
        self.discover = discover
        self.cache = Path(temp_dir(self))
        for p in (mock.patch.object(discover, "TVDB_PROXY_CACHE", self.cache),
                  mock.patch("httpx.AsyncClient", _NoClient)):
            p.start()
            self.addCleanup(p.stop)
        self.key = hashlib.md5(self.URL.encode()).hexdigest()

    def test_a_hanging_resolver_does_not_freeze_the_loop(self):
        res = _HangingResolver({"artworks.thetvdb.com"})
        with mock.patch.object(ssrf.socket, "getaddrinfo", res):
            result, ticks, elapsed = asyncio.run(
                _with_ticker(lambda: self.discover.image_proxy(self.key, self.URL)))
        self.assertEqual(400, getattr(result, "status_code", None), result)   # still refused
        self.assertEqual("Invalid URL", result.detail)
        self.assertGreaterEqual(
            ticks, MIN_TICKS,
            f"image_proxy blocked the event loop for {elapsed:.2f}s ({ticks} ticks, "
            f"{len(res.calls)} lookup(s) on the loop)")

    def test_a_cached_image_needs_no_lookup(self):
        # Already fetched (and checked) once; serving it reaches nothing.
        (self.cache / f"{self.key}.jpg").write_bytes(b"JPEGDATA")
        res = _HangingResolver({"artworks.thetvdb.com"}, hang=0)
        with mock.patch.object(ssrf.socket, "getaddrinfo", res):
            try:
                resp = asyncio.run(self.discover.image_proxy(self.key, self.URL))
            except HTTPException as e:
                self.fail(f"cached artwork refused with {e.status_code} {e.detail!r} during a DNS "
                          f"outage ({len(res.calls)} lookup(s) made for a cache hit)")
        self.assertEqual(200, resp.status_code)
        self.assertEqual(b"JPEGDATA", resp.body)
        self.assertEqual([], res.calls)

    def test_an_old_cache_file_for_another_host_is_still_refused(self):
        # Before the allowlist (2026-06-15) the proxy cached any URL that merely
        # contained "thetvdb.com", and nothing prunes the cache. Such a file, even
        # under md5(url), is never served: the host check comes before the cache,
        # as on main.
        res = _HangingResolver({"artworks.thetvdb.com"}, hang=0)
        for bad in ("http://169.254.169.254/latest/meta-data/iam/x.jpg?thetvdb.com",
                    "http://127.0.0.1:8096/Users?x=thetvdb.com",
                    "http://thetvdb.com.evil.test/poster.jpg"):
            key = hashlib.md5(bad.encode()).hexdigest()
            (self.cache / f"{key}.jpg").write_bytes(b"INTERNAL-RESPONSE")
            with self.subTest(url=bad), mock.patch.object(ssrf.socket, "getaddrinfo", res):
                try:
                    resp = asyncio.run(self.discover.image_proxy(key, bad))
                except HTTPException as e:
                    self.assertEqual((400, "Invalid URL"), (e.status_code, e.detail))
                else:
                    self.fail(f"served {resp.status_code} {resp.body!r} from an old cache file "
                              f"for {bad} (main: 400 Invalid URL)")
        self.assertEqual([], res.calls)

    def test_a_cache_key_that_does_not_match_still_reads_nothing(self):
        # The key check still comes before the cache: another URL cannot be
        # served this image, and a made-up key reaches no file.
        (self.cache / f"{self.key}.jpg").write_bytes(b"JPEGDATA")
        other = "https://artworks.thetvdb.com/banners/v4/movie/2/posters/y.jpg"
        for key, url in ((self.key, other), ("0" * 32, self.URL), (self.key, "http://169.254.169.254/x.jpg")):
            with self.subTest(url=url), self.assertRaises(HTTPException) as cm:
                asyncio.run(self.discover.image_proxy(key, url))
            self.assertEqual(400, cm.exception.status_code)


# ---------------------------------------------------------------------------
# VOD through Tentacle: /api/vod/{kind}/{token}
# ---------------------------------------------------------------------------

class VodRouteLookup(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        import routers.vod as vod
        self.vod = vod
        livetv._stream_slots = livetv._StreamSlots()
        vod._playbacks.clear()
        self.addCleanup(lambda: setattr(livetv, "_stream_slots", livetv._StreamSlots()))
        self.addCleanup(vod._playbacks.clear)
        self.db, [(_, self.token_file)] = _vod_db(self, ["http://provider.test"])
        p = mock.patch("httpx.AsyncClient", _NoClient)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, route, method):
        res = _HangingResolver({"provider.test"})
        with mock.patch.object(ssrf.socket, "getaddrinfo", res):
            result, ticks, elapsed = asyncio.run(_with_ticker(
                lambda: route("movie", self.token_file, _request(method), self.db)))
        self.assertEqual(502, getattr(result, "status_code", None), result)   # still refused
        self.assertEqual(2, len(res.calls))                                    # as many lookups as before
        self.assertGreaterEqual(
            ticks, MIN_TICKS,
            f"{route.__name__} blocked the event loop for {elapsed:.2f}s ({ticks} ticks, "
            f"{len(res.calls)} lookup(s) on the loop)")

    def test_head_does_not_freeze_the_loop(self):
        self._run(self.vod.vod_head, "HEAD")

    def test_get_does_not_freeze_the_loop(self):
        self._run(self.vod.vod_stream, "GET")


# ---------------------------------------------------------------------------
# Redirect hops: vod._open and livetv._send_checked
# ---------------------------------------------------------------------------

class RedirectHopLookup(unittest.TestCase):
    START = "http://93.184.216.34/movie/u/p/1.mkv"     # an IP literal: no lookup
    HOP = "http://cdn.test/movie/u/p/1.mkv"

    def _run(self, make_call, name):
        res = _HangingResolver({"cdn.test"})
        with mock.patch.object(ssrf.socket, "getaddrinfo", res), \
                self.assertLogs(level="WARNING"):
            result, ticks, elapsed = asyncio.run(_with_ticker(make_call))
        self.assertEqual(502, getattr(result, "status_code", None), result)   # still refused
        self.assertGreaterEqual(
            ticks, MIN_TICKS,
            f"{name} blocked the event loop for {elapsed:.2f}s ({ticks} ticks, "
            f"{len(res.calls)} lookup(s) on the loop)")

    def test_vod_redirect_hop_does_not_freeze_the_loop(self):
        import routers.vod as vod
        client = _RedirectClient(self.HOP)
        self._run(lambda: vod._open(client, "GET", self.START, {}, ssrf.is_safe_url), "vod._open")

    def test_live_redirect_hop_does_not_freeze_the_loop(self):
        import routers.livetv as livetv
        client = _RedirectClient(self.HOP)
        self._run(lambda: livetv._send_checked(client, self.START, {}, ssrf.is_safe_url),
                  "livetv._send_checked")


# ---------------------------------------------------------------------------
# Live TV HLS: every URL the stream checks -- the redirect hop at open, the
# variant, and the worker's playlist lines -- is checked off the loop.
# ---------------------------------------------------------------------------

_MASTER = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
           "http://variant.example/360p/index.m3u8\n")
_MEDIA = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:100\n"
          "#EXTINF:4.0,\nhttp://segments.example/seg100.ts\n"
          "#EXTINF:4.0,\nhttp://segments.example/seg101.ts\n#EXT-X-ENDLIST\n")
_TS = b"\x47" + b"TS-SEGMENT-BYTES" * 8


class _HlsResp:
    def __init__(self, url, body=b"", content_type="", status_code=200, location=None):
        self.url = url
        self._body = body
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if location:
            self.headers["location"] = location

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    async def aread(self):
        return self._body

    async def aclose(self):
        return None

    async def aiter_bytes(self, chunk_size=65536):
        yield self._body

    @property
    def content(self):
        return self._body

    @property
    def text(self):
        return self._body.decode()


class _HlsClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        return None

    def build_request(self, method, url, headers=None):
        return type("Req", (), {"url": url})()

    async def send(self, request, stream=False):
        return self._route(request.url)

    async def get(self, url, headers=None):
        return self._route(url)

    @staticmethod
    def _route(url):
        if url.startswith("http://provider.example/"):
            return _HlsResp(url, status_code=302, location="http://cdn.example/tok/1.m3u8")
        if url.endswith("index.m3u8"):
            return _HlsResp(url, _MEDIA.encode(), "application/vnd.apple.mpegurl")
        if url.endswith(".ts"):
            return _HlsResp(url, _TS, "video/mp2t")
        return _HlsResp(url, _MASTER.encode(), "application/vnd.apple.mpegurl")


class LiveHlsChecksOffTheLoop(unittest.TestCase):
    def test_every_hls_url_check_runs_off_the_event_loop(self):
        import routers.livetv as livetv
        checks = []

        def guard(url):
            checks.append((url, threading.get_ident()))
            return True

        async def scenario():
            loop_thread = threading.get_ident()
            resp = await livetv._stream_proxy_inner(
                1, "UA/1", "http://provider.example/live/u/p/1.m3u8", lambda: None, guard)
            chunks = []

            async def pump():
                async for chunk in resp.body_iterator:
                    chunks.append(chunk)
                    if _TS in chunks:
                        return

            try:
                await asyncio.wait_for(pump(), 5.0)
            except asyncio.TimeoutError:
                pass
            close = getattr(resp, "close_upstream", None)
            if close is not None:
                await close()
            return loop_thread, chunks

        with mock.patch.object(httpx, "AsyncClient", _HlsClient):
            loop_thread, chunks = asyncio.run(scenario())
        self.assertIn(_TS, chunks, "the stream never delivered a segment")
        checked = {url for url, _ in checks}
        for url in ("http://cdn.example/tok/1.m3u8",                  # redirect hop at open
                    "http://variant.example/360p/index.m3u8",          # the variant
                    "http://segments.example/seg100.ts"):              # a playlist line (line_guard)
            self.assertIn(url, checked)
        on_loop = [url for url, ident in checks if ident == loop_thread]
        self.assertEqual([], on_loop, "these URLs were checked on the event loop")


# ---------------------------------------------------------------------------
# Property: the same decisions and the same number of lookups as calling the
# guard directly, and not one lookup on the event loop.
# ---------------------------------------------------------------------------

_POOL = ["93.184.216.34", "8.8.8.8", "10.0.0.5", "192.168.2.52", "127.0.0.1", "0.0.0.0",
         "169.254.169.254", "100.64.0.1", "fc00::5", "::1", "2606:4700::1111"]
_HOSTS = ["provider.example", "cdn.example", "lan.example", "artworks.thetvdb.com",
          "thetvdb.com.evil.test"]
_SERVERS = ["http://provider.example", "http://lan.example:8890", "http://192.168.2.52:8890",
            "https://provider.example:8443"]
_HOPS = ["http://cdn.example/x.ts", "http://lan.example:8890/x.ts", "http://lan.example:8891/x.ts",
         "https://provider.example:8443/x.ts", "http://127.0.0.1:8096/Users",
         "http://169.254.169.254/latest", "http://93.184.216.34/x.ts", "http://192.168.2.52:8890/x.ts",
         "ftp://cdn.example/x"]
_IMAGES = ["https://artworks.thetvdb.com/banners/{seed}.jpg", "https://thetvdb.com.evil.test/{seed}.jpg",
           "http://169.254.169.254/{seed}.jpg?x=thetvdb.com", "https://93.184.216.34/{seed}.jpg"]


class _ScriptedDns:
    """getaddrinfo answering from a table fixed per seed, so the direct call
    and the route see the very same answers. Records which thread asked."""

    def __init__(self, rng):
        self.rng = rng
        self.table = {}
        self.calls = []

    def reroll(self):
        self.table = {}
        for h in _HOSTS:
            kind = self.rng.random()
            if kind < 0.15:
                self.table[h] = EAI_AGAIN
            elif kind < 0.2:
                self.table[h] = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            elif kind < 0.25:
                self.table[h] = []
            else:
                self.table[h] = self.rng.sample(_POOL, self.rng.randint(1, 2))

    def __call__(self, host, *a, **kw):
        self.calls.append((host, threading.get_ident()))
        answer = self.table.get(host)
        if answer is None:
            try:
                ipaddress.ip_address(host)
                answer = [host]
            except ValueError:
                answer = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return _answer(answer)


class _StubClient:
    """httpx.AsyncClient stand-in: the image proxy's GET and the VOD HEAD
    both get a 200, without the network."""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return httpx.Response(200, content=b"fetched:" + url.encode(),
                              headers={"content-type": "image/jpeg"})

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        return httpx.Response(200, headers={"content-length": "7"}, request=request)

    async def aclose(self):
        pass


class SameDecisionsOffTheLoop(unittest.TestCase):
    SEEDS = int(os.environ.get("SSRF_PROP_SEEDS", "1000"))

    def setUp(self):
        import routers.discover as discover
        import routers.livetv as livetv
        self.discover = discover
        self.cache = Path(temp_dir(self))
        for p in (mock.patch.object(discover, "TVDB_PROXY_CACHE", self.cache),
                  mock.patch("httpx.AsyncClient", _StubClient)):
            p.start()
            self.addCleanup(p.stop)
        livetv._stream_slots = livetv._StreamSlots()
        self.addCleanup(lambda: setattr(livetv, "_stream_slots", livetv._StreamSlots()))
        self.db, self.vod_rows = _vod_db(self, _SERVERS)

    def test_decisions_and_lookups_match_the_guard_called_directly(self):
        base = int(os.environ.get("SSRF_PROP_SEED", "0"))
        logging.disable(logging.WARNING)       # hundreds of "Blocked redirect" lines
        self.addCleanup(logging.disable, logging.NOTSET)

        async def run_all():
            loop_thread = threading.get_ident()
            for seed in range(base, base + self.SEEDS):
                rng = random.Random(seed)
                dns = _ScriptedDns(rng)
                with mock.patch.object(ssrf.socket, "getaddrinfo", dns):
                    for site in (self._vod_head, self._hop, self._image):
                        dns.reroll()
                        await site(rng, dns, seed, loop_thread)

        asyncio.run(run_all())

    def _direct(self, dns, fn, *args):
        """(verdict, lookups) of the guard called the way main did."""
        dns.calls = []
        verdict = fn(*args)
        return verdict, len(dns.calls)

    def _off_loop(self, dns, loop_thread, n_expected, msg):
        self.assertEqual(n_expected, len(dns.calls), "a different number of lookups: " + msg)
        on_loop = [h for h, ident in dns.calls if ident == loop_thread]
        self.assertEqual([], on_loop, "looked up on the event loop: " + msg)

    async def _vod_head(self, rng, dns, seed, loop_thread):
        import routers.vod as vod
        prov, token_file = rng.choice(self.vod_rows)
        url = (f"{prov.server_url.rstrip('/')}/movie/{prov.username}/{prov.password}/2141622.mkv")
        msg = f"seed={seed} vod-head server={prov.server_url!r} dns={dns.table}"

        def main_way():
            return bool(ssrf.lan_origin_guard(prov.server_url)(url))

        expected, n = self._direct(dns, main_way)
        dns.calls = []
        try:
            resp = await vod.vod_head("movie", token_file, _request("HEAD"), self.db)
            self.assertEqual(200, resp.status_code, msg)
            allowed = True
        except HTTPException as e:
            self.assertEqual(502, e.status_code, msg)
            self.assertEqual("Stream URL points to a non-public host", e.detail, msg)
            allowed = False
        self.assertIs(expected, allowed, msg)
        self._off_loop(dns, loop_thread, n, msg)

    async def _hop(self, rng, dns, seed, loop_thread):
        import routers.livetv as livetv
        import routers.vod as vod
        server = rng.choice(_SERVERS)
        dns.calls = []
        guard = rng.choice([ssrf.is_safe_url, ssrf.lan_origin_guard(server)])
        hop = rng.choice(_HOPS)
        which = rng.choice(["vod", "live"])
        msg = f"seed={seed} {which} hop={hop!r} server={server!r} dns={dns.table}"
        expected, n = self._direct(dns, lambda: bool(guard(hop)))
        client = _RedirectClient(hop)
        dns.calls = []
        try:
            if which == "vod":
                resp = await vod._open(client, "GET", "http://93.184.216.34/x.ts", {}, guard)
            else:
                resp = await livetv._send_checked(client, "http://93.184.216.34/x.ts", {}, guard)
            self.assertEqual(200, resp.status_code, msg)
            allowed = True
        except HTTPException as e:
            self.assertEqual(502, e.status_code, msg)
            allowed = False
        self.assertIs(expected, allowed, msg)
        self.assertEqual(2 if allowed else 1, len(client.sent), msg)   # a refused hop is never fetched
        self._off_loop(dns, loop_thread, n, msg)

    async def _image(self, rng, dns, seed, loop_thread):
        url = rng.choice(_IMAGES).format(seed=seed)
        key = hashlib.md5(url.encode()).hexdigest()
        # A file under md5(url): written by this version after a passing check,
        # or left by an older one that cached any URL containing "thetvdb.com".
        cached = rng.random() < 0.3
        if cached:
            (self.cache / f"{key}.jpg").write_bytes(b"cached")
        from_disk = cached and url.startswith("https://artworks.thetvdb.com/")
        msg = f"seed={seed} image={url!r} cached={cached} dns={dns.table}"
        if from_disk:
            expected, n = True, 0     # the one difference from main: served with no lookup
        else:
            expected, n = self._direct(dns, lambda: ssrf.is_safe_url(url, allowed_hosts={"thetvdb.com"}))
        dns.calls = []
        try:
            resp = await self.discover.image_proxy(key, url)
            self.assertEqual(200, resp.status_code, msg)
            self.assertEqual(b"cached" if from_disk else b"fetched:" + url.encode(), resp.body, msg)
            allowed = True
        except HTTPException as e:
            self.assertEqual(400, e.status_code, msg)
            self.assertEqual("Invalid URL", e.detail, msg)
            allowed = False
        self.assertIs(expected, allowed, msg)
        self._off_loop(dns, loop_thread, n, msg)


if __name__ == "__main__":
    unittest.main()

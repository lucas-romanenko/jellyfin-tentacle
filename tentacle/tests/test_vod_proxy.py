"""Provider VOD served through Tentacle: ranges pass through, a dropped
upstream is resumed from the byte it stopped at, one playback holds one
lease across its seeks, and a recording outranks it.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Measured on the system this was written for: Jellyfin plays a movie as a
long series of `Range: bytes=N-` requests (840 in two days, seeks included,
no HEAD), and a movie started on the TV was what made the provider 509 the
recording running at the same time.
"""
import asyncio
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from test_livetv_raw_reconnect import _Body, _dropped

PANEL = "http://provider.test"


def _fresh_db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                           connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class _Client:
    """httpx.AsyncClient stand-in: URL -> list of responses, logs every request."""

    def __init__(self, script, log, **kw):
        self._script, self._log = script, log

    async def aclose(self):
        pass

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        self._log.append((request.method, str(request.url), request.headers.get("range")))
        queue = self._script[str(request.url)]
        return queue[0] if len(queue) == 1 else queue.pop(0)


def _file(status, pieces, then=None, url=PANEL, total=1000, start=0, ct="video/x-matroska"):
    headers = {"content-type": ct, "accept-ranges": "bytes"}
    if status == 206:
        headers["content-range"] = f"bytes {start}-{total - 1}/{total}"
        headers["content-length"] = str(total - start)
    else:
        headers["content-length"] = str(total)
    return httpx.Response(status, headers=headers, stream=_Body(pieces, then), request=httpx.Request("GET", url))


def _plain(status, url=PANEL):
    return httpx.Response(status, request=httpx.Request("GET", url))


def _request(range_header=None, method="GET"):
    headers = [(b"host", b"tentacle")]
    if range_header:
        headers.append((b"range", range_header.encode()))
    return Request({"type": "http", "method": method, "headers": headers, "path": "/", "query_string": b"",
                    "scheme": "http", "server": ("tentacle", 8888), "client": ("10.0.0.5", 1)})


class _Base(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        import routers.vod as vod
        self.livetv, self.vod = livetv, vod
        livetv._stream_slots = livetv._StreamSlots()
        vod._playbacks.clear()
        vod._sweeper = None
        self.db = _fresh_db()
        from models.database import Provider, set_setting
        from services import vod_tokens
        self.provider = Provider(name="P", server_url=PANEL, username="u", password="p",
                                 provider_type="xtream", user_agent="UA/1")
        self.db.add(self.provider)
        set_setting(self.db, "vod_token_secret", "s" * 64)
        set_setting(self.db, "livetv_max_concurrent_streams", "1")
        self.db.commit()
        self.url = vod_tokens.url("http://tentacle:8888", "s" * 64, self.provider.id, "movie", 2141622, "mkv")
        self.token_file = self.url.rsplit("/", 1)[1]
        self.upstream = f"{PANEL}/movie/u/p/2141622.mkv"
        self.log = []

    def _play(self, script, range_header=None, limit_pieces=None, before=None):
        """Call the route, read the body; returns (status, headers, body bytes)."""
        vod = self.vod
        real_sleep = asyncio.sleep
        self.slept = []

        async def fast_sleep(d):
            self.slept.append(d)
            await real_sleep(0)

        async def go():
            if before:
                await before()
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch("asyncio.sleep", fast_sleep):
                resp = await vod.vod_stream("movie", self.token_file, _request(range_header), self.db)
                out = b""
                n = 0
                async for piece in resp.body_iterator:
                    out += piece
                    n += 1
                    if limit_pieces and n >= limit_pieces:
                        await resp.body_iterator.aclose()
                        break
                return resp.status_code, dict(resp.headers), out
        return asyncio.run(go())


class TokenGate(_Base):
    def test_unknown_or_forged_token_is_404_without_touching_the_provider(self):
        async def go():
            for bad in ("2141622.mkv", self.token_file.replace(self.token_file[-9:-4], "00000")):
                with self.assertRaises(HTTPException) as cm:
                    await self.vod.vod_stream("movie", bad, _request(), self.db)
                self.assertEqual(404, cm.exception.status_code)
        asyncio.run(go())
        self.assertEqual([], self.log)
        self.assertEqual(0, self.livetv._stream_slots.active)


class RangesAndResume(_Base):
    def test_the_clients_range_is_passed_through_and_the_body_streamed(self):
        script = {self.upstream: [_file(206, [b"AAAA", b"BBBB"], start=100)]}
        status, headers, body = self._play(script, range_header="bytes=100-")
        self.assertEqual(206, status)
        self.assertEqual(b"AAAABBBB", body)
        self.assertEqual("bytes 100-999/1000", headers.get("content-range"))
        self.assertEqual([("GET", self.upstream, "bytes=100-")], self.log)

    def test_a_dropped_upstream_is_resumed_from_the_byte_it_stopped_at(self):
        script = {self.upstream: [_file(206, [b"AAAA"], then=_dropped(), start=100),
                                  _file(206, [b"BBBB"], start=104)]}
        status, headers, body = self._play(script, range_header="bytes=100-")
        self.assertEqual(b"AAAABBBB", body, "the film would have stopped at the drop")
        self.assertEqual("bytes=104-", self.log[1][2], "resume from start + bytes already sent")
        self.assertGreaterEqual(len(self.slept), 1, "the resume is paced, not immediate")

    def test_a_provider_that_will_not_resume_ends_the_stream_instead_of_restarting(self):
        script = {self.upstream: [_file(206, [b"AAAA"], then=_dropped(), start=0),
                                  _file(200, [b"AAAA", b"BBBB"], start=0)]}
        status, headers, body = self._play(script, range_header="bytes=0-")
        self.assertEqual(b"AAAA", body, "must never replay bytes the client already has")

    def test_a_refusal_on_open_is_waited_out(self):
        script = {self.upstream: [_plain(509), _plain(509), _file(206, [b"AAAA"], start=0)]}
        status, headers, body = self._play(script, range_header="bytes=0-")
        self.assertEqual(b"AAAA", body)
        self.assertGreaterEqual(len(self.slept), 2)

    def test_a_status_that_will_never_fix_itself_fails_at_once(self):
        script = {self.upstream: [_plain(404)]}
        with self.assertRaises(HTTPException) as cm:
            self._play(script)
        self.assertEqual(404, cm.exception.status_code)
        self.assertEqual(1, len(self.log))


class OnePlaybackOneLease(_Base):
    def test_seeks_reuse_the_lease(self):
        script = {self.upstream: [_file(206, [b"AAAA"], start=0), _file(206, [b"BBBB"], start=500)]}
        self._play(script, range_header="bytes=0-")
        self._play(script, range_header="bytes=500-")
        self.assertEqual(1, self.livetv._stream_slots.active, "a seek is not a second connection slot")
        self.assertEqual(1, len(self.vod._playbacks))

    def test_a_playback_is_listed_with_the_live_streams(self):
        script = {self.upstream: [_file(206, [b"AAAA"], start=0)]}
        self._play(script, range_header="bytes=0-")
        snap = self.livetv._stream_snapshot(self.db)
        vods = [s for s in snap if s["kind"] == "vod"]
        self.assertEqual(1, len(vods))
        self.assertEqual("vod:movie:%d:2141622" % self.provider.id, vods[0]["channel"])
        self.assertIn(vods[0]["state"], ("idle", "streaming"))

    def test_a_seek_ends_the_previous_range_so_one_playback_holds_one_connection(self):
        vod, livetv = self.vod, self.livetv

        class _Endless(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b"X"
                    await asyncio.sleep(0)

            async def aclose(self):
                pass

        def endless():
            return httpx.Response(206, headers={"content-type": "video/x-matroska", "accept-ranges": "bytes",
                                                "content-range": "bytes 0-999/1000", "content-length": "1000"},
                                  stream=_Endless(), request=httpx.Request("GET", self.upstream))
        script = {self.upstream: [endless(), endless()]}

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                first = await vod.vod_stream("movie", self.token_file, _request("bytes=0-"), self.db)
                it1 = first.body_iterator
                self.assertTrue((await it1.__anext__()).startswith(b"X"))
                second = await vod.vod_stream("movie", self.token_file, _request("bytes=500-"), self.db)
                it2 = second.body_iterator
                self.assertTrue((await it2.__anext__()).startswith(b"X"))
                for _ in range(200):
                    try:
                        await asyncio.wait_for(it1.__anext__(), 1.0)
                    except StopAsyncIteration:
                        break
                else:
                    self.fail("the superseded range kept streaming alongside the seek")
                self.assertEqual(1, livetv._stream_slots.active)
                await it2.aclose()
        asyncio.run(go())

    def test_an_idle_playback_is_released_and_a_stopped_one_too(self):
        script = {self.upstream: [_file(206, [b"AAAA"], start=0)]}
        self._play(script, range_header="bytes=0-")
        pb = next(iter(self.vod._playbacks.values()))
        self.assertEqual(0, self.vod._sweep_once(pb.last_used + 1.0), "still within the idle window")
        self.assertEqual(1, self.vod._sweep_once(pb.last_used + pb.idle + 1.0))
        self.assertEqual(0, self.livetv._stream_slots.active)
        self.assertEqual({}, self.vod._playbacks)

    def test_at_capacity_a_vod_never_takes_a_slot_from_a_recording_or_a_viewer(self):
        for kind in ("recording", "live"):
            self.setUp()

            async def hold():
                await self.livetv._stream_slots.acquire_lease(1, 0.01, kind, "channel:1")
            script = {self.upstream: [_file(206, [b"AAAA"], start=0)]}
            with self.assertRaises(HTTPException) as cm:
                self._play(script, before=hold)
            self.assertEqual(503, cm.exception.status_code, kind)
            self.assertEqual([], self.log, "refused before any provider request")

    def test_a_recording_stops_a_playing_vod(self):
        async def endless():
            while True:
                yield b"X"
                await asyncio.sleep(0)

        class _Endless(httpx.AsyncByteStream):
            async def __aiter__(self):
                async for p in endless():
                    yield p

            async def aclose(self):
                pass
        upstream = httpx.Response(206, headers={"content-type": "video/x-matroska", "accept-ranges": "bytes",
                                                "content-range": "bytes 0-999/1000", "content-length": "1000"},
                                  stream=_Endless(), request=httpx.Request("GET", self.upstream))
        script = {self.upstream: [upstream]}
        vod, livetv = self.vod, self.livetv

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                resp = await vod.vod_stream("movie", self.token_file, _request("bytes=0-"), self.db)
                it = resp.body_iterator
                self.assertTrue((await it.__anext__()).startswith(b"X"))
                rec = await livetv._stream_slots.acquire_lease(1, 0.01, "recording", "channel:9")
                self.assertIsNotNone(rec, "the recording must get the slot")
                got = 0
                for _ in range(200):
                    try:
                        await asyncio.wait_for(it.__anext__(), 1.0)
                        got += 1
                    except StopAsyncIteration:
                        break
                else:
                    self.fail("the pre-empted playback did not end")
                self.assertLessEqual(got, 2, "stops at the next chunk")
                pb = next(iter(vod._playbacks.values()))
                self.assertTrue(pb.stopped.is_set())
                self.assertEqual(1, vod._sweep_once(asyncio.get_running_loop().time()))
                self.assertEqual({"channel:9"}, {l.owner for l in livetv._stream_slots.leases.values()})
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()

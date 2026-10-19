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
import unittest
from unittest import mock

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from test_livetv_raw_reconnect import _Body, _dropped
from tmp_dirs import temp_dir

PANEL = "http://provider.test"


def _fresh_db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{temp_dir()}/t.db",
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


def _file(status, pieces, then=None, url=PANEL, total=None, start=0, ct="video/x-matroska", last=None):
    """A provider answer. Its Content-Length/Range is what the pieces fill
    (`total` defaults to the end of the pieces), unless the body breaks
    (`then`), or `total`/`last` say the file goes on: the relay treats a body
    that ends short of what it declared as a drop."""
    if total is None:
        total = start + sum(len(p) for p in pieces) if then is None else 1000
    last = total - 1 if last is None else last
    headers = {"content-type": ct, "accept-ranges": "bytes"}
    if status == 206:
        headers["content-range"] = f"bytes {start}-{last}/{total}"
        headers["content-length"] = str(last - start + 1)
    else:
        headers["content-length"] = str(total)
    return httpx.Response(status, headers=headers, stream=_Body(pieces, then), request=httpx.Request("GET", url))


def _plain(status, url=PANEL):
    return httpx.Response(status, request=httpx.Request("GET", url))


def _request(range_header=None, method="GET", client="10.0.0.5"):
    headers = [(b"host", b"tentacle")]
    if range_header:
        headers.append((b"range", range_header.encode()))
    async def receive():                # a client that is still there
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": method, "headers": headers, "path": "/", "query_string": b"",
                    "scheme": "http", "server": ("tentacle", 8888), "client": (client, 1)}, receive)


class _Base(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        import routers.vod as vod
        self.livetv, self.vod = livetv, vod
        livetv._stream_slots = livetv._StreamSlots()
        vod._playbacks.clear()
        vod._sweeper = None
        # Leave nothing held behind: a lease still counted as a live stream
        # makes any later test that runs a sync wait for it (up to 4 hours).
        self.addCleanup(lambda: setattr(livetv, "_stream_slots", livetv._StreamSlots()))
        self.addCleanup(vod._playbacks.clear)
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

    def _play(self, script, range_header=None, limit_pieces=None, before=None, client="10.0.0.5"):
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
                resp = await vod.vod_stream("movie", self.token_file, _request(range_header, client=client), self.db)
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


class MiddlewareStaysOutOfTheWay(unittest.TestCase):
    """Starlette's BaseHTTPMiddleware turns a client hang-up into a garbage-
    collected generator instead of a cancellation, and hides the socket from
    `request.is_disconnected()`; every streaming route depends on neither
    happening. The dashboard's no-cache headers are added by pure ASGI."""

    def test_no_base_http_middleware_is_installed(self):
        from starlette.middleware.base import BaseHTTPMiddleware
        import main
        classes = [m.cls for m in main.app.user_middleware]
        self.assertFalse(any(issubclass(c, BaseHTTPMiddleware) for c in classes), classes)
        self.assertIn(main.NoCacheStatic, classes)

    def test_static_assets_get_no_cache_headers_and_nothing_else_does(self):
        import main
        sent = []

        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/css")]})
            await send({"type": "http.response.body", "body": b""})

        async def send(message):
            sent.append(message)

        async def go(path):
            sent.clear()
            await main.NoCacheStatic(inner)({"type": "http", "path": path}, None, send)
            return dict(sent[0]["headers"])
        h = asyncio.run(go("/static/js/pages.js"))
        self.assertEqual(b"no-cache, no-store, must-revalidate", h[b"cache-control"])
        h = asyncio.run(go("/api/vod/movie/x"))
        self.assertNotIn(b"cache-control", h)


class TokenGate(_Base):
    def test_an_install_without_a_secret_answers_404_and_makes_none(self):
        """The secret is made by the sync that writes links; an anonymous
        request must not create one (a SQLite write on the event loop, from
        a public route, that can wait on the sync's lock)."""
        from models.database import Setting, get_setting
        self.db.query(Setting).filter(Setting.key == "vod_token_secret").delete()
        self.db.commit()
        with self.assertRaises(HTTPException) as cm:
            self._play({self.upstream: [_file(206, [b"AAAA"], start=0)]})
        self.assertEqual(404, cm.exception.status_code)
        self.assertEqual("", get_setting(self.db, "vod_token_secret", "") or "")
        self.assertEqual([], self.log)

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
        self.assertEqual("bytes 100-107/108", headers.get("content-range"))
        self.assertEqual([("GET", self.upstream, "bytes=100-")], self.log)

    def test_a_dropped_upstream_is_resumed_from_the_byte_it_stopped_at(self):
        script = {self.upstream: [_file(206, [b"AAAA"], then=_dropped(), start=100, total=108),
                                  _file(206, [b"BBBB"], start=104)]}
        status, headers, body = self._play(script, range_header="bytes=100-")
        self.assertEqual(b"AAAABBBB", body, "the film would have stopped at the drop")
        self.assertEqual("bytes=104-107", self.log[1][2],
                         "resume from start + bytes already sent, to the end the provider named")
        self.assertGreaterEqual(len(self.slept), 1, "the resume is paced, not immediate")

    def test_a_bounded_range_is_resumed_to_its_end_not_beyond(self):
        """`bytes=100-107` of a 200-byte file dropped after four bytes: the
        resume asks for 104-107. Asking for 104- would fetch the rest of the
        file into a response the player sized at 8 bytes."""
        script = {self.upstream: [_file(206, [b"AAAA"], then=_dropped(), start=100, total=200, last=107),
                                  _file(206, [b"BBBB"], start=104, total=200, last=107)]}
        status, headers, body = self._play(script, range_header="bytes=100-107")
        self.assertEqual(b"AAAABBBB", body)
        self.assertEqual("bytes=104-107", self.log[1][2])

    def test_a_bounded_range_delivered_in_full_is_not_resumed(self):
        script = {self.upstream: [_file(206, [b"AAAA"], then=_dropped(), start=100, total=104)]}
        status, headers, body = self._play(script, range_header="bytes=100-103")
        self.assertEqual(b"AAAA", body)
        self.assertEqual(1, len(self.log), "nothing left to ask for")

    def test_a_suffix_range_resumes_from_where_the_provider_said_it_started(self):
        """`bytes=-500` has no start until the 206's Content-Range names it."""
        script = {self.upstream: [_file(206, [b"AAAA"], then=_dropped(), start=500, total=508),
                                  _file(206, [b"BBBB"], start=504)]}
        status, headers, body = self._play(script, range_header="bytes=-500")
        self.assertEqual(b"AAAABBBB", body)
        self.assertEqual("bytes=504-507", self.log[1][2])

    def test_a_provider_that_ignores_the_range_is_not_resumed_from_the_wrong_offset(self):
        """200 to a Range request = the provider sent byte 0 and cannot be
        asked to continue from anywhere; end the stream rather than splice
        the wrong bytes in."""
        script = {self.upstream: [_file(200, [b"AAAA"], then=_dropped()), _file(206, [b"BBBB"], start=4)]}
        status, headers, body = self._play(script, range_header="bytes=100-")
        self.assertEqual(200, status)
        self.assertEqual(b"AAAA", body)
        self.assertEqual(1, len(self.log), "no resume attempt")

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
        self.assertEqual(0, self.livetv._stream_slots.active, "a failed open holds no slot")
        self.assertEqual({}, self.vod._playbacks)

    def test_a_player_that_leaves_during_the_open_gives_the_slot_back(self):
        """The 509 wait can last seconds; a player that gives up meanwhile
        cancels the request. The playback must not sit on its slot for the
        idle window -- the next request (theirs or another TV's) needs it."""
        script = {self.upstream: [_plain(509), _plain(509), _plain(509)]}

        async def gone(d):
            raise asyncio.CancelledError()
        with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                mock.patch("asyncio.sleep", gone):
            async def go():
                await self.vod.vod_stream("movie", self.token_file, _request("bytes=0-"), self.db)
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(go())
        self.assertEqual(0, self.livetv._stream_slots.active)
        self.assertEqual({}, self.vod._playbacks)

    def test_a_player_that_leaves_before_the_first_byte_gives_the_slot_back(self):
        """Starlette does not cancel a handler on disconnect; it cancels the
        SEND task, and when that lands before the body generator is entered
        the generator's `finally` never runs. Only the response's background
        task does -- and it must do the same bookkeeping."""
        script = {self.upstream: [_file(206, [b"AAAA"], start=0)]}
        vod = self.vod

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch.object(vod, "DISCONNECT_GRACE_SECONDS", 0.01):
                resp = await vod.vod_stream("movie", self.token_file, _request("bytes=0-"), self.db)
                pb = next(iter(vod._playbacks.values()))
                self.assertEqual(1, pb.active_bodies)

                async def receive():                # the client hangs up at once
                    return {"type": "http.disconnect"}

                async def send(message):            # nothing is ever sent
                    await asyncio.sleep(0.01)
                await resp({"type": "http"}, receive, send)      # Starlette's own __call__
                self.assertEqual(0, pb.active_bodies, "the never-entered generator still counts down")
                await asyncio.sleep(0.05)
                self.assertEqual({}, vod._playbacks, "released after the grace, not the idle window")
                self.assertEqual(0, self.livetv._stream_slots.active)
                resp._finish()                      # idempotent with the generator's own finally
        asyncio.run(go())

    def test_a_player_that_leaves_while_the_provider_refuses_stops_the_waiting(self):
        """The handler is not cancelled on disconnect, so the open loop asks
        the request whether its client is still there before each retry."""
        script = {self.upstream: [_plain(509), _plain(509), _file(206, [b"AAAA"], start=0)]}
        vod = self.vod
        req = _request("bytes=0-")

        async def gone():
            return True

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch.object(req, "is_disconnected", gone):
                with self.assertRaises(HTTPException) as cm:
                    await vod.vod_stream("movie", self.token_file, req, self.db)
                self.assertEqual(503, cm.exception.status_code)
        asyncio.run(go())
        self.assertEqual(1, len(self.log), "no retry for a player that is gone")
        self.assertEqual({}, vod._playbacks)
        self.assertEqual(0, self.livetv._stream_slots.active)

    def test_a_range_served_in_full_keeps_the_slot_for_the_next_range(self):
        script = {self.upstream: [_file(206, [b"AAAA"], start=0)]}
        with mock.patch.object(self.vod, "DISCONNECT_GRACE_SECONDS", 0.01):
            self._play(script, range_header="bytes=0-")
            asyncio.run(asyncio.sleep(0.05))
        self.assertEqual(1, len(self.vod._playbacks), "the player will ask for the next range")

    def test_a_seek_that_is_still_opening_when_the_grace_ends_keeps_its_slot(self):
        """ffmpeg drops the old range on every seek, so the grace release is
        scheduled; if the seek's own open is slow (a 509 first), the grace
        must not free the lease under it -- the seek would then stream on a
        playback with no slot, i.e. an uncounted provider connection."""
        vod, livetv = self.vod, self.livetv
        script = {self.upstream: [_file(206, [b"AAAA", b"BBBB"], start=0), _plain(509), _file(206, [b"CCCC"], start=500)]}
        real_sleep = asyncio.sleep

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch.object(vod, "DISCONNECT_GRACE_SECONDS", 0.02):
                first = await vod.vod_stream("movie", self.token_file, _request("bytes=0-"), self.db)
                it = first.body_iterator.__aiter__()
                await it.__anext__()
                await it.aclose()                       # the player dropped the range: grace scheduled
                pb = next(iter(vod._playbacks.values()))
                seek = await vod.vod_stream("movie", self.token_file, _request("bytes=500-"), self.db)
                # the seek's open waited out a 509 (~1 s), well past the 0.02 s grace
                self.assertIs(pb, next(iter(vod._playbacks.values())), "same playback, not released")
                self.assertEqual(1, livetv._stream_slots.active, "the lease survived the grace")
                out = b""
                async for chunk in seek.body_iterator:
                    out += chunk
                self.assertEqual(b"CCCC", out)
                await real_sleep(0.05)
                self.assertEqual(1, len(vod._playbacks), "served to the end: kept for the next range")
        asyncio.run(go())

    def test_two_first_requests_for_one_title_share_one_lease(self):
        """Both wait for a slot; both get one; the second must not overwrite
        the first's playback (whose lease would then never be released)."""
        vod, livetv = self.vod, self.livetv
        from models.database import set_setting
        set_setting(self.db, "livetv_max_concurrent_streams", "2")     # room for both
        self.db.commit()
        gate = asyncio.Event()
        real_acquire = livetv._stream_slots.acquire_lease

        async def slow_acquire(*a, **kw):
            await gate.wait()
            return await real_acquire(*a, **kw)

        async def go():
            with mock.patch.object(livetv._stream_slots, "acquire_lease", slow_acquire):
                t1 = asyncio.ensure_future(vod._playback_for(self.db, "movie/x", "vod:movie:1:1", "10.0.0.5"))
                t2 = asyncio.ensure_future(vod._playback_for(self.db, "movie/x", "vod:movie:1:1", "10.0.0.5"))
                await asyncio.sleep(0)
                gate.set()
                a, b = await asyncio.gather(t1, t2)
            self.assertIs(a, b)
            self.assertEqual(1, livetv._stream_slots.active)
            self.assertEqual(1, len(vod._playbacks))
        asyncio.run(go())

    def test_a_failed_seek_keeps_a_playback_that_is_still_streaming(self):
        """Only an open with NO body running releases the playback: a seek
        that fails while the previous range still streams must not pull the
        slot from under it."""
        script = {self.upstream: [_file(206, [b"AAAA"], start=0), _plain(404)]}
        vod = self.vod

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                first = await vod.vod_stream("movie", self.token_file, _request("bytes=0-"), self.db)
                it = first.body_iterator.__aiter__()
                await it.__anext__()
                with self.assertRaises(HTTPException):
                    await vod.vod_stream("movie", self.token_file, _request("bytes=500-"), self.db)
                self.assertEqual(1, len(vod._playbacks))
                self.assertEqual(1, self.livetv._stream_slots.active)
                await it.aclose()
        asyncio.run(go())


class OnePlaybackOneLease(_Base):
    def test_seeks_reuse_the_lease(self):
        script = {self.upstream: [_file(206, [b"AAAA"], start=0), _file(206, [b"BBBB"], start=500)]}
        self._play(script, range_header="bytes=0-")
        self._play(script, range_header="bytes=500-")
        self.assertEqual(1, self.livetv._stream_slots.active, "a seek is not a second connection slot")
        self.assertEqual(1, len(self.vod._playbacks))

    def test_a_preempted_paused_playback_drops_its_provider_connection_at_once(self):
        """A paused player leaves the body blocked sending to it; the
        provider connection must still close the moment a recording takes
        the slot -- that open connection is what 509s the recording."""
        vod, livetv = self.vod, self.livetv

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client({self.upstream: [_file(206, [b"A"] * 50, start=0)]},
                                                                      self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                resp = await vod.vod_stream("movie", self.token_file, _request("bytes=0-"), self.db)
                it = resp.body_iterator.__aiter__()
                await it.__anext__()                         # playing, then paused: nothing reads on
                pb = next(iter(vod._playbacks.values()))
                upstream = pb.upstream
                self.assertIsNotNone(upstream)
                await livetv._stream_slots.acquire_lease(1, 0.01, "recording", "channel:1")
                await asyncio.sleep(0.01)
                self.assertTrue(upstream.is_closed, "closed without waiting for the player")
                self.assertEqual({}, vod._playbacks, "no longer listed while its request is still stuck")
                self.assertEqual([], [x for x in livetv._stream_snapshot(self.db) if x["kind"] == "vod"])
                self.assertEqual(["channel:1"], [l.owner for l in livetv._stream_slots.leases.values()])
                rest = [c async for c in it]
                self.assertLessEqual(len(rest), 1, "the body ends instead of resuming")
                self.assertEqual(1, len(self.log), "no resume after a pre-emption")
        asyncio.run(go())

    def test_pre_empted_while_waiting_out_a_509_makes_no_further_attempt(self):
        """Pre-empted during the open's backoff: the next attempt would be
        an uncounted provider connection while the recording opens."""
        vod, livetv = self.vod, self.livetv
        script = {self.upstream: [_plain(509), _file(206, [b"AAAA"], start=0)]}
        real_sleep = asyncio.sleep

        async def sleep_then_lose_the_slot(d):
            for pb in list(vod._playbacks.values()):
                pb.stop()
            await real_sleep(0)

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch("asyncio.sleep", sleep_then_lose_the_slot):
                with self.assertRaises(HTTPException) as cm:
                    await vod.vod_stream("movie", self.token_file, _request("bytes=0-"), self.db)
                self.assertEqual(503, cm.exception.status_code)
        asyncio.run(go())
        self.assertEqual(1, len(self.log), "only the refused attempt reached the provider")

    def test_two_tvs_on_the_same_title_are_two_playbacks(self):
        """Keyed by title alone, the second TV would take over the first
        one's playback and each new range from either would end the other's
        (the generation check). Two clients: two playbacks, two slots."""
        from models.database import set_setting
        set_setting(self.db, "livetv_max_concurrent_streams", "2")
        self.db.commit()
        script = {self.upstream: [_file(206, [b"AAAA"], start=0), _file(206, [b"BBBB"], start=0)]}
        self._play(script, range_header="bytes=0-", client="10.0.0.5")
        self._play(script, range_header="bytes=0-", client="10.0.0.6")
        self.assertEqual(2, len(self.vod._playbacks))
        self.assertEqual(2, self.livetv._stream_slots.active)
        snap = [s for s in self.livetv._stream_snapshot(self.db) if s["kind"] == "vod"]
        self.assertEqual({"10.0.0.5", "10.0.0.6"}, {s["client"] for s in snap})
        self.assertEqual({"vod:movie:%d:2141622" % self.provider.id}, {s["channel"] for s in snap})

    def test_a_playback_is_listed_with_the_live_streams(self):
        script = {self.upstream: [_file(206, [b"AAAA"], start=0)]}
        self._play(script, range_header="bytes=0-")
        snap = self.livetv._stream_snapshot(self.db)
        vods = [s for s in snap if s["kind"] == "vod"]
        self.assertEqual(1, len(vods))
        self.assertEqual("vod:movie:%d:2141622" % self.provider.id, vods[0]["channel"])
        self.assertIn(vods[0]["state"], ("idle", "streaming"))

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
                self.assertEqual({}, vod._playbacks, "a pre-empted playback is gone at once, not at the next sweep")
                self.assertEqual(0, vod._sweep_once(asyncio.get_running_loop().time()))
                self.assertEqual({"channel:9"}, {l.owner for l in livetv._stream_slots.leases.values()})
        asyncio.run(go())


class _RangeFake:
    """httpx.AsyncClient stand-in for a provider that honours Range on one
    file, sends it 10 bytes at a time, and keeps count of the connections
    open at once (a closed response is no longer open)."""

    def __init__(self, data: bytes, log: list):
        self.data, self.log = data, log
        self.responses = []
        self.most_open = 0

    def __call__(self, **kw):
        return self

    async def aclose(self):
        pass

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    def open_now(self):
        return sum(1 for r in self.responses if not r.is_closed)

    async def send(self, request, stream=False):
        rng = request.headers.get("range")
        self.log.append((request.method, rng, self.open_now()))
        total = len(self.data)
        a, b = (rng[len("bytes="):].split("-") if rng else ("0", ""))
        first, last = int(a), (int(b) if b else total - 1)
        data = self.data[first:last + 1]

        class _Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for k in range(0, len(data), 10):
                    yield data[k:k + 10]
                    await asyncio.sleep(0)

            async def aclose(self):
                pass
        resp = httpx.Response(206, headers={"content-type": "video/mp4", "accept-ranges": "bytes",
                                            "content-range": f"bytes {first}-{last}/{total}",
                                            "content-length": str(len(data))},
                              stream=_Stream(), request=request)
        self.responses.append(resp)
        self.most_open = max(self.most_open, self.open_now())
        return resp


class JellyfinsParallelRequestsShareOneConnection(_Base):
    """Jellyfin opens more than one request for the title it plays: a
    subtitle extraction next to the playback, a restart 0.1 s after a stop,
    two transcodes at once, ffmpeg's seeks. The provider cuts an account
    with two connections open (and counts one that just closed for a few
    seconds), so they share the playback's one connection: the old one is
    closed before a new one opens, a parked request carries on from its own
    byte, and nobody gets an early end."""

    DATA = bytes(range(256)) * 4      # 1024 bytes, 10 per piece

    def _run(self, scenario):
        fake = _RangeFake(self.DATA, self.log)
        self.fake = fake

        async def go():
            with mock.patch("httpx.AsyncClient", fake), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch.object(self.vod, "QUANTUM_SECONDS", 0.02), \
                    mock.patch.object(self.vod, "PREEMPT_GRACE_SECONDS", 0.02):
                return await scenario()
        return asyncio.run(go())

    async def _open(self, range_header):
        return await self.vod.vod_stream("movie", self.token_file, _request(range_header), self.db)

    async def _read(self, resp):
        return b"".join([c async for c in resp.body_iterator])

    def test_a_subtitle_extraction_next_to_the_playback_gets_its_turns_and_both_get_the_whole_file(self):
        async def scenario():
            playback = await self._open("bytes=0-")
            subtitles = await self._open("bytes=0-")
            return await asyncio.gather(self._read(playback), self._read(subtitles))
        a, b = self._run(scenario)
        self.assertEqual(self.DATA, a, "the playback ended early")
        self.assertEqual(self.DATA, b)
        self.assertEqual(1, self.fake.most_open, "never two provider connections at once")
        self.assertTrue(all(open_before == 0 for _, _, open_before in self.log),
                        "every connection opened after the previous one was closed")
        self.assertGreater(len(self.log), 2, "they took turns")
        self.assertEqual(1, self.livetv._stream_slots.active, "one playback, one slot")

    def test_a_seek_takes_the_connection_and_the_range_it_left_waits_without_ending(self):
        async def scenario():
            first = await self._open("bytes=0-")
            it1 = first.body_iterator.__aiter__()
            got = await it1.__anext__()
            seek = await self._open("bytes=500-")
            self.assertEqual(0, self.log[-1][2], "the old connection was closed before the seek opened")
            rest = await self._read(seek)
            # The player let go of the old range (ffmpeg closes it after a seek).
            await it1.aclose()
            return got, rest
        got, rest = self._run(scenario)
        self.assertEqual(self.DATA[:10], got)
        self.assertEqual(self.DATA[500:], rest)
        self.assertEqual(1, self.fake.most_open)

    def test_the_range_a_newer_request_parked_goes_on_from_its_own_byte(self):
        async def scenario():
            first = await self._open("bytes=0-")
            it1 = first.body_iterator.__aiter__()
            head = await it1.__anext__()
            second = await self._open("bytes=1000-")
            tail = await self._read(second)           # the newer one finishes...
            rest = b"".join([c async for c in it1])  # ...and the parked one carries on
            return head + rest, tail
        whole, tail = self._run(scenario)
        self.assertEqual(self.DATA, whole, "the parked range ended early or skipped bytes")
        self.assertEqual(self.DATA[1000:], tail)
        self.assertEqual("bytes=10-1023", self.log[-1][1], "resumed from the byte it had reached")
        self.assertEqual(1, self.fake.most_open)

    def test_a_restart_right_after_a_stop_waits_for_the_old_connection_to_close(self):
        async def scenario():
            first = await self._open("bytes=0-")
            it1 = first.body_iterator.__aiter__()
            await it1.__anext__()
            await it1.aclose()                         # "q": the transcode stopped
            again = await self._open("bytes=0-")       # and restarts 0.1 s later
            return await self._read(again)
        body = self._run(scenario)
        self.assertEqual(self.DATA, body)
        self.assertEqual(0, self.log[-1][2], "the stopped one's connection was closed first")
        self.assertEqual(1, self.fake.most_open)

    def test_a_paused_holder_does_not_starve_a_waiting_request(self):
        """The holder's player stopped reading (paused): its body cannot
        notice its turn is over, so the waiting request takes it."""
        async def scenario():
            paused = await self._open("bytes=0-")
            itp = paused.body_iterator.__aiter__()
            other = await self._open("bytes=0-")
            ito = other.body_iterator.__aiter__()
            await ito.__anext__()                      # `other` holds it, then its player pauses
            got = await asyncio.wait_for(itp.__anext__(), 5)   # the first one is not stuck behind it
            await itp.aclose()
            await ito.aclose()
            return got
        self.assertEqual(self.DATA[:10], self._run(scenario))

    def test_a_head_during_the_playback_opens_no_connection(self):
        async def scenario():
            playing = await self._open("bytes=0-")
            it = playing.body_iterator.__aiter__()
            await it.__anext__()
            before = len(self.log)
            head = await self.vod.vod_head("movie", self.token_file, _request(method="HEAD"), self.db)
            await it.aclose()
            return head, len(self.log) - before
        head, opened = self._run(scenario)
        self.assertEqual(0, opened)
        self.assertEqual(200, head.status_code)
        self.assertEqual(str(len(self.DATA)), head.headers["content-length"])


class ABodyThatEndsShortIsADrop(_Base):
    """`Stream ends prematurely at X, should be Y`: the provider ends the
    body cleanly (no error, no reset) before its Content-Length. That is a
    drop, resumed from the byte reached, never a normal end."""

    def test_a_clean_short_end_is_resumed(self):
        script = {self.upstream: [_file(206, [b"AAAA"], start=0, total=8),
                                  _file(206, [b"BBBB"], start=4, total=8)]}
        status, headers, body = self._play(script, range_header="bytes=0-")
        self.assertEqual(b"AAAABBBB", body)
        self.assertEqual("bytes=4-7", self.log[1][2])

    def test_a_200_that_ends_short_of_its_length_is_resumed(self):
        script = {self.upstream: [_file(200, [b"AAAA"], total=8), _file(206, [b"BBBB"], start=4, total=8)]}
        status, headers, body = self._play(script)
        self.assertEqual(200, status)
        self.assertEqual(b"AAAABBBB", body)
        self.assertEqual("bytes=4-", self.log[1][2])

    def test_dozens_of_drops_that_each_deliver_are_all_resumed(self):
        """The provider cuts every 5-20 s after 60-160 MB: a film is dozens
        of drops. Each connection that delivered resets the budget."""
        pieces = 30
        script = {self.upstream: [_file(206, [bytes([65 + k])] * 1, start=k, total=pieces) for k in range(pieces)]}
        with mock.patch.object(self.vod, "PROGRESS_RESET_BYTES", 1):
            status, headers, body = self._play(script, range_header="bytes=0-")
        self.assertEqual(bytes(65 + k for k in range(pieces)), body)
        self.assertEqual(pieces, len(self.log))


class _DroppingProvider:
    """A provider on 127.0.0.1 that serves one file with byte ranges and, for
    its first `drops` connections, closes the socket cleanly after `cut`
    bytes, short of its Content-Length: what Jellyfin's ffmpeg logged as
    "partial file" before it took the episode as finished."""

    def __init__(self, data: bytes, drops: int, cut: int):
        self.data, self.drops, self.cut = data, drops, cut
        self.requests = []          # (path, Range header)
        self.open = self.most_open = 0

    async def handle(self, reader, writer):
        self.open += 1
        self.most_open = max(self.most_open, self.open)
        try:
            await self._serve(reader, writer)
        finally:
            self.open -= 1

    async def _serve(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        path = head[0].split(" ")[1]
        rng = next((h.split(":", 1)[1].strip() for h in head[1:] if h.lower().startswith("range:")), None)
        self.requests.append((path, rng))
        total = len(self.data)
        start, end = 0, total - 1
        if rng:
            a, b = rng[len("bytes="):].split("-")
            start, end = int(a), (int(b) if b else total - 1)
        body = self.data[start:end + 1]
        status = "206 Partial Content" if rng else "200 OK"
        head_out = (f"HTTP/1.1 {status}\r\nContent-Type: video/mp4\r\nAccept-Ranges: bytes\r\n"
                    f"Content-Length: {len(body)}\r\n")
        if rng:
            head_out += f"Content-Range: bytes {start}-{end}/{total}\r\n"
        writer.write((head_out + "Connection: close\r\n\r\n").encode())
        if self.drops > 0:
            self.drops -= 1
            body = body[:self.cut]
        try:
            writer.write(body)
            await writer.drain()
        except ConnectionError:
            pass                    # the relay closed it (handed to another request)
        writer.close()


class AProviderThatDropsTheConnection(_Base):
    """Through a real socket and the real httpx: the provider ends the
    connection mid-episode (a clean close short of its Content-Length), and
    Jellyfin's ffmpeg, which asks `Range: bytes=0-` and then seeks with
    `bytes=N-`, still gets every byte of the file, with no early end."""

    DATA = bytes(range(256)) * 1200       # 307200 bytes

    def _play_real(self, provider, range_header, readers=1):
        vod, real_sleep = self.vod, asyncio.sleep
        stream_id = 800506
        url = self.vod_tokens.url("http://tentacle:8888", "s" * 64, self.provider.id, "series", stream_id, "mp4")

        async def fast_sleep(d):
            await real_sleep(0)

        async def go():
            server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            self.provider.server_url = f"http://127.0.0.1:{port}"
            self.db.commit()
            try:
                with mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                        mock.patch("routers.vod.asyncio.sleep", fast_sleep), \
                        mock.patch.object(vod, "QUANTUM_SECONDS", 0.01):
                    resps = [await vod.vod_stream("series", url.rsplit("/", 1)[1], _request(range_header), self.db)
                             for _ in range(readers)]

                    async def read(resp):
                        return b"".join([piece async for piece in resp.body_iterator])
                    bodies = await asyncio.gather(*(read(r) for r in resps))
                    if readers > 1:
                        return bodies
                    return resps[0].status_code, dict(resps[0].headers), bodies[0]
            finally:
                server.close()
                await server.wait_closed()
        return asyncio.run(go())

    def setUp(self):
        super().setUp()
        from services import vod_tokens
        self.vod_tokens = vod_tokens

    def test_the_first_request_gets_the_whole_file_across_two_drops(self):
        provider = _DroppingProvider(self.DATA, drops=2, cut=100_000)
        status, headers, body = self._play_real(provider, "bytes=0-")
        self.assertEqual(206, status)
        self.assertEqual(str(len(self.DATA)), headers.get("content-length"))
        self.assertEqual(len(self.DATA), len(body), "the episode would have ended at the drop")
        self.assertEqual(self.DATA, body)
        self.assertEqual([("/series/u/p/800506.mp4", "bytes=0-"),
                          ("/series/u/p/800506.mp4", "bytes=100000-307199"),
                          ("/series/u/p/800506.mp4", "bytes=200000-307199")], provider.requests)

    def test_a_playback_and_a_subtitle_extraction_share_one_connection_through_drops(self):
        provider = _DroppingProvider(self.DATA, drops=3, cut=60_000)
        playback, subtitles = self._play_real(provider, "bytes=0-", readers=2)
        self.assertEqual(self.DATA, playback, "the episode ended early")
        self.assertEqual(self.DATA, subtitles)
        self.assertEqual(1, provider.most_open, "the provider saw two connections at once")

    def test_a_seek_gets_the_rest_of_the_file_across_a_drop(self):
        provider = _DroppingProvider(self.DATA, drops=1, cut=50_000)
        status, headers, body = self._play_real(provider, "bytes=150000-")
        self.assertEqual(206, status)
        self.assertEqual(f"bytes 150000-307199/{len(self.DATA)}", headers.get("content-range"))
        self.assertEqual(self.DATA[150000:], body)
        self.assertEqual(["bytes=150000-", "bytes=200000-307199"], [r for _, r in provider.requests])


if __name__ == "__main__":
    unittest.main()

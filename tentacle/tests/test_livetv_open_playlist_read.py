"""Opening an HLS channel: a first playlist that never arrives fails the open
cleanly, within the open budget.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Seen on a QA stack: the channel URL answered its headers and then no body.
The opener read that body bounded only by the client's 120 s read timeout
(the variant read right after it has 10 s), outside the retry loop, so after
120 s an unhandled ReadTimeout became an ASGI traceback. Jellyfin had given
up on the tuner at 100 s and the TV app at 30 s; all that time the open held
its concurrency slot and a provider connection, and a second viewer of the
channel waited for all of it. A body that trickles a byte at a time resets
the read timeout, so it held them indefinitely; a connection dropped in the
middle of the body raised an unhandled error too.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from test_livetv_open_single_fetch import PANEL, PLAYLIST_CT, TOKENIZED, FakeClient, _redirect, _resp

PLAYLIST = (b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:1\n"
            b"#EXTINF:6.0,\nhttp://edge.provider.test/seg/1.ts\n")


class _Clock:
    t = 0.0


class _StalledBody(httpx.AsyncByteStream):
    """Headers came; the body sends `pieces`, `gap` s apart, then hangs for ever."""

    def __init__(self, pieces=(), gap=5.0):
        self.pieces, self.gap = list(pieces), gap
        self.closed = False

    async def __aiter__(self):
        for p in self.pieces:
            await asyncio.sleep(self.gap)
            yield p
        while True:
            await asyncio.sleep(3600)
            yield b""

    async def aclose(self):
        self.closed = True


def _stalled(pieces=(), gap=5.0):
    body = _StalledBody(pieces, gap)
    return httpx.Response(200, headers=PLAYLIST_CT, stream=body,
                          request=httpx.Request("GET", TOKENIZED)), body


async def _open(script):
    """Open the channel on a virtual clock; returns (outcome, log, elapsed)."""
    import routers.livetv as livetv
    log, closed = [], []
    loop = asyncio.get_running_loop()
    loop.slow_callback_duration = 3600      # the virtual clock jumps on purpose
    real_sleep, real_wait_for = asyncio.sleep, asyncio.wait_for
    clock = _Clock()

    async def vsleep(delay, *a):
        # Time moves in small steps, so a timeout elsewhere can fire in between.
        end = clock.t + delay
        while clock.t < end:
            clock.t = min(end, clock.t + 0.25)
            await real_sleep(0)

    async def vwait_for(aw, timeout):
        # A virtual-time wait_for: run the awaitable; if it only sleeps past the
        # timeout, cancel it and raise, as the real one would.
        task = asyncio.ensure_future(aw)
        start = clock.t
        while not task.done():
            if timeout is not None and clock.t - start >= timeout:
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                raise asyncio.TimeoutError()
            await real_sleep(0)
        return task.result()

    released = []
    with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, closed)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", vsleep), patch("asyncio.wait_for", vwait_for), \
            patch.object(loop, "time", lambda: clock.t):
        try:
            outcome = await real_wait_for(livetv._stream_proxy_inner(
                channel_id=1, user_agent="T/1", stream_url=PANEL,
                _release_sem=lambda: released.append(1), guard=None), 300)   # virtual seconds
        except HTTPException as e:
            outcome = e
        except asyncio.TimeoutError:
            outcome = "still opening after 300 s (the read never ended)"
    return outcome, log, clock.t, closed, released


class FirstPlaylistRead(unittest.IsolatedAsyncioTestCase):
    async def test_a_stalled_first_playlist_fails_the_open_within_the_budget(self):
        r1, b1 = _stalled()
        r2, b2 = _stalled()
        script = {PANEL: [_redirect()], TOKENIZED: [r1, r2]}
        outcome, log, elapsed, closed, _ = await _open(script)
        self.assertIsInstance(outcome, HTTPException, f"the open did not fail cleanly: {outcome}")
        self.assertEqual(502, outcome.status_code)
        self.assertLessEqual(elapsed, 30, "held the open past a TV app's patience")
        self.assertTrue(b1.closed and b2.closed, "a stalled provider connection was left open")
        self.assertTrue(closed, "the client was not closed")

    async def test_a_trickling_first_playlist_is_bounded_too(self):
        r1, _ = _stalled([b"#"] * 100, gap=5.0)       # one byte every 5 s: never a read timeout
        r2, _ = _stalled([b"#"] * 100, gap=5.0)
        script = {PANEL: [_redirect()], TOKENIZED: [r1, r2]}
        outcome, log, elapsed, *_ = await _open(script)
        self.assertIsInstance(outcome, HTTPException, f"{outcome}")
        self.assertLessEqual(elapsed, 30)

    async def test_a_stall_once_is_retried_and_the_channel_opens(self):
        r1, b1 = _stalled()
        ok = httpx.Response(200, headers=PLAYLIST_CT, content=PLAYLIST, request=httpx.Request("GET", TOKENIZED))
        script = {PANEL: [_redirect()], TOKENIZED: [r1, ok]}
        outcome, log, elapsed, *_ = await _open(script)
        self.assertNotIsInstance(outcome, (HTTPException, str), f"one stalled answer failed the open: {outcome}")
        self.assertTrue(b1.closed)
        self.assertLessEqual(elapsed, 30)
        close = getattr(outcome, "close_upstream", None)
        if close is not None:
            await close()

    async def test_a_connection_dropped_mid_body_is_retried(self):
        class _Dropped(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"#EXTM3U\n"
                raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

            async def aclose(self):
                pass
        dropped = httpx.Response(200, headers=PLAYLIST_CT, stream=_Dropped(), request=httpx.Request("GET", TOKENIZED))
        ok = httpx.Response(200, headers=PLAYLIST_CT, content=PLAYLIST, request=httpx.Request("GET", TOKENIZED))
        script = {PANEL: [_redirect()], TOKENIZED: [dropped, ok]}
        try:
            outcome, log, elapsed, *_ = await _open(script)
        except httpx.HTTPError as e:
            self.fail(f"the dropped body escaped the open: {e!r}")
        self.assertNotIsInstance(outcome, (HTTPException, str), f"{outcome}")
        self.assertLessEqual(elapsed, 30)
        close = getattr(outcome, "close_upstream", None)
        if close is not None:
            await close()

    async def test_a_prompt_playlist_opens_as_before(self):
        ok = httpx.Response(200, headers=PLAYLIST_CT, content=PLAYLIST, request=httpx.Request("GET", TOKENIZED))
        script = {PANEL: [_redirect()], TOKENIZED: [ok]}
        outcome, log, elapsed, *_ = await _open(script)
        self.assertNotIsInstance(outcome, HTTPException)
        self.assertEqual([PANEL, TOKENIZED], log[:2])
        self.assertLess(elapsed, 1)
        close = getattr(outcome, "close_upstream", None)
        if close is not None:
            await close()


if __name__ == "__main__":
    unittest.main()

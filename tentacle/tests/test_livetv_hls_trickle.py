"""A running HLS stream is not frozen by a playlist or segment that trickles.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The HLS worker read every playlist refresh and segment with a bare aread()
under the client's 30 s read timeout. That timeout is per read: a body that
trickles in a byte at a time never reaches it, so one such answer froze the
stream -- a recording got no data for as long as the provider kept trickling,
and nothing ended it. A stall (no bytes at all) was already caught after 30 s
and retried; a trickle is now bounded the same way, and retried the same way:
within the budget for a viewer, for as long as the client is attached for a
recording.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

BASE = "http://provider.test/live/u/p/1.m3u8"
SEG = "http://provider.test/seg/{}.ts"
PLAYLIST_CT = {"content-type": "application/vnd.apple.mpegurl"}
TS_CT = {"content-type": "video/mp2t"}
DATA = {n: b"G" + bytes([n]) * 187 for n in range(1, 6)}


def _playlist(*seqs, end=False):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6",
             f"#EXT-X-MEDIA-SEQUENCE:{seqs[0] if seqs else 0}"]
    for n in seqs:
        lines += ["#EXTINF:6.0,", SEG.format(n)]
    if end:
        lines.append("#EXT-X-ENDLIST")      # lets a test finish; a live playlist never has it
    return ("\n".join(lines) + "\n").encode()


def _ok(url, body, ct):
    return httpx.Response(200, headers=ct, content=body, request=httpx.Request("GET", url))


class _Trickle(httpx.AsyncByteStream):
    """One byte every 5 s, for ever: never a per-read timeout."""

    async def __aiter__(self):
        while True:
            await asyncio.sleep(5.0)
            yield b"#"

    async def aclose(self):
        pass


def _trickle(url, ct):
    return httpx.Response(200, headers=ct, stream=_Trickle(), request=httpx.Request("GET", url))


class _Client:
    def __init__(self, script, log, **kw):
        self.script, self.log = script, log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aclose(self):
        pass

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        url = str(request.url)
        self.log.append(url)
        q = self.script[url]
        item = q[0] if len(q) == 1 else q.pop(0)
        return item() if callable(item) else item


async def _run(script, failure_budget, is_recording):
    """Stream the channel on a virtual clock. (bytes, virtual seconds, urls)."""
    import routers.livetv as livetv
    loop = asyncio.get_running_loop()
    loop.slow_callback_duration = 3600      # the virtual clock jumps on purpose
    real_sleep = asyncio.sleep
    clock = {"t": 0.0}
    log = []

    async def vsleep(delay, *a):
        end = clock["t"] + delay
        while clock["t"] < end:
            clock["t"] = min(end, clock["t"] + 0.25)
            await real_sleep(0)

    out = b""
    with patch("httpx.AsyncClient", lambda **kw: _Client(script, log)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", vsleep), \
            patch.object(loop, "time", lambda: clock["t"]):
        resp = await livetv._stream_proxy_inner(
            channel_id=1, user_agent="T/1", stream_url=BASE, _release_sem=lambda: None,
            guard=None, failure_budget=failure_budget, is_recording=is_recording)
        pieces = []

        async def consume():
            async for piece in resp.body_iterator:
                pieces.append(piece)
        task = asyncio.ensure_future(consume())
        while not task.done():
            if clock["t"] > 2000:           # virtual seconds: the stream is frozen
                task.cancel()
                break
            await real_sleep(0)
        try:
            await task
        except asyncio.CancelledError:
            pass
        out = b"".join(pieces)
    return out, clock["t"], log


class RunningStreamTrickle(unittest.IsolatedAsyncioTestCase):
    async def test_a_trickling_segment_is_retried_and_the_recording_goes_on(self):
        script = {BASE: [_ok(BASE, _playlist(1, 2, 3, end=True), PLAYLIST_CT)],
                  SEG.format(1): [_ok(SEG.format(1), DATA[1], TS_CT)],
                  SEG.format(2): [_trickle(SEG.format(2), TS_CT), _ok(SEG.format(2), DATA[2], TS_CT)],
                  SEG.format(3): [_ok(SEG.format(3), DATA[3], TS_CT)]}
        out, t, log = await _run(script, failure_budget=0, is_recording=lambda: True)
        self.assertEqual(DATA[1] + DATA[2] + DATA[3], out, "the stream froze on the trickling segment")
        self.assertLess(t, 120)

    async def test_a_trickling_playlist_refresh_is_retried(self):
        script = {BASE: [_ok(BASE, _playlist(1), PLAYLIST_CT), _trickle(BASE, PLAYLIST_CT),
                         _ok(BASE, _playlist(1, 2, end=True), PLAYLIST_CT)],
                  SEG.format(1): [_ok(SEG.format(1), DATA[1], TS_CT)],
                  SEG.format(2): [_ok(SEG.format(2), DATA[2], TS_CT)]}
        out, t, log = await _run(script, failure_budget=0, is_recording=lambda: True)
        self.assertEqual(DATA[1] + DATA[2], out, "the stream froze on the trickling refresh")
        self.assertLess(t, 120)

    async def test_a_viewer_on_a_segment_that_always_trickles_gives_up_within_the_budget(self):
        script = {BASE: [_ok(BASE, _playlist(1, 2, end=True), PLAYLIST_CT)],
                  SEG.format(1): [_ok(SEG.format(1), DATA[1], TS_CT)],
                  SEG.format(2): [lambda: _trickle(SEG.format(2), TS_CT)]}
        out, t, log = await _run(script, failure_budget=60, is_recording=lambda: False)
        self.assertEqual(DATA[1], out)
        self.assertLess(t, 60 + 2 * 20 + 15, "the viewer's budget did not bound a trickle")

    def test_the_segment_bound_scales_with_the_segment(self):
        import routers.livetv as livetv
        self.assertEqual(20.0, livetv._segment_read_limit(2))
        self.assertEqual(30.0, livetv._segment_read_limit(10))
        self.assertEqual(20.0, livetv._segment_read_limit(None))


if __name__ == "__main__":
    unittest.main()

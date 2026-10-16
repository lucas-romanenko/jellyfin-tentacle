"""#136 on the HLS path: a recording never gives up while its client is
attached, whatever the viewer's reconnect budget says.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The raw MPEG-TS path pins this (test_livetv_reconnect_budget,
test_livetv_raw_redial_refusals), but no HLS test did: making
_stream_proxy_inner's _budget_spent() ignore `is_recording` failed only the
two raw tests. The HLS worker measures its outage on the event loop's clock,
not on the sleeps it asked for, so this test runs the loop on a fake clock
that each (instant) sleep moves forward.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

BASE = "http://provider.test/live/u/p/1.m3u8"
CT = {"content-type": "application/vnd.apple.mpegurl"}


def _playlist(seq, *chunks, end=False):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6",
             f"#EXT-X-MEDIA-SEQUENCE:{seq}"]
    for c in chunks:
        lines += ["#EXTINF:6.0,", c]
    if end:
        lines.append("#EXT-X-ENDLIST")     # lets the worker finish on its own
    return "\n".join(lines) + "\n"


def _resp(status, url, content=b"", headers=None):
    return httpx.Response(status, headers=headers or {}, content=content,
                          request=httpx.Request("GET", url))


class FakeClient:
    def __init__(self, script, **kw):
        self._script = script

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aclose(self):
        return None

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        return self._answer(str(request.url))

    async def get(self, url, headers=None):
        return self._answer(str(url))

    def _answer(self, url):
        q = self._script[url]
        return q[0] if len(q) == 1 else q.pop(0)


def _script(refusals):
    c1, c2 = "http://provider.test/live/u/p/c1.ts", "http://provider.test/live/u/p/c2.ts"
    return {
        BASE: ([_resp(200, BASE, _playlist(1, c1).encode(), CT)]
               + [_resp(509, BASE)] * refusals
               + [_resp(200, BASE, _playlist(1, c1, c2, end=True).encode(), CT)]),
        c1: [_resp(200, c1, b"AAAA", {"content-type": "video/mp2t"})],
        c2: [_resp(200, c2, b"BBBB", {"content-type": "video/mp2t"})],
    }


async def _play(script, failure_budget, is_recording):
    import routers.livetv as livetv
    loop = asyncio.get_running_loop()
    real_time, real_sleep = loop.time, asyncio.sleep
    offset = [0.0]
    loop.time = lambda: real_time() + offset[0]
    # The fake clock makes every step look slow to asyncio's debug mode.
    real_slow = loop.slow_callback_duration
    loop.slow_callback_duration = 1e9

    async def fast_sleep(delay, *a, **k):
        offset[0] += max(0.0, delay)
        await real_sleep(0)

    try:
        with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("asyncio.sleep", fast_sleep):
            response = await livetv._stream_proxy_inner(
                channel_id=1, user_agent="TestAgent/1.0", stream_url=BASE,
                _release_sem=lambda: None, guard=None, failure_budget=failure_budget,
                is_recording=is_recording)
            body = b""
            async for piece in response.body_iterator:
                body += piece
    finally:
        loop.time = real_time
        loop.slow_callback_duration = real_slow
    return body, offset[0]


class HlsRecordingOutlastsTheViewerBudget(unittest.IsolatedAsyncioTestCase):
    async def test_a_recording_rides_out_a_refusal_longer_than_the_budget(self):
        body, waited = await _play(_script(30), failure_budget=30, is_recording=lambda: True)
        self.assertEqual(b"AAAABBBB", body,
                         "an HLS recording gave up on a 30 s viewer budget: Jellyfin starts a new file")
        self.assertGreater(waited, 60, "the refusals were not waited out on the fake clock")

    async def test_a_viewer_still_honours_the_budget(self):
        body, waited = await _play(_script(30), failure_budget=30, is_recording=lambda: False)
        self.assertEqual(b"AAAA", body, "a viewer kept retrying past its budget")
        self.assertLess(waited, 60)


if __name__ == "__main__":
    unittest.main()

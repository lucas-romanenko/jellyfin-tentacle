"""#137 (1): what went wrong during a stream is counted and said.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A recording that lost minutes to provider drops looked exactly like a good
one until it was played. The stream code already knows each re-dial, each
wait on a failing provider and each segment it gave up on; it now counts
them per stream, shows them in GET /api/live/streams ("health" on a running
stream, "recent" for ended ones), logs a summary when the stream ends, and
writes an Activity line when a RECORDING ended damaged.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

import routers.livetv as livetv
from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp
from test_livetv_raw_reconnect import _dropped, _live

HLS = "http://provider.test/live/u/p/7.m3u8"
PL = {"content-type": "application/vnd.apple.mpegurl"}
TS = {"content-type": "video/mp2t"}


async def _play(script, url, recording=False):
    log, closed = [], []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, closed, **kw)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep), \
            patch.object(livetv, "SessionLocal", side_effect=RuntimeError("no db in this test")):
        response = await livetv._stream_proxy_inner(
            channel_id=7, user_agent="UA", stream_url=url, _release_sem=lambda: None,
            guard=None, is_recording=lambda: recording)
        body = b""
        async for piece in response.body_iterator:
            body += piece
    return body


class RawHealth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        livetv._recent_streams.clear()

    async def test_a_reconnect_is_counted_and_summarised(self):
        script = {
            PANEL: [_redirect(), _redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"G" * 188], then=_dropped()), _live([b"G" * 188])],
        }
        with self.assertLogs("routers.livetv", "WARNING") as logs:
            await _play(script, PANEL, recording=True)
        last = livetv._recent_streams[-1]
        self.assertEqual(7, last["channel_id"])
        self.assertTrue(last["recording"])
        self.assertEqual(1, last["reconnects"])
        self.assertTrue(any("recording ran" in m and "1 interruption(s) recovered" in m for m in logs.output), logs.output)

    async def test_a_clean_stream_reads_clean(self):
        script = {PANEL: [_redirect()], TOKENIZED: [_live([b"G" * 188])]}
        # after the clean close the redial is refused for good (404): the
        # stream ends without ever reconnecting
        script[PANEL].append(_resp(404, PANEL))
        await _play(script, PANEL)
        last = livetv._recent_streams[-1]
        self.assertEqual((0, 0), (last["reconnects"], last["segments_skipped"]))
        self.assertFalse(last["recording"])

    async def test_the_list_of_ended_streams_is_bounded(self):
        for _ in range(livetv._RECENT_STREAMS_MAX + 5):
            livetv._stream_ended(1, {"opened_at": asyncio.get_running_loop().time(),
                                     "health": livetv._new_health()}, False)
        self.assertEqual(livetv._RECENT_STREAMS_MAX, len(livetv._recent_streams))


class HlsHealth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        livetv._recent_streams.clear()

    async def test_a_skipped_segment_is_counted(self):
        s1, s2 = "http://provider.test/live/u/p/s1.ts", "http://provider.test/live/u/p/s2.ts"
        media = (b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\ns1.ts\n#EXTINF:4,\ns2.ts\n"
                 b"#EXT-X-ENDLIST\n")
        script = {HLS: [_resp(200, HLS, content=media, headers=PL)],
                  s1: [_resp(404, s1)],
                  s2: [_resp(200, s2, content=b"G" * 188, headers=TS)]}
        body = await _play(script, HLS)
        self.assertEqual(b"G" * 188, body)
        self.assertEqual(1, livetv._recent_streams[-1]["segments_skipped"])

    async def test_a_retried_request_is_counted(self):
        s1 = "http://provider.test/live/u/p/s1.ts"
        media = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\ns1.ts\n#EXT-X-ENDLIST\n"
        script = {HLS: [_resp(200, HLS, content=media, headers=PL)],
                  s1: [_resp(509, s1), _resp(200, s1, content=b"G" * 188, headers=TS)]}
        body = await _play(script, HLS)
        self.assertEqual(b"G" * 188, body)
        last = livetv._recent_streams[-1]
        self.assertEqual(1, last["errors"])
        self.assertEqual(0, last["segments_skipped"])

    async def test_an_hls_outage_that_recovers_is_an_interruption_not_zero(self):
        """A11: the HLS summary said "0 reconnect(s)" after minutes of waiting."""
        s1, s2 = "http://provider.test/live/u/p/s1.ts", "http://provider.test/live/u/p/s2.ts"
        media = (b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\ns1.ts\n#EXTINF:4,\ns2.ts\n"
                 b"#EXT-X-ENDLIST\n")
        script = {HLS: [_resp(200, HLS, content=media, headers=PL)],
                  s1: [_resp(509, s1), _resp(509, s1), _resp(200, s1, content=b"G" * 188, headers=TS)],
                  s2: [_resp(200, s2, content=b"H" * 188, headers=TS)]}
        with self.assertLogs("routers.livetv", "WARNING") as logs:
            await _play(script, HLS, recording=True)
        last = livetv._recent_streams[-1]
        self.assertEqual((1, 2), (last["reconnects"], last["errors"]))
        self.assertTrue(any("1 interruption(s) recovered" in m for m in logs.output), logs.output)


class StatusShape(unittest.IsolatedAsyncioTestCase):
    async def test_a_running_stream_shows_its_health(self):
        entry = livetv._status_open(99)
        try:
            entry["health"]["reconnects"] = 2

            class _Q:
                def filter(self, *a):
                    return self

                def first(self):
                    return None

            class _Db:
                def query(self, *a):
                    return _Q()
            snap = [s for s in livetv._stream_snapshot(_Db()) if s["channel_id"] == 99][0]
            self.assertEqual(2, snap["health"]["reconnects"])
        finally:
            livetv._status_clear(99, entry)


if __name__ == "__main__":
    unittest.main()

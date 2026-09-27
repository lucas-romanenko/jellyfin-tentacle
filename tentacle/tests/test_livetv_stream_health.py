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
from unittest.mock import MagicMock, patch

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
        with self.assertLogs("routers.livetv", "INFO") as logs:
            await _play(script, HLS, recording=True)
        last = livetv._recent_streams[-1]
        self.assertEqual((1, 2), (last["reconnects"], last["errors"]))
        self.assertTrue(any("1 interruption(s) recovered" in m for m in logs.output), logs.output)


class HlsDamagedThreshold(unittest.IsolatedAsyncioTestCase):
    """e3875e9 review: one HLS segment retried in place (well under a second
    of waiting, nothing lost) must not mark a recording as damaged."""

    def setUp(self):
        livetv._recent_streams.clear()

    async def test_a_quick_in_place_retry_is_not_damage(self):
        s1 = "http://provider.test/live/u/p/s1.ts"
        media = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\ns1.ts\n#EXT-X-ENDLIST\n"
        script = {HLS: [_resp(200, HLS, content=media, headers=PL)],
                  s1: [_resp(509, s1), _resp(200, s1, content=b"G" * 188, headers=TS)]}
        writes = []
        with patch.object(livetv, "log_activity", lambda db, ev, msg, detail=None: writes.append(ev)):
            with self.assertLogs("routers.livetv", "INFO") as logs:
                await _play(script, HLS, recording=True)
            await asyncio.sleep(0.05)
        self.assertEqual(1, livetv._recent_streams[-1]["reconnects"])
        self.assertFalse(any("recording ran" in m and "WARNING" in m for m in logs.output), logs.output)
        self.assertNotIn("livetv_recording_damaged", writes)

    def test_raw_ts_reconnects_still_count_as_damage(self):
        async def run():
            h = livetv._new_health()
            h["reconnects"] = 1
            with self.assertLogs("routers.livetv", "WARNING"):
                livetv._stream_ended(5, {"opened_at": asyncio.get_running_loop().time(), "health": h}, False)
        asyncio.run(run())


class EndedOnError(unittest.IsolatedAsyncioTestCase):
    """Impact research, defects A and B: a stream that ENDS while still
    failing was summarised as "retried in time, nothing lost" / "no upstream
    trouble" -- the outage still open at the end was never added, and a
    fatal answer was never counted."""

    def setUp(self):
        livetv._recent_streams.clear()

    async def _run(self, script, recording, seconds=0.3):
        log, closed, writes = [], [], []
        real_sleep = asyncio.sleep

        async def fast_sleep(delay):
            await real_sleep(0.001)
        with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, closed, **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("asyncio.sleep", fast_sleep), \
                patch.object(livetv, "log_activity", lambda db, ev, msg, detail=None: writes.append((ev, msg))), \
                patch.object(livetv, "SessionLocal", MagicMock()):
            with self.assertLogs("routers.livetv", "INFO") as logs:
                resp = await livetv._stream_proxy_inner(
                    channel_id=7, user_agent="UA", stream_url=HLS, _release_sem=lambda: None,
                    guard=None, is_recording=lambda: recording)

                async def collect():
                    async for _ in resp.body_iterator:
                        pass
                try:
                    await asyncio.wait_for(collect(), timeout=seconds)
                except asyncio.TimeoutError:
                    pass          # Jellyfin's timer ends: the client goes away
                await resp.body_iterator.aclose()
                for _ in range(20):          # the Activity line is written from the executor
                    if writes:
                        break
                    await real_sleep(0.02)
        return livetv._recent_streams[-1], logs.output, writes

    async def test_a_recording_refused_until_its_timer_ends_is_reported(self):
        s1 = "http://provider.test/live/u/p/s1.ts"
        media = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\ns1.ts\n"   # live: no ENDLIST
        script = {HLS: [_resp(200, HLS, content=media, headers=PL), _resp(509, HLS)],
                  s1: [_resp(200, s1, content=b"G" * 188, headers=TS)]}
        # a real second or more of refusals (under one is not an outage)
        last, logs, writes = await self._run(script, recording=True, seconds=1.3)
        self.assertTrue(last["ended_on_error"], last)
        self.assertGreater(last["reconnecting_seconds"], 0.0, last)
        self.assertTrue(any("WARNING" in m and "still failing" in m for m in logs), logs)
        self.assertTrue(any(ev == "livetv_recording_damaged" and "still failing" in msg
                            for ev, msg in writes), writes)

    async def test_a_stream_ended_by_a_fatal_answer_is_reported(self):
        s1 = "http://provider.test/live/u/p/s1.ts"
        media = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\ns1.ts\n"
        script = {HLS: [_resp(200, HLS, content=media, headers=PL), _resp(400, HLS)],
                  s1: [_resp(200, s1, content=b"G" * 188, headers=TS)]}
        last, logs, _ = await self._run(script, recording=False, seconds=2)
        self.assertTrue(last["ended_on_error"], last)
        self.assertFalse(any("no upstream trouble" in m for m in logs), logs)

    async def test_a_token_that_stays_refused_is_reported(self):
        s1 = "http://provider.test/live/u/p/s1.ts"
        media = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\ns1.ts\n"
        script = {HLS: [_resp(200, HLS, content=media, headers=PL), _resp(407, HLS)],
                  s1: [_resp(200, s1, content=b"G" * 188, headers=TS)]}
        last, logs, _ = await self._run(script, recording=False, seconds=2)
        self.assertTrue(last["ended_on_error"], last)
        self.assertFalse(any("retried in time, nothing lost" in m for m in logs), logs)

    async def test_a_recovered_outage_is_still_not_an_error(self):
        s1, s2 = "http://provider.test/live/u/p/s1.ts", "http://provider.test/live/u/p/s2.ts"
        media = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\ns1.ts\n#EXTINF:4,\ns2.ts\n#EXT-X-ENDLIST\n"
        script = {HLS: [_resp(200, HLS, content=media, headers=PL)],
                  s1: [_resp(509, s1), _resp(200, s1, content=b"G" * 188, headers=TS)],
                  s2: [_resp(200, s2, content=b"H" * 188, headers=TS)]}
        last, _, _ = await self._run(script, recording=True, seconds=2)
        self.assertFalse(last["ended_on_error"], last)

    def test_the_api_never_shows_the_internal_marker(self):
        async def run():
            entry = livetv._status_open(98)
            entry["health"]["_failing_since"] = 1.0

            class _Q:
                def filter(self, *a):
                    return self

                def first(self):
                    return None

            class _Db:
                def query(self, *a):
                    return _Q()
            try:
                snap = [x for x in livetv._stream_snapshot(_Db()) if x["channel_id"] == 98][0]
                self.assertNotIn("_failing_since", snap["health"])
            finally:
                livetv._status_clear(98, entry)
        asyncio.run(run())


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


class OutageFlag(unittest.TestCase):
    """The "ended while the provider was still failing (0s)" line read as a
    zero-length outage; under a second the figure is left out."""

    def _warning(self, seconds):
        h = livetv._new_health()
        h["reconnecting_seconds"] = seconds
        h["ended_on_error"] = True
        with self.assertLogs("routers.livetv", "WARNING") as logs:
            livetv._stream_ended(7, {"health": h, "opened_at": 0.0}, recording=False)
        return logs.output[-1]

    def test_under_a_second_has_no_figure(self):
        msg = self._warning(0.3)
        self.assertIn("ended while the provider was still failing — ", msg)
        self.assertNotIn("(0s)", msg)

    def test_a_second_or_more_keeps_it(self):
        self.assertIn("still failing (4s) — ", self._warning(4.2))


class OpenOutageUnderASecond(unittest.TestCase):
    """Round-4 low: a stream that ends a fraction of a second into a failure
    (one refused request as the client leaves) did not end on an outage."""

    def _summary(self, ago):
        async def run():
            loop = asyncio.get_running_loop()
            h = livetv._new_health()
            h["_failing_since"] = loop.time() - ago
            livetv._stream_ended(7, {"health": h, "opened_at": loop.time() - 30}, recording=True)
            return livetv._recent_streams[-1]
        with patch.object(livetv, "log_activity", lambda *a, **k: None), \
                patch.object(livetv, "SessionLocal", MagicMock()):
            return asyncio.run(run())

    def test_under_a_second_is_not_ended_on_error(self):
        self.assertFalse(self._summary(0.2)["ended_on_error"])

    def test_a_second_or_more_is(self):
        self.assertTrue(self._summary(3.0)["ended_on_error"])


if __name__ == "__main__":
    unittest.main()

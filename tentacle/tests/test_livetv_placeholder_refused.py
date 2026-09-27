"""A provider's placeholder segment is not the channel (#140).

Run from the tentacle/ directory:  python -m unittest discover -s tests

With nothing to serve on a channel, a provider answers 200 and a playlist
holding one `black.ts` (tuliprox answers /cvs/.../channel_unavailable.ts).
Tentacle proxied it as the channel, so a scheduled 3h25m recording "succeeded"
with ten minutes of black that Jellyfin never retried, or died on its own
buffer with a FileNotFoundException that said nothing about the cause.

A placeholder is recognised by name and never fetched. While the channel is
opening it is refused with a 503 before any byte goes out, so Jellyfin fails
the timer and tries again a minute later. A running stream waits it out like a
509. Every refusal is counted in /api/live/streams and logged.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

import routers.livetv as livetv
from test_livetv_hls_transient_errors import BASE, PLAYLIST_CT, _drive, _playlist, _resp
from test_livetv_open_single_fetch import PANEL, TOKENIZED, _open, _redirect
from test_livetv_raw_reconnect import _dropped, _live, _play

BLACK = "http://provider.test/video/black.ts"
MASTER_URL = "http://provider.test/live/u/p/220.m3u8"
VARIANT = "http://provider.test/live/u/p/220/index.m3u8"
MASTER = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4000000\n"
          "http://provider.test/live/u/p/220/index.m3u8\n")


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        livetv._placeholders.clear()
        livetv._placeholder_activity_at.clear()
        self.activity = []
        patcher = patch.object(livetv, "_record_activity_for_placeholder",
                               lambda cid, seg: self.activity.append((cid, seg)))
        patcher.start()
        self.addCleanup(patcher.stop)


class PlaceholderNames(unittest.TestCase):
    def test_known_placeholders(self):
        for url in (BLACK, "http://p/video/black.ts?token=1", "http://p/cvs/abc/channel_unavailable.ts",
                    "http://p/x/USER_CONNECTIONS_EXHAUSTED.ts"):
            self.assertTrue(livetv._placeholder_name(url), url)

    def test_real_segments_are_not(self):
        for url in ("http://p/live/u/p/277127.ts", "http://p/hls/blackadder.ts", "http://p/black.m3u8",
                    "http://p/live/black/1.ts"):
            self.assertIsNone(livetv._placeholder_name(url), url)


class RefusedAtOpen(_Base):
    async def _refused(self, script, stream_url):
        with patch("routers.livetv.is_safe_url", lambda *a, **k: True):
            from test_livetv_open_single_fetch import FakeClient
            log, closed = [], []
            with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, closed, **kw)):
                with self.assertRaises(HTTPException) as cm:
                    await livetv._stream_proxy_inner(channel_id=220, user_agent="t", stream_url=stream_url,
                                                     _release_sem=lambda: None)
        self.assertEqual(503, cm.exception.status_code)
        self.assertIn("placeholder", cm.exception.detail)
        return log

    async def test_a_variant_holding_only_black_ts_is_refused_before_any_byte(self):
        """The exact shape seen live: master -> variant -> black.ts + ENDLIST."""
        log = await self._refused({
            MASTER_URL: [_resp(200, MASTER_URL, MASTER.encode(), PLAYLIST_CT)],
            VARIANT: [_resp(200, VARIANT, _playlist("/video/black.ts", end=True).encode(), PLAYLIST_CT)],
            BLACK: [_resp(200, BLACK, b"G" * 188)],
        }, MASTER_URL)
        self.assertNotIn(BLACK, log, "the placeholder itself must never be fetched")
        self.assertEqual(1, livetv._placeholders[220]["count"])
        self.assertEqual("black.ts", livetv._placeholders[220]["segment"])
        self.assertEqual([(220, "black.ts")], self.activity)

    async def test_a_media_playlist_holding_only_a_placeholder_is_refused(self):
        await self._refused({
            BASE: [_resp(200, BASE, _playlist("/cvs/x/channel_unavailable.ts").encode(), PLAYLIST_CT)],
        }, BASE)

    async def test_a_redirect_to_a_placeholder_is_refused(self):
        await self._refused({
            PANEL: [_resp(302, PANEL, headers={"location": BLACK})],
            BLACK: [_resp(200, BLACK, b"G" * 188, {"content-type": "video/mp2t"})],
        }, PANEL)

    async def test_activity_hears_about_a_channel_at_most_hourly(self):
        script = {BASE: [_resp(200, BASE, _playlist("/video/black.ts").encode(), PLAYLIST_CT)]}
        for _ in range(3):
            await self._refused(script, BASE)
        self.assertEqual(3, livetv._placeholders[220]["count"])
        self.assertEqual(1, len(self.activity))

    async def test_a_normal_channel_still_opens(self):
        response, body, *_ = await _open({
            PANEL: [_redirect()],
            TOKENIZED: [_resp(200, TOKENIZED, b"G" * 376, {"content-type": "video/mp2t"})],
        }, read_body=False)
        self.assertEqual(200, response.status_code)
        self.assertEqual({}, livetv._placeholders)


class RunningStreams(_Base):
    async def test_an_hls_stream_waits_out_a_spell_of_placeholder(self):
        first = _playlist("c1.ts")
        black = _playlist("c1.ts", "/video/black.ts")
        recovered = _playlist("c1.ts", "/video/black.ts", "c2.ts")
        done = _playlist("c1.ts", "c2.ts", end=True)
        seg = "http://provider.test/live/u/p/"
        script = {
            BASE: [_resp(200, BASE, first.encode(), PLAYLIST_CT)]
            + [_resp(200, BASE, black.encode(), PLAYLIST_CT) for _ in range(4)]
            + [_resp(200, BASE, recovered.encode(), PLAYLIST_CT), _resp(200, BASE, done.encode(), PLAYLIST_CT)],
            seg + "c1.ts": [_resp(200, seg + "c1.ts", b"CHUNK1")],
            seg + "c2.ts": [_resp(200, seg + "c2.ts", b"CHUNK2")],
            BLACK: [_resp(200, BLACK, b"BLACK")],
        }
        body, log, _slept = await _drive(script)
        self.assertEqual(b"CHUNK1CHUNK2", body)
        self.assertNotIn(BLACK, log)
        self.assertEqual(1, livetv._placeholders[1]["count"], "one spell, one count")

    async def test_a_viewer_stuck_on_a_placeholder_gives_up_inside_the_budget(self):
        """Each playlist refresh "succeeds", and that used to reset the failure
        clock, so a stream parked on black.ts was re-read for ever."""
        seg = "http://provider.test/live/u/p/"
        stuck = _playlist("c1.ts", "/video/black.ts")
        script = {
            BASE: [_resp(200, BASE, _playlist("c1.ts").encode(), PLAYLIST_CT),
                   _resp(200, BASE, stuck.encode(), PLAYLIST_CT)],   # for ever after
            seg + "c1.ts": [_resp(200, seg + "c1.ts", b"CHUNK1")],
            BLACK: [_resp(200, BLACK, b"BLACK")],
        }
        from test_livetv_hls_transient_errors import FakeClient as HlsClient
        loop = asyncio.get_running_loop()
        clock = [loop.time()]
        real_sleep = asyncio.sleep
        slept = []

        async def fast_sleep(delay):
            slept.append(delay)
            clock[0] += delay
            await real_sleep(0)

        log = []
        with patch("httpx.AsyncClient", lambda **kw: HlsClient(script, log, **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("asyncio.sleep", fast_sleep), \
                patch.object(loop, "time", lambda: clock[0]):
            response = await livetv._stream_proxy_inner(
                channel_id=1, user_agent="t", stream_url=BASE, _release_sem=lambda: None,
                failure_budget=30, is_recording=lambda: False)

            async def collect():
                out = b""
                async for piece in response.body_iterator:
                    out += piece
                return out
            # wait_for's deadline runs on the same (fake) clock: an hour of
            # fake time is the "it never gave up" guard.
            body = await asyncio.wait_for(collect(), timeout=3600)
        self.assertEqual(b"CHUNK1", body)
        self.assertNotIn(BLACK, log)
        self.assertLessEqual(sum(slept), 60, "kept re-reading a placeholder far past the budget")

    async def test_an_hls_stream_that_ends_on_a_placeholder_stops(self):
        seg = "http://provider.test/live/u/p/"
        script = {
            BASE: [_resp(200, BASE, _playlist("c1.ts").encode(), PLAYLIST_CT),
                   _resp(200, BASE, _playlist("/video/black.ts", end=True).encode(), PLAYLIST_CT)],
            seg + "c1.ts": [_resp(200, seg + "c1.ts", b"CHUNK1")],
            BLACK: [_resp(200, BLACK, b"BLACK")],
        }
        body, log, _slept = await _drive(script)
        self.assertEqual(b"CHUNK1", body)
        self.assertNotIn(BLACK, log)

    async def _record(self, script, recording=True):
        """Drive the HLS worker as a recording (or a viewer) on a fake clock."""
        from test_livetv_hls_transient_errors import FakeClient as HlsClient
        loop = asyncio.get_running_loop()
        clock = [loop.time()]
        real_sleep = asyncio.sleep

        async def fast_sleep(delay):
            clock[0] += delay
            await real_sleep(0)

        log = []
        with patch("httpx.AsyncClient", lambda **kw: HlsClient(script, log, **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("asyncio.sleep", fast_sleep), \
                patch.object(loop, "time", lambda: clock[0]):
            response = await livetv._stream_proxy_inner(
                channel_id=1, user_agent="t", stream_url=BASE, _release_sem=lambda: None,
                failure_budget=30, is_recording=lambda: recording)

            async def collect():
                out = b""
                async for piece in response.body_iterator:
                    out += piece
                return out
            body = await asyncio.wait_for(collect(), timeout=3600)
        return body, log

    async def test_a_recording_waits_out_a_placeholder_that_ends_the_playlist(self):
        """Rob, #140: a recording whose playlist became black.ts + ENDLIST stopped,
        and Jellyfin filed the rest of the event as recorded. It waits, asks the
        channel URL again, and carries on when the channel is back."""
        seg = "http://provider.test/live/u/p/"
        ended = _playlist("/video/black.ts", end=True)
        script = {
            BASE: [_resp(200, BASE, _playlist("c1.ts").encode(), PLAYLIST_CT)]
            + [_resp(200, BASE, ended.encode(), PLAYLIST_CT) for _ in range(4)]
            + [_resp(200, BASE, _playlist("c2.ts", end=True).encode(), PLAYLIST_CT)],
            seg + "c1.ts": [_resp(200, seg + "c1.ts", b"CHUNK1")],
            seg + "c2.ts": [_resp(200, seg + "c2.ts", b"CHUNK2")],
            BLACK: [_resp(200, BLACK, b"BLACK")],
        }
        body, log = await self._record(script)
        self.assertEqual(b"CHUNK1CHUNK2", body)
        self.assertNotIn(BLACK, log)

    async def test_a_viewer_still_stops_on_a_placeholder_that_ends_the_playlist(self):
        seg = "http://provider.test/live/u/p/"
        script = {
            BASE: [_resp(200, BASE, _playlist("c1.ts").encode(), PLAYLIST_CT),
                   _resp(200, BASE, _playlist("/video/black.ts", end=True).encode(), PLAYLIST_CT),
                   _resp(200, BASE, _playlist("c2.ts", end=True).encode(), PLAYLIST_CT)],
            seg + "c1.ts": [_resp(200, seg + "c1.ts", b"CHUNK1")],
            seg + "c2.ts": [_resp(200, seg + "c2.ts", b"CHUNK2")],
        }
        body, _log = await self._record(script, recording=False)
        self.assertEqual(b"CHUNK1", body)

    async def test_a_segment_that_redirects_to_a_placeholder_is_never_written(self):
        """Rob, #140: an ordinary segment URL answering with a redirect to
        black.ts was fetched and written into the stream."""
        seg = "http://provider.test/live/u/p/"
        script = {
            BASE: [_resp(200, BASE, _playlist("c1.ts", "c2.ts").encode(), PLAYLIST_CT),
                   _resp(200, BASE, _playlist("c1.ts", "c2.ts", "c3.ts", end=True).encode(), PLAYLIST_CT)],
            seg + "c1.ts": [_resp(200, seg + "c1.ts", b"CHUNK1")],
            seg + "c2.ts": [_resp(302, seg + "c2.ts", headers={"location": BLACK}),
                            _resp(200, seg + "c2.ts", b"CHUNK2")],
            seg + "c3.ts": [_resp(200, seg + "c3.ts", b"CHUNK3")],
            BLACK: [_resp(200, BLACK, b"BLACK")],
        }
        body, _log = await self._record(script)
        self.assertNotIn(b"BLACK", body)
        self.assertEqual(b"CHUNK1CHUNK2CHUNK3", body)
        self.assertEqual(1, livetv._placeholders[1]["count"])

    async def test_a_raw_redial_that_lands_on_a_placeholder_is_retried(self):
        script = {
            PANEL: [_redirect(), _resp(302, PANEL, headers={"location": BLACK}), _redirect(),
                    _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"], then=_dropped()), _live([b"BBBB"])],
            BLACK: [_live([b"BLACK"], url=BLACK)],
        }
        body, log, *_ = await _play(script)
        self.assertEqual(b"AAAABBBB", body, "the placeholder's bytes reached the recording")
        self.assertEqual(1, livetv._placeholders[1]["count"])


class Reporting(unittest.TestCase):
    def test_live_streams_lists_placeholder_refusals(self):
        import shutil
        import tempfile
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models.database as mdb
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        self.addCleanup(db.close)
        livetv._placeholders.clear()
        livetv._placeholders[220] = {"count": 2, "segment": "black.ts", "at": "2026-09-23T11:30:00Z"}
        self.addCleanup(livetv._placeholders.clear)
        out = livetv.live_streams(db=db)
        self.assertEqual([{"channel_id": 220, "count": 2, "segment": "black.ts",
                           "at": "2026-09-23T11:30:00Z"}], out["placeholders"])


if __name__ == "__main__":
    unittest.main()

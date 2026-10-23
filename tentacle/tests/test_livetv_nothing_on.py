""""Nothing on right now" instead of a long wait and an error.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Seen on 21 Oct 2026: an ESPN+ event channel whose event starts in two days
took the provider 21 s to answer, then it hung up, then it served its
black.ts placeholder; the viewer waited about a minute and got "playback
error". A Sportsnet PPV channel with no fight on answered black.ts at once,
and the 503 made the TV app go back to Search without a word.

Now a viewer gets, at once, a short stream that says "Nothing on right now"
(services/livetv_slate.py): for an event channel opened well before its
event without asking the provider at all, and for a placeholder at open or
on a re-dial before anything was delivered. Recordings are refused as
before (Jellyfin retries them; a recording never saves the slate).
"""
import asyncio
import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routers.livetv as livetv
from services import livetv_slate as S
from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp
from test_livetv_raw_reconnect import TS, _Body
from tmp_dirs import temp_dir

TORONTO = timezone(timedelta(hours=-4))
NOW = datetime(2026, 10, 21, 23, 52, tzinfo=timezone.utc)      # Wed 19:52 in Toronto
SLATE = b"G" + b"\x00" * 187
BLACK = "http://provider.test/video/black.ts"


class Lines(unittest.TestCase):
    def ev(self, start, timed=True):
        return {"title": "ESPN+: Houston vs. #24 Baylor", "start": start, "stop": start + timedelta(hours=3),
                "timed": timed, "description": None}

    def test_an_event_days_away_is_not_asked_for(self):
        self.assertEqual(("ESPN+: Houston vs. #24 Baylor", "starts Friday at 8:00 PM"),
                         S.early(self.ev(datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)), NOW, TORONTO))

    def test_when_reads_naturally(self):
        at = lambda **kw: S.early(self.ev(NOW + timedelta(**kw)), NOW, TORONTO)[1]
        self.assertEqual("starts today at 9:22 PM", at(hours=1, minutes=30))
        self.assertEqual("starts tomorrow at 7:52 PM", at(days=1))
        self.assertEqual("starts Nov 4 at 7:52 PM", at(days=14))

    def test_an_event_about_to_start_or_on_is_asked_for(self):
        self.assertIsNone(S.early(self.ev(NOW + timedelta(minutes=20)), NOW, TORONTO))
        self.assertIsNone(S.early(self.ev(NOW - timedelta(hours=1)), NOW, TORONTO))
        self.assertIsNone(S.early(self.ev(NOW + timedelta(days=2), timed=False), NOW, TORONTO))

    def test_ordinary_channels_and_no_event_slots(self):
        self.assertIsNone(S.early(None, NOW, TORONTO))
        self.assertEqual("No event on this channel at the moment", S.early("no_event", NOW, TORONTO)[0])

    def test_placeholder_lines(self):
        self.assertEqual(("Sportsnet PPV", "has no live event at the moment"), S.unavailable(None, "Sportsnet PPV"))
        self.assertEqual("ESPN+: Houston vs. #24 Baylor", S.unavailable(self.ev(NOW), "x")[0])


class Paced(unittest.IsolatedAsyncioTestCase):
    async def test_every_byte_goes_out_the_first_seconds_at_once(self):
        data = bytes(range(256)) * 3000
        slept = []
        real_sleep = asyncio.sleep

        async def fake_sleep(d):
            slept.append(d)
            await real_sleep(0)

        out = b""
        with patch("asyncio.sleep", fake_sleep):
            async for piece in S.paced(data):
                if not slept:
                    self.assertLessEqual(len(out), len(data) * S._BURST_SECONDS // S.SECONDS + 188 * 64)
                out += piece
        self.assertEqual(data, out)
        self.assertAlmostEqual(S.SECONDS - S._BURST_SECONDS, sum(slept), delta=1)


@unittest.skipUnless(shutil.which("ffmpeg") and S._font(), "needs ffmpeg and the DejaVu font (both in the image)")
class Picture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        S._cache.clear()
        self.addCleanup(S._cache.clear)

    async def test_a_playable_ts_made_once_per_text(self):
        with patch.object(S, "SECONDS", 2):
            data = await S.picture(("ESPN+: Houston vs. #24 Baylor", "starts Friday at 8:00 PM"))
            self.assertTrue(data and data[:1] == b"G" and len(data) % 188 == 0)
            self.assertIs(data, await S.picture(("ESPN+: Houston vs. #24 Baylor", "starts Friday at 8:00 PM")))


class _Db(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add(mdb.Provider(id=1, name="p", server_url="http://provider.test", username="u", password="p"))
        start = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.db.add(mdb.LiveChannel(id=7, provider_id=1, enabled=True, stream_url=PANEL, group_title="US| ESPN+ PPV",
                                    name=f"US (ESPN+ 447) | ESPN+: Houston vs. #24 Baylor ({start})"))
        self.db.add(mdb.LiveChannel(id=8, provider_id=1, enabled=True, stream_url=PANEL, name="Sportsnet PPV"))
        self.db.commit()
        from services import livetv_catalog
        livetv_catalog._event_cache.update(at=0.0, value={})
        self.slots = livetv._StreamSlots()
        livetv._placeholders.clear()
        self.addCleanup(livetv._placeholders.clear)
        self.recording = set()

        async def recording_ids(db, **kw):
            return set(self.recording)
        for p in (patch.object(livetv, "_stream_slots", self.slots),
                  patch.object(livetv, "_recording_channel_ids", recording_ids),
                  patch.object(livetv, "_record_activity_for_placeholder", lambda *a: None),
                  patch.object(S, "picture", self.fake_picture),
                  patch("routers.livetv.is_safe_url", lambda *a, **k: True),
                  patch.object(livetv, "explain_url", lambda guard, url: (True, None))):
            p.start()
            self.addCleanup(p.stop)
        self.pictures = []

    async def fake_picture(self, lines):
        self.pictures.append(lines)
        return SLATE * 3

    async def body(self, response):
        out = b""
        async for piece in response.body_iterator:
            out += piece
        return out


class EventLater(_Db):
    async def test_a_viewer_is_told_at_once_without_a_provider_connection(self):
        def no_client(**kw):
            raise AssertionError("the provider must not be asked")
        with patch("httpx.AsyncClient", no_client):
            response = await livetv.stream_proxy(7, db=self.db)
            self.assertEqual("nothing on right now", response.headers["X-Tentacle-Reason"])
            self.assertEqual(SLATE * 3, await self.body(response))
        self.assertEqual("ESPN+: Houston vs. #24 Baylor", self.pictures[0][0])
        self.assertTrue(self.pictures[0][1].startswith("starts "))
        self.assertEqual(0, self.slots.active, "no connection slot is held for it")

    async def test_a_recording_still_goes_to_the_provider(self):
        self.recording = {7}

        async def inner(*a, **kw):
            raise livetv._TunerRefusal("asked the provider")
        with patch.object(livetv, "_stream_proxy_inner", inner):
            with self.assertRaises(HTTPException) as cm:
                await livetv.stream_proxy(7, db=self.db)
        self.assertEqual("asked the provider", cm.exception.detail)
        self.assertEqual([], self.pictures)


class PlaceholderAtOpen(_Db):
    async def placeholder(self, *a, **kw):
        await livetv._refuse_placeholder(8, "black.ts")

    async def test_a_viewer_is_told_and_the_slot_is_free(self):
        with patch.object(livetv, "_stream_proxy_inner", self.placeholder):
            response = await livetv.stream_proxy(8, db=self.db)
            self.assertEqual(SLATE * 3, await self.body(response))
        self.assertEqual([("Sportsnet PPV", "has no live event at the moment")], self.pictures)
        self.assertEqual(0, self.slots.active)
        self.assertEqual(1, livetv._placeholders[8]["count"])

    async def test_a_recording_is_refused_as_before(self):
        self.recording = {8}
        with patch.object(livetv, "_stream_proxy_inner", self.placeholder):
            with self.assertRaises(livetv._PlaceholderRefusal):
                await livetv.stream_proxy(8, db=self.db)
        self.assertEqual([], self.pictures)
        self.assertEqual(0, self.slots.active)

    async def test_without_ffmpeg_the_refusal_stands(self):
        async def none(lines):
            return None
        with patch.object(livetv, "_stream_proxy_inner", self.placeholder), patch.object(S, "picture", none):
            with self.assertRaises(livetv._PlaceholderRefusal):
                await livetv.stream_proxy(8, db=self.db)


class ProviderDoesNotDeliver(_Db):
    """Lucas, 22 Oct 2026: "if a stream cannot play from the provider we tell
    them that". A viewer whose channel the provider refuses (or sends nothing
    for) used to get a spinner and "playback error"; now the player says it's
    the provider, or that someone else holds the connection."""

    def setUp(self):
        super().setUp()
        import models.database as mdb
        self.db.add(mdb.LiveChannel(id=9, provider_id=1, enabled=True, stream_url=PANEL, name="PBS Kids"))
        self.db.commit()

    async def refused(self, *a, **kw):
        raise HTTPException(502, "Failed to connect to stream: the provider answered 403")

    async def test_a_viewer_is_told_it_is_the_provider(self):
        with patch.object(livetv, "_stream_proxy_inner", self.refused):
            response = await livetv.stream_proxy(9, db=self.db)
            self.assertEqual("provider not delivering", response.headers["X-Tentacle-Reason"])
            self.assertEqual(SLATE * 3, await self.body(response))
        self.assertEqual([S.not_coming_through()], self.pictures)
        self.assertEqual("Not coming through", self.pictures[0][0])
        self.assertEqual(0, self.slots.active, "the slot is free again")

    async def test_while_another_stream_of_the_account_runs_it_says_in_use(self):
        held = await self.slots.acquire_lease(0, 0, "live", "channel:8", provider_id=1)
        self.addCleanup(self.slots.release_lease, held)
        with patch.object(livetv, "_stream_proxy_inner", self.refused):
            response = await livetv.stream_proxy(9, db=self.db)
            self.assertEqual("connection in use", response.headers["X-Tentacle-Reason"])
            await self.body(response)
        self.assertEqual("Connection in use", self.pictures[0][0])

    async def test_a_recording_is_refused_as_before(self):
        self.recording = {9}
        with patch.object(livetv, "_stream_proxy_inner", self.refused):
            with self.assertRaises(HTTPException) as cm:
                await livetv.stream_proxy(9, db=self.db)
        self.assertEqual(502, cm.exception.status_code)
        self.assertEqual([], self.pictures)

    async def test_other_failures_are_not_blamed_on_the_provider(self):
        async def blocked(*a, **kw):
            raise HTTPException(502, "Stream URL points to a non-public host")
        with patch.object(livetv, "_stream_proxy_inner", blocked):
            with self.assertRaises(HTTPException):
                await livetv.stream_proxy(9, db=self.db)
        self.assertEqual([], self.pictures)

    async def test_at_capacity_a_viewer_is_told_the_connections_are_in_use(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_max_concurrent_streams", "1")
        self.db.commit()
        held = await self.slots.acquire_lease(0, 0, "live", "channel:8", provider_id=1)
        self.addCleanup(self.slots.release_lease, held)
        with patch.object(livetv, "_SLOT_WAIT_SECONDS", 0.01):
            response = await livetv.stream_proxy(9, db=self.db)
            self.assertEqual("connection in use", response.headers["X-Tentacle-Reason"])
            await self.body(response)
        self.assertEqual([S.connection_in_use(1)], self.pictures)
        self.assertEqual(1, self.slots.refused, "still counted as a refusal")


class ProviderLines(unittest.TestCase):
    def test_three_lines_short_enough_for_the_picture(self):
        for lines in (S.not_coming_through(), S.connection_in_use(1), S.connection_in_use(3)):
            self.assertEqual(3, len(lines))
            self.assertTrue(all(len(x) <= 50 for x in lines), lines)
        self.assertIn("Someone else", S.connection_in_use(1)[1])
        self.assertIn("All 3", S.connection_in_use(3)[1])


class _Enough(Exception):
    pass


class PlaceholderOnRedial(unittest.IsolatedAsyncioTestCase):
    """The ESPN+ shape: 200 and the provider hangs up at once, then black.ts."""

    async def play(self, recording):
        script = {PANEL: [_redirect(), _resp(302, PANEL, headers={"location": BLACK})],
                  TOKENIZED: [httpx.Response(200, headers=TS, stream=_Body([]), request=httpx.Request("GET", TOKENIZED))]}
        real_sleep = asyncio.sleep
        sleeps = []

        async def fast_sleep(d):
            sleeps.append(d)
            if len(sleeps) > 8:         # a recording waits a placeholder out for ever: enough
                raise _Enough()
            await real_sleep(0)

        async def picture():
            return SLATE * 2
        with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, [], [], **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch.object(livetv, "_record_activity_for_placeholder", lambda *a: None), \
                patch("asyncio.sleep", fast_sleep):
            response = await livetv._stream_proxy_inner(
                channel_id=9, user_agent="t", stream_url=PANEL, _release_sem=lambda: None,
                failure_budget=5, is_recording=lambda: recording, nothing_on=picture)
            out = b""

            async def collect():
                nonlocal out
                async for piece in response.body_iterator:
                    out += piece
            try:
                await asyncio.wait_for(collect(), timeout=30)
            except _Enough:
                pass
        return out

    async def test_a_viewer_gets_the_slate(self):
        self.assertEqual(SLATE * 2, await self.play(recording=False))

    async def test_a_recording_never_saves_it(self):
        self.assertEqual(b"", await self.play(recording=True))


if __name__ == "__main__":
    unittest.main()

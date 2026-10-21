"""Live TV background stream checker (services/livetv_checker.py).

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_checker

Pinned: the order (pool first, unchecked then oldest; other variants after),
standing aside while anyone streams, being pre-empted by a stream open (the
check in flight is cancelled before the viewer's slot is granted), storing
results (broken only after two failures in a row; a network = its chosen
variant), backing off a provider that says busy, and judging first bytes.
"""
import asyncio
import logging
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import livetv_checker as C
from tmp_dirs import temp_dir

TS = bytes([0x47] + [0] * 187) * 4


def _reset():
    C._pool_cache.update(at=-1e9, ids=[])
    C._backoff.clear()
    C._last_open = -1e9
    C._inflight = None
    C.status.update(state="stopped", paused_reason=None, current=None, last=None, checks=0,
                    preempted=0, backoff={}, next_at=None)


class Base(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db", connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                              live_tv_enabled=True)
        self.db.add(self.p)
        self.db.commit()
        self.ch = {}
        for i, name in enumerate(["A", "B", "C", "D", "E"], 1):
            c = mdb.LiveChannel(provider_id=self.p.id, name=name, stream_id=str(i),
                                stream_url=f"http://192.0.2.10/live/u/p/{i}.ts", enabled=True)
            self.db.add(c)
            self.db.commit()
            self.ch[name] = c.id
        _reset()
        self.addCleanup(_reset)
        # Pool: A, B as single channels, network 7 whose chosen variant is C.
        # D and E are other variants (enabled, not in the pool).
        self.pool = [{"key": f"c{self.ch['A']}"}, {"key": f"c{self.ch['B']}"}, {"key": "n7"}]
        patches = [
            mock.patch("services.livetv_lineups.pool", lambda db: list(self.pool)),
            mock.patch.object(C, "_chosen_variants", lambda db: {7: self.ch["C"]}),
            mock.patch.object(C, "SessionLocal", self.Session),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def set_checked(self, name, ago_hours, state="works", fails=0):
        c = self.db.get(mdb.LiveChannel, self.ch[name])
        c.check_at = datetime.utcnow() - timedelta(hours=ago_hours)
        c.check_state, c.check_fails = state, fails
        self.db.commit()


class Order(Base):
    def test_pool_first_unchecked_then_oldest_then_variants(self):
        self.set_checked("A", 30)
        self.set_checked("B", 50)
        # C (network's chosen variant) unchecked; D, E variants unchecked
        ids = C.candidates(self.db)
        n = self.ch
        self.assertEqual([n["C"], n["B"], n["A"], n["D"], n["E"]], ids)

    def test_fresh_results_are_not_due(self):
        self.set_checked("A", 2)
        self.set_checked("D", 30)        # a variant: checked less often (3 days)
        ids = C.candidates(self.db)
        self.assertNotIn(self.ch["A"], ids)
        self.assertNotIn(self.ch["D"], ids)
        self.set_checked("D", 80)
        self.assertIn(self.ch["D"], C.candidates(self.db))

    def test_a_first_failure_is_retried_soon(self):
        self.set_checked("A", 1, state=None, fails=1)
        self.assertIn(self.ch["A"], C.candidates(self.db))

    def test_failing_provider_or_live_tv_off_is_skipped(self):
        with mock.patch("services.provider_health.load_state", lambda db: {str(self.p.id): {"state": "failing"}}):
            self.assertEqual([], C.candidates(self.db))
        self.p.live_tv_enabled = False
        self.db.commit()
        self.assertEqual([], C.candidates(self.db))

    def test_pace_cycles_the_pool_in_a_day(self):
        self.assertEqual(C.MIN_GAP_SECONDS, C.gap_seconds(100000))
        self.assertAlmostEqual(86400 / 1000, C.gap_seconds(1000))


class Results(Base):
    def test_broken_only_after_two_failures(self):
        a = self.ch["A"]
        C.record(self.db, a, False, "HTTP 404")
        self.assertEqual({"state": "unchecked", "at": None}, C.state_of(C.results(self.db), f"c{a}"))
        C.record(self.db, a, False, "HTTP 404")
        r = C.results(self.db)[f"c{a}"]
        self.assertEqual("broken", r["state"])
        self.assertTrue(r["at"].endswith("Z"))
        C.record(self.db, a, True, "MPEG-TS")
        self.assertEqual("works", C.results(self.db)[f"c{a}"]["state"])
        self.assertEqual(0, self.db.get(mdb.LiveChannel, a).check_fails)

    def test_network_is_its_chosen_variant(self):
        C.record(self.db, self.ch["C"], True)
        res = C.results(self.db)
        self.assertEqual(res[f"c{self.ch['C']}"], res["n7"])
        self.assertEqual("works", res["n7"]["state"])
        self.assertEqual("unchecked", C.results(self.db, networks={8: self.ch["E"]})["n8"]["state"])


class FakeSlots:
    def __init__(self, active=0, recording=False):
        self.active, self._rec = active, recording

    def recording_active(self):
        return self._rec


class Loop(Base, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.setUp(self)
        from routers import livetv
        self.livetv = livetv
        p = mock.patch.object(livetv, "_stream_slots", FakeSlots())
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

    async def ok_check(self, cid):
        self.calls.append(cid)
        return "works", 200, "MPEG-TS", self.p.id

    async def test_checks_the_first_due_and_stores_it(self):
        wait = await C.step(self.ok_check)
        self.assertEqual([self.ch["A"]], self.calls)
        self.db.expire_all()
        self.assertEqual("works", self.db.get(mdb.LiveChannel, self.ch["A"]).check_state)
        self.assertEqual(C.gap_seconds(3), wait)

    async def test_stands_aside_while_someone_streams(self):
        self.livetv._stream_slots.active = 1
        self.assertEqual(C.BUSY_POLL_SECONDS, await C.step(self.ok_check))
        self.assertEqual([], self.calls)
        self.assertEqual("paused", C.status["state"])
        self.assertIn("watching", C.status["paused_reason"])

    async def test_stands_aside_while_jellyfin_plays(self):
        with mock.patch("services.provider_activity.jellyfin_playing", lambda db: True):
            await C.step(self.ok_check)
        self.assertEqual([], self.calls)
        self.assertIn("Jellyfin", C.status["paused_reason"])

    async def test_waits_after_a_stream_opened(self):
        await C.preempt("test")
        await C.step(self.ok_check)
        self.assertEqual([], self.calls)
        C._last_open = time.monotonic() - C.QUIET_AFTER_SECONDS - 1
        await C.step(self.ok_check)
        self.assertEqual(1, len(self.calls))

    async def test_a_stream_open_preempts_the_check(self):
        started, closed = asyncio.Event(), asyncio.Event()

        async def slow_check(cid):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                closed.set()        # the connection is closed before the viewer goes on
            return "works", 200, "", self.p.id

        step = asyncio.create_task(C.step(slow_check))
        await asyncio.wait_for(started.wait(), 2)
        self.assertEqual("checking", C.status["state"])
        # A real viewer open through the real slot broker.
        slots = self.livetv._StreamSlots()
        lease = await slots.acquire_lease(1, 0.01, "live", "channel:1", provider_id=self.p.id, provider_limit=1)
        self.assertIsNotNone(lease)
        self.assertTrue(closed.is_set())
        self.assertEqual(C.BUSY_POLL_SECONDS, await asyncio.wait_for(step, 2))
        self.assertEqual(1, C.status["preempted"])
        self.db.expire_all()
        self.assertIsNone(self.db.get(mdb.LiveChannel, self.ch["A"]).check_at)   # no verdict
        # and the checker now waits before trying again
        self.assertEqual(C.BUSY_POLL_SECONDS, await C.step(self.ok_check))
        self.assertEqual([], self.calls)

    async def test_a_check_is_capped_and_silence_is_a_failure_not_broken(self):
        async def hangs(client, url, ua, hops=3):
            await asyncio.sleep(30)

        self.assertLessEqual(C.CHECK_SECONDS, 5.0)
        for other in "BCDE":
            self.set_checked(other, 1)          # only A is due
        with mock.patch.object(C, "CHECK_SECONDS", 0.2), mock.patch.object(C, "probe", hangs):
            t = time.monotonic()
            verdict, code, detail, pid = await C.check_channel(self.ch["A"])
            self.assertLess(time.monotonic() - t, 2)
            self.assertEqual(("fail", self.p.id), (verdict, pid))
            for _ in range(2):
                C._last_open = -1e9
                await C.step()
                self.db.expire_all()
                a = self.db.get(mdb.LiveChannel, self.ch["A"])
                if a.check_fails == 1:
                    self.assertIsNone(a.check_state)        # once: not broken, retried later
                    self.assertNotIn(self.ch["A"], C.candidates(self.db))
                    self.set_checked("A", 1, state=None, fails=1)   # the retry is due
            self.assertEqual("broken", a.check_state)       # twice in a row: broken

    async def test_preempt_with_nothing_in_flight(self):
        self.assertFalse(await C.preempt())

    async def test_busy_provider_backs_off_and_doubles(self):
        async def busy(cid):
            return "busy", 509, "refused (509)", self.p.id

        await C.step(busy)
        self.assertEqual(C.BACKOFF_START, C._backoff[self.p.id]["seconds"])
        self.assertEqual([], C.candidates(self.db))
        self.db.expire_all()
        self.assertIsNone(self.db.get(mdb.LiveChannel, self.ch["A"]).check_state)   # no verdict
        self.assertEqual(C.MAX_IDLE_SECONDS, await C.step(self.ok_check))      # nothing to check
        self.assertEqual("backoff", C.status["state"])
        C._backoff[self.p.id]["until"] = time.monotonic() - 1
        await C.step(busy)
        self.assertEqual(2 * C.BACKOFF_START, C._backoff[self.p.id]["seconds"])
        C._backoff[self.p.id]["until"] = time.monotonic() - 1
        await C.step(self.ok_check)
        self.assertNotIn(self.p.id, C._backoff)


class Probe(unittest.IsolatedAsyncioTestCase):
    async def run_probe(self, handler, url="http://192.0.2.10/live/u/p/1.ts"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
            return await C.probe(c, url, "UA")

    async def test_mpegts_works(self):
        self.assertEqual("works", (await self.run_probe(lambda r: httpx.Response(200, content=TS)))[0])

    async def test_hls_follows_to_the_first_segment(self):
        def handler(req):
            if req.url.path == "/live/u/p/1.ts":
                return httpx.Response(200, text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhi/index.m3u8\n")
            if req.url.path.endswith("index.m3u8"):
                return httpx.Response(200, text="#EXTM3U\n#EXTINF:4,\nseg1.ts\n")
            if req.url.path.endswith("hi/seg1.ts"):
                return httpx.Response(200, content=TS)
            return httpx.Response(404)
        self.assertEqual("works", (await self.run_probe(handler))[0])

    async def test_error_page_and_404_fail_busy_is_busy(self):
        page = lambda r: httpx.Response(200, text="<html>expired</html>", headers={"content-type": "text/html"})
        self.assertEqual("fail", (await self.run_probe(page))[0])
        self.assertEqual("fail", (await self.run_probe(lambda r: httpx.Response(404)))[0])
        self.assertEqual("fail", (await self.run_probe(lambda r: httpx.Response(200)))[0])
        self.assertEqual(("busy", 509), (await self.run_probe(lambda r: httpx.Response(509)))[:2])

    async def test_unreachable_is_busy_not_broken(self):
        def down(req):
            raise httpx.ConnectError("refused")
        self.assertEqual("busy", (await self.run_probe(down))[0])


if __name__ == "__main__":
    unittest.main()

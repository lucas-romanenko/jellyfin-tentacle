"""A running upstream says whether it is streaming or waiting out a provider
failure, and GET /api/live/streams reports it.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A stream that is re-dialling after a 509 writes nothing to its subscribers
for as long as the provider keeps refusing. From the outside -- a DVR front
end watching the recording's file size -- that looks exactly like a dead
stream, and cancelling the timer to "recover" it is what turns one recording
into two files. With the reconnect budget raised (#136) that silence can
last minutes, so the front end needs to be able to ask.
"""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp
from test_livetv_raw_reconnect import _dropped, _live


class StatusEntriesBelongToTheStreamThatMadeThem(unittest.IsolatedAsyncioTestCase):
    async def test_a_late_finally_does_not_erase_the_reopened_streams_entry(self):
        from routers import livetv
        livetv._stream_status.clear()
        old = livetv._status_open(7)
        new = livetv._status_open(7)          # closed and reopened within the second
        livetv._status_clear(7, old)          # the old generator's finally, running late
        self.assertIs(new, livetv._stream_status.get(7), "the new stream stays listed")
        livetv._status_clear(7, new)
        self.assertNotIn(7, livetv._stream_status)

    async def test_the_timer_refresher_does_not_run_for_vod_alone(self):
        from routers import livetv
        livetv._stream_slots = livetv._StreamSlots()
        await livetv._stream_slots.acquire_lease(6, 0.01, "vod", "vod:movie:1:1")
        self.assertFalse(livetv._live_leases_exist())
        await livetv._stream_slots.acquire_lease(6, 0.01, "live", "channel:1")
        self.assertTrue(livetv._live_leases_exist())
        livetv._stream_slots = livetv._StreamSlots()


class StatusTransitions(unittest.IsolatedAsyncioTestCase):
    async def test_states_and_since_move_together(self):
        import routers.livetv as livetv
        livetv._stream_status.clear()
        livetv._status_set(7, "streaming")
        first = livetv._stream_status[7]["since"]
        livetv._status_set(7, "streaming")          # same state: since unchanged
        self.assertEqual(first, livetv._stream_status[7]["since"])
        await asyncio.sleep(0.01)
        livetv._status_set(7, "reconnecting", "509")
        st = livetv._stream_status[7]
        self.assertEqual("reconnecting", st["state"])
        self.assertGreater(st["since"], first)
        self.assertEqual("509", st["last_error"])
        self.assertEqual(first, st["opened_at"], "opened_at is when the upstream was first opened")
        livetv._status_clear(7)
        self.assertNotIn(7, livetv._stream_status)


class RawStreamReportsItself(unittest.IsolatedAsyncioTestCase):
    async def test_reconnecting_while_the_provider_refuses_then_streaming_again(self):
        import routers.livetv as livetv
        livetv._stream_status.clear()
        script = {
            PANEL: [_redirect(), _resp(509, PANEL), _resp(509, PANEL), _redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"], then=_dropped()), _live([b"BBBB"])],
        }
        seen = []
        log, closed = [], []
        real_sleep = asyncio.sleep

        async def sleep_and_peek(delay):
            seen.append(dict(livetv._stream_status.get(1) or {}))
            await real_sleep(0)

        with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, closed, **kw)), \
                patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
                patch("asyncio.sleep", sleep_and_peek):
            response = await livetv._stream_proxy_inner(
                channel_id=1, user_agent="TestAgent/1.0", stream_url=PANEL,
                _release_sem=lambda: None, guard=None)
            states_while_streaming = []
            pieces = []
            async for piece in response.body_iterator:
                pieces.append(piece)
                states_while_streaming.append((livetv._stream_status.get(1) or {}).get("state"))
        self.assertEqual(b"AAAABBBB", b"".join(pieces))
        self.assertTrue(any(s.get("state") == "reconnecting" for s in seen),
                        f"never reported reconnecting during the refusals: {seen}")
        self.assertTrue(all(s.get("last_error") for s in seen if s.get("state") == "reconnecting"))
        self.assertIn("streaming", states_while_streaming)
        self.assertNotIn(1, livetv._stream_status, "status must be cleared when the stream ends")


class RouteReportsStreams(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def test_snapshot_lists_state_duration_and_name(self):
        import routers.livetv as livetv
        from models.database import LiveChannel
        livetv._stream_status.clear()
        self.db.add(LiveChannel(id=138, provider_id=1, name="CA: SPORTSNET ONE", stream_id="1238991",
                                stream_url="http://p/live/u/p/1238991.m3u8"))
        self.db.commit()

        async def go():
            livetv._status_set(138, "streaming")
            livetv._status_set(138, "reconnecting", "509 Bandwidth Limit Exceeded")
            livetv._status_set(9, "streaming")
        asyncio.run(go())
        out = livetv.live_streams(self.db)
        by_id = {s["channel_id"]: s for s in out["streams"]}
        self.assertEqual({9, 138}, set(by_id))
        self.assertEqual("reconnecting", by_id[138]["state"])
        self.assertEqual("CA: SPORTSNET ONE", by_id[138]["channel"])
        self.assertEqual("1238991", by_id[138]["stream_id"], "the GuideNumber a DVR front end matches timers by")
        self.assertEqual("509 Bandwidth Limit Exceeded", by_id[138]["last_error"])
        self.assertGreaterEqual(by_id[138]["for_seconds"], 0.0)
        self.assertIsNone(by_id[9]["channel"], "an unknown channel id is still reported")
        self.assertIn("reconnect_budget_seconds", out)
        livetv._stream_status.clear()

    def test_capacity_carries_the_same_snapshot(self):
        import routers.livetv as livetv
        livetv._stream_status.clear()
        self.assertEqual([], livetv.live_capacity(self.db)["streams"])


if __name__ == "__main__":
    unittest.main()

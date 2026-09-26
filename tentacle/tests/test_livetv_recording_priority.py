"""At capacity, a recording takes a viewer's connection slot, never the other
way round -- and Tentacle knows which channels are being recorded.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Jellyfin opens the same tuner URL for a recording as for a viewer, so the
request cannot say what it is. Its timers can: an InProgress timer names the
channel as ExternalChannelId "hdhr_<GuideNumber>", and GuideNumber is the
channel's stream_id in this lineup. A DVR front end can also reserve a
channel ahead of the timer (pre-padding). tvheadend weights recordings 300
and viewers 100 for the same reason: a viewer who is cut off changes
channel; a recording that is cut off is gone for good.
"""
import asyncio
import tempfile
import unittest
from unittest import mock

from fastapi.responses import StreamingResponse


def _fresh_db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                           connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _priority(kind):
    from routers import livetv
    return livetv._LEASE_PRIORITY[kind]


class Broker(unittest.IsolatedAsyncioTestCase):
    def _slots(self):
        from routers import livetv
        return livetv._StreamSlots()

    async def test_a_recording_takes_the_newest_viewers_slot(self):
        s = self._slots()
        a = await s.acquire_lease(2, 0.01, "live", "channel:1")
        b = await s.acquire_lease(2, 0.01, "live", "channel:2")
        stopped = []
        a.on_preempt = lambda: stopped.append("a")
        b.on_preempt = lambda: stopped.append("b")
        r = await s.acquire_lease(2, 0.01, "recording", "channel:3")
        self.assertIsNotNone(r)
        self.assertEqual(["b"], stopped, "the most recent viewer gives way")
        self.assertEqual(2, s.active)
        self.assertEqual({"channel:1", "channel:3"}, {l.owner for l in s.leases.values()})
        self.assertEqual(1, s.preempted_since_start)

    async def test_an_async_on_preempt_is_awaited(self):
        s = self._slots()
        a = await s.acquire_lease(1, 0.01, "live", "channel:1")
        done = asyncio.Event()

        async def stop():
            done.set()
        a.on_preempt = stop
        await s.acquire_lease(1, 0.01, "recording", "channel:2")
        self.assertTrue(done.is_set())

    async def test_a_viewer_never_takes_a_slot_from_anyone(self):
        s = self._slots()
        rec = await s.acquire_lease(1, 0.01, "recording", "channel:1")
        rec.on_preempt = lambda: self.fail("a recording was pre-empted by a viewer")
        self.assertIsNone(await s.acquire_lease(1, 0.01, "live", "channel:2"))
        self.assertEqual(1, s.active)
        # and not from another viewer either
        s2 = self._slots()
        v = await s2.acquire_lease(1, 0.01, "live", "channel:1")
        v.on_preempt = lambda: self.fail("a viewer was pre-empted by a viewer")
        self.assertIsNone(await s2.acquire_lease(1, 0.01, "live", "channel:2"))

    async def test_a_recording_never_takes_a_slot_from_a_recording(self):
        s = self._slots()
        r1 = await s.acquire_lease(1, 0.01, "recording", "channel:1")
        r1.on_preempt = lambda: self.fail("a recording was pre-empted by a recording")
        self.assertIsNone(await s.acquire_lease(1, 0.01, "recording", "channel:2"))

    async def test_a_freed_slot_is_taken_without_preempting(self):
        s = self._slots()
        v = await s.acquire_lease(1, 0.5, "live", "channel:1")
        v.on_preempt = lambda: self.fail("pre-empted although a slot was about to free")
        s.release_lease(v)
        r = await s.acquire_lease(1, 0.5, "recording", "channel:2")
        self.assertIsNotNone(r)
        self.assertEqual(0, s.preempted_since_start)

    async def test_the_old_shape_still_counts_and_releases(self):
        s = self._slots()
        self.assertEqual([True, True, False], [await s.acquire(2, 0.01) for _ in range(3)])
        self.assertEqual(2, s.active)
        s.release()
        self.assertEqual(1, s.active)
        self.assertTrue(await s.acquire(2, 0.01))

    async def test_releasing_a_preempted_lease_later_is_harmless(self):
        s = self._slots()
        v = await s.acquire_lease(1, 0.01, "live", "channel:1")
        await s.acquire_lease(1, 0.01, "recording", "channel:2")
        s.release_lease(v)      # the viewer's generator finishes afterwards
        self.assertEqual(1, s.active, "the recording's slot must survive the late release")

    async def test_a_viewer_pull_is_upgraded_once_its_channel_is_being_recorded(self):
        """Jellyfin marks a timer InProgress only AFTER the tuner stream is
        open (RecordingsManager: OpenLiveStreamInternal, then Status), so the
        pull that starts a recording is a "viewer" for its first seconds."""
        s = self._slots()
        v = await s.acquire_lease(1, 0.01, "live", "channel:7", stream_key="277123")
        v.on_preempt = lambda: self.fail("the recording's own pull was pre-empted")
        self.assertEqual(1, s.sync_recordings({"277123", "5"}))
        self.assertEqual("recording", v.kind)
        self.assertEqual(0, s.sync_recordings({"277123"}), "already upgraded")
        self.assertIsNone(await s.acquire_lease(1, 0.01, "recording", "channel:8"),
                          "an upgraded recording must not give way to another")
        self.assertIsNone(await s.acquire_lease(1, 0.01, "live", "channel:9"))

    async def test_a_finished_recording_goes_back_to_viewer_priority(self):
        """The viewer who keeps watching after the timer ends must not hold
        a recording's rank for ever: the next timer on another channel
        would be refused for it."""
        s = self._slots()
        v = await s.acquire_lease(1, 0.01, "live", "channel:7", stream_key="277123")
        s.sync_recordings({"277123"})
        self.assertEqual("recording", v.kind)
        self.assertEqual(1, s.sync_recordings(set()), "the timer ended")
        self.assertEqual("live", v.kind)
        self.assertEqual(_priority("live"), v.priority)
        stopped = []
        v.on_preempt = lambda: stopped.append(1)
        r = await s.acquire_lease(1, 0.01, "recording", "channel:8")
        self.assertIsNotNone(r, "the next recording gets the slot")
        self.assertEqual([1], stopped)

    async def test_a_waiting_recording_gets_the_freed_slot_before_a_waiting_viewer(self):
        """Both slots held by recordings; a viewer starts waiting, then a
        recording (pre-padding). When one recording ends, the waiting
        recording must get the slot -- the viewer can be refused, the
        recording cannot."""
        for early in ("live", "vod"):
            s = self._slots()
            r1 = await s.acquire_lease(2, 0.01, "recording", "channel:1")
            await s.acquire_lease(2, 0.01, "recording", "channel:2")
            viewer = asyncio.ensure_future(s.acquire_lease(2, 1.0, early, "early"))
            await asyncio.sleep(0.01)
            rec = asyncio.ensure_future(s.acquire_lease(2, 1.0, "recording", "channel:3"))
            await asyncio.sleep(0.01)
            s.release_lease(r1)
            got = await rec
            self.assertIsNotNone(got, early)
            self.assertIsNone(await viewer, f"the {early} waiter must not have jumped the queue")
            self.assertEqual({"channel:2", "channel:3"}, {l.owner for l in s.leases.values()})

    async def test_equals_are_served_in_arrival_order(self):
        s = self._slots()
        held = await s.acquire_lease(1, 0.01, "live", "channel:1")
        first = asyncio.ensure_future(s.acquire_lease(1, 1.0, "live", "first"))
        await asyncio.sleep(0.01)
        second = asyncio.ensure_future(s.acquire_lease(1, 1.0, "live", "second"))
        await asyncio.sleep(0.01)
        s.release_lease(held)
        self.assertIsNotNone(await first)
        self.assertIsNone(await second)

    async def test_a_newcomer_does_not_jump_a_waiter_that_is_as_important(self):
        s = self._slots()
        held = await s.acquire_lease(1, 0.01, "recording", "channel:1")
        waiting = asyncio.ensure_future(s.acquire_lease(1, 1.0, "recording", "channel:2"))
        await asyncio.sleep(0.01)
        s.leases.pop(held.id)           # the slot frees without a wake-up reaching the waiter yet
        self.assertIsNone(await s.acquire_lease(1, 0.01, "recording", "channel:3"))
        s.release_lease(None)
        self.assertIsNotNone(await waiting)

    async def test_a_recording_waiting_takes_a_viewer_that_was_demoted_meanwhile(self):
        s = self._slots()
        v = await s.acquire_lease(1, 0.01, "live", "channel:7", stream_key="277123")
        s.sync_recordings({"277123"})                     # being recorded: not takeable
        rec = asyncio.ensure_future(s.acquire_lease(1, 1.0, "recording", "channel:8"))
        await asyncio.sleep(0.01)
        stopped = []
        v.on_preempt = lambda: stopped.append(1)
        s.sync_recordings(set())                          # its timer ended: a viewer again
        self.assertIsNotNone(await rec)
        self.assertEqual([1], stopped)

    async def test_the_slot_moves_to_the_newcomer_before_the_victim_is_stopped(self):
        """Stopping the victim can yield; a waiter woken meanwhile must not
        find the slot free and put the count over the limit."""
        s = self._slots()
        v = await s.acquire_lease(1, 0.01, "live", "channel:1")
        seen = []

        async def slow_stop():
            seen.append(s.active)
            await asyncio.sleep(0)
        v.on_preempt = slow_stop
        waiter = asyncio.ensure_future(s.acquire_lease(1, 0.2, "live", "channel:2"))
        await asyncio.sleep(0)
        await s.acquire_lease(1, 0.01, "recording", "channel:3")
        self.assertEqual([1], seen, "the newcomer already holds the slot while the victim stops")
        self.assertIsNone(await waiter)
        self.assertEqual(1, s.active)

    async def test_a_newcomer_cancelled_while_its_victim_stops_leaves_no_lease(self):
        s = self._slots()
        v = await s.acquire_lease(1, 0.01, "live", "channel:1")

        async def slow_stop():
            await asyncio.sleep(0.2)
        v.on_preempt = slow_stop
        rec = asyncio.ensure_future(s.acquire_lease(1, 0.01, "recording", "channel:2"))
        await asyncio.sleep(0.01)
        rec.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await rec
        self.assertEqual(0, s.active, "the slot is free again")
        self.assertIsNotNone(await s.acquire_lease(1, 0.01, "live", "channel:3"))

    async def test_a_lease_without_a_stream_key_is_never_touched(self):
        s = self._slots()
        r = await s.acquire_lease(2, 0.01, "recording", "channel:1")
        self.assertEqual(0, s.sync_recordings(set()))
        self.assertEqual("recording", r.kind)


class GivingTheConnectionBack(unittest.IsolatedAsyncioTestCase):
    """aclose() on a generator that never started runs nothing -- not its
    finally -- so abandoning an opened stream must close the connection
    explicitly, or the provider keeps a slot we no longer count."""

    async def test_close_upstream_closes_the_raw_connection_and_frees_the_slot(self):
        from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp
        from test_livetv_raw_reconnect import _live
        import routers.livetv as livetv
        script = {PANEL: [_redirect(), _resp(404, PANEL)], TOKENIZED: [_live([b"AAAA"])]}
        log, closed, released = [], [], []
        with mock.patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, closed, **kw)), \
                mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True):
            resp = await livetv._stream_proxy_inner(channel_id=1, user_agent="UA", stream_url=PANEL,
                                                    _release_sem=lambda: released.append(1), guard=None)
        self.assertEqual([], closed, "nothing closed yet: the stream is open and unread")
        await resp.close_upstream()
        self.assertEqual(1, len(closed), "the httpx client (and its response) must be closed")
        self.assertEqual([1], released)

    async def test_a_shared_upstream_retired_before_its_pump_ran_closes_the_connection(self):
        import routers.livetv as livetv
        closed = []

        async def close_upstream():
            closed.append(1)

        async def never_read():
            yield b"X"     # never reached
        released = []
        shared = livetv._SharedUpstream(5, lambda: released.append(1), close_upstream=close_upstream)
        shared.task = asyncio.get_running_loop().create_task(shared._pump(never_read()))
        await shared.preempt()          # before the pump's first turn
        await asyncio.sleep(0.01)
        self.assertEqual([1], closed, "the pump never ran, so its finally never closed anything")
        self.assertEqual([1], released)

    async def test_a_shared_upstream_whose_pump_ran_lets_the_pump_close(self):
        import routers.livetv as livetv
        closed, iter_closed = [], []

        async def close_upstream():
            closed.append(1)

        async def body():
            try:
                while True:
                    yield b"X"
                    await asyncio.sleep(0)
            finally:
                iter_closed.append(1)
        shared = livetv._SharedUpstream(6, lambda: None, close_upstream=close_upstream)
        q = shared.subscribe()
        shared.task = asyncio.get_running_loop().create_task(shared._pump(body()))
        await q.get()                   # the pump is running
        await shared.preempt()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if iter_closed:
                break
        self.assertEqual([1], iter_closed, "the pump's own finally closes the iterator")
        self.assertEqual([], closed, "not closed twice")


class KnowingWhatIsRecording(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        livetv._recording_cache.update(at=-1e9, sids=set(), pending=None, failures=0, retry_at=-1e9)
        livetv._reserved_channels.clear()
        self.db = _fresh_db()
        from models.database import LiveChannel, Provider, set_setting
        prov = Provider(name="P", server_url="http://provider.test", username="u", password="p")
        self.db.add(prov)
        self.db.commit()
        self.a = LiveChannel(provider_id=prov.id, name="A", stream_id="277123", stream_url="http://p/a.m3u8")
        self.b = LiveChannel(provider_id=prov.id, name="B", stream_id="999", stream_url="http://p/b.m3u8")
        self.db.add_all([self.a, self.b])
        set_setting(self.db, "jellyfin_url", "http://jf.test")
        set_setting(self.db, "jellyfin_api_key", "k")
        self.db.commit()

    def test_timers_are_read_by_external_channel_id(self):
        from services.jellyfin import JellyfinService
        timers = {"Items": [
            {"Status": "InProgress", "ExternalChannelId": "hdhr_277123"},
            {"Status": "New", "ExternalChannelId": "hdhr_999", "StartDate": "2099-01-01T00:00:00.0000000Z"},
            {"Status": "InProgress", "ExternalChannelId": "m3u_5"},
            {"Status": "InProgress"},
        ]}
        with mock.patch.object(self.livetv, "_jellyfin_timers", lambda u, k: timers):
            self.assertEqual({"277123"}, self.livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k"))
        with mock.patch.object(self.livetv, "_jellyfin_timers", lambda u, k: None):
            self.assertIsNone(self.livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k"))

    def test_a_timer_that_is_due_counts_as_recording_before_jellyfin_marks_it(self):
        """Jellyfin opens the stream first and marks InProgress after; at
        capacity the opening pull must already outrank a viewer."""
        from datetime import datetime, timedelta
        from services.jellyfin import JellyfinService
        now = datetime.utcnow()
        fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        timers = {"Items": [
            # starts in 5 min with 15 min pre-padding: recording is due now
            {"Status": "New", "ExternalChannelId": "hdhr_1", "StartDate": fmt(now + timedelta(minutes=5)), "PrePaddingSeconds": 900},
            # starts in 90 s, no padding: due within the imminent window
            {"Status": "New", "ExternalChannelId": "hdhr_2", "StartDate": fmt(now + timedelta(seconds=90))},
            # starts in an hour: not yet
            {"Status": "New", "ExternalChannelId": "hdhr_3", "StartDate": fmt(now + timedelta(hours=1))},
            # cancelled: never
            {"Status": "Cancelled", "ExternalChannelId": "hdhr_4", "StartDate": fmt(now)},
            {"Status": "New", "ExternalChannelId": "hdhr_5", "StartDate": "garbage"},
            # never started and long over (Jellyfin leaves it "New"): not recording
            {"Status": "New", "ExternalChannelId": "hdhr_6", "StartDate": fmt(now - timedelta(hours=3)),
             "EndDate": fmt(now - timedelta(hours=1)), "PostPaddingSeconds": 600},
            # late but still inside its end + post-padding: still due
            {"Status": "New", "ExternalChannelId": "hdhr_7", "StartDate": fmt(now - timedelta(hours=2)),
             "EndDate": fmt(now - timedelta(minutes=5)), "PostPaddingSeconds": 600},
        ]}
        with mock.patch.object(self.livetv, "_jellyfin_timers", lambda u, k: timers):
            self.assertEqual({"1", "2", "7"}, self.livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k"))

    async def test_a_failing_jellyfin_is_asked_less_often_and_the_last_answer_kept(self):
        """A bad key or a Jellyfin that is down must not cost every tuner
        open the lookup wait, nor log an error every five seconds."""
        calls = []

        def failing(url, key):
            calls.append(1)
            raise RuntimeError("Jellyfin answered HTTP 401")
        self.livetv._recording_cache.update({"at": -1e9, "sids": {"277123"}, "pending": None,
                                             "failures": 0, "retry_at": -1e9})
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", failing), \
                self.assertLogs("routers.livetv", "WARNING") as logs:
            self.assertEqual({self.a.id}, await self.livetv._recording_channel_ids(self.db, force=True, background=True),
                             "the last answer is kept")
            for _ in range(5):
                self.livetv._recording_cache["at"] = -1e9            # TTL expired
                await self.livetv._recording_channel_ids(self.db)
                await self.livetv._recording_channel_ids(self.db, force=True, background=True)
            self.assertEqual(1, len(calls), "routine asks back off inside the retry window")
            await self.livetv._recording_channel_ids(self.db, force=True)
            self.assertEqual(2, len(calls), "a tuner open at capacity always asks: Jellyfin may be back")
        self.assertEqual(1, sum("Could not ask Jellyfin" in m for m in logs.output), "reported once")
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: set()):
            self.livetv._recording_cache["retry_at"] = -1e9
            self.assertEqual(set(), await self.livetv._recording_channel_ids(self.db, force=True))
        self.assertEqual(0, self.livetv._recording_cache["failures"], "recovered")

    async def test_the_refresher_promotes_a_running_pull_without_another_open(self):
        """A lone recording (nothing else opening) must still become
        'recording' once Jellyfin's timer flips."""
        answers = [set()]
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: answers[-1]), \
                mock.patch.object(self.livetv, "SessionLocal", lambda: self.db), \
                mock.patch.object(self.db, "close", lambda: None):
            self.livetv._stream_slots = self.livetv._StreamSlots()
            lease = await self.livetv._stream_slots.acquire_lease(6, 0.01, "live", "channel:9", stream_key="277123")
            self.assertEqual("live", lease.kind)
            answers.append({"277123"})       # the timer flipped
            await self.livetv._refresh_recordings_once()
            self.assertEqual("recording", lease.kind)

    async def test_channel_ids_come_from_timers_and_are_cached(self):
        calls = []

        def fake(url, key):
            calls.append(url)
            return {"277123"}
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", fake):
            self.assertEqual({self.a.id}, await self.livetv._recording_channel_ids(self.db))
            self.assertEqual({self.a.id}, await self.livetv._recording_channel_ids(self.db))
        self.assertEqual(1, len(calls), "one Jellyfin call per few seconds, not per open")

    async def test_jellyfin_unreachable_means_nothing_is_recording(self):
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: None):
            self.assertEqual(set(), await self.livetv._recording_channel_ids(self.db))

    async def test_a_reservation_counts_and_lapses(self):
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: set()):
            out = await self.livetv.live_reserve(self.livetv.ReserveRequest(channel_id=self.b.id, seconds=1), self.db)
            self.assertEqual(1, out["reserved_for_seconds"])
            self.assertEqual({self.b.id}, await self.livetv._recording_channel_ids(self.db))
            self.livetv._reserved_channels[self.b.id]["until"] = asyncio.get_running_loop().time() - 1
            self.livetv._recording_cache["at"] = -1e9
            self.assertEqual(set(), await self.livetv._recording_channel_ids(self.db))
            await self.livetv.live_reserve(self.livetv.ReserveRequest(channel_id=self.b.id), self.db)
            await self.livetv.live_unreserve(self.b.id)
            self.assertNotIn(self.b.id, self.livetv._reserved_channels)

    async def test_a_reservation_keeps_a_running_pull_at_recording_rank(self):
        """Reserved ahead of the timer: the pull already running on that
        channel is promoted, and NOT demoted while the reservation holds
        even though Jellyfin has no InProgress timer for it yet."""
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: set()):
            self.livetv._stream_slots = self.livetv._StreamSlots()
            lease = await self.livetv._stream_slots.acquire_lease(
                6, 0.01, "live", f"channel:{self.b.id}", stream_key=self.b.stream_id or str(self.b.id))
            await self.livetv.live_reserve(self.livetv.ReserveRequest(channel_id=self.b.id, seconds=60), self.db)
            self.livetv._recording_cache["at"] = -1e9
            await self.livetv._recording_channel_ids(self.db)
            self.assertEqual("recording", lease.kind)
            self.livetv._recording_cache["at"] = -1e9
            await self.livetv._recording_channel_ids(self.db)
            self.assertEqual("recording", lease.kind, "still reserved: not demoted")
            await self.livetv.live_unreserve(self.b.id)
            self.livetv._recording_cache["at"] = -1e9
            await self.livetv._recording_channel_ids(self.db)
            self.assertEqual("live", lease.kind, "reservation dropped and no timer: back to viewer")

    async def test_a_reservation_by_guide_number_as_jellyfin_names_it(self):
        from fastapi import HTTPException
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: set()):
            out = await self.livetv.live_reserve(self.livetv.ReserveRequest(stream_id="hdhr_277123", seconds=60), self.db)
            self.assertEqual(self.a.id, out["channel_id"])
            self.assertEqual({self.a.id}, await self.livetv._recording_channel_ids(self.db))
            with self.assertRaises(HTTPException) as cm:
                await self.livetv.live_reserve(self.livetv.ReserveRequest(stream_id="nope"), self.db)
            self.assertEqual(404, cm.exception.status_code)
            with self.assertRaises(HTTPException) as cm:
                await self.livetv.live_reserve(self.livetv.ReserveRequest(), self.db)
            self.assertEqual(422, cm.exception.status_code)

    async def test_a_slow_jellyfin_does_not_hold_up_a_tuner_open(self):
        """D4: the lookup is capped; the last answer is used and the answer
        that arrives later is kept for the next open."""
        import time as _time
        started = asyncio.get_running_loop().time()

        def slow(url, key):
            _time.sleep(0.8)
            return {"277123"}
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", slow), \
                mock.patch.object(self.livetv, "_RECORDING_LOOKUP_WAIT", 0.2):
            ids = await self.livetv._recording_channel_ids(self.db)
            elapsed = asyncio.get_running_loop().time() - started
            self.assertLess(elapsed, 0.6, "the open must not wait for a slow Jellyfin")
            self.assertEqual(set(), ids, "no earlier answer: nothing is recording")
            pending = self.livetv._recording_cache["pending"]
            self.assertIsNotNone(pending, "the lookup carries on in the background")
            await pending
            await asyncio.sleep(0)     # let the done-callback run
            self.assertEqual({self.a.id}, await self.livetv._recording_channel_ids(self.db),
                             "the late answer is used from then on")


class RouteGivesRecordingsTheSlot(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        livetv._stream_slots = livetv._StreamSlots()
        livetv._shared_lock = None
        livetv._shared_streams.clear()
        livetv._stream_status.clear()
        livetv._recording_cache.update(at=-1e9, sids=set(), pending=None, failures=0, retry_at=-1e9)
        livetv._reserved_channels.clear()
        self.db = _fresh_db()
        from models.database import LiveChannel, Provider, set_setting
        prov = Provider(name="P", server_url="http://provider.test", username="u", password="p")
        self.db.add(prov)
        self.db.commit()
        self.viewer = LiveChannel(provider_id=prov.id, name="Viewer", stream_id="1", stream_url="http://provider.test/live/u/p/1.m3u8")
        self.rec = LiveChannel(provider_id=prov.id, name="Rec", stream_id="2", stream_url="http://provider.test/live/u/p/2.m3u8")
        self.db.add_all([self.viewer, self.rec])
        set_setting(self.db, "livetv_max_concurrent_streams", "1")
        set_setting(self.db, "jellyfin_url", "http://jf.test")
        set_setting(self.db, "jellyfin_api_key", "k")
        self.db.commit()

    def test_at_capacity_a_recording_stops_the_viewer_and_streams(self):
        livetv = self.livetv
        opened = []

        async def fake_inner(channel_id, ua, url, release, guard=None, **kw):
            opened.append(channel_id)

            async def gen():
                try:
                    while True:
                        yield b"X"
                        await asyncio.sleep(0.01)
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def drive():
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard", lambda *a, **k: (lambda url: True)), \
                    mock.patch.object(livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: {"2"}):
                v = await livetv.stream_proxy(self.viewer.id, self.db)
                first = await v.body_iterator.__anext__()
                self.assertEqual(b"X", first)
                self.assertEqual("live", livetv._stream_slots.lease_for(f"channel:{self.viewer.id}").kind)

                r = await livetv.stream_proxy(self.rec.id, self.db)   # capacity 1: must pre-empt
                self.assertEqual(b"X", await r.body_iterator.__anext__())
                self.assertEqual("recording", livetv._stream_slots.lease_for(f"channel:{self.rec.id}").kind)
                self.assertIsNone(livetv._stream_slots.lease_for(f"channel:{self.viewer.id}"))

                # the viewer's stream ends cleanly rather than hanging
                rest = b""
                for _ in range(50):
                    try:
                        rest += await asyncio.wait_for(v.body_iterator.__anext__(), 1.0)
                    except StopAsyncIteration:
                        break
                else:
                    self.fail("the pre-empted viewer's stream did not end")
                await r.body_iterator.aclose()
                return opened
        opened = asyncio.run(drive())
        self.assertEqual([self.viewer.id, self.rec.id], opened)


    def test_a_recording_whose_timer_flips_after_the_open_is_still_protected(self):
        """D2: at the moment the recording's stream opens, Jellyfin's timer is
        not InProgress yet. The lease starts as a viewer; the next lookup --
        forced when a slot is about to be taken -- upgrades it, and the
        viewer that arrives at capacity is refused instead."""
        livetv = self.livetv
        answers = [set(), {"2"}]      # first lookup: nothing recording; then channel "2" is

        async def fake_inner(channel_id, ua, url, release, guard=None, **kw):
            async def gen():
                try:
                    while True:
                        yield b"X"
                        await asyncio.sleep(0.01)
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        def lookup(u, k):
            return answers.pop(0) if len(answers) > 1 else answers[0]

        async def drive():
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard", lambda *a, **k: (lambda url: True)), \
                    mock.patch.object(livetv, "_recording_stream_ids_from_jellyfin", lookup):
                r = await livetv.stream_proxy(self.rec.id, self.db)
                self.assertEqual(b"X", await r.body_iterator.__anext__())
                self.assertEqual("live", livetv._stream_slots.lease_for(f"channel:{self.rec.id}").kind,
                                 "classified as a viewer at open, as Jellyfin's timing dictates")
                livetv._recording_cache["at"] = -1e9      # the 5 s cache has aged
                from fastapi import HTTPException
                with self.assertRaises(HTTPException) as cm:
                    await livetv.stream_proxy(self.viewer.id, self.db)
                self.assertEqual(503, cm.exception.status_code)
                lease = livetv._stream_slots.lease_for(f"channel:{self.rec.id}")
                self.assertIsNotNone(lease, "the recording kept its slot")
                self.assertEqual("recording", lease.kind, "upgraded by the forced lookup")
                self.assertEqual(b"X", await r.body_iterator.__anext__(), "and it is still streaming")
                await r.body_iterator.aclose()
        asyncio.run(drive())

    def test_a_slot_taken_while_the_viewer_is_still_opening_is_not_streamed_uncounted(self):
        """D5: a recording pre-empts a viewer whose pump does not exist yet."""
        livetv = self.livetv
        gate = None

        async def fake_inner(channel_id, ua, url, release, guard=None, **kw):
            if channel_id == self.viewer.id:
                await gate.wait()       # the viewer's open is slow

            async def gen():
                try:
                    while True:
                        yield b"X"
                        await asyncio.sleep(0.01)
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def drive():
            nonlocal gate
            gate = asyncio.Event()
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard", lambda *a, **k: (lambda url: True)), \
                    mock.patch.object(livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: {"2"}):
                viewer_open = asyncio.ensure_future(livetv.stream_proxy(self.viewer.id, self.db))
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if livetv._stream_slots.lease_for(f"channel:{self.viewer.id}") is not None:
                        break
                r = await livetv.stream_proxy(self.rec.id, self.db)      # takes the viewer's slot mid-open
                self.assertEqual(b"X", await r.body_iterator.__anext__())
                gate.set()
                from fastapi import HTTPException
                with self.assertRaises(HTTPException) as cm:
                    await viewer_open
                self.assertEqual(503, cm.exception.status_code)
                self.assertEqual(1, livetv._stream_slots.active, "only the recording holds a slot")
                self.assertNotIn(self.viewer.id, livetv._shared_streams)
                await r.body_iterator.aclose()
        asyncio.run(drive())

if __name__ == "__main__":
    unittest.main()

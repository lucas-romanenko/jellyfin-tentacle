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
        self.assertEqual(1, s.upgrade_recordings({"277123", "5"}))
        self.assertEqual("recording", v.kind)
        self.assertEqual(0, s.upgrade_recordings({"277123"}), "already upgraded")
        self.assertIsNone(await s.acquire_lease(1, 0.01, "recording", "channel:8"),
                          "an upgraded recording must not give way to another")
        self.assertIsNone(await s.acquire_lease(1, 0.01, "live", "channel:9"))


class KnowingWhatIsRecording(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        livetv._recording_cache.update(at=-1e9, sids=set(), pending=None)
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
            {"Status": "New", "ExternalChannelId": "hdhr_999"},
            {"Status": "InProgress", "ExternalChannelId": "m3u_5"},
            {"Status": "InProgress"},
        ]}
        with mock.patch.object(JellyfinService, "_get", lambda self, path, params=None: timers):
            self.assertEqual({"277123"}, self.livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k"))
        with mock.patch.object(JellyfinService, "_get", lambda self, path, params=None: None):
            self.assertIsNone(self.livetv._recording_stream_ids_from_jellyfin("http://jf.test", "k"))

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
            self.livetv._reserved_channels[self.b.id] = asyncio.get_running_loop().time() - 1
            self.livetv._recording_cache["at"] = -1e9
            self.assertEqual(set(), await self.livetv._recording_channel_ids(self.db))
            await self.livetv.live_reserve(self.livetv.ReserveRequest(channel_id=self.b.id), self.db)
            await self.livetv.live_unreserve(self.b.id)
            self.assertNotIn(self.b.id, self.livetv._reserved_channels)

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


class RouteGivesRecordingsTheSlot(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        livetv._stream_slots = livetv._StreamSlots()
        livetv._shared_lock = None
        livetv._shared_streams.clear()
        livetv._stream_status.clear()
        livetv._recording_cache.update(at=-1e9, sids=set(), pending=None)
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

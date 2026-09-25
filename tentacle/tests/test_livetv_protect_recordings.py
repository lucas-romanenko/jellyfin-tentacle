"""Recording protection (setting `livetv_protect_recordings`): while a
recording is being pulled, nothing else opens a provider connection.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Measured on one household's Xtream account (2026-09-23/24): two TS
recordings at once were closed by the provider every ~8 s, in turn; a TV
episode played next to a TS recording closed the recording every ~20 s; and
any new connection put a running HLS recording into a 509 storm of 30 s to
3 min. With the setting on: a new viewer or film upstream is refused (503)
while a recording runs -- a viewer of a channel already being pulled still
attaches, and a second recording still opens; a recording that starts stops
every running viewer and film first; background provider work waits.
Off (the default) changes nothing.
"""
import asyncio
import tempfile
import types
import unittest
from unittest import mock

from fastapi import HTTPException


def _fresh_db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db",
                           connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _livetv():
    from routers import livetv
    return livetv


class ProtectedBroker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _livetv()._recording_cache.pop("answer_issued_at", None)

    def _slots(self, protect=True):
        s = _livetv()._StreamSlots()
        s.protect = protect
        return s

    async def test_off_changes_nothing(self):
        """Default: a film opens next to a recording, and a recording below
        the limit stops nobody."""
        s = self._slots(protect=False)
        v = await s.acquire_lease(6, 0.01, "live", "channel:1")
        v.on_preempt = lambda: self.fail("stopped with protection off")
        await s.acquire_lease(6, 0.01, "recording", "channel:2")
        self.assertIsNotNone(await s.acquire_lease(6, 0.01, "vod", "vod:1"))
        self.assertIsNotNone(await s.acquire_lease(6, 0.01, "live", "channel:3"))
        self.assertEqual(0, s.protected_refusals)
        self.assertEqual(0, s.protected_preemptions)
        self.assertEqual(4, s.active)

    async def test_nothing_is_refused_while_no_recording_runs(self):
        s = self._slots()
        self.assertIsNotNone(await s.acquire_lease(6, 0.01, "live", "channel:1"))
        self.assertIsNotNone(await s.acquire_lease(6, 0.01, "vod", "vod:1"))
        self.assertIsNotNone(await s.acquire_lease(6, 0.01, "live", "channel:2"))
        self.assertEqual(0, s.protected_refusals)

    async def test_a_viewer_or_film_is_refused_while_a_recording_runs(self):
        livetv = _livetv()
        for limit in (0, 1, 6):
            s = self._slots()
            await s.acquire_lease(limit, 0.01, "recording", "channel:1")
            for kind in ("live", "vod"):
                with self.assertRaises(livetv.RecordingProtected, msg=f"{kind} limit {limit}"):
                    await s.acquire_lease(limit, 0.01, kind, f"{kind}:x")
            self.assertEqual(2, s.protected_refusals)
            self.assertEqual(1, s.active, "a refused pull holds nothing")
            self.assertEqual("vod", s.last_protected_refusal["kind"])
            self.assertEqual(0, s.refused, "not counted as a capacity refusal")

    async def test_a_second_recording_always_opens(self):
        s = self._slots()
        r1 = await s.acquire_lease(6, 0.01, "recording", "channel:1")
        r1.on_preempt = lambda: self.fail("a recording was stopped for a recording")
        r2 = await s.acquire_lease(6, 0.01, "recording", "channel:2")
        self.assertIsNotNone(r2)
        self.assertEqual(2, s.active)
        self.assertEqual(0, s.protected_preemptions)

    async def test_a_recording_stops_every_viewer_and_film_before_it_opens(self):
        """Below the limit too: on the account this is for, a film running
        next to a recording cuts the recording every ~20 s."""
        s = self._slots()
        order = []
        v1 = await s.acquire_lease(6, 0.01, "live", "channel:1")
        v2 = await s.acquire_lease(6, 0.01, "live", "channel:2")
        f = await s.acquire_lease(6, 0.01, "vod", "vod:1")
        v1.on_preempt = lambda: order.append("v1")

        async def stop_v2():
            await asyncio.sleep(0)
            order.append("v2")
        v2.on_preempt = stop_v2
        f.on_preempt = lambda: order.append("film")
        rec = await s.acquire_lease(6, 0.01, "recording", "channel:3")
        self.assertEqual({"v1", "v2", "film"}, set(order), "all stopped, the async one awaited")
        self.assertEqual([rec.id], list(s.leases), "only the recording holds a slot")
        self.assertTrue(v1.preempted and v2.preempted and f.preempted)
        self.assertEqual(3, s.protected_preemptions)
        self.assertEqual(3, s.preempted_since_start)

    async def test_a_recording_that_cannot_be_sure_stops_films_only(self):
        """Jellyfin did not answer just now: a live pull may be another
        recording that opened as a viewer. Stopping it could cost a recording."""
        s = self._slots()
        v = await s.acquire_lease(6, 0.01, "live", "channel:1")
        v.on_preempt = lambda: self.fail("a live pull was stopped on an uncertain answer")
        f = await s.acquire_lease(6, 0.01, "vod", "vod:1")
        stopped = []
        f.on_preempt = lambda: stopped.append(1)
        await s.acquire_lease(6, 0.01, "recording", "channel:2", certain=False)
        self.assertEqual([1], stopped)
        self.assertIn(v.id, s.leases)

    async def test_an_uncertain_live_open_is_let_through_but_a_film_is_not(self):
        livetv = _livetv()
        s = self._slots()
        await s.acquire_lease(6, 0.01, "recording", "channel:1")
        self.assertIsNotNone(await s.acquire_lease(6, 0.01, "live", "channel:2", certain=False),
                             "it may be the next recording: never refused on a stale answer")
        with self.assertRaises(livetv.RecordingProtected):
            await s.acquire_lease(6, 0.01, "vod", "vod:1", certain=False)

    async def test_the_count_never_exceeds_the_limit_and_a_woken_waiter_is_refused(self):
        """At the limit a film is waiting for a slot. A recording starts and
        stops the viewer: the waiter wakes to a running recording, not to a
        free slot."""
        livetv = _livetv()
        s = self._slots()
        v = await s.acquire_lease(1, 0.01, "live", "channel:1")
        seen = []

        async def slow_stop():
            seen.append(s.active)
            await asyncio.sleep(0.01)
            s.release_lease(v)          # the viewer's pump ends and gives its slot back
        v.on_preempt = slow_stop
        waiter = asyncio.ensure_future(s.acquire_lease(1, 1.0, "vod", "vod:1"))
        await asyncio.sleep(0)
        rec = await s.acquire_lease(1, 0.01, "recording", "channel:2")
        self.assertEqual([1], seen, "the recording holds the slot while the viewer stops")
        with self.assertRaises(livetv.RecordingProtected):
            await waiter
        self.assertEqual([rec.id], list(s.leases))

    async def test_a_waiter_is_refused_when_a_recording_starts_while_it_waits(self):
        livetv = _livetv()
        s = self._slots()
        r1 = await s.acquire_lease(1, 0.01, "recording", "channel:1")
        s.protect = False
        waiter = asyncio.ensure_future(s.acquire_lease(1, 1.0, "live", "channel:2"))
        await asyncio.sleep(0.01)
        s.protect = True
        s.release_lease(None)           # a wake-up, the recording still runs
        with self.assertRaises(livetv.RecordingProtected):
            await waiter
        self.assertEqual([r1.id], list(s.leases))
        self.assertEqual({}, s._waiting)

    async def test_a_recording_cancelled_while_its_victims_stop_leaves_no_lease(self):
        s = self._slots()
        v = await s.acquire_lease(6, 0.01, "live", "channel:1")

        async def slow_stop():
            await asyncio.sleep(0.2)
        v.on_preempt = slow_stop
        rec = asyncio.ensure_future(s.acquire_lease(6, 0.01, "recording", "channel:2"))
        await asyncio.sleep(0.01)
        rec.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await rec
        self.assertEqual(0, s.active)
        self.assertFalse(s.recording_active())

    async def test_a_pull_recognised_as_a_recording_clears_the_way(self):
        """Jellyfin marks a timer InProgress after the stream opened: the
        recording may have opened as a viewer. Once it is recognised, the
        film playing next to it is stopped."""
        s = self._slots()
        rec = await s.acquire_lease(6, 0.01, "live", "channel:1", stream_key="277123")
        rec.on_preempt = lambda: self.fail("the recording itself was stopped")
        f = await s.acquire_lease(6, 0.01, "vod", "vod:1")
        stopped = []
        f.on_preempt = lambda: stopped.append(1)
        livetv = _livetv()
        with mock.patch.object(livetv, "_recording_answer_is_current", lambda: True):
            self.assertEqual(1, s.sync_recordings({"277123"}))
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertEqual([1], stopped)
        self.assertEqual([rec.id], list(s.leases))

    async def test_recognition_on_a_stale_answer_stops_films_only(self):
        s = self._slots()
        rec = await s.acquire_lease(6, 0.01, "live", "channel:1", stream_key="277123")
        other = await s.acquire_lease(6, 0.01, "live", "channel:2", stream_key="5")
        other.on_preempt = lambda: self.fail("a live pull was stopped on a stale answer")
        livetv = _livetv()
        with mock.patch.object(livetv, "_recording_answer_is_current", lambda: False):
            s.sync_recordings({"277123"})
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertIn(other.id, s.leases)

    async def test_turning_protection_on_during_a_recording_clears_the_way(self):
        s = self._slots(protect=False)
        await s.acquire_lease(6, 0.01, "recording", "channel:1")
        f = await s.acquire_lease(6, 0.01, "vod", "vod:1")
        stopped = []
        f.on_preempt = lambda: stopped.append(1)
        s.set_protect(False)
        await asyncio.sleep(0)
        self.assertEqual([], stopped)
        s.set_protect(True)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual([1], stopped)

    async def test_a_finished_recording_lifts_the_protection(self):
        s = self._slots()
        rec = await s.acquire_lease(6, 0.01, "live", "channel:1", stream_key="277123")
        s.sync_recordings({"277123"})
        self.assertTrue(s.recording_active())
        s.sync_recordings(set())            # the timer ended; a viewer keeps watching
        self.assertFalse(s.recording_active())
        self.assertIsNotNone(await s.acquire_lease(6, 0.01, "vod", "vod:1"))
        s.release_lease(rec)
        self.assertIsNotNone(await s.acquire_lease(6, 0.01, "live", "channel:2"))

    async def test_refusals_are_logged_once_a_minute_and_counted(self):
        livetv = _livetv()
        s = self._slots()
        await s.acquire_lease(6, 0.01, "recording", "channel:1")
        clock = [1000.0]
        loop = asyncio.get_running_loop()
        with mock.patch.object(loop, "time", lambda: clock[0]), \
                self.assertLogs("routers.livetv", "WARNING") as logs:
            for i in range(5):
                with self.assertRaises(livetv.RecordingProtected):
                    await s.acquire_lease(6, 0.01, "vod", f"vod:{i}")
                clock[0] += 10
            clock[0] += 60
            with self.assertRaises(livetv.RecordingProtected):
                await s.acquire_lease(6, 0.01, "live", "channel:9")
        lines = [m for m in logs.output if "Recording protection: refused" in m]
        self.assertEqual(2, len(lines), lines)
        self.assertIn("4 more refused", lines[1])
        self.assertEqual(6, s.protected_refusals)


class TheSetting(unittest.TestCase):
    def test_default_off_and_truthy_values(self):
        livetv = _livetv()
        from models.database import set_setting
        db = _fresh_db()
        self.assertFalse(livetv._protect_recordings(db))
        for raw, want in (("true", True), ("1", True), ("On", True), ("yes", True),
                          ("false", False), ("0", False), ("", False), ("garbage", False)):
            set_setting(db, "livetv_protect_recordings", raw)
            db.commit()
            self.assertEqual(want, livetv._protect_recordings(db), raw)


class TunerOpens(unittest.IsolatedAsyncioTestCase):
    """The tuner route: refused before any provider contact; a viewer of a
    channel that is already pulled attaches; a recording opens."""

    def setUp(self):
        livetv = _livetv()
        self.livetv = livetv
        livetv._stream_slots = livetv._StreamSlots()
        livetv._shared_streams.clear()
        livetv._pending_opens.clear()
        livetv._reserved_channels.clear()
        livetv._recording_cache.update(at=-1e9, sids=set(), pending=None, failures=0, retry_at=-1e9)
        livetv._recording_cache.pop("answer_issued_at", None)
        livetv._recording_cache.pop("pending_issued", None)
        self.db = _fresh_db()
        from models.database import LiveChannel, Provider, set_setting
        prov = Provider(name="P", server_url="http://provider.test", username="u", password="p")
        self.db.add(prov)
        self.db.commit()
        self.rec_ch = LiveChannel(provider_id=prov.id, name="Game", stream_id="100", stream_url="http://provider.test/live/u/p/100.ts")
        self.other = LiveChannel(provider_id=prov.id, name="Other", stream_id="200", stream_url="http://provider.test/live/u/p/200.ts")
        self.db.add_all([self.rec_ch, self.other])
        set_setting(self.db, "jellyfin_url", "http://jf.test")
        set_setting(self.db, "jellyfin_api_key", "k")
        set_setting(self.db, "livetv_protect_recordings", "true")
        self.db.commit()
        self.timers = {"100"}

    def _patches(self, opened):
        async def no_provider(*a, **k):
            opened.append(a)
            raise AssertionError("the provider was contacted")
        return (mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: set(self.timers)),
                mock.patch.object(self.livetv, "_stream_proxy_inner", no_provider), \
                mock.patch.object(self.livetv, "lan_origin_guard", lambda *a, **k: (lambda u: True)))

    async def test_a_viewer_of_another_channel_is_refused_without_touching_the_provider(self):
        rec = await self.livetv._stream_slots.acquire_lease(6, 0.01, "recording", f"channel:{self.rec_ch.id}",
                                                            stream_key="100")
        opened = []
        p1, p2, p3 = self._patches(opened)
        with p1, p2, p3:
            with self.assertRaises(HTTPException) as cm:
                await self.livetv.stream_proxy(self.other.id, self.db)
        self.assertEqual(503, cm.exception.status_code)
        self.assertIn("recording protection", cm.exception.detail)
        self.assertEqual([], opened)
        self.assertEqual([rec.id], list(self.livetv._stream_slots.leases))
        self.assertEqual(1, self.livetv._stream_slots.protected_refusals)
        self.assertNotIn(self.other.id, self.livetv._pending_opens, "the opener registration is cleaned up")

    async def test_a_viewer_of_the_recorded_channel_attaches(self):
        await self.livetv._stream_slots.acquire_lease(6, 0.01, "recording", f"channel:{self.rec_ch.id}",
                                                      stream_key="100")
        shared = self.livetv._SharedUpstream(self.rec_ch.id, lambda: None)
        shared.subscribe()                      # the recording's own client
        self.livetv._shared_streams[self.rec_ch.id] = shared
        opened = []
        p1, p2, p3 = self._patches(opened)
        with p1, p2, p3:
            resp = await self.livetv.stream_proxy(self.rec_ch.id, self.db)
        self.assertIsInstance(resp, self.livetv._SubscriberResponse)
        self.assertEqual(2, len(shared.subscribers))
        self.assertEqual(0, self.livetv._stream_slots.protected_refusals)
        self.assertEqual([], opened)

    async def test_a_second_recording_opens_and_is_classified_on_a_fresh_answer(self):
        await self.livetv._stream_slots.acquire_lease(6, 0.01, "recording", f"channel:{self.rec_ch.id}",
                                                      stream_key="100")
        self.livetv._recording_cache.update(at=asyncio.get_running_loop().time(), sids={"100"})
        self.timers = {"100", "200"}           # the second timer is due now: only a fresh lookup knows
        seen = []

        async def fake_inner(channel_id, *a, **k):
            seen.append(channel_id)
            raise HTTPException(502, "stop here")   # past the broker: that is all this test needs
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: set(self.timers)), \
                mock.patch.object(self.livetv, "_stream_proxy_inner", fake_inner), \
                mock.patch.object(self.livetv, "lan_origin_guard", lambda *a, **k: (lambda u: True)):
            with self.assertRaises(HTTPException) as cm:
                await self.livetv.stream_proxy(self.other.id, self.db)
        self.assertEqual(502, cm.exception.status_code, "reached the provider open, not refused")
        self.assertEqual([self.other.id], seen)
        self.assertEqual(0, self.livetv._stream_slots.protected_refusals)

    async def test_jellyfin_not_answering_lets_a_live_open_through(self):
        await self.livetv._stream_slots.acquire_lease(6, 0.01, "recording", f"channel:{self.rec_ch.id}",
                                                      stream_key="100")
        seen = []

        def down(u, k):
            raise RuntimeError("connection refused")

        async def fake_inner(channel_id, *a, **k):
            seen.append(channel_id)
            raise HTTPException(502, "stop here")
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", down), \
                mock.patch.object(self.livetv, "_stream_proxy_inner", fake_inner), \
                mock.patch.object(self.livetv, "lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                self.assertLogs("routers.livetv", "WARNING") as logs:
            with self.assertRaises(HTTPException) as cm:
                await self.livetv.stream_proxy(self.other.id, self.db)
        self.assertEqual(502, cm.exception.status_code)
        self.assertEqual([self.other.id], seen, "may be the next recording: opened")
        self.assertTrue(any("did not say in time" in m for m in logs.output))

    async def test_setting_off_opens_as_before(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_protect_recordings", "false")
        self.db.commit()
        await self.livetv._stream_slots.acquire_lease(6, 0.01, "recording", f"channel:{self.rec_ch.id}",
                                                      stream_key="100")
        seen = []

        async def fake_inner(channel_id, *a, **k):
            seen.append(channel_id)
            raise HTTPException(502, "stop here")
        with mock.patch.object(self.livetv, "_recording_stream_ids_from_jellyfin", lambda u, k: {"100"}), \
                mock.patch.object(self.livetv, "_stream_proxy_inner", fake_inner), \
                mock.patch.object(self.livetv, "lan_origin_guard", lambda *a, **k: (lambda u: True)):
            with self.assertRaises(HTTPException):
                await self.livetv.stream_proxy(self.other.id, self.db)
        self.assertEqual([self.other.id], seen)
        self.assertFalse(self.livetv._stream_slots.protect)

    def test_streams_endpoint_reports_protection(self):
        s = self.livetv._stream_slots
        s.protected_refusals, s.protected_preemptions = 3, 1
        s.last_protected_refusal = {"kind": "vod", "owner": "vod:x", "at": "t"}
        out = self.livetv.live_streams(self.db)
        self.assertTrue(out["protect_recordings"])
        self.assertEqual(3, out["protected_refusals"])
        self.assertEqual(1, out["protected_preemptions"])
        self.assertFalse(out["recording_active"])
        self.assertEqual("vod", out["last_protected_refusal"]["kind"])
        cap = self.livetv.live_capacity(self.db)
        self.assertEqual(3, cap["protected_refusals"])


class FilmsThroughTentacle(unittest.TestCase):
    """A film (GET or HEAD) is refused while a recording runs; a running one
    is stopped when a recording starts and its provider connection closes."""

    def setUp(self):
        import routers.livetv as livetv
        import routers.vod as vod
        from test_vod_proxy import PANEL
        self.livetv, self.vod = livetv, vod
        livetv._stream_slots = livetv._StreamSlots()
        vod._playbacks.clear()
        vod._sweeper = None
        self.db = _fresh_db()
        from models.database import Provider, set_setting
        from services import vod_tokens
        self.provider = Provider(name="P", server_url=PANEL, username="u", password="p",
                                 provider_type="xtream", user_agent="UA/1")
        self.db.add(self.provider)
        set_setting(self.db, "vod_token_secret", "s" * 64)
        set_setting(self.db, "livetv_protect_recordings", "true")
        self.db.commit()
        url = vod_tokens.url("http://tentacle:8888", "s" * 64, self.provider.id, "movie", 2141622, "mkv")
        self.token_file = url.rsplit("/", 1)[1]

    def test_get_and_head_are_refused_without_contacting_the_provider(self):
        from test_vod_proxy import _request

        def no_client(**kw):
            raise AssertionError("the provider was contacted")

        async def go():
            await self.livetv._stream_slots.acquire_lease(6, 0.01, "recording", "channel:1")
            with mock.patch("httpx.AsyncClient", no_client), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                for call in (self.vod.vod_stream, self.vod.vod_head):
                    with self.assertRaises(HTTPException) as cm:
                        await call("movie", self.token_file, _request(), self.db)
                    self.assertEqual(503, cm.exception.status_code)
                    self.assertIn("recording protection", cm.exception.detail)
        asyncio.run(go())
        self.assertEqual({}, self.vod._playbacks)
        self.assertEqual(2, self.livetv._stream_slots.protected_refusals)

    def test_a_recording_starting_stops_a_playing_film_and_closes_its_connection(self):
        from test_vod_proxy import _Client, _file, _request
        log = []
        upstream = f"{self.provider.server_url}/movie/u/p/2141622.mkv"
        closed = []

        class _Endless:
            pass
        import httpx

        class _Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b"F" * 1000
                    await asyncio.sleep(0)

            async def aclose(self):
                closed.append(1)
        resp = httpx.Response(206, headers={"content-type": "video/x-matroska", "accept-ranges": "bytes",
                                            "content-range": "bytes 0-9999999/10000000", "content-length": "10000000"},
                              stream=_Stream(), request=httpx.Request("GET", upstream))

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client({upstream: [resp]}, log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                r = await self.vod.vod_stream("movie", self.token_file, _request("bytes=0-"), self.db)
                it = r.body_iterator
                await it.__anext__()
                self.assertEqual(1, len(self.vod._playbacks))
                rec = await self.livetv._stream_slots.acquire_lease(6, 0.01, "recording", "channel:1")
                got = b""
                async for piece in it:
                    got += piece
                    if len(got) > 10_000_000:
                        self.fail("the film kept streaming after the recording started")
                for _ in range(10):
                    await asyncio.sleep(0)
                return rec
        rec = asyncio.run(go())
        self.assertEqual({}, self.vod._playbacks, "delisted")
        self.assertEqual([rec.id], list(self.livetv._stream_slots.leases))
        self.assertTrue(closed, "the provider connection was closed")
        self.assertEqual(1, self.livetv._stream_slots.protected_preemptions)


class BackgroundWork(unittest.TestCase):
    """The sync, discovery and the EPG download wait for a protected
    recording without spending their live-TV budget; buttons that would
    contact the provider answer 503."""

    def setUp(self):
        import services.provider_activity as pa
        self.pa = pa
        self.db = _fresh_db()
        self.addCleanup(self.db.close)
        from models.database import set_setting
        set_setting(self.db, "livetv_protect_recordings", "true")
        set_setting(self.db, "provider_jobs_defer_while_live_seconds", "60")
        self.db.commit()
        self.recording = True
        self.live = True
        self.clock = 0.0
        self.slept = []

        def fake_sleep(s):
            self.slept.append(s)
            self.clock += s
            if self.ends_after is not None and len(self.slept) >= self.ends_after:
                self.recording = False
                self.live = self.live_after
        self.ends_after, self.live_after = None, False
        livetv = _livetv()
        fake_slots = types.SimpleNamespace(recording_active=lambda: self.recording)
        for p in (mock.patch.object(pa, "live_streams_active", lambda: self.live),
                  mock.patch.object(livetv, "_stream_slots", fake_slots),
                  mock.patch.object(pa.time, "sleep", fake_sleep),
                  mock.patch.object(pa.time, "monotonic", lambda: self.clock)):
            p.start()
            self.addCleanup(p.stop)

    def _off(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_protect_recordings", "false")
        self.db.commit()

    def test_recording_protected_needs_both(self):
        self.assertTrue(self.pa.recording_protected(self.db))
        self.recording = False
        self.assertFalse(self.pa.recording_protected(self.db))
        self.recording = True
        self._off()
        self.assertFalse(self.pa.recording_protected(self.db))

    def test_a_wait_outlasts_the_budget_while_a_recording_runs(self):
        self.ends_after = 20                    # 20 polls of 30 s = 600 s > the 60 s budget
        self.assertTrue(self.pa.wait_until_quiet(self.db, "sync"))
        self.assertEqual(20, len(self.slept))

    def test_budget_spent_only_on_viewer_time(self):
        self.ends_after, self.live_after = 20, True     # recording ends, a viewer stays on
        self.assertFalse(self.pa.wait_until_quiet(self.db, "sync"), "then the normal 60 s budget applies")
        self.assertAlmostEqual(600 + 60, sum(self.slept), delta=1.0)

    def test_zero_budget_still_waits_for_a_protected_recording(self):
        from models.database import set_setting
        set_setting(self.db, "provider_jobs_defer_while_live_seconds", "0")
        self.db.commit()
        self.ends_after = 3
        self.assertTrue(self.pa.wait_until_quiet(self.db, "sync"))
        self.assertEqual(3, len(self.slept))

    def test_off_keeps_the_old_budget(self):
        self._off()
        self.assertFalse(self.pa.wait_until_quiet(self.db, "sync"))
        self.assertAlmostEqual(60.0, sum(self.slept), delta=1.0)

    def test_job_pause_waits_without_spending_and_can_be_cancelled(self):
        pause = self.pa.JobPause(self.db, "sync")
        self.assertTrue(pause.would_wait())
        self.ends_after = 10
        self.assertTrue(pause())
        self.assertEqual(0.0, pause.spent, "a protected recording costs the budget nothing")
        self.recording, self.live, self.ends_after = True, True, None
        cancelled = self.pa.JobPause(self.db, "sync", cancel_check=lambda: True)
        self.assertFalse(cancelled())
        self.assertEqual([], self.slept[10:], "cancelled at once")

    def test_job_pause_with_budget_spent_still_waits_for_a_recording(self):
        pause = self.pa.JobPause(self.db, "sync")
        pause.spent = pause.limit
        self.ends_after = 4
        self.assertTrue(pause(), "quiet once the recording ended")
        self.assertEqual(4, len(self.slept))

    def test_a_recording_that_starts_during_a_viewer_wait_pauses_the_budget(self):
        pause = self.pa.JobPause(self.db, "sync")
        self.recording = False
        calls = {"n": 0}
        real = self.pa.recording_protected

        def flips(db):
            calls["n"] += 1
            if calls["n"] == 3:
                self.recording = True       # starts during the wait
                self.ends_after = len(self.slept) + 5
            return real(db)
        with mock.patch.object(self.pa, "recording_protected", flips):
            self.live_after = False
            pause()
        self.assertEqual(60.0, pause.spent + 30.0, "only the 30 s before the recording counted")
        self.assertGreater(sum(self.slept), 60.0, "and yet it waited past the budget, for the recording")

    def test_wait_for_recordings(self):
        self.ends_after = 2
        self.assertTrue(self.pa.wait_for_recordings(self.db, "EPG"))
        self.assertEqual(2, len(self.slept))
        self.recording = True
        self.assertFalse(self.pa.wait_for_recordings(self.db, "EPG", cancel_check=lambda: True))
        self._off()
        n = len(self.slept)
        self.assertTrue(self.pa.wait_for_recordings(self.db, "EPG"))
        self.assertEqual(n, len(self.slept))

    def test_buttons_answer_503(self):
        with self.assertRaises(HTTPException) as cm:
            self.pa.refuse_while_recording(self.db, "Testing the provider")
        self.assertEqual(503, cm.exception.status_code)
        self.recording = False
        self.pa.refuse_while_recording(self.db, "Testing the provider")   # no raise
        self.recording = True
        self._off()
        self.pa.refuse_while_recording(self.db, "Testing the provider")

    def test_every_button_that_contacts_the_provider_is_guarded(self):
        """Source check: each interactive route that makes a provider request
        refuses first; the nightly EPG download waits."""
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        livetv_src = (root / "routers" / "livetv.py").read_text()
        for what in ("Testing the provider", "A channel group sync", "A channel sync", "An EPG sync"):
            self.assertIn(f'refuse_while_recording(db, "{what}")', livetv_src)
        self.assertIn("recording protection is on — not downloading the guide now", livetv_src)
        providers_src = (root / "routers" / "providers.py").read_text()
        for what in ("Testing the provider", "Fetching the provider's categories", "A sync preview"):
            self.assertIn(f'refuse_while_recording(db, "{what}")', providers_src)
        self.assertIn('refuse_while_recording(db, "A stream check")', (root / "routers" / "health.py").read_text())
        radarr_src = (root / "routers" / "radarr.py").read_text()
        for what in ("A provider migration preview", "A provider migration"):
            self.assertIn(f'refuse_while_recording(db, "{what}")', radarr_src)
        main_src = (root / "main.py").read_text()
        self.assertIn('wait_for_recordings(db, "the scheduled EPG sync",', main_src)
        self.assertIn("max_seconds=EPG_WAIT_FOR_RECORDING_SECONDS", main_src)
        # the wait sits just before the download, not before the whole EPG step
        self.assertLess(main_src.index('wait_for_recordings(db, "the scheduled EPG sync"'),
                        main_src.index("if _run_epg_sync_background(provider_data):"))
        self.assertLess(main_src.index("for lp in live_providers:"),
                        main_src.index('wait_for_recordings(db, "the scheduled EPG sync"'))


class BoundedWaits(unittest.TestCase):
    """Review F4: the nightly EPG wait is capped; a protected wait is not a
    stuck sync."""
    setUp = BackgroundWork.setUp
    _off = BackgroundWork._off

    def test_wait_for_recordings_gives_up_after_its_cap(self):
        self.assertFalse(self.pa.wait_for_recordings(self.db, "EPG", max_seconds=90))
        self.assertAlmostEqual(90.0, sum(self.slept), delta=0.1)
        self.ends_after = len(self.slept) + 1
        self.assertTrue(self.pa.wait_for_recordings(self.db, "EPG", max_seconds=90))

    def test_the_protected_wait_is_accounted(self):
        self.pa.reset_protected_wait_seconds()
        seen = []
        real_sleep = self.pa.time.sleep

        def sleep_and_look(s):
            seen.append(self.pa.protected_wait_state(7)[0])
            real_sleep(s)
        self.ends_after = 3
        with mock.patch.object(self.pa.time, "sleep", sleep_and_look):
            self.assertTrue(self.pa.wait_for_recordings(self.db, "sync", run_id=7))
        self.assertEqual([True, True, True], seen, "waiting while it sleeps")
        self.assertEqual((False, 90.0), self.pa.protected_wait_state(7))
        self.assertEqual((False, 0.0), self.pa.protected_wait_state(8), "booked to its own run only")
        self.pa.forget_protected_waits(keep=[])
        self.assertEqual((False, 0.0), self.pa.protected_wait_state(7))

    def _stale_run(self, hours):
        from datetime import datetime, timedelta
        from models.database import SyncRun, Provider
        p = Provider(name="P", server_url="http://provider.test", username="u", password="p")
        self.db.add(p)
        self.db.commit()
        run = SyncRun(provider_id=p.id, status="running", started_at=datetime.utcnow() - timedelta(hours=hours))
        self.db.add(run)
        self.db.commit()
        return run

    def test_a_run_waiting_for_a_protected_recording_is_not_failed_as_stuck(self):
        from routers import sync as sync_router
        run = self._stale_run(10)          # past 4 h + the 60 s budget
        with mock.patch.object(self.pa, "protected_wait_state", lambda rid=None: (rid == run.id, 0.0)):
            sync_router.get_sync_status(self.db)
        self.db.refresh(run)
        self.assertEqual("running", run.status)
        with mock.patch.object(self.pa, "protected_wait_state", lambda rid=None: (False, 7 * 3600.0 if rid == run.id else 0.0)):
            sync_router.get_sync_status(self.db)
        self.db.refresh(run)
        self.assertEqual("running", run.status, "7 h spent waiting for recordings extend the cutoff")
        with mock.patch.object(self.pa, "protected_wait_state", lambda rid=None: (False, 0.0)):
            sync_router.get_sync_status(self.db)
        self.db.refresh(run)
        self.assertEqual("failed", run.status, "without that it is stuck, as before")


class ProtectedWaitIsWallTimePerRun(unittest.TestCase):
    """Review R1: concurrent waiters count wall time once, per run; a run
    that is stuck (not waiting) is still failed while another job waits."""

    def setUp(self):
        import services.provider_activity as pa
        self.pa = pa
        pa.reset_protected_wait_seconds()
        self.addCleanup(pa.reset_protected_wait_seconds)

    def test_three_concurrent_waiters_count_one_second(self):
        import threading, time as _t
        release = threading.Event()
        with mock.patch.object(self.pa, "recording_protected", lambda db: not release.is_set()):
            ts = [threading.Thread(target=self.pa.wait_for_recordings, args=(None, "sync"),
                                   kwargs={"poll_seconds": 0.02, "run_id": 5}) for _ in range(3)]
            t0 = _t.monotonic()
            for t in ts:
                t.start()
            _t.sleep(1.0)
            release.set()
            for t in ts:
                t.join(5)
            wall = _t.monotonic() - t0
        waiting, seconds = self.pa.protected_wait_state(5)
        self.assertFalse(waiting)
        self.assertAlmostEqual(1.0, seconds, delta=0.25)
        self.assertLessEqual(seconds, wall + 0.01, "never more than the wall time")

    def test_a_stuck_run_is_failed_while_another_run_waits(self):
        from datetime import datetime, timedelta
        from models.database import SyncRun, Provider
        from routers import sync as sync_router
        import threading
        db = _fresh_db()
        p = Provider(name="P", server_url="http://provider.test", username="u", password="p")
        db.add(p)
        db.commit()
        stuck = SyncRun(provider_id=p.id, status="running", started_at=datetime.utcnow() - timedelta(hours=30))
        waiter = SyncRun(provider_id=p.id, status="running", started_at=datetime.utcnow() - timedelta(hours=30))
        db.add_all([stuck, waiter])
        db.commit()
        release = threading.Event()
        with mock.patch.object(self.pa, "recording_protected", lambda d: not release.is_set()):
            t = threading.Thread(target=self.pa.wait_for_recordings, args=(None, "sync"),
                                 kwargs={"poll_seconds": 0.02, "run_id": waiter.id})
            t.start()
            try:
                for _ in range(100):
                    if self.pa.protected_wait_state(waiter.id)[0]:
                        break
                    import time as _t; _t.sleep(0.01)
                sync_router.get_sync_status(db)
            finally:
                release.set()
                t.join(5)
        db.refresh(stuck)
        db.refresh(waiter)
        self.assertEqual("failed", stuck.status, "not waiting: stuck, whatever else waits")
        self.assertEqual("running", waiter.status, "waiting for a protected recording right now")


class ButtonsRefuse(unittest.TestCase):
    """The guarded routes themselves answer 503 during a protected recording."""

    def test_live_provider_test_and_syncs(self):
        livetv = _livetv()
        db = _fresh_db()
        from models.database import Provider, set_setting
        db.add(Provider(name="P", server_url="http://provider.test", username="u", password="p"))
        set_setting(db, "livetv_protect_recordings", "true")
        db.commit()
        fake_slots = types.SimpleNamespace(recording_active=lambda: True)
        with mock.patch.object(livetv, "_stream_slots", fake_slots), \
                mock.patch("requests.get", side_effect=AssertionError("provider contacted")), \
                mock.patch("requests.Session.get", side_effect=AssertionError("provider contacted")):
            for call in (lambda: livetv.test_live_provider(db), lambda: livetv.sync_live_groups(1, db),
                         lambda: livetv.sync_live_channels(1, db), lambda: livetv.sync_epg(1, db)):
                with self.assertRaises(HTTPException) as cm:
                    call()
                self.assertEqual(503, cm.exception.status_code)
            from routers import providers, radarr
            db.add(Provider(name="Q", server_url="http://provider2.test", username="u", password="p"))
            db.commit()
            for call in (lambda: providers.test_provider(1, db), lambda: providers.fetch_categories(1, db),
                         lambda: radarr.preview_migration_endpoint(1, 2, db),
                         lambda: radarr.run_migration(radarr.MigrateRequest(from_provider_id=1, to_provider_id=2,
                                                                            dry_run=True), db)):
                with self.assertRaises(HTTPException) as cm:
                    call()
                self.assertEqual(503, cm.exception.status_code)


if __name__ == "__main__":
    unittest.main()

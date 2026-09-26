"""Recording protection, review findings F1/F2 (independent review of 7235940).

Run from the tentacle/ directory:  python -m unittest discover -s tests

F1: a viewer let through while Jellyfin could not answer -- or left running
because protection was switched on from a stale answer -- was never stopped
once Jellyfin answered normally again: the way was cleared only when a
lookup changed a rank. F2: a tuner open under protection reused a lookup
that was already in flight, issued before a "record now" timer existed, and
refused the new recording on that answer (Jellyfin retries a failed
recording open only once a minute). Also: a pull that opened after a lookup
was issued is never stopped on that lookup's answer.
"""
import asyncio, tempfile, unittest
from unittest import mock
from fastapi import HTTPException


def _fresh_db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    global SM
    SM = sessionmaker(bind=engine)
    return SM()


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from routers import livetv
        self.livetv = livetv
        livetv._stream_slots = livetv._StreamSlots()
        livetv._shared_streams.clear(); livetv._pending_opens.clear(); livetv._reserved_channels.clear()
        livetv._recording_cache.update(at=-1e9, sids=set(), pending=None, failures=0, retry_at=-1e9)
        livetv._recording_cache.pop("answer_issued_at", None)
        livetv._recording_cache.pop("pending_issued", None)
        self.db = _fresh_db()
        from models.database import LiveChannel, Provider, set_setting
        prov = Provider(name="P", server_url="http://provider.test", username="u", password="p")
        self.db.add(prov); self.db.commit()
        self.a = LiveChannel(provider_id=prov.id, name="A", stream_id="100", stream_url="http://provider.test/live/u/p/100.ts")
        self.b = LiveChannel(provider_id=prov.id, name="B", stream_id="200", stream_url="http://provider.test/live/u/p/200.ts")
        self.c = LiveChannel(provider_id=prov.id, name="C", stream_id="300", stream_url="http://provider.test/live/u/p/300.ts")
        self.db.add_all([self.a, self.b, self.c])
        set_setting(self.db, "jellyfin_url", "http://jf.test"); set_setting(self.db, "jellyfin_api_key", "k")
        set_setting(self.db, "livetv_protect_recordings", "true"); self.db.commit()
        p = mock.patch.object(livetv, "SessionLocal", lambda: SM()); p.start(); self.addCleanup(p.stop)



class ReviewF1F2(_Base):
    async def test_F1_uncertain_start_viewer_never_stopped_later(self):
        """Viewer on 300 running. Recording on 100 starts while Jellyfin is slow
        (cached answer already said 100 is due). films_only -> viewer kept.
        Later Jellyfin answers normally: is the viewer ever stopped?"""
        lt = self.livetv
        s = lt._stream_slots
        s.protect = True
        v = await s.acquire_lease(6, 0.01, "live", f"channel:{self.c.id}", stream_key="300")
        stopped = []
        v.on_preempt = lambda: stopped.append("viewer")
        loop = asyncio.get_running_loop()
        lt._recording_cache.update(at=loop.time() - 1, sids={"100"})
        slow = asyncio.Event()

        def slow_lookup(u, k):
            import time; time.sleep(4.0)   # > _RECORDING_LOOKUP_WAIT
            return {"100"}
        seen = []

        async def fake_inner(channel_id, *a, **k):
            seen.append(channel_id)
            raise HTTPException(502, "stop here")
        with mock.patch.object(lt, "_recording_stream_ids_from_jellyfin", slow_lookup), \
                mock.patch.object(lt, "_stream_proxy_inner", fake_inner), \
                mock.patch.object(lt, "lan_origin_guard", lambda *a, **k: (lambda u: True)):
            # hold the recording's lease: grab the lease the way _open_shared_upstream does
            rec_ids = await lt._recording_channel_ids(self.db, force=True)
            certain = lt._recording_answer_is_current()
            kind = "recording" if self.a.id in rec_ids else "live"
            rec = await s.acquire_lease(6, 0.01, kind, f"channel:{self.a.id}", stream_key="100", certain=certain)
            # let the slow lookup finish, then several refresher rounds with Jellyfin healthy
            await asyncio.sleep(1.5)
        with mock.patch.object(lt, "_recording_stream_ids_from_jellyfin", lambda u, k: {"100"}):
            for _ in range(3):
                await lt._refresh_recordings_once(); await asyncio.sleep(0.05)
        self.assertTrue(stopped, "viewer on another channel runs next to the recording for its whole length")

    async def test_F2_inflight_lookup_refuses_a_new_recording(self):
        """Recording on 100 runs. The refresher's lookup is in flight (Jellyfin
        answered before the 'record now' timer on 200 existed). The recording's
        own open on 200 awaits that stale answer and is refused."""
        lt = self.livetv
        s = lt._stream_slots
        await s.acquire_lease(6, 0.01, "recording", f"channel:{self.a.id}", stream_key="100")
        s.protect = True
        loop = asyncio.get_running_loop()
        lt._recording_cache.update(at=loop.time() - 5, sids={"100"})
        jellyfin_now = {"100"}

        def lookup(u, k):
            snap = set(jellyfin_now)   # Jellyfin computes its answer now...
            import time; time.sleep(0.3)   # ...and it arrives a moment later
            return snap
        with mock.patch.object(lt, "_recording_stream_ids_from_jellyfin", lookup), \
                mock.patch.object(lt, "lan_origin_guard", lambda *a, **k: (lambda u: True)):
            refresher = asyncio.ensure_future(lt._refresh_recordings_once())
            await asyncio.sleep(0.1)          # lookup issued, Jellyfin answered {"100"}
            jellyfin_now.add("200")           # timer created; Jellyfin opens the tuner at once
            seen = []

            async def fake_inner(channel_id, *a, **k):
                seen.append(channel_id); raise HTTPException(502, "stop here")
            with mock.patch.object(lt, "_stream_proxy_inner", fake_inner):
                try:
                    await lt.stream_proxy(self.b.id, self.db)
                except HTTPException as e:
                    code = e.status_code
            await refresher
        self.assertEqual(502, code, "the recording was refused (503) on an answer older than its timer")


class ReviewF1Toggle(_Base):
    async def test_F1b_turning_on_mid_recording_leaves_viewers(self):
        lt = self.livetv
        s = lt._stream_slots
        from models.database import set_setting
        set_setting(self.db, "livetv_protect_recordings", "false"); self.db.commit()
        rec = await s.acquire_lease(6, 0.01, "recording", f"channel:{self.a.id}", stream_key="100")
        v = await s.acquire_lease(6, 0.01, "live", f"channel:{self.c.id}", stream_key="300")
        f = await s.acquire_lease(6, 0.01, "vod", "vod:1")
        stopped = []
        v.on_preempt = lambda: stopped.append("viewer")
        f.on_preempt = lambda: stopped.append("film")
        loop = asyncio.get_running_loop()
        # the refresher's previous answer completed one TTL + a bit ago (it sleeps TTL between rounds)
        lt._recording_cache.update(at=loop.time() - lt._RECORDING_LOOKUP_TTL - 0.2, sids={"100"})
        set_setting(self.db, "livetv_protect_recordings", "true"); self.db.commit()
        with mock.patch.object(lt, "_recording_stream_ids_from_jellyfin", lambda u, k: {"100"}):
            for _ in range(3):
                await lt._refresh_recordings_once(); await asyncio.sleep(0.05)
        self.assertIn("viewer", stopped)
        self.assertIn("film", stopped)
        self.assertEqual([rec.id], list(s.leases))


class ReviewIssuedBefore(_Base):
    async def test_a_pull_opened_after_a_lookup_was_issued_is_not_stopped_by_its_answer(self):
        """A recording let through as 'live' while an older lookup was still
        in flight must not be stopped when that older answer (which cannot
        know its timer) arrives."""
        lt = self.livetv
        s = lt._stream_slots
        s.protect = True
        await s.acquire_lease(6, 0.01, "recording", f"channel:{self.a.id}", stream_key="100")
        release = asyncio.Event()
        import threading
        gate = threading.Event()

        def slow(u, k):
            gate.wait(5)
            return {"100"}          # issued before the timer on 300 existed
        with mock.patch.object(lt, "_recording_stream_ids_from_jellyfin", slow):
            refresher = asyncio.ensure_future(lt._refresh_recordings_once())
            await asyncio.sleep(0.05)
            late = await s.acquire_lease(6, 0.01, "live", f"channel:{self.c.id}", stream_key="300", certain=False)
            late.on_preempt = lambda: self.fail("stopped on an answer older than the pull")
            gate.set()
            await refresher
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertIn(late.id, s.leases)
        # the next, newer answer does stop it (it is not a recording after all)
        stopped = []
        late.on_preempt = lambda: stopped.append(1)
        with mock.patch.object(lt, "_recording_stream_ids_from_jellyfin", lambda u, k: {"100"}):
            await lt._refresh_recordings_once()
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertEqual([1], stopped)

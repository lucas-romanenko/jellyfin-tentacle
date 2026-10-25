"""A viewer's stream starts on a keyframe with sound; a recording's is untouched.

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_viewer_start

2026-10-24: TSN 2's audio ran ~9 s ahead of its video and Jellyfin Web froze
at ~6 s on it, every time (services/ts_start has the story). Every viewer of
a shared upstream gets services/ts_start's start, the first one and those
who join later (from the upstream's last bytes, without waiting for new
ones); a recording gets the provider's bytes exactly as before.
"""
import asyncio
import unittest
from unittest import mock

from fastapi.responses import StreamingResponse
from tmp_dirs import temp_dir

from services.ts_start import AvStart
from test_ts_start import V, pes_starts, stream

PIECE = 188 * 88


def _fresh_db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{temp_dir()}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _gated(data):
    g = AvStart()
    return g.feed(data) + g.flush()


class ViewerStart(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        livetv._stream_slots = livetv._StreamSlots()
        self.addCleanup(lambda: setattr(livetv, "_stream_slots", livetv._StreamSlots()))
        livetv._shared_lock = None
        livetv._shared_streams.clear()
        self.db = _fresh_db()
        from models.database import LiveChannel, Provider
        prov = Provider(name="P", server_url="http://provider.test", username="u", password="p")
        self.db.add(prov)
        self.db.commit()
        ch = LiveChannel(provider_id=prov.id, name="TSN 2",
                         stream_url="http://provider.test/live/u/p/1.ts", enabled=True)
        self.db.add(ch)
        self.db.commit()
        self.cid = ch.id
        self.src = stream(seconds=40, skew=9.0)
        self.pieces = [self.src[i:i + PIECE] for i in range(0, len(self.src), PIECE)]
        self.half = len(self.pieces) // 2
        self.recording = set()

    def _drive(self, body):
        livetv = self.livetv
        self.go_on = asyncio.Event()
        self.halfway = asyncio.Event()

        async def fake_inner(channel_id, ua, url, release, guard=None, **kw):
            async def gen():
                try:
                    for k, piece in enumerate(self.pieces):
                        if k == self.half:
                            self.halfway.set()
                            await self.go_on.wait()
                        yield piece
                        await asyncio.sleep(0)
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def recording_ids(db, *a, **k):
            return set(self.recording)

        async def drive():
            self.go_on, self.halfway = asyncio.Event(), asyncio.Event()
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "_recording_channel_ids", recording_ids), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard", lambda *a, **k: (lambda url: True)):
                return await body()

        return asyncio.run(drive())

    async def _read_all(self, resp):
        out = b""
        async for piece in resp.body_iterator:
            out += piece
        return out

    def test_the_first_viewer_starts_on_a_keyframe_with_sound(self):
        async def body():
            self.halfway.set()
            resp = await self.livetv.stream_proxy(self.cid, self.db)
            self.go_on.set()
            return await self._read_all(resp)

        out = self._drive(body)
        self.assertEqual(out, _gated(self.src))
        first_v = next(t for pid, t in pes_starts(out) if pid == V)
        self.assertGreaterEqual(first_v, 1008.0)

    def test_a_recording_gets_the_providers_bytes_unchanged(self):
        self.recording = {self.cid}

        async def body():
            resp = await self.livetv.stream_proxy(self.cid, self.db)
            self.go_on.set()
            return await self._read_all(resp)

        self.assertEqual(self._drive(body), self.src)

    def _join(self, joiner_is_recording, first_is_recording=False):
        async def body():
            if first_is_recording:
                self.recording = {self.cid}
            first = await self.livetv.stream_proxy(self.cid, self.db)
            reader = asyncio.ensure_future(self._read_all(first))
            await self.halfway.wait()
            for _ in range(20):
                await asyncio.sleep(0)      # the first half reaches the pump
            if joiner_is_recording:
                self.recording = {self.cid}
            second = await self.livetv.stream_proxy(self.cid, self.db)
            waiting = second._q.qsize()     # what it has before the provider sends more
            self.go_on.set()
            out = await self._read_all(second)
            await reader
            return waiting, out

        return self._drive(body)

    def test_a_viewer_joining_starts_from_what_already_came(self):
        waiting, out = self._join(joiner_is_recording=False)
        self.assertGreater(waiting, 0, "the joining viewer waited for new bytes from the provider")
        first_v = next(t for pid, t in pes_starts(out) if pid == V)
        self.assertGreaterEqual(first_v, 1008.0)
        # It starts inside the backlog, before the bytes that came after it joined.
        joined_at = next(t for pid, t in pes_starts(b"".join(self.pieces[self.half:])) if pid == V)
        self.assertLess(first_v, joined_at)
        self.assertEqual(out, _gated(self.src))

    def test_a_recording_joining_gets_only_what_comes_after_unchanged(self):
        waiting, out = self._join(joiner_is_recording=True)
        self.assertEqual(waiting, 0, "a joining recording was given earlier bytes")
        self.assertEqual(out, b"".join(self.pieces[self.half:]))

    def test_a_viewer_joining_a_recording_gets_a_viewers_start(self):
        # Someone tunes to a channel while it records: the pull is the
        # recording's, so the newcomer is watching (it used to get the
        # recording's start: no backlog, no keyframe).
        waiting, out = self._join(joiner_is_recording=True, first_is_recording=True)
        self.assertEqual(self.livetv._stream_slots.lease_for(f"channel:{self.cid}"), None)  # all ended
        self.assertGreater(waiting, 0, "the viewer joining a recording waited for new bytes from the provider")
        first_v = next(t for pid, t in pes_starts(out) if pid == V)
        self.assertGreaterEqual(first_v, 1008.0)
        joined_at = next(t for pid, t in pes_starts(b"".join(self.pieces[self.half:])) if pid == V)
        self.assertLess(first_v, joined_at)


if __name__ == "__main__":
    unittest.main()

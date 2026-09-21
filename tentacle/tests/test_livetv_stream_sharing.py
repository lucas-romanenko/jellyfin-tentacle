"""One upstream connection per channel, however many clients want it.

Run from the tentacle/ directory:  python -m unittest discover -s tests

An IPTV account caps simultaneous connections (Xtream's max_connections;
commonly 1 or 2). Jellyfin opens a separate tuner stream for every recording
and every viewer, so recording a channel while watching that same channel is
two requests to /api/live/stream/{id}. If each opens its own upstream fetch
chain, the allowance is spent twice on byte-identical data and the provider
starts answering 509 -- which is what truncates recordings in practice.

Reading one channel N times must cost the provider one connection, with all N
clients fed from it. Different channels are NOT shared: they genuinely need
their own upstream.
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
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class SharedUpstream(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        self.livetv = livetv
        # These are bound to a running loop, and every test runs its own.
        livetv._stream_slots = livetv._StreamSlots()
        livetv._shared_lock = None
        livetv._shared_streams.clear()
        self.db = _fresh_db()

    def _channel(self, name="C"):
        from models.database import LiveChannel, Provider
        prov = self.db.query(Provider).first()
        if prov is None:
            prov = Provider(name="P", server_url="http://provider.test",
                            username="u", password="p")
            self.db.add(prov)
            self.db.commit()
        ch = LiveChannel(provider_id=prov.id, name=name,
                         stream_url="http://provider.test/live/u/p/1.m3u8")
        self.db.add(ch)
        self.db.commit()
        return ch.id

    def _run(self, body):
        """Drive the route with a scripted upstream; returns (bodies, upstreams)."""
        livetv = self.livetv
        upstreams = []

        async def fake_inner(channel_id, ua, url, release, guard=None):
            upstreams.append(channel_id)

            async def gen():
                try:
                    for piece in (b"SEG1", b"SEG2"):
                        yield piece
                        await asyncio.sleep(0)
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def drive():
            # lan_origin_guard() hands back the real is_safe_url for a public
            # provider, so patching the module name alone is not enough.
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard",
                                      lambda *a, **k: (lambda url: True)):
                return await body()

        return asyncio.run(drive()), upstreams

    async def _read(self, resp, limit=10):
        out = b""
        for _ in range(limit):
            try:
                out += await resp.body_iterator.__anext__()
            except StopAsyncIteration:
                break
        return out

    def test_two_clients_on_one_channel_open_one_upstream(self):
        cid = self._channel()

        async def body():
            a = await self.livetv.stream_proxy(cid, self.db)
            b = await self.livetv.stream_proxy(cid, self.db)
            return await asyncio.gather(self._read(a), self._read(b))

        (body_a, body_b), upstreams = self._run(body)
        self.assertEqual(
            len(upstreams), 1,
            f"{len(upstreams)} upstream connections were opened for 2 clients on "
            f"one channel; on a max_connections=1 account that is a 509")
        self.assertIn(b"SEG1", body_a)
        self.assertIn(b"SEG1", body_b, "the attached client got no data")

    def test_two_different_channels_still_get_their_own_upstream(self):
        c1, c2 = self._channel("A"), self._channel("B")

        async def body():
            a = await self.livetv.stream_proxy(c1, self.db)
            b = await self.livetv.stream_proxy(c2, self.db)
            return await asyncio.gather(self._read(a), self._read(b))

        _, upstreams = self._run(body)
        self.assertEqual(sorted(upstreams), sorted([c1, c2]),
                         "different channels must not be conflated")

    def test_the_upstream_is_released_when_the_last_client_leaves(self):
        cid = self._channel()

        async def body():
            a = await self.livetv.stream_proxy(cid, self.db)
            await self._read(a, limit=1)
            await a.body_iterator.aclose()
            await asyncio.sleep(0)
            return self.livetv._shared_streams.get(cid)

        leftover, _ = self._run(body)
        self.assertIsNone(leftover, "a finished channel stayed registered, so the "
                                    "next viewer would attach to a dead stream")

    def test_a_second_client_does_not_consume_a_concurrency_slot(self):
        """The cap counts upstream connections, not viewers."""
        cid = self._channel()

        async def body():
            resps = [await self.livetv.stream_proxy(cid, self.db) for _ in range(10)]
            return len(resps), self.livetv._stream_slots.active

        (n, in_use), upstreams = self._run(body)
        self.assertEqual(n, 10)
        self.assertEqual(len(upstreams), 1)
        self.assertEqual(in_use, 1,
                         "viewers of an already-open channel used up capacity")



class ConcurrentOpeners(SharedUpstream):
    """Two clients arriving while the channel is still being OPENED must end up
    on one upstream. The open takes a while (slot wait, redirect chain, 509
    backoff), and it happened outside the registry lock: the second arrival
    found no entry, took its own slot and opened a second provider connection."""

    def _run_slow(self, body, delay=0.05):
        livetv = self.livetv
        upstreams = []

        async def slow_inner(channel_id, ua, url, release, guard=None):
            await asyncio.sleep(delay)          # the open takes time
            upstreams.append(channel_id)

            async def gen():
                # A live stream keeps going: give the second client time to
                # attach before the second segment (a fake that ends at once
                # would end the channel before anyone else could join it).
                try:
                    yield b"SEG1"
                    await asyncio.sleep(delay)
                    yield b"SEG2"
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def drive():
            with mock.patch.object(livetv, "_stream_proxy_inner", slow_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard",
                                      lambda *a, **k: (lambda url: True)):
                return await body()

        return asyncio.run(drive()), upstreams

    def test_clients_arriving_during_the_open_attach_to_it(self):
        cid = self._channel()

        async def body():
            a, b = await asyncio.gather(self.livetv.stream_proxy(cid, self.db),
                                        self.livetv.stream_proxy(cid, self.db))
            return await asyncio.gather(self._read(a), self._read(b))

        (body_a, body_b), upstreams = self._run_slow(body)
        self.assertEqual(len(upstreams), 1,
                         f"{len(upstreams)} upstreams opened for 2 clients that arrived together")
        self.assertIn(b"SEG1", body_a)
        self.assertIn(b"SEG2", body_b, "the client that arrived during the open got no data")
        self.assertEqual(self.livetv._pending_opens, {})

    def test_a_failed_open_does_not_strand_the_next_client(self):
        cid = self._channel()
        livetv = self.livetv
        calls = []

        async def failing_then_ok(channel_id, ua, url, release, guard=None):
            calls.append(1)
            await asyncio.sleep(0.02)
            if len(calls) == 1:
                release()
                from fastapi import HTTPException
                raise HTTPException(502, "provider refused")

            async def gen():
                try:
                    yield b"SEG1"
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def body():
            with mock.patch.object(livetv, "_stream_proxy_inner", failing_then_ok), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard", lambda *a, **k: (lambda url: True)):
                results = await asyncio.gather(livetv.stream_proxy(cid, self.db),
                                               livetv.stream_proxy(cid, self.db),
                                               return_exceptions=True)
                return results

        results = asyncio.run(body())
        kinds = sorted(type(r).__name__ for r in results)
        # One failed with the provider's error, the other opened its own and streams.
        self.assertIn("HTTPException", kinds)
        self.assertIn("_SubscriberResponse", kinds)
        self.assertEqual(livetv._pending_opens, {})


if __name__ == "__main__":
    unittest.main()

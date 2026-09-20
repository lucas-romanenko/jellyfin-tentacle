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
        livetv._stream_semaphore = None
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
            return len(resps), self.livetv._get_stream_semaphore()._value

        (n, remaining), upstreams = self._run(body)
        self.assertEqual(n, 10)
        self.assertEqual(len(upstreams), 1)
        self.assertEqual(remaining, self.livetv._MAX_CONCURRENT_STREAMS - 1,
                         "viewers of an already-open channel used up capacity")


if __name__ == "__main__":
    unittest.main()

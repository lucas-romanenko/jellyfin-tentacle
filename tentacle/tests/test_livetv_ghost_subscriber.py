"""A client that goes away before its first byte must not hold the channel open.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A viewer is subscribed to the shared upstream when the response OBJECT is built,
but was only unsubscribed from the body generator's `finally`. An async
generator that is never started never runs its `finally`, so a client that
disconnected between the headers and the first chunk stayed subscribed for
ever: the upstream kept pulling from the provider with nobody watching, and its
concurrency slot was never returned. Enough of those and every new stream is
refused.
"""
import asyncio
import unittest

from unittest import mock

from fastapi.responses import StreamingResponse

import test_livetv_stream_sharing as _sharing


def _scope():
    return {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"",
            "asgi": {"version": "3.0", "spec_version": "2.3"}}


class GhostSubscriber(unittest.TestCase):
    # Borrow the sharing suite's fixtures without inheriting (and re-running) its tests.
    setUp = _sharing.SharedUpstream.setUp
    _channel = _sharing.SharedUpstream._channel

    def _run(self, body):
        """Like the sharing suite's runner, but the upstream is LIVE: it never
        ends by itself, which is the only case where a ghost can be seen."""
        livetv = self.livetv
        upstreams = []

        async def fake_inner(channel_id, ua, url, release, guard=None, **kw):
            upstreams.append(channel_id)

            async def gen():
                try:
                    n = 0
                    while True:
                        n += 1
                        yield b"SEG%d" % n
                        await asyncio.sleep(0.005)
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def drive():
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard",
                                      lambda *a, **k: (lambda url: True)):
                return await body()

        return asyncio.run(drive()), upstreams

    def _serve_to_a_client_that_is_already_gone(self, cid):
        """Drive the response the way the ASGI server does, with a client whose
        socket fails on the very first send (the headers)."""
        async def body():
            resp = await self.livetv.stream_proxy(cid, self.db)

            async def receive():
                await asyncio.sleep(3600)

            async def send(message):
                raise OSError("client went away")

            try:
                await resp(_scope(), receive, send)
            except BaseException:
                pass
            await asyncio.sleep(0.05)     # let a cancelled pump finish unwinding
            shared = self.livetv._shared_streams.get(cid)
            return (shared is not None and len(shared.subscribers)), self.livetv._stream_slots.active
        return self._run(body)[0]

    def test_no_ghost_subscriber_is_left_behind(self):
        ghosts, _ = self._serve_to_a_client_that_is_already_gone(self._channel())
        self.assertFalse(ghosts, "a client that never read a byte is still subscribed, so the "
                                 "upstream will be pulled from the provider for ever")

    def test_the_concurrency_slot_comes_back(self):
        _, active = self._serve_to_a_client_that_is_already_gone(self._channel())
        self.assertEqual(0, active, "the slot leaked; enough of these and every stream is refused")

    def test_a_second_viewer_that_vanishes_does_not_take_the_first_one_down(self):
        cid = self._channel()

        async def body():
            first = await self.livetv.stream_proxy(cid, self.db)
            it = first.body_iterator.__aiter__()
            got = await it.__anext__()                      # first viewer is really watching
            second = await self.livetv.stream_proxy(cid, self.db)

            async def receive():
                await asyncio.sleep(3600)

            async def send(message):
                raise OSError("client went away")

            try:
                await second(_scope(), receive, send)
            except BaseException:
                pass
            shared = self.livetv._shared_streams.get(cid)
            n = len(shared.subscribers) if shared else -1
            await it.aclose()
            return got, n

        (got, n), _ = self._run(body)
        self.assertTrue(got.startswith(b"SEG"))
        self.assertEqual(1, n, "only the viewer that is actually watching should remain")


if __name__ == "__main__":
    unittest.main()

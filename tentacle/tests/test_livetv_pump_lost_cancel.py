"""The shared pump must stop even when its one cancel() is lost.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Seen live (QA stack, real uvicorn, real sockets): open /api/live/stream/<id> on
a few channels and drop the connection at once. The ghost subscriber was removed
and /api/live/capacity went back to 0 -- and the provider went on being pulled
for those channels, for good, by pumps that no longer had a subscriber, a
registry entry or a slot.

The pump is stopped with task.cancel(). At that moment it is opening the
connection for its first chunk, inside anyio's connect_tcp(), which cancels its
own cancel scope as soon as a connection attempt wins; a scope that is being
cancelled takes a CancelledError raised in its host task for its own and
swallows it. A cancel() of ours that lands in that window never reaches the
worker. The upstream here does the same thing: it eats the first CancelledError.
"""
import asyncio
import unittest

from unittest import mock

from fastapi.responses import StreamingResponse

import test_livetv_stream_sharing as _sharing
from test_livetv_ghost_subscriber import _scope


class PumpLosesItsCancel(unittest.TestCase):
    setUp = _sharing.SharedUpstream.setUp
    _channel = _sharing.SharedUpstream._channel

    def _ghost(self, settle=0.3):
        """One client that is gone before its first byte, on an upstream whose
        stack swallows the first cancellation. Returns what is left behind."""
        livetv = self.livetv
        cid = self._channel()
        seen = {"pulls": 0, "swallowed": 0, "ended": False}

        async def fake_inner(channel_id, ua, url, release, guard=None):
            async def gen():
                try:
                    while True:
                        seen["pulls"] += 1
                        yield b"SEG"
                        try:
                            await asyncio.sleep(0.005)
                        except asyncio.CancelledError:
                            if seen["swallowed"]:
                                raise
                            seen["swallowed"] += 1     # anyio's scope took it for its own
                finally:
                    seen["ended"] = True
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def drive():
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard",
                                      lambda *a, **k: (lambda url: True)), \
                    mock.patch.object(livetv, "_PUMP_RECANCEL_SECONDS", 0.02, create=True):
                resp = await livetv.stream_proxy(cid, self.db)
                await asyncio.sleep(0.02)              # the pump is running, mid-"connect"

                async def receive():
                    return {"type": "http.disconnect"}

                async def send(message):
                    pass

                try:
                    await resp(_scope(), receive, send)
                except BaseException:
                    pass
                await asyncio.sleep(settle)
                before = seen["pulls"]
                await asyncio.sleep(0.1)
                return dict(seen, still_pulling=seen["pulls"] > before,
                            active=livetv._stream_slots.active)

        return asyncio.run(drive())

    def test_the_cancel_really_was_lost(self):
        """Guards the fixture: if nothing is swallowed these tests prove nothing."""
        self.assertEqual(1, self._ghost()["swallowed"])

    def test_the_upstream_is_not_pulled_with_nobody_attached(self):
        left = self._ghost()
        self.assertFalse(left["still_pulling"],
                         "the provider is still being pulled for a channel nobody is on")

    def test_the_upstream_is_closed(self):
        left = self._ghost()
        self.assertTrue(left["ended"], "the upstream generator never ran its finally, so the "
                                       "provider connection stays open")
        self.assertEqual(0, left["active"])


if __name__ == "__main__":
    unittest.main()

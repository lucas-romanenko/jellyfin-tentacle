"""A guide preview the browser gave up on must not hold the channel open.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The web guide's muted preview (/api/live/preview/<item>/stream.mp4) joins the
channel through stream_proxy and wraps the subscription in an fMP4 stream
whose generator gives it back from its `finally`. A browser that dropped the
request while the channel was still opening (the next channel focused, the
guide closed, Play pressed) is already gone when the response starts:
Starlette then cancels the response before the generator ever runs, an async
generator that never started never runs its `finally`, and the subscription
stayed for ever. The upstream kept pulling from the provider with nobody
watching ("ghost streams", 27 Oct 2026: Cartoon Network for 25 minutes, on an
account that allows one connection, so it fought every channel opened after).

The same shape is guarded against at the shared-upstream level too: a
subscriber that takes nothing for _IDLE_SUBSCRIBER_SECONDS while its stream
flows is let go, and the upstream closes when it was the last.
"""
import asyncio
import unittest
from unittest import mock

from fastapi.responses import StreamingResponse

import test_livetv_stream_sharing as _sharing


def _scope():
    return {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"",
            "asgi": {"version": "3.0", "spec_version": "2.3"}}


class _Live(unittest.TestCase):
    setUp = _sharing.SharedUpstream.setUp
    _channel = _sharing.SharedUpstream._channel

    def _run(self, body):
        """A LIVE fake upstream (it never ends by itself), local only."""
        livetv = self.livetv

        async def fake_inner(channel_id, ua, url, release, guard=None, **kw):
            async def gen():
                try:
                    n = 0
                    while True:
                        n += 1
                        yield b"G" + b"%07d" % n
                        await asyncio.sleep(0.002)
                finally:
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def drive():
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "lan_origin_guard",
                                      lambda *a, **k: (lambda url: True)):
                return await body()

        return asyncio.run(drive())


class PreviewGhost(_Live):
    async def _preview(self, cid):
        import routers.multiview as R

        async def live_preview(**kw):
            return {"previews": True}

        user = mock.Mock(jellyfin_user_id="u1")
        with mock.patch.object(R, "_picks", lambda *a, **k: [(cid, "C", "c1", "item")]), \
                mock.patch.object(R.MV, "_ffmpeg", lambda: "/bin/false"), \
                mock.patch.object(self.livetv, "live_preview", live_preview):
            return await R.preview_stream("item", user=user, db=self.db)

    def _state(self, cid):
        shared = self.livetv._shared_streams.get(cid)
        return (len(shared.subscribers) if shared is not None else 0), self.livetv._stream_slots.active

    def test_a_preview_whose_browser_already_left_gives_the_channel_back(self):
        cid = self._channel()

        async def body():
            resp = await self._preview(cid)

            async def receive():
                return {"type": "http.disconnect"}

            async def send(message):
                raise OSError("client went away")

            try:
                await resp(_scope(), receive, send)
            except BaseException:
                pass
            await asyncio.sleep(0.05)     # let the cancelled pump unwind
            return self._state(cid)

        subscribers, active = self._run(body)
        self.assertEqual(0, subscribers, "the preview is still subscribed with no browser behind it")
        self.assertEqual(0, active, "the provider connection is still held for a preview nobody sees")

    def test_a_preview_joining_a_watched_channel_leaves_the_viewer_alone(self):
        cid = self._channel()

        async def body():
            first = await self.livetv.stream_proxy(cid, self.db)
            it = first.body_iterator.__aiter__()
            await it.__anext__()                      # someone is really watching
            resp = await self._preview(cid)

            async def receive():
                return {"type": "http.disconnect"}

            async def send(message):
                raise OSError("client went away")

            try:
                await resp(_scope(), receive, send)
            except BaseException:
                pass
            n, _ = self._state(cid)
            await it.aclose()
            return n

        self.assertEqual(1, self._run(body), "only the viewer that is actually watching should remain")


class IdleSubscriber(_Live):
    def test_a_subscriber_that_takes_nothing_is_let_go_and_the_upstream_closes(self):
        cid = self._channel()
        livetv = self.livetv

        async def body():
            with mock.patch.object(livetv, "_IDLE_SUBSCRIBER_SECONDS", 0.2):
                resp = await livetv.stream_proxy(cid, self.db)   # subscribed, never read
                shared = livetv._shared_streams.get(cid)
                self.assertIsNotNone(shared)
                with self.assertLogs("routers.livetv", "WARNING") as logs:
                    for _ in range(100):
                        await asyncio.sleep(0.02)
                        if shared._closed:
                            break
                    await asyncio.sleep(0.05)
                return shared._closed, cid in livetv._shared_streams, \
                    livetv._stream_slots.active, "\n".join(logs.output), resp

        closed, registered, active, log, _ = self._run(body)
        self.assertTrue(closed, "an upstream whose only client never takes anything keeps pulling")
        self.assertFalse(registered)
        self.assertEqual(0, active, "the provider connection was not given back")
        self.assertIn("took nothing", log)

    def test_a_reading_viewer_is_never_let_go(self):
        cid = self._channel()
        livetv = self.livetv

        async def body():
            with mock.patch.object(livetv, "_IDLE_SUBSCRIBER_SECONDS", 0.2):
                resp = await livetv.stream_proxy(cid, self.db)
                it = resp.body_iterator.__aiter__()
                loop = asyncio.get_running_loop()
                end = loop.time() + 0.8
                got = 0
                while loop.time() < end:
                    await it.__anext__()
                    got += 1
                shared = livetv._shared_streams.get(cid)
                alive = shared is not None and not shared._closed and len(shared.subscribers) == 1
                await it.aclose()
                return alive, got

        alive, got = self._run(body)
        self.assertTrue(alive, "a viewer that keeps reading was dropped")
        self.assertGreater(got, 10)

    def test_an_idle_one_is_let_go_while_the_reader_keeps_the_channel(self):
        cid = self._channel()
        livetv = self.livetv

        async def body():
            with mock.patch.object(livetv, "_IDLE_SUBSCRIBER_SECONDS", 0.2):
                watching = await livetv.stream_proxy(cid, self.db)
                ghost = await livetv.stream_proxy(cid, self.db)     # attached, never read
                it = watching.body_iterator.__aiter__()
                loop = asyncio.get_running_loop()
                end = loop.time() + 0.8
                while loop.time() < end:
                    await it.__anext__()
                shared = livetv._shared_streams.get(cid)
                state = (not shared._closed, len(shared.subscribers), ghost._q in shared.subscribers)
                await it.aclose()
                return state

        open_, n, ghost_kept = self._run(body)
        self.assertTrue(open_, "the channel closed under the viewer who is watching")
        self.assertEqual(1, n)
        self.assertFalse(ghost_kept)


if __name__ == "__main__":
    unittest.main()

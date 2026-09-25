"""#18: a channel whose URL serves a continuous MPEG-TS body must start streaming.

Before c4af4f7 the redirect resolver in _stream_proxy_inner fetched the channel URL
with httpx's get(), which reads the WHOLE body before returning. A raw .ts channel's
body never ends, so the coroutine never returned and the tuner got no response at all.

This drives _stream_proxy_inner against a real local HTTP server that writes TS
packets until the client goes away, and requires the first bytes within a few
seconds. It uses a real socket rather than a mocked httpx client, so it runs
unchanged on c4af4f7^ (where it times out) and on every later version.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import inspect
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from web_stubs import _ensure_web_stubs

_ensure_web_stubs()

PACKET = b"\x47" + b"\x1f\xff\x10" + b"\xff" * 184   # a null TS packet, 188 bytes


class _EndlessTs(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        _EndlessTs.requests += 1
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Connection", "close")   # no length, no chunking: an endless body
        self.end_headers()
        try:
            while not self.server.stopping:
                self.wfile.write(PACKET * 64)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class TestIssue18RawTsChannelStarts(unittest.TestCase):
    def setUp(self):
        _EndlessTs.requests = 0
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _EndlessTs)
        self.srv.stopping = False
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        def stop():
            self.srv.stopping = True
            self.srv.shutdown()
            self.srv.server_close()
        self.addCleanup(stop)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/live/u/p/1001.ts"

    def test_first_bytes_arrive_from_an_endless_ts_body(self):
        import routers.livetv as livetv
        allow = lambda *a, **k: True  # noqa: E731  (127.0.0.1 is not a public host)
        kwargs = dict(channel_id=1, user_agent="Test/1.0", stream_url=self.url, _release_sem=lambda: None)
        if "guard" in inspect.signature(livetv._stream_proxy_inner).parameters:
            kwargs["guard"] = allow

        async def first_bytes():
            response = await livetv._stream_proxy_inner(**kwargs)
            got = b""
            async for piece in response.body_iterator:
                got += piece
                if len(got) >= 188:
                    break
            await response.body_iterator.aclose()
            return got

        async def run():
            return await asyncio.wait_for(first_bytes(), timeout=8)

        with mock.patch.object(livetv, "is_safe_url", allow):
            try:
                got = asyncio.run(run())
            except asyncio.TimeoutError:
                self.fail("#18: no response within 8 s -- the proxy is still reading the endless body")
        self.assertTrue(got.startswith(b"\x47"), got[:8])
        self.assertGreaterEqual(_EndlessTs.requests, 1)


if __name__ == "__main__":
    unittest.main()

"""A VOD range ended on purpose closes quietly, not with two ERROR tracebacks (#285).

Run from the tentacle/ directory:  python -m unittest discover -s tests

VOD through Tentacle passes the provider's Content-Length on, and a seek from
the same player ends the older range's body early on purpose. uvicorn's h11
then refuses the end of the message ("Too little data for declared
Content-Length"), which closes the old connection -- what tells the player
that range is over -- but it also logged two full ERROR tracebacks: Tentacle's
catch-all handler and uvicorn's "Exception in ASGI application".

The connection must still close at once (catching the error instead leaves
the old player hanging until its read timeout), so only the logging changes.
Served by real uvicorn (h11) to a real httpx client over a local socket.
"""
import asyncio
import logging
import socket
import threading
import time
import unittest

import httpx
import uvicorn
from fastapi import FastAPI
from starlette.responses import StreamingResponse


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class EarlyRangeEndIsQuiet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main  # installs the log filter and the catch-all handler
        from routers.vod import _VodResponse

        cls.finished = []
        app = FastAPI()
        app.add_exception_handler(Exception, main.global_exception_handler)

        @app.get("/early")
        async def early():
            async def body():
                yield b"x" * 1000
                await asyncio.sleep(0.05)
                return                  # superseded by a seek: ends below Content-Length
            return _VodResponse(body(), status_code=206, media_type="video/x-matroska",
                                headers={"Content-Length": "50000000"},
                                finish=lambda: cls.finished.append(True))

        @app.get("/boom")
        async def boom():
            raise ValueError("a real bug")

        cls.port = _free_port()
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port,
                                                   log_level="warning", http="h11", lifespan="off"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def setUp(self):
        self.grab = _Grab()
        self.grab.setLevel(logging.DEBUG)
        for name in ("", "uvicorn.error", "main"):
            logging.getLogger(name).addHandler(self.grab)

    def tearDown(self):
        for name in ("", "uvicorn.error", "main"):
            logging.getLogger(name).removeHandler(self.grab)

    def _errors(self):
        time.sleep(0.2)
        return [r for r in self.grab.records if r.levelno >= logging.ERROR]

    def test_an_early_end_logs_no_error_and_still_closes_the_connection_at_once(self):
        started = time.monotonic()
        with httpx.Client(timeout=10) as c:
            with c.stream("GET", f"http://127.0.0.1:{self.port}/early") as r:
                self.assertEqual(206, r.status_code)
                with self.assertRaises(httpx.RemoteProtocolError):
                    for _ in r.iter_bytes():
                        pass
        self.assertLess(time.monotonic() - started, 5, "the old range was left hanging")
        self.assertEqual([], [r.getMessage()[:120] for r in self._errors()])
        self.assertTrue(self.finished)

    def test_a_real_error_is_still_logged(self):
        with httpx.Client(timeout=10) as c:
            self.assertEqual(500, c.get(f"http://127.0.0.1:{self.port}/boom").status_code)
        self.assertTrue(any("a real bug" in (r.exc_text or "") + r.getMessage() for r in self._errors()))


if __name__ == "__main__":
    unittest.main()

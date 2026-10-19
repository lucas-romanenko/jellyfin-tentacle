"""A refused tuner open answers 503 with no body (#140).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Jellyfin's tuner (SharedHttpStream.Open, 10.11.8) never looks at the status
code: it copies whatever body arrives. FastAPI's JSON error body "opened" as a
stream, was probed for 3 s, and left a recording .nfo and show folder behind on
every retry. An empty body fails at once ("Zero bytes copied"), and Jellyfin
retries a minute later. The reason goes in X-Tentacle-Reason.
"""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import livetv


def _app(exc):
    app = FastAPI()
    app.add_exception_handler(livetv._TunerRefusal, livetv.tuner_refusal_handler)

    @app.get("/api/live/stream/{channel_id}")
    async def stream(channel_id: int):
        raise exc

    return TestClient(app)


class EmptyBody(unittest.TestCase):
    def test_a_placeholder_refusal_has_no_body_and_says_why_in_a_header(self):
        r = _app(livetv._PlaceholderRefusal("Channel unavailable: the provider served a placeholder (black.ts)")) \
            .get("/api/live/stream/1")
        self.assertEqual(503, r.status_code)
        self.assertEqual(b"", r.content)
        self.assertIn("black.ts", r.headers["X-Tentacle-Reason"])
        self.assertEqual("60", r.headers["Retry-After"])

    def test_every_refusal_kind_is_the_same_shape(self):
        for detail in (livetv.PROTECTED_REFUSAL_DETAIL, "Too many concurrent live streams (limit 2)",
                       "A recording needed this connection slot"):
            r = _app(livetv._TunerRefusal(detail)).get("/api/live/stream/1")
            self.assertEqual((503, b""), (r.status_code, r.content), detail)

    def test_a_reason_that_is_not_ascii_still_fits_in_a_header(self):
        r = _app(livetv._TunerRefusal("Channel “Sky” — unavailable")).get("/api/live/stream/1")
        self.assertEqual(503, r.status_code)
        self.assertIn("unavailable", r.headers["X-Tentacle-Reason"])

    def test_a_refusal_is_still_an_http_exception(self):
        """Callers and tests that catch HTTPException 503 keep working."""
        from fastapi import HTTPException
        exc = livetv._PlaceholderRefusal("x")
        self.assertIsInstance(exc, HTTPException)
        self.assertEqual(503, exc.status_code)

    def test_the_app_answers_refusals_this_way(self):
        import main
        self.assertIs(livetv.tuner_refusal_handler, main.app.exception_handlers.get(livetv._TunerRefusal))


if __name__ == "__main__":
    unittest.main()

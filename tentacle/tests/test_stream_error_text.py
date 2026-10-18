"""A failed stream open answers with the status, not the provider's URL.

Run from the tentacle/ directory:  python -m unittest discover -s tests

GET /api/live/stream/{channel_id} (the HDHomeRun tuner route) answered a
provider refusal with 502 "Failed to connect to stream: {e}". httpx's error
text is "Client error '404 Not Found' for url '<url>'", and for an Xtream
channel that URL is http://host/live/<username>/<password>/<id>.ts. The log
line beside it is already redacted; the reply was not. The VOD HEAD route
(/api/vod/...) echoed its httpx error the same way.

Both replies now name the status (or the error type) only.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from test_livetv_hls_transient_errors import FakeClient, _resp

ACCOUNT, PASSWORD = "acct-7f3a", "pw-91c2"
STREAM = f"http://provider.test/live/{ACCOUNT}/{PASSWORD}/1.m3u8"


class _RaisingClient(FakeClient):
    async def _answer(self, url):
        self._log.append(url)
        raise httpx.ConnectError(f"connection failed for url '{url}'",
                                 request=httpx.Request("GET", url))


async def _open(client_cls, script):
    import routers.livetv as livetv

    log = []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    with patch("httpx.AsyncClient", lambda **kw: client_cls(script, log, **kw)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep), \
            patch.object(livetv, "_OPEN_RETRY_BUDGET", 0):
        try:
            await livetv._stream_proxy_inner(channel_id=1, user_agent="TestAgent/1.0",
                                             stream_url=STREAM, _release_sem=lambda: None)
        except HTTPException as exc:
            return exc
    return None


class TestStreamOpenErrorText(unittest.IsolatedAsyncioTestCase):
    def assertNoAccount(self, exc):
        self.assertIsInstance(exc, HTTPException)
        self.assertEqual(exc.status_code, 502)
        text = str(exc.detail) + repr(exc.headers or {})
        self.assertNotIn(ACCOUNT, text)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("provider.test", text)

    async def test_every_refusal_status_names_the_status_only(self):
        for status in (401, 403, 404, 407, 410, 429, 503, 509):
            with self.subTest(status=status):
                exc = await _open(FakeClient, {STREAM: [_resp(status, STREAM)]})
                self.assertNoAccount(exc)
                self.assertIn(str(status), exc.detail)

    async def test_a_connection_error_names_the_error_type(self):
        exc = await _open(_RaisingClient, {})
        self.assertNoAccount(exc)
        self.assertIn("ConnectError", exc.detail)


class TestVodHeadErrorText(unittest.IsolatedAsyncioTestCase):
    async def test_transport_error_names_the_error_type(self):
        import routers.vod as vod
        url = f"http://provider.test/movie/{ACCOUNT}/{PASSWORD}/42.mkv"

        async def fail(*a, **k):
            raise httpx.ConnectError(f"connection failed for url '{url}'")

        # No DB here: recording protection (read by vod_head) is off.
        with patch.object(vod, "_resolve", lambda db, kind, tf: (url, None, "UA", "vod:movie:1:42")), \
                patch.object(vod.livetv, "_protect_recordings", lambda db: False), \
                patch.object(vod, "_open", fail):
            with self.assertRaises(HTTPException) as ctx:
                await vod.vod_head("movie", "x.mkv", request=None, db=None)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertNotIn(ACCOUNT, ctx.exception.detail)
        self.assertNotIn(PASSWORD, ctx.exception.detail)
        self.assertIn("ConnectError", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()

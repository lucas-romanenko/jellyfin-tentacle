"""A transient 509 while *opening* a live stream must not cost the recording.

#86 taught the running HLS worker to wait out 429/509 on a growing delay. The
open path was left behind: `_stream_proxy_inner` probes the tokenized URL once
and turns any HTTP error -- including a 509 that would have cleared a second
later -- straight into `502 Failed to connect to stream`.

Phase-3 QA measured what that costs (6 streams against a provider capped at 8
connections, merged fix tree): every stream that managed to open ran gap-free
through 27 backed-off retries, but three tuner opens got a 509 on the probe and
were refused outright. Jellyfin re-tries a failed tuner open only once a minute,
so one recording started 60 s late and two never produced a file -- while the
log showed the in-flight retry logic working perfectly.

The open path gets the same regime, with a much shorter budget because a tuner
client is blocked on the response: retry retryable statuses / transport errors
with backoff for a bounded time, fail fast on anything else.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from test_livetv_hls_transient_errors import (
    BASE, PLAYLIST_CT, FakeClient, _playlist, _resp,
)

CHUNK1 = "http://provider.test/live/u/p/c1.ts"


async def _open(script):
    """Open the proxy against `script`; returns (first bytes | exception, log, slept)."""
    import routers.livetv as livetv

    log, slept = [], []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        slept.append(delay)
        await real_sleep(0)

    with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, **kw)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep):
        try:
            response = await livetv._stream_proxy_inner(
                channel_id=1, user_agent="TestAgent/1.0", stream_url=BASE,
                _release_sem=lambda: None)
        except HTTPException as exc:
            return exc, log, slept

        async def collect():
            out = b""
            async for piece in response.body_iterator:
                out += piece
            return out

        return await asyncio.wait_for(collect(), timeout=30), log, slept


def _script(open_failures, status=509):
    done = _playlist("c1.ts", end=True)
    ok = _resp(200, BASE, done.encode(), PLAYLIST_CT)
    return {
        # hop 1 is the redirect-follow (status ignored unless 3xx); the probe
        # comes next and is the request whose failure refuses the tuner.
        BASE: [ok] + [_resp(status, BASE) for _ in range(open_failures)] + [ok, ok],
        CHUNK1: [_resp(200, CHUNK1, b"CHUNK1")],
    }


class TestTransientErrorsWhileOpening(unittest.IsolatedAsyncioTestCase):
    async def test_a_509_on_open_is_waited_out(self):
        result, _, slept = await _open(_script(open_failures=3))
        self.assertNotIsInstance(
            result, HTTPException,
            "one 509 on the opening probe refused the tuner outright; Jellyfin "
            "only re-tries a minute later, or the recording is lost")
        self.assertIn(b"CHUNK1", result)
        self.assertGreaterEqual(len(slept), 3, "retried without waiting")

    async def test_open_retries_back_off(self):
        _, _, slept = await _open(_script(open_failures=4))
        waits = slept[:4]
        self.assertGreater(waits[-1], waits[0], f"no growth in the waits: {waits}")

    async def test_open_gives_up_within_a_bounded_time(self):
        """A tuner client is blocked on this response; never hold it for long."""
        import routers.livetv as livetv
        result, log, slept = await _open(_script(open_failures=10_000))
        self.assertIsInstance(result, HTTPException)
        self.assertEqual(502, result.status_code)
        self.assertLessEqual(sum(slept), livetv._OPEN_RETRY_BUDGET * 1.25 + 5)
        self.assertLess(len(log), 40, "hammered the provider while refused")

    async def test_a_fatal_status_on_open_still_fails_at_once(self):
        result, log, slept = await _open(_script(open_failures=10_000, status=404))
        self.assertIsInstance(result, HTTPException)
        self.assertEqual([], slept, "waited on a status that will never clear")
        self.assertEqual(2, len(log))


if __name__ == "__main__":
    unittest.main()

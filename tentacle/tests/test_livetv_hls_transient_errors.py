"""A transient provider hiccup must not end a live stream for good.

An Xtream provider answers 509 (Bandwidth Limit Exceeded) while the account's
connection allowance is momentarily saturated -- two recordings whose short HLS
requests happen to collide, say. 509 is by definition temporary: the next
request a second later usually succeeds.

The HLS worker in routers/livetv.py counts every failure into one
`consecutive_errors` and gives up once it passes 5, with no delay growth
between attempts and no idea that some statuses are worth waiting out. A
playlist refresh runs every `target_duration / 2` seconds, so six 509s in a row
-- under ten seconds of provider saturation on a typical six-second segment --
permanently ends the stream. Jellyfin then stops the recording and opens a new
file, which is how a single three-hour sports recording becomes several partial
ones with the action missing at every seam.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx


PLAYLIST_CT = {"content-type": "application/vnd.apple.mpegurl"}
BASE = "http://provider.test/live/u/p/1.m3u8"


def _playlist(*chunks, end=False):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6"]
    for c in chunks:
        lines += ["#EXTINF:6.0,", c]
    if end:
        # Lets the worker finish on its own so a test can never hang; a real
        # live playlist never carries this.
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _resp(status, url, content=b"", headers=None):
    return httpx.Response(
        status,
        headers=headers or {},
        content=content,
        request=httpx.Request("GET", url),
    )


class FakeClient:
    """Stands in for httpx.AsyncClient for all three phases of the proxy.

    `script` maps a URL to a list of responses; the last one repeats once the
    list runs down, so a steady state can be expressed without padding.
    """

    def __init__(self, script, log, **kwargs):
        self._script = script
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aclose(self):
        return None

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        return await self._answer(str(request.url))

    async def get(self, url, headers=None):
        return await self._answer(str(url))

    async def _answer(self, url):
        self._log.append(url)
        queue = self._script[url]
        return queue[0] if len(queue) == 1 else queue.pop(0)


async def _drive(script, max_chunks=40):
    """Run the proxy against `script` and return (yielded bytes, urls fetched)."""
    import routers.livetv as livetv

    log = []
    real_sleep = asyncio.sleep
    slept = []

    async def fast_sleep(delay):
        slept.append(delay)
        await real_sleep(0)

    def client_factory(**kwargs):
        return FakeClient(script, log, **kwargs)

    with patch("httpx.AsyncClient", client_factory), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep):
        response = await livetv._stream_proxy_inner(
            channel_id=1,
            user_agent="TestAgent/1.0",
            stream_url=BASE,
            _release_sem=lambda: None,
        )

        async def collect():
            out = b""
            count = 0
            async for piece in response.body_iterator:
                out += piece
                count += 1
                if count >= max_chunks:
                    break
            return out

        # Every script ends the playlist, so a hang means the worker is stuck
        # rather than the script being short.
        body = await asyncio.wait_for(collect(), timeout=30)

    return body, log, slept


class TestTransientProviderErrors(unittest.IsolatedAsyncioTestCase):
    def _script_with_burst(self, burst, status=509):
        """Two good chunks, then `burst` failed playlist refreshes, then the
        provider recovers and offers two more chunks."""
        first = _playlist("c1.ts", "c2.ts")
        recovered = _playlist("c1.ts", "c2.ts", "c3.ts", "c4.ts")
        done = _playlist("c1.ts", "c2.ts", "c3.ts", "c4.ts", end=True)
        refreshes = [_resp(status, BASE) for _ in range(burst)]
        refreshes.append(_resp(200, BASE, recovered.encode(), PLAYLIST_CT))
        refreshes.append(_resp(200, BASE, done.encode(), PLAYLIST_CT))
        return {
            # The open reads this URL once and the worker re-reads it on every
            # refresh. (The open used to read it twice; the spare copy is now
            # simply consumed as a reload that has nothing new in it.)
            BASE: [
                _resp(200, BASE, first.encode(), PLAYLIST_CT),
                _resp(200, BASE, first.encode(), PLAYLIST_CT),
            ] + refreshes,
            "http://provider.test/live/u/p/c1.ts": [_resp(200, "http://provider.test/live/u/p/c1.ts", b"CHUNK1")],
            "http://provider.test/live/u/p/c2.ts": [_resp(200, "http://provider.test/live/u/p/c2.ts", b"CHUNK2")],
            "http://provider.test/live/u/p/c3.ts": [_resp(200, "http://provider.test/live/u/p/c3.ts", b"CHUNK3")],
            "http://provider.test/live/u/p/c4.ts": [_resp(200, "http://provider.test/live/u/p/c4.ts", b"CHUNK4")],
        }

    async def test_a_short_burst_of_509s_does_not_end_the_stream(self):
        """Six 509s is under ten seconds of saturation -- the stream must ride
        it out and keep delivering once the provider recovers."""
        body, _, _ = await _drive(self._script_with_burst(6))
        self.assertIn(b"CHUNK1", body)
        self.assertIn(b"CHUNK3", body,
                      "stream gave up during a transient 509 burst and never "
                      "resumed; Jellyfin sees EOF and splits the recording")
        self.assertIn(b"CHUNK4", body)

    async def test_a_longer_burst_of_509s_is_still_survived(self):
        """A minute-long squeeze is still temporary. Recording through it is
        the whole point: the alternative is a truncated file."""
        body, _, _ = await _drive(self._script_with_burst(20))
        self.assertIn(b"CHUNK3", body)

    async def test_retries_back_off_instead_of_hammering(self):
        """Retrying a rate-limit at a fixed interval prolongs the squeeze. The
        waits must grow while the provider keeps refusing."""
        _, _, slept = await _drive(self._script_with_burst(8))
        self.assertGreater(max(slept), 3.0,
                           f"no backoff: every wait was the plain segment "
                           f"interval ({sorted(set(slept))})")

    async def test_a_chunk_that_failed_once_is_retried(self):
        """A chunk is marked seen before it is fetched, so a chunk lost to a
        transient 509 is never asked for again -- a silent hole in the
        recording even when the stream itself survives."""
        first = _playlist("c1.ts", "c2.ts")
        done = _playlist("c1.ts", "c2.ts", end=True)
        chunk2 = "http://provider.test/live/u/p/c2.ts"
        script = {
            BASE: [
                _resp(200, BASE, first.encode(), PLAYLIST_CT),
                _resp(200, BASE, first.encode(), PLAYLIST_CT),
                _resp(200, BASE, first.encode(), PLAYLIST_CT),
                _resp(200, BASE, done.encode(), PLAYLIST_CT),
            ],
            "http://provider.test/live/u/p/c1.ts": [_resp(200, "http://provider.test/live/u/p/c1.ts", b"CHUNK1")],
            chunk2: [
                _resp(509, chunk2),
                _resp(200, chunk2, b"CHUNK2"),
            ],
        }
        body, log, _ = await _drive(script, max_chunks=3)
        self.assertIn(b"CHUNK2", body,
                      "a transiently-failed chunk was dropped from the "
                      "recording and never retried")

    async def test_a_fatal_status_still_fails_fast(self):
        """404 is not going to fix itself. The tolerance added for 509 must not
        make a genuinely dead channel hang around retrying."""
        first = _playlist("c1.ts")
        script = {
            BASE: [
                _resp(200, BASE, first.encode(), PLAYLIST_CT),
                _resp(200, BASE, first.encode(), PLAYLIST_CT),
                _resp(404, BASE),
            ],
            "http://provider.test/live/u/p/c1.ts": [_resp(200, "http://provider.test/live/u/p/c1.ts", b"CHUNK1")],
        }
        body, log, slept = await _drive(script, max_chunks=10)
        self.assertIn(b"CHUNK1", body)
        self.assertLessEqual(sum(slept), 60,
                             "a dead channel was nursed along with long "
                             f"backoff waits ({sorted(set(slept))})")


if __name__ == "__main__":
    unittest.main()

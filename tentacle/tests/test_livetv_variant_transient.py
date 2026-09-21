"""#68 x #86: a transient failure while fetching a variant playlist.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Once #86's retry regime covers the variant fetch, the worker can come back
around the loop still holding the MASTER playlist. A master's non-comment lines
are variant playlist URIs, not media segments, so falling through to the chunk
parser would fetch a variant playlist and pipe its ASCII text out as MPEG-TS --
the exact failure #68 exists to prevent. The retry has to re-read the master
instead.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from test_livetv_hls_transient_errors import FakeClient, PLAYLIST_CT, _resp

BASE = "http://provider.test/live/u/p/1.m3u8"
VARIANT = "http://provider.test/live/u/p/1_hi.m3u8"

MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n"
    "1_hi.m3u8\n"
)


def _media(*chunks, end=False):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6"]
    for c in chunks:
        lines += ["#EXTINF:6.0,", c]
    if end:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


async def _drive(script, max_chunks=20):
    import routers.livetv as livetv

    log = []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, **kw)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep):
        response = await livetv._stream_proxy_inner(
            channel_id=1, user_agent="TestAgent/1.0", stream_url=BASE,
            _release_sem=lambda: None,
        )

        async def collect():
            out = b""
            for _ in range(max_chunks):
                try:
                    out += await response.body_iterator.__anext__()
                except StopAsyncIteration:
                    break
            return out

        body = await asyncio.wait_for(collect(), timeout=30)
    return body, log


class VariantTransientFailure(unittest.IsolatedAsyncioTestCase):
    async def test_a_509_on_the_variant_is_retried_not_fatal(self):
        script = {
            BASE: [_resp(200, BASE, MASTER.encode(), PLAYLIST_CT)],
            VARIANT: [
                _resp(509, VARIANT),
                _resp(200, VARIANT, _media("c1.ts", end=True).encode(), PLAYLIST_CT),
            ],
            "http://provider.test/live/u/p/c1.ts": [
                _resp(200, "http://provider.test/live/u/p/c1.ts", b"CHUNK1")],
        }
        body, log = await _drive(script)
        self.assertIn(b"CHUNK1", body,
                      "a transient 509 on the variant playlist ended the stream")

    async def test_a_failed_variant_never_leaks_playlist_text_as_video(self):
        """The master must not be parsed as a media playlist on the retry: that
        would fetch 1_hi.m3u8 as a 'chunk' and yield its text as MPEG-TS."""
        script = {
            BASE: [_resp(200, BASE, MASTER.encode(), PLAYLIST_CT)],
            VARIANT: [
                _resp(509, VARIANT),
                _resp(200, VARIANT, _media("c1.ts", end=True).encode(), PLAYLIST_CT),
            ],
            "http://provider.test/live/u/p/c1.ts": [
                _resp(200, "http://provider.test/live/u/p/c1.ts", b"CHUNK1")],
        }
        body, log = await _drive(script)
        self.assertNotIn(b"#EXTM3U", body,
                         "playlist text was piped to the tuner as video")
        self.assertEqual(body.count(b"CHUNK1"), 1)


if __name__ == "__main__":
    unittest.main()

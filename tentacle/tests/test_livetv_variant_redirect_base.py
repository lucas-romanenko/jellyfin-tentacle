"""Segments of a redirected variant playlist resolve against where it came from (#140).

Run from the tentacle/ directory:  python -m unittest discover -s tests

A master playlist's variant can answer with a redirect -- to a CDN edge, or a
tokenized path on the same host -- and a media playlist there lists its
segments relative to itself (RFC 8216 4.1: relative URIs resolve against the
playlist's URI; RFC 3986 5.1.3: after redirects, the URI it was retrieved
from). The open, the worker's own variant hop and the playlist refresh all
resolved them against the variant URL from the master, before the redirect,
so every segment was asked for at a path that does not exist and the stream
failed. test_livetv_variant_redirects.py serves .ts from any path, so it could
not see this.

Reloads still ask for the variant's own URL (a redirect target can be a
short-lived token); only the base for its URIs is where the hop's redirect
landed.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

import routers.livetv as livetv
from test_livetv_open_single_fetch import FakeClient, _resp

MASTER_URL = "http://provider.test/live/u/p/220.m3u8"
VARIANT = "http://provider.test/live/u/p/high/index.m3u8"
EDGE = "http://edge.provider.test/tok123/index.m3u8"
WRONG = "http://provider.test/live/u/p/high/"      # the variant's directory
PL = {"content-type": "application/vnd.apple.mpegurl"}
TS = {"content-type": "video/mp2t"}
MASTER = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2500000\nhigh/index.m3u8\n"


def _media(*chunks, end=True):
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0"]
    for c in chunks:
        lines += ["#EXTINF:2.0,", c]
    if end:
        lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode()


def _redirect(src, dst):
    return _resp(302, src, headers={"location": dst})


async def _play(script):
    """Open MASTER_URL and read the stream to its end (or 0.5 s of failure)."""
    log = []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    with patch("httpx.AsyncClient", lambda **kw: FakeClient(script, log, [], **kw)), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep):
        resp = await livetv._stream_proxy_inner(
            channel_id=1, user_agent="UA", stream_url=MASTER_URL, _release_sem=lambda: None,
            failure_budget=0.5)
        body = b""

        async def collect():
            nonlocal body
            async for piece in resp.body_iterator:
                body += piece
        await asyncio.wait_for(collect(), timeout=30)
    return body, log


def _script(variant_answers, **more):
    script = {MASTER_URL: [_resp(200, MASTER_URL, MASTER, PL)],
              VARIANT: variant_answers,
              EDGE: [_resp(200, EDGE, _media("s1.ts"), PL)],
              "http://edge.provider.test/tok123/s1.ts": [_resp(200, EDGE, b"G" * 188, TS)],
              # where the segment was asked for before: not there
              WRONG + "s1.ts": [_resp(404, WRONG + "s1.ts")],
              WRONG + "s2.ts": [_resp(404, WRONG + "s2.ts")]}
    script.update(more)
    return script


class VariantRedirectBase(unittest.IsolatedAsyncioTestCase):
    async def test_open_segments_resolve_against_the_redirect_target(self):
        body, log = await _play(_script([_redirect(VARIANT, EDGE)]))
        self.assertNotIn(WRONG + "s1.ts", log, "the segment was asked for under the variant's own path")
        self.assertEqual(b"G" * 188, body)

    async def test_the_worker_hop_too(self):
        """The open's variant read was refused, so the worker follows the variant."""
        body, log = await _play(_script([_resp(509, VARIANT), _redirect(VARIANT, EDGE)]))
        self.assertNotIn(WRONG + "s1.ts", log, "the segment was asked for under the variant's own path")
        self.assertEqual(b"G" * 188, body)

    async def test_a_live_playlist_reloads_the_variant_and_keeps_the_base(self):
        script = _script(
            [_redirect(VARIANT, EDGE)],
            **{EDGE: [_resp(200, EDGE, _media("s1.ts", end=False), PL),
                      _resp(200, EDGE, _media("s1.ts", "s2.ts"), PL)],
               "http://edge.provider.test/tok123/s2.ts": [_resp(200, EDGE, b"H" * 188, TS)]})
        body, log = await _play(script)
        self.assertNotIn(WRONG + "s2.ts", log, "the segment was asked for under the variant's own path")
        self.assertEqual(b"G" * 188 + b"H" * 188, body, "a segment was lost or sent twice")
        # The reload asked the variant's own URL again, as before.
        self.assertEqual(2, log.count(VARIANT))

    async def test_without_a_redirect_nothing_changes(self):
        script = {MASTER_URL: [_resp(200, MASTER_URL, MASTER, PL)],
                  VARIANT: [_resp(200, VARIANT, _media("s1.ts"), PL)],
                  WRONG + "s1.ts": [_resp(200, VARIANT, b"G" * 188, TS)]}
        body, log = await _play(script)
        self.assertEqual(b"G" * 188, body)
        self.assertEqual([MASTER_URL, VARIANT, WRONG + "s1.ts"], log)


if __name__ == "__main__":
    unittest.main()

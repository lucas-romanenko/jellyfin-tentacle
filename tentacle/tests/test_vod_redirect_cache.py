"""A provider film's redirect target is reused for a few minutes, so the
requests of one play don't each pay the provider's 302.

Measured on the system this was written for (Oct 2026): the provider's panel
URL answers every request with a 302 to one of its edge servers. Through the
relay each request of a play (Jellyfin's probe, the TV's open, each seek)
took 0.5-0.8 s to its first bytes; straight to the edge it already knew,
0.15 s. A play is a dozen such requests before the first picture.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import time
import unittest
from unittest import mock

import httpx

from test_vod_proxy import PANEL, _Base, _Client, _file, _plain, _request

EDGE = "http://edge1.provider.test/live/u/p/2141622"
EDGE2 = "http://edge2.provider.test/live/u/p/2141622"


def _redirect(location, url=PANEL):
    return httpx.Response(302, headers={"location": location}, request=httpx.Request("GET", url))


class _Broken:
    """A response stand-in whose send raises (the edge refused the connection)."""


class _FlakyClient(_Client):
    """Like _Client, but a scripted `_Broken` raises a connect error."""

    async def send(self, request, stream=False):
        resp = await super().send(request, stream)
        if isinstance(resp, _Broken):
            raise httpx.ConnectError("refused", request=request)
        return resp


class RedirectTargetCache(_Base):
    def _urls(self):
        return [u for _, u, _ in self.log]

    def test_a_seek_goes_straight_to_the_edge_the_play_was_sent_to(self):
        script = {self.upstream: [_redirect(EDGE)],
                  EDGE: [_file(206, [b"AAAA"], start=0, url=EDGE), _file(206, [b"BBBB"], start=500, url=EDGE)]}
        self._play(script, range_header="bytes=0-")
        status, _, body = self._play(script, range_header="bytes=500-")
        self.assertEqual((206, b"BBBB"), (status, body))
        self.assertEqual([self.upstream, EDGE, EDGE], self._urls(), "one 302 for the play, not one per request")

    def test_an_expired_target_asks_the_panel_again(self):
        script = {self.upstream: [_redirect(EDGE), _redirect(EDGE2)],
                  EDGE: [_file(206, [b"AAAA"], start=0, url=EDGE)],
                  EDGE2: [_file(206, [b"BBBB"], start=500, url=EDGE2)]}
        self._play(script, range_header="bytes=0-")
        target, expires = self.vod._targets[self.upstream]
        self.assertEqual(EDGE, target)
        self.assertAlmostEqual(time.monotonic() + self.vod.TARGET_SECONDS, expires, delta=5)
        self.vod._targets[self.upstream] = (target, time.monotonic() - 1)     # its time is up
        status, _, body = self._play(script, range_header="bytes=500-")
        self.assertEqual((206, b"BBBB"), (status, body))
        self.assertEqual([self.upstream, EDGE, self.upstream, EDGE2], self._urls())
        self.assertEqual(EDGE2, self.vod._targets[self.upstream][0])

    def test_a_target_that_refuses_is_forgotten_and_the_panel_asked_at_once(self):
        """An expired provider token answers 403 (or 404, 509...): the same
        request goes back to the panel for a fresh target, no error to the player."""
        script = {self.upstream: [_redirect(EDGE), _redirect(EDGE2)],
                  EDGE: [_file(206, [b"AAAA"], start=0, url=EDGE), _plain(403, url=EDGE)],
                  EDGE2: [_file(206, [b"BBBB"], start=500, url=EDGE2)]}
        self._play(script, range_header="bytes=0-")
        status, _, body = self._play(script, range_header="bytes=500-")
        self.assertEqual((206, b"BBBB"), (status, body))
        self.assertEqual([self.upstream, EDGE, EDGE, self.upstream, EDGE2], self._urls())
        self.assertEqual(EDGE2, self.vod._targets[self.upstream][0])

    def test_a_target_that_cannot_be_reached_is_forgotten_too(self):
        script = {self.upstream: [_redirect(EDGE), _redirect(EDGE2)],
                  EDGE: [_file(206, [b"AAAA"], start=0, url=EDGE), _Broken()],
                  EDGE2: [_file(206, [b"BBBB"], start=500, url=EDGE2)]}
        vod = self.vod

        async def go(rng):
            with mock.patch("httpx.AsyncClient", lambda **kw: _FlakyClient(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                resp = await vod.vod_stream("movie", self.token_file, _request(rng), self.db)
                return b"".join([p async for p in resp.body_iterator])
        self.assertEqual(b"AAAA", asyncio.run(go("bytes=0-")))
        self.assertEqual(b"BBBB", asyncio.run(go("bytes=500-")))
        self.assertEqual([self.upstream, EDGE, EDGE, self.upstream, EDGE2], self._urls())

    def test_each_title_has_its_own_target(self):
        from services import vod_tokens
        other_file = vod_tokens.url("http://tentacle:8888", "s" * 64, self.provider.id, "movie", 777, "mkv") \
            .rsplit("/", 1)[1]
        other = f"{PANEL}/movie/u/p/777.mkv"
        script = {self.upstream: [_redirect(EDGE)], other: [_redirect(EDGE2)],
                  EDGE: [_file(206, [b"AAAA"], start=0, url=EDGE)],
                  EDGE2: [_file(206, [b"CCCC"], start=0, url=EDGE2)]}
        self._play(script, range_header="bytes=0-")
        self.vod._playbacks.clear()
        self.livetv._stream_slots = self.livetv._StreamSlots()
        token_file, self.token_file = self.token_file, other_file
        try:
            status, _, body = self._play(script, range_header="bytes=0-")
        finally:
            self.token_file = token_file
        self.assertEqual((206, b"CCCC"), (status, body))
        self.assertEqual([self.upstream, EDGE, other, EDGE2], self._urls(), "another title asks its own panel URL")
        self.assertEqual(EDGE, self.vod._targets[self.upstream][0])
        self.assertEqual(EDGE2, self.vod._targets[other][0])

    def test_a_target_is_kept_only_when_it_served_the_file(self):
        script = {self.upstream: [_redirect(EDGE)], EDGE: [_plain(404, url=EDGE)]}
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._play(script, range_header="bytes=0-")
        self.assertNotIn(self.upstream, self.vod._targets)

    def test_a_direct_answer_caches_nothing(self):
        script = {self.upstream: [_file(206, [b"AAAA"], start=0), _file(206, [b"BBBB"], start=500)]}
        self._play(script, range_header="bytes=0-")
        self._play(script, range_header="bytes=500-")
        self.assertEqual({}, self.vod._targets)
        self.assertEqual([self.upstream, self.upstream], self._urls())

    def test_the_requests_of_a_play_share_one_pooled_client(self):
        """One client (connection pool, keep-alive to the provider) for every
        request, instead of a new client, DNS and connection per seek."""
        made = []
        script = {self.upstream: [_file(206, [b"AAAA"], start=0), _file(206, [b"BBBB"], start=500)]}
        vod = self.vod

        def factory(**kw):
            made.append(kw)
            return _Client(script, self.log, **kw)

        async def go():
            with mock.patch("httpx.AsyncClient", factory), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                for rng in ("bytes=0-", "bytes=500-"):
                    resp = await vod.vod_stream("movie", self.token_file, _request(rng), self.db)
                    [p async for p in resp.body_iterator]
        asyncio.run(go())
        self.assertEqual(1, len(made))
        self.assertFalse(made[0]["follow_redirects"], "redirects are followed by hand, each hop re-validated")
        self.assertEqual(vod.KEEPALIVE_SECONDS, made[0]["limits"].keepalive_expiry)


if __name__ == "__main__":
    unittest.main()

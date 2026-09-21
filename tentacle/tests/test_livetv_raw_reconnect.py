"""A raw MPEG-TS channel must survive its upstream connection dropping.

Run from the tentacle/ directory:  python -m unittest discover -s tests

An HLS channel is many short requests, and #86 taught the worker to wait out a
run of failed ones. A raw MPEG-TS channel is ONE long GET, and none of that
reached it: when the provider dropped the connection (seen live: "peer closed
connection without sending complete message body") the generator logged
"Stream interrupted" and ended. Jellyfin sees EOF, stops the recording there and
starts a new file -- the same split recording #86 is about, by another road.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from test_livetv_open_single_fetch import PANEL, TOKENIZED, FakeClient, _redirect, _resp

TS = {"content-type": "video/mp2t"}


class _Body(httpx.AsyncByteStream):
    """A response body that yields its pieces and then either ends or breaks."""

    def __init__(self, pieces, then=None):
        self._pieces, self._then = pieces, then

    async def __aiter__(self):
        for piece in self._pieces:
            yield piece
        if self._then is not None:
            raise self._then

    async def aclose(self):
        pass


def _live(pieces, then=None, url=TOKENIZED):
    return httpx.Response(200, headers=TS, stream=_Body(pieces, then),
                          request=httpx.Request("GET", url))


def _dropped():
    return httpx.RemoteProtocolError("peer closed connection without sending complete message body")


async def _play(script, limit=None):
    """Open the channel and read it to the end (or `limit` pieces)."""
    import routers.livetv as livetv
    log, closed, made, slept = [], [], [], []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        slept.append(delay)
        await real_sleep(0)

    def factory(**kw):
        c = FakeClient(script, log, closed, **kw)
        made.append(c)
        return c

    with patch("httpx.AsyncClient", factory), \
            patch("routers.livetv.is_safe_url", lambda *a, **k: True), \
            patch("asyncio.sleep", fast_sleep):
        response = await livetv._stream_proxy_inner(
            channel_id=1, user_agent="TestAgent/1.0", stream_url=PANEL,
            _release_sem=lambda: None, guard=None)

        async def collect():
            out = []
            async for piece in response.body_iterator:
                out.append(piece)
                if limit and len(out) >= limit:
                    await response.body_iterator.aclose()
                    break
            return out
        pieces = await asyncio.wait_for(collect(), timeout=30)
    return b"".join(pieces), log, made, closed, slept


class RawStreamReconnects(unittest.IsolatedAsyncioTestCase):
    async def test_a_dropped_connection_is_redialled_and_the_stream_goes_on(self):
        script = {
            PANEL: [_redirect(), _redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"], then=_dropped()), _live([b"BBBB"])],
        }
        body, log, *_ = await _play(script)
        self.assertEqual(b"AAAABBBB", body,
                         "the stream ended at the drop; Jellyfin would cut the recording there")

    async def test_a_clean_close_by_the_provider_is_redialled_too(self):
        """Read-until-close delivery makes a provider-side drop look like a tidy EOF."""
        script = {
            PANEL: [_redirect(), _redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"]), _live([b"BBBB"])],
        }
        body, *_ = await _play(script)
        self.assertEqual(b"AAAABBBB", body)

    async def test_the_redial_starts_from_the_channel_url(self):
        """A tokenized URL is often good for one connection only."""
        script = {
            PANEL: [_redirect(), _redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"], then=_dropped()), _live([b"BBBB"])],
        }
        _, log, *_ = await _play(script)
        self.assertEqual([PANEL, TOKENIZED, PANEL, TOKENIZED, PANEL], log)

    async def test_a_refusal_while_redialling_is_waited_out(self):
        script = {
            PANEL: [_redirect(), _resp(509, PANEL), _resp(509, PANEL), _redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"], then=_dropped()), _live([b"BBBB"])],
        }
        body, _, _, _, slept = await _play(script)
        self.assertEqual(b"AAAABBBB", body)
        self.assertGreaterEqual(len(slept), 3, "refusals must be paced, not hammered")
        self.assertTrue(all(d >= 0.5 for d in slept), slept)

    async def test_a_status_that_will_never_fix_itself_stops_at_once(self):
        script = {
            PANEL: [_redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"AAAA"], then=_dropped())],
        }
        body, log, _, _, slept = await _play(script)
        self.assertEqual(b"AAAA", body)
        self.assertEqual(1, len(slept), "a 404 must not be retried")

    async def test_unbroken_failure_gives_up_inside_the_budget(self):
        script = {
            PANEL: [_redirect(), _resp(509, PANEL)],       # refuses for ever afterwards
            TOKENIZED: [_live([b"AAAA"], then=_dropped())],
        }
        body, log, _, _, slept = await _play(script)
        self.assertEqual(b"AAAA", body)
        self.assertLessEqual(sum(slept), 150, "kept re-dialling far past the failure budget")
        self.assertGreater(sum(slept), 60, "gave up long before the budget was spent")

    async def test_a_provider_that_hangs_up_at_once_every_time_is_not_redialled_for_ever(self):
        """Data arriving must not, on its own, renew the budget."""
        script = {
            PANEL: [_redirect()],                           # always redirects
            TOKENIZED: [_live([b"X"])],                     # always a byte, then EOF
        }
        with patch("test_livetv_open_single_fetch.FakeClient.send", _endless_send):
            body, log, *_ = await _play(script)
        self.assertLess(len(body), 200, "re-dialled a useless upstream without limit")

    async def test_upstream_and_client_are_closed_when_the_viewer_leaves(self):
        script = {
            PANEL: [_redirect()],
            TOKENIZED: [_live([b"AAAA", b"BBBB", b"CCCC"])],
        }
        _, _, made, closed, _ = await _play(script, limit=1)
        self.assertEqual({id(c) for c in made}, {id(c) for c in closed},
                         "an httpx client opened for the stream was never closed")


async def _endless_send(self, request, stream=False):
    """Every GET of the tokenized URL gets a FRESH one-byte body."""
    url = str(request.url)
    self._log.append(url)
    if url == TOKENIZED:
        return _live([b"X"])
    return _redirect()


class RawStreamPacketAlignment(unittest.IsolatedAsyncioTestCase):
    async def test_a_packet_cut_by_the_drop_never_reaches_the_recording(self):
        def packet(n):
            return b"G" + bytes([n]) * 187

        cut = packet(1) + packet(2) + packet(3)[:50]        # the drop lands mid-packet
        script = {
            PANEL: [_redirect(), _redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([cut], then=_dropped()), _live([packet(4) + packet(5)])],
        }
        body, *_ = await _play(script)
        self.assertEqual(0, len(body) % 188)
        packets = [body[i:i + 188] for i in range(0, len(body), 188)]
        self.assertTrue(all(p[:1] == b"G" for p in packets),
                        "a partial packet was passed on, so every later packet is out of step")
        self.assertEqual([1, 2, 4, 5], [p[1] for p in packets])

    async def test_a_stream_that_is_not_packet_aligned_is_passed_through_untouched(self):
        script = {
            PANEL: [_redirect(), _resp(404, PANEL)],
            TOKENIZED: [_live([b"not-a-ts-stream, 23 by"])],
        }
        body, *_ = await _play(script)
        self.assertEqual(b"not-a-ts-stream, 23 by", body)


if __name__ == "__main__":
    unittest.main()

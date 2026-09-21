"""Opening a live stream costs the provider one GET of the tokenized URL.

routers/livetv.py opened a stream in stages, each with its own GET of the same
tokenized URL: one to follow the redirect chain (body thrown away), one to look
at the content type, and -- for a raw MPEG-TS channel -- a third to actually
stream. Every one of those is a connection in the provider's accounting, all
inside a second, at exactly the moment (a recording starting while another
stream is running) the provider is most likely to answer 509.

The response that ends the redirect chain already carries the content type and
the body. One checked GET is enough -- and it must stay a *checked* one: every
redirect hop still goes through the SSRF guard (#73), and a master playlist is
still followed to its variant (#68).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from collections import Counter
from unittest.mock import patch

import httpx
from fastapi import HTTPException

PANEL = "http://provider.test/live/u/p/1.ts"
TOKENIZED = "http://edge.provider.test/stream/1?token=abc"
ROOT = "http://edge.provider.test/stream/"
PLAYLIST_CT = {"content-type": "application/vnd.apple.mpegurl"}


def _resp(status, url, content=b"", headers=None):
    return httpx.Response(status, headers=headers or {}, content=content,
                          request=httpx.Request("GET", url))


class FakeClient:
    def __init__(self, script, log, closed, **kwargs):
        self._script = script
        self._log = log
        self._closed = closed

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
        return False

    async def aclose(self):
        self._closed.append(self)

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        url = str(request.url)
        self._log.append(url)
        queue = self._script[url]
        return queue[0] if len(queue) == 1 else queue.pop(0)


async def _open(script, guard=None, read_body=True):
    import routers.livetv as livetv

    log, closed, made = [], [], []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
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
            _release_sem=lambda: None, guard=guard)
        body = b""
        if read_body:
            async def collect():
                out = b""
                async for piece in response.body_iterator:
                    out += piece
                return out
            body = await asyncio.wait_for(collect(), timeout=30)
    return response, body, log, made, closed


def _redirect():
    return _resp(302, PANEL, headers={"location": TOKENIZED})


def _gone():
    """What the panel says to a re-open. A raw stream that ends is re-dialled
    (see test_livetv_raw_reconnect.py); these tests are about the OPEN, so the
    scripted channel ends for good once its body has been read."""
    return _resp(404, PANEL)


def _at_open(log):
    """The requests made to open the stream: everything before the re-dial."""
    return log[:-1] if log and log[-1] == PANEL and log.count(PANEL) > 1 else log


class TestOpenFetchesOnce(unittest.IsolatedAsyncioTestCase):
    async def test_raw_ts_channel_is_opened_with_one_get(self):
        script = {
            PANEL: [_redirect(), _gone()],
            TOKENIZED: [_resp(200, TOKENIZED, b"TSDATA", {"content-type": "video/mp2t"})],
        }
        response, body, log, made, closed = await _open(script)
        self.assertEqual(body, b"TSDATA")
        self.assertEqual(response.media_type, "video/mp2t")
        hits = Counter(_at_open(log))
        self.assertEqual(hits[TOKENIZED], 1,
                         f"the tokenized URL was fetched {hits[TOKENIZED]}x to open one "
                         f"raw-TS stream; each GET is a provider connection: {log}")
        self.assertEqual(hits[PANEL], 1, f"the panel URL was fetched {hits[PANEL]}x: {log}")

    async def test_raw_ts_upstream_is_closed_when_the_stream_ends(self):
        """Handing the open response to the generator must not leak it."""
        script = {
            PANEL: [_redirect(), _gone()],
            TOKENIZED: [_resp(200, TOKENIZED, b"TSDATA", {"content-type": "video/mp2t"})],
        }
        _, _, _, made, closed = await _open(script)
        self.assertEqual({id(c) for c in made}, {id(c) for c in closed},
                         "an httpx client opened for the stream was never closed")

    async def test_hls_channel_is_opened_with_one_get(self):
        playlist = ("#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nc1.ts\n"
                    "#EXT-X-ENDLIST\n")
        script = {
            PANEL: [_redirect()],
            TOKENIZED: [_resp(200, TOKENIZED, playlist.encode(), PLAYLIST_CT)],
            ROOT + "c1.ts": [_resp(200, ROOT + "c1.ts", b"CHUNK1")],
        }
        _, body, log, made, closed = await _open(script)
        self.assertEqual(body, b"CHUNK1",
                         "relative segment URIs must still resolve against the "
                         "tokenized URL, not the panel URL")
        hits = Counter(log)
        self.assertEqual(hits[TOKENIZED], 1,
                         f"the tokenized URL was fetched {hits[TOKENIZED]}x before the "
                         f"first segment of a VOD-style playlist: {log}")
        self.assertEqual(hits[PANEL], 1)
        self.assertEqual({id(c) for c in made}, {id(c) for c in closed})

    async def test_master_playlist_is_still_followed(self):
        """#68 must survive: a master playlist's lines are variants, not video."""
        master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow.m3u8\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=2800000\nhigh.m3u8\n")
        media = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg1.ts\n#EXT-X-ENDLIST\n"
        script = {
            PANEL: [_redirect()],
            TOKENIZED: [_resp(200, TOKENIZED, master.encode(), PLAYLIST_CT)],
            ROOT + "high.m3u8": [_resp(200, ROOT + "high.m3u8", media.encode(), PLAYLIST_CT)],
            ROOT + "seg1.ts": [_resp(200, ROOT + "seg1.ts", b"SEG1")],
        }
        _, body, log, _, _ = await _open(script)
        self.assertEqual(body, b"SEG1")
        self.assertEqual(Counter(log)[TOKENIZED], 1)

    async def test_every_redirect_hop_is_still_validated(self):
        """#73 must survive: the hop the provider redirects to is checked before
        it is fetched, and a refused hop is never requested."""
        checked = []

        def guard(url):
            checked.append(url)
            return "edge.provider.test" not in url

        script = {
            PANEL: [_redirect()],
            TOKENIZED: [_resp(200, TOKENIZED, b"SECRET", {"content-type": "video/mp2t"})],
        }
        with self.assertRaises(HTTPException) as ctx:
            await _open(script, guard=guard)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn(TOKENIZED, checked)

    async def test_a_refused_hop_is_never_fetched_and_nothing_leaks(self):

        def guard(url):
            return "edge.provider.test" not in url

        import routers.livetv as livetv
        log, closed, made = [], [], []

        def factory(**kw):
            c = FakeClient({PANEL: [_redirect()],
                            TOKENIZED: [_resp(200, TOKENIZED, b"SECRET")]}, log, closed, **kw)
            made.append(c)
            return c

        with patch("httpx.AsyncClient", factory):
            with self.assertRaises(HTTPException):
                await livetv._stream_proxy_inner(
                    channel_id=1, user_agent="UA", stream_url=PANEL,
                    _release_sem=lambda: None, guard=guard)
        self.assertNotIn(TOKENIZED, log)
        self.assertEqual({id(c) for c in made}, {id(c) for c in closed},
                         "client leaked on the refused-redirect path")

    async def test_a_509_at_open_retries_the_server_that_refused(self):
        """#86's open retry must survive, and must not re-walk the redirect chain
        (one more provider request per attempt) to do it."""
        script = {
            PANEL: [_redirect(), _gone()],
            TOKENIZED: [_resp(509, TOKENIZED),
                        _resp(509, TOKENIZED),
                        _resp(200, TOKENIZED, b"TSDATA", {"content-type": "video/mp2t"})],
        }
        _, body, log, _, _ = await _open(script)
        self.assertEqual(body, b"TSDATA")
        hits = Counter(_at_open(log))
        self.assertEqual(hits[TOKENIZED], 3, log)
        self.assertEqual(hits[PANEL], 1,
                         f"each retry re-walked the redirect chain: {log}")


if __name__ == "__main__":
    unittest.main()

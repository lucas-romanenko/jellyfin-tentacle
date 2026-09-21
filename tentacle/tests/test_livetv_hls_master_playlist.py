"""Tests for the Live TV HLS stream proxy (routers.livetv._stream_proxy_inner).

The HLS worker treats every non-comment line of the playlist as a media
segment. An Xtream `.m3u8` live URL very often resolves to a MASTER playlist
(`#EXT-X-STREAM-INF` + variant URIs), so the worker downloads the variant
playlist and yields its *text* to Jellyfin as if it were MPEG-TS, then — the
master having no `#EXT-X-ENDLIST` — loops forever re-reading the same master
and yielding nothing, because every variant URI is already in `seen_chunks`.
The tuner gets a few hundred bytes of ASCII and then silence.

Requires: fastapi + httpx (routers.livetv imports both). Run from the
tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from unittest import mock

import httpx

import routers.livetv as livetv

MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
    "360p/index.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\n"
    "720p/index.m3u8\n"
)

MEDIA = (
    "#EXTM3U\n"
    "#EXT-X-TARGETDURATION:4\n"
    "#EXT-X-MEDIA-SEQUENCE:100\n"
    "#EXTINF:4.0,\n"
    "seg100.ts\n"
    "#EXTINF:4.0,\n"
    "seg101.ts\n"
    "#EXT-X-ENDLIST\n"
)

TS_PAYLOAD = b"\x47" + b"TS-SEGMENT-BYTES" * 8


class _Resp:
    def __init__(self, url, body: bytes, content_type: str, status_code=200):
        self.url = url
        self._body = body
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    async def aread(self):
        return self._body

    async def aclose(self):
        return None

    async def aiter_bytes(self, chunk_size=65536):
        yield self._body

    @property
    def content(self):
        return self._body

    @property
    def text(self):
        return self._body.decode()


class _Request:
    def __init__(self, url):
        self.url = url


def _route(url: str):
    """The provider: .m3u8 → master, variant → media playlist, .ts → bytes."""
    if url.endswith("index.m3u8"):
        return _Resp(url, MEDIA.encode(), "application/vnd.apple.mpegurl")
    if url.endswith(".ts"):
        return _Resp(url, TS_PAYLOAD, "video/mp2t")
    return _Resp(url, MASTER.encode(), "application/vnd.apple.mpegurl")


class _FakeClient:
    requested: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        return None

    def build_request(self, method, url, headers=None):
        return _Request(url)

    async def send(self, request, stream=False):
        _FakeClient.requested.append(request.url)
        return _route(request.url)

    async def get(self, url, headers=None):
        _FakeClient.requested.append(url)
        return _route(url)


async def _collect(max_chunks=2, timeout=5.0):
    resp = await livetv._stream_proxy_inner(
        1, "TiviMate/4.7.0",
        "http://provider.example/live/u/p/1.m3u8",
        lambda: None,
    )
    chunks = []

    async def pump():
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
            if len(chunks) >= max_chunks:
                return

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return chunks


class HLSMasterPlaylistTests(unittest.TestCase):
    def setUp(self):
        _FakeClient.requested = []
        patcher = mock.patch.object(httpx, "AsyncClient", _FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        # provider.example does not resolve; stand in for a public provider.
        safe = mock.patch.object(livetv, "is_safe_url", lambda url: True)
        safe.start()
        self.addCleanup(safe.stop)

    def test_master_playlist_is_resolved_to_a_media_playlist(self):
        chunks = _run(_collect())
        self.assertTrue(chunks, "the tuner received no data at all")
        self.assertFalse(
            chunks[0].lstrip().startswith(b"#EXTM3U"),
            "an m3u8 playlist was piped to the tuner as if it were video",
        )

    def test_master_playlist_yields_the_real_segments(self):
        chunks = _run(_collect())
        self.assertIn(TS_PAYLOAD, chunks,
                      "no MPEG-TS segment ever reached the tuner")

    def test_a_variant_playlist_is_actually_fetched(self):
        _run(_collect())
        self.assertTrue(
            any(u.endswith("index.m3u8") for u in _FakeClient.requested),
            "the variant playlist was never requested",
        )


def _run(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()

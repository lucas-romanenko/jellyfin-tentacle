"""#68 (HLS master resolution) x #73 (redirect re-validation) together.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Once #73 builds the HLS client with follow_redirects=False, a variant playlist
fetched with a plain client.get() no longer follows a CDN redirect at all (the
3xx fails raise_for_status and the channel stops), and a redirect it did follow
would not be re-validated. The variant fetch must go through _send_checked like
every other upstream request in the proxy.
"""
import asyncio
import unittest
from unittest import mock

import httpx

import routers.livetv as livetv
from test_livetv_hls_master_playlist import (MASTER, MEDIA, TS_PAYLOAD, _FakeClient,
                                             _Request, _Resp, _collect)

CDN = "http://cdn.example/"


class _Redirect(_Resp):
    def __init__(self, url, location):
        super().__init__(url, b"", "text/plain", status_code=302)
        self.headers = {"location": location}


def _route_via(target_host):
    def _route(url):
        if url.startswith("http://provider.example/") and url.endswith("index.m3u8"):
            return _Redirect(url, target_host + url.split("/", 3)[3])
        if url.endswith("index.m3u8"):
            return _Resp(url, MEDIA.encode(), "application/vnd.apple.mpegurl")
        if url.endswith(".ts"):
            return _Resp(url, TS_PAYLOAD, "video/mp2t")
        return _Resp(url, MASTER.encode(), "application/vnd.apple.mpegurl")
    return _route


class _Client(_FakeClient):
    route = None

    async def send(self, request, stream=False):
        _FakeClient.requested.append(request.url)
        return _Client.route(request.url)

    async def get(self, url, headers=None):
        _FakeClient.requested.append(url)
        return _Client.route(url)


def _safe(url):
    return "10.0.0.5" not in url


class VariantRedirectTests(unittest.TestCase):
    def setUp(self):
        _FakeClient.requested = []
        for p in (mock.patch.object(httpx, "AsyncClient", _Client),
                  mock.patch.object(livetv, "is_safe_url", _safe)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_variant_behind_a_cdn_redirect_still_plays(self):
        _Client.route = staticmethod(_route_via(CDN))
        chunks = asyncio.run(_collect())
        self.assertTrue(chunks and chunks[0][:1] == b"\x47",
                        "a variant playlist behind a 302 to a public CDN produced no video")

    def test_a_variant_redirecting_to_a_private_host_is_not_fetched(self):
        _Client.route = staticmethod(_route_via("http://10.0.0.5:8096/"))
        asyncio.run(_collect())
        self.assertFalse(any("10.0.0.5" in u for u in _FakeClient.requested),
                         "the proxy fetched a private address a variant redirected to")


if __name__ == "__main__":
    unittest.main()

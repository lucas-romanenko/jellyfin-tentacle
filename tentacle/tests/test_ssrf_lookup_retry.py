"""A provider name that fails to resolve is asked again, not refused as an
SSRF block.

Run from the tentacle/ directory:  python -m unittest discover -s tests

An IPTV provider redirects every open to a fresh random subdomain. With four
channels opened at once (web multi-view) one of those lookups failed now and
then, and the redirect was refused as "Stream redirect points to a non-public
host": the log and the viewer were told about an address that was never
there, and the channel stayed off until opened again by hand.

A name that did not resolve (or resolved to no address) is now asked again
after a short pause (ssrf.LOOKUP_RETRY_WAITS) before giving up, and when it
does give up it says it was a lookup. A name that resolved to a private,
loopback or CGNAT address is refused on the first lookup, as before.
Everything here runs on a fake resolver and fake streams: no network.
"""
import asyncio
import socket
import unittest
from unittest import mock

import httpx
from fastapi import HTTPException

import routers.livetv as livetv
import routers.vod as vod
from services import ssrf
from test_livetv_hls_master_playlist import _FakeClient, _Request, _Resp

EAI_AGAIN = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
PANEL = "http://provider.example/live/u/p/1376.ts"
RANDOM = "http://hvdq86942747w.us1.cdn.example:80/live/u/p/324987"
LOOKUP_TEXT = "Couldn't look up the provider's server (DNS lookup failed)"


class _Resolver:
    """getaddrinfo with a script per host: each lookup takes the next answer
    (a list of IPs or an exception); the last one repeats."""

    def __init__(self, **script):
        self.script = {h.replace("_", "."): list(a) for h, a in script.items()}
        self.asked = []

    def __call__(self, host, *a, **kw):
        self.asked.append(host)
        answers = self.script.get(host) or [socket.gaierror(socket.EAI_NONAME, "Name or service not known")]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answer]

    def count(self, host):
        return self.asked.count(host)


class _Redirecting(_FakeClient):
    """The provider: its panel URL 302s to a random subdomain that serves TS."""

    def __init__(self, *a, **kw):
        self.sent = []

    async def send(self, request, stream=False):
        self.sent.append(request.url)
        if request.url == PANEL:
            r = _Resp(request.url, b"", "text/plain", status_code=302)
            r.headers = {"location": RANDOM}
            return r
        return _Resp(request.url, b"\x47" * 188, "video/mp2t")


class _Base(unittest.TestCase):
    waits = (0.01, 0.02)

    def setUp(self):
        p = mock.patch.object(ssrf, "LOOKUP_RETRY_WAITS", self.waits)
        p.start()
        self.addCleanup(p.stop)

    def resolve(self, **script):
        r = _Resolver(**script)
        p = mock.patch.object(ssrf.socket, "getaddrinfo", r)
        p.start()
        self.addCleanup(p.stop)
        return r


class TheWaits(unittest.TestCase):
    def test_two_retries_half_a_second_then_one_and_a_half(self):
        self.assertEqual((0.5, 1.5), ssrf.LOOKUP_RETRY_WAITS)


class CheckUrl(_Base):
    host = "hvdq86942747w.us1.cdn.example"

    def test_a_lookup_that_fails_once_is_asked_again_and_allowed(self):
        r = self.resolve(**{self.host: [EAI_AGAIN, ["104.18.12.246", "104.18.13.246"]]})
        with self.assertLogs("services.ssrf", level="INFO") as logs:
            ok, why = asyncio.run(ssrf.check_url(ssrf.is_safe_url, RANDOM))
        self.assertEqual((True, ""), (ok, why))
        self.assertEqual(2, r.count(self.host))
        self.assertIn("asking again in 0.01s", logs.output[0])

    def test_no_address_is_asked_again_too(self):
        r = self.resolve(**{self.host: [[], ["104.18.12.246"]]})
        self.assertTrue(asyncio.run(ssrf.check_url(ssrf.is_safe_url, RANDOM))[0])
        self.assertEqual(2, r.count(self.host))

    def test_a_lookup_that_keeps_failing_gives_up_after_two_retries(self):
        r = self.resolve(**{self.host: [EAI_AGAIN]})
        ok, why = asyncio.run(ssrf.check_url(ssrf.is_safe_url, RANDOM))
        self.assertFalse(ok)
        self.assertIsInstance(why, ssrf.LookupFailed)
        self.assertIn("could not be resolved", why)
        self.assertEqual(3, r.count(self.host))
        self.assertEqual(LOOKUP_TEXT, ssrf.refusal_text(why))

    def test_a_non_public_answer_is_refused_at_once_without_a_retry(self):
        for answer in (["10.0.0.5"], ["192.168.2.77"], ["127.0.0.1"], ["100.64.0.7"],
                       ["169.254.169.254"], ["104.18.12.246", "10.0.0.5"], ["::1"]):
            with self.subTest(answer=answer):
                r = self.resolve(**{self.host: [answer]})
                ok, why = asyncio.run(ssrf.check_url(ssrf.is_safe_url, RANDOM))
                self.assertFalse(ok)
                self.assertNotIsInstance(why, ssrf.LookupFailed)
                self.assertIn("not public", why)
                self.assertEqual(1, r.count(self.host))
                self.assertEqual("Stream URL points to a non-public host", ssrf.refusal_text(why))

    def test_a_retry_that_answers_private_is_still_refused(self):
        self.resolve(**{self.host: [EAI_AGAIN, ["10.0.0.5"]]})
        ok, why = asyncio.run(ssrf.check_url(ssrf.is_safe_url, RANDOM))
        self.assertFalse(ok)
        self.assertIn("not public: 10.0.0.5", why)

    def test_a_refusal_that_needs_no_lookup_is_not_retried(self):
        r = self.resolve()
        guard = ssrf.lan_origin_guard("http://provider.example")
        for url in ("ftp://provider.example/x", "http:///x", ""):
            self.assertEqual(False, asyncio.run(ssrf.check_url(guard, url))[0])
        self.assertEqual([], [h for h in r.asked if h != "provider.example"])

    def test_a_guard_without_a_reason_is_asked_once(self):
        calls = []
        guard = lambda url: calls.append(url) or False   # noqa: E731
        self.assertEqual((False, ""), asyncio.run(ssrf.check_url(guard, RANDOM)))
        self.assertEqual(1, len(calls))

    def test_a_lan_provider_whose_name_blips_is_asked_again(self):
        self.resolve(tuliprox_lan=[["192.168.2.52"], EAI_AGAIN, ["192.168.2.52"]])
        guard = ssrf.lan_origin_guard("http://tuliprox.lan:8890")
        self.assertTrue(asyncio.run(ssrf.check_url(guard, "http://tuliprox.lan:8890/live/u/p/1.ts"))[0])


class LiveRedirect(_Base):
    """_send_checked: the redirect hop to the random subdomain."""
    host = "hvdq86942747w.us1.cdn.example"
    log = "routers.livetv"

    def _open(self):
        client = _Redirecting()
        return client, asyncio.run(livetv._send_checked(client, PANEL, {}, ssrf.is_safe_url))

    def test_one_failed_lookup_of_the_redirect_target_still_opens_the_stream(self):
        r = self.resolve(**{self.host: [EAI_AGAIN, ["104.18.12.246"]]})
        client, resp = self._open()
        self.assertEqual(200, resp.status_code)
        self.assertEqual([PANEL, RANDOM], client.sent)
        self.assertEqual(2, r.count(self.host))

    def test_a_lookup_that_keeps_failing_says_so(self):
        self.resolve(**{self.host: [EAI_AGAIN]})
        with self.assertLogs(self.log, level="WARNING") as logs, \
                self.assertRaises(HTTPException) as cm:
            self._open()
        self.assertEqual(502, cm.exception.status_code)
        self.assertEqual(LOOKUP_TEXT, cm.exception.detail)
        line = "\n".join(logs.output)
        self.assertIn(f"{self.host} could not be resolved", line)
        self.assertIn("Temporary failure in name resolution", line)
        self.assertNotIn("non-public", line)

    def test_a_redirect_to_a_private_address_is_blocked_at_once(self):
        r = self.resolve(**{self.host: [["10.0.0.5"]]})
        with self.assertLogs(self.log, level="WARNING") as logs, \
                self.assertRaises(HTTPException) as cm:
            self._open()
        self.assertEqual("Stream redirect points to a non-public host", cm.exception.detail)
        self.assertIn("not public: 10.0.0.5", "\n".join(logs.output))
        self.assertEqual(1, r.count(self.host))


class VodRedirect(LiveRedirect):
    """The VOD relay's own redirect walk, the same way."""

    log = "routers.vod"

    def setUp(self):
        super().setUp()
        vod._targets.clear()
        self.addCleanup(vod._targets.clear)

    def _open(self):
        client = _Redirecting()
        return client, asyncio.run(vod._open(client, "GET", PANEL, {}, ssrf.is_safe_url))


class HlsSegments(_Base):
    """The HLS worker's per-origin check of segment lines: a lookup that
    failed even after its retries is not kept for the rest of the stream."""

    def test_a_segment_host_that_blips_is_asked_again_for_the_next_segment(self):
        seg = "seg.example"
        media = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:100\n"
                 f"#EXTINF:4.0,\nhttp://{seg}/a/seg100.ts\n"
                 f"#EXTINF:4.0,\nhttp://{seg}/a/seg101.ts\n#EXT-X-ENDLIST\n")
        sent = []

        class Client(_FakeClient):
            async def send(self, request, stream=False):
                sent.append(request.url)
                if request.url.endswith(".ts"):
                    return _Resp(request.url, b"\x47" + request.url[-9:].encode(), "video/mp2t")
                return _Resp(request.url, media.encode(), "application/vnd.apple.mpegurl")

        # The first segment's lookup fails all three times, the second's works.
        self.resolve(provider_example=[["93.184.216.34"]],
                     seg_example=[EAI_AGAIN, EAI_AGAIN, EAI_AGAIN, ["93.184.216.36"]])

        async def run():
            resp = await livetv._stream_proxy_inner(1, "UA", "http://provider.example/live/u/p/1.m3u8",
                                                    lambda: None)
            out = []

            async def pump():
                async for chunk in resp.body_iterator:
                    out.append(chunk)
            try:
                await asyncio.wait_for(pump(), 5.0)
            except asyncio.TimeoutError:
                pass
            return out

        with mock.patch.object(httpx, "AsyncClient", Client), \
                self.assertLogs("routers.livetv", level="WARNING") as logs:
            chunks = asyncio.run(run())
        self.assertNotIn(f"http://{seg}/a/seg100.ts", sent)
        self.assertIn(f"http://{seg}/a/seg101.ts", sent)
        self.assertTrue(any(b"seg101" in c for c in chunks), chunks)
        self.assertTrue(any("seg.example could not be resolved" in l for l in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()

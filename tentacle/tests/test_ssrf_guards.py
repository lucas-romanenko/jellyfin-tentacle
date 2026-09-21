"""SSRF guard coverage.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Nothing here touches the network: every case either uses an IP literal (which
getaddrinfo resolves locally) or is rejected by the host allowlist before DNS.
"""
import unittest
from unittest import mock

import httpx

from services.ssrf import _ip_is_blocked, is_safe_url


class IpRangeTests(unittest.TestCase):
    def test_blocks_the_usual_internal_ranges(self):
        for ip in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.1",
                   "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(_ip_is_blocked(ip), f"{ip} should be blocked")

    def test_blocks_rfc6598_shared_address_space(self):
        """100.64.0.0/10 is the CGNAT range Tailscale/Headscale hand out.

        It is neither `is_private` nor `is_reserved`, so the original predicate
        list let it through — on a home server that is exactly the internal
        network the guard exists to protect.
        """
        for ip in ("100.64.0.1", "100.100.100.100", "100.127.255.254"):
            self.assertTrue(_ip_is_blocked(ip), f"{ip} (CGNAT) should be blocked")
        self.assertFalse(is_safe_url("http://100.64.0.1/x"))

    def test_still_allows_public_addresses(self):
        for ip in ("8.8.8.8", "1.1.1.1"):
            self.assertFalse(_ip_is_blocked(ip), f"{ip} should be allowed")
        self.assertTrue(is_safe_url("http://8.8.8.8/x"))


class ListSubscriptionUrlTests(unittest.TestCase):
    """A list URL is supplied by any logged-in (non-admin) user and is re-fetched
    by the nightly scheduler, so it must be pinned to the provider's own host."""

    def test_rejects_internal_targets(self):
        from routers.lists import list_url_is_allowed
        for url in ("http://127.0.0.1:8096/Users",
                    "http://10.0.0.5:7878/api/v3/system/status",
                    "http://169.254.169.254/latest/meta-data/"):
            self.assertFalse(list_url_is_allowed("letterboxd", url), url)
            self.assertFalse(list_url_is_allowed("trakt", url), url)

    def test_rejects_arbitrary_public_hosts(self):
        """The Trakt branch sends the real trakt-api-key as a header, so an
        off-host URL leaks the credential as well as enabling the fetch."""
        from routers.lists import list_url_is_allowed
        self.assertFalse(list_url_is_allowed("trakt", "https://attacker.example/users/x/lists/y"))
        self.assertFalse(list_url_is_allowed("letterboxd", "https://attacker.example/x/list/y/"))
        # look-alike suffixes must not pass either
        self.assertFalse(list_url_is_allowed("trakt", "https://trakt.tv.attacker.example/x"))

    def test_allows_the_real_providers(self):
        from routers.lists import list_url_is_allowed
        with mock.patch("services.ssrf.host_is_public", return_value=True):
            self.assertTrue(list_url_is_allowed("letterboxd", "https://letterboxd.com/u/list/l/"))
            self.assertTrue(list_url_is_allowed("trakt", "https://trakt.tv/users/u/lists/l"))

    def test_types_that_never_fetch_the_user_url_are_untouched(self):
        from routers.lists import list_url_is_allowed
        self.assertTrue(list_url_is_allowed("imdb_rss", "https://www.imdb.com/chart/top/"))


class StreamRedirectTests(unittest.IsolatedAsyncioTestCase):
    """The live stream proxy is a public, unauthenticated route. Following
    redirects with httpx's own follow_redirects defeats the is_safe_url
    pre-flight, because the URL that was checked is not the URL finally fetched.
    """

    async def test_redirect_to_an_internal_host_is_refused(self):
        from fastapi import HTTPException
        from routers.livetv import _send_checked

        def handler(request):
            if request.url.host == "cdn.example":
                return httpx.Response(302, headers={"location": "http://127.0.0.1:8096/Users"})
            return httpx.Response(200, content=b"internal")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     follow_redirects=False) as client:
            with self.assertRaises(HTTPException) as ctx:
                await _send_checked(client, "http://cdn.example/live.ts", {})
        self.assertEqual(ctx.exception.status_code, 502)

    async def test_ordinary_response_passes_through(self):
        from routers.livetv import _send_checked

        def handler(request):
            return httpx.Response(200, content=b"ts-bytes")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     follow_redirects=False) as client:
            resp = await _send_checked(client, "http://cdn.example/live.ts", {})
            try:
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(await resp.aread(), b"ts-bytes")
            finally:
                await resp.aclose()

    async def test_redirect_to_a_public_host_is_followed(self):
        from routers.livetv import _send_checked

        def handler(request):
            if request.url.path == "/live.ts":
                return httpx.Response(302, headers={"location": "http://8.8.8.8/token.ts"})
            return httpx.Response(200, content=b"ok")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     follow_redirects=False) as client:
            resp = await _send_checked(client, "http://cdn.example/live.ts", {})
            try:
                self.assertEqual(resp.status_code, 200)
            finally:
                await resp.aclose()


if __name__ == "__main__":
    unittest.main()

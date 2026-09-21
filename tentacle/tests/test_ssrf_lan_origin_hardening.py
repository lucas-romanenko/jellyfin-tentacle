"""#76 hardening: the LAN-provider exemption must not be widenable.

Run from the tentacle/ directory:  python -m unittest discover -s tests

lan_origin_guard() deliberately opens a hole in the SSRF guard: one origin,
for one provider the admin configured on a LAN address. These tests pin the
edges of that hole, so a later refactor can't quietly widen it into "any
private address is fine".
"""
import unittest
from unittest import mock

from services import ssrf


def _dns(mapping):
    """Patch getaddrinfo with a name -> [ip] map; unknown names fail to resolve."""
    def fake(host, *a, **kw):
        if host in mapping:
            return [(None, None, None, None, (ip, 0)) for ip in mapping[host]]
        import socket
        raise socket.gaierror(f"no such host {host}")
    return mock.patch.object(ssrf.socket, "getaddrinfo", fake)


LAN = {"tuliprox.lan": ["192.168.1.5"], "evil.example": ["93.184.216.34"],
       "mixed.lan": ["192.168.1.5", "93.184.216.34"]}


class GuardEdges(unittest.TestCase):
    def test_userinfo_cannot_smuggle_a_lan_target_past_the_origin_check(self):
        """A userinfo part that looks like the trusted origin must not buy trust:
        the host is what follows the @, and it gets no exemption."""
        with _dns({**LAN, "other.lan": ["10.0.0.5"]}):
            guard = ssrf.lan_origin_guard("http://tuliprox.lan:8890")
            # The real target is another LAN host -> refused.
            self.assertFalse(guard("http://tuliprox.lan@other.lan:8890/x.ts"))
            self.assertFalse(guard("http://tuliprox.lan@10.0.0.5:8890/x.ts"))
            # A genuinely public host is allowed, but by is_safe_url, not by the
            # exemption -- so it is still refused once it points back at the LAN.
            self.assertTrue(guard("http://tuliprox.lan@evil.example:8890/x.ts"))

    def test_a_trailing_dot_fqdn_does_not_match_the_origin(self):
        """Fails closed: the name differs, so it falls back to is_safe_url."""
        with _dns({**LAN, "tuliprox.lan.": ["192.168.1.5"]}):
            guard = ssrf.lan_origin_guard("http://tuliprox.lan:8890")
            self.assertFalse(guard("http://tuliprox.lan.:8890/x.ts"))

    def test_case_is_normalised(self):
        with _dns(LAN):
            guard = ssrf.lan_origin_guard("http://tuliprox.lan:8890")
            self.assertTrue(guard("http://TULIPROX.LAN:8890/x.ts"))

    def test_default_port_is_normalised_both_ways(self):
        with _dns(LAN):
            self.assertTrue(ssrf.lan_origin_guard("http://tuliprox.lan")("http://tuliprox.lan:80/x.ts"))
            self.assertTrue(ssrf.lan_origin_guard("http://tuliprox.lan:80")("http://tuliprox.lan/x.ts"))

    def test_https_provider_does_not_trust_http_origin(self):
        with _dns(LAN):
            guard = ssrf.lan_origin_guard("https://tuliprox.lan:8890")
            self.assertFalse(guard("http://tuliprox.lan:8890/x.ts"))

    def test_a_provider_resolving_to_both_lan_and_public_is_not_trusted(self):
        """Fail closed: a name that also answers with a public address could be
        attacker-controlled DNS, so it gets no exemption at all."""
        with _dns(LAN):
            guard = ssrf.lan_origin_guard("http://mixed.lan:8890")
            self.assertIs(guard, ssrf.is_safe_url)

    def test_non_http_schemes_are_refused_even_on_the_trusted_origin(self):
        with _dns(LAN):
            guard = ssrf.lan_origin_guard("http://tuliprox.lan:8890")
            for url in ("file:///etc/passwd", "gopher://tuliprox.lan:8890/x",
                        "ftp://tuliprox.lan:8890/x"):
                self.assertFalse(guard(url), url)

    def test_empty_and_malformed_urls_are_refused(self):
        with _dns(LAN):
            guard = ssrf.lan_origin_guard("http://tuliprox.lan:8890")
            for url in ("", None, "http://", "not a url", "http://tuliprox.lan:99999/x"):
                self.assertFalse(guard(url), repr(url))

    def test_an_unparseable_provider_url_yields_the_strict_guard(self):
        for bad in ("", None, "not a url", "file:///x", "http://:8890"):
            self.assertIs(ssrf.lan_origin_guard(bad), ssrf.is_safe_url, repr(bad))

    def test_an_ipv6_ula_provider_is_lan(self):
        """Bare IP literals must not depend on DNS working: getaddrinfo returns
        the literal itself, so real resolution is used here deliberately."""
        guard = ssrf.lan_origin_guard("http://[fd00::1]:8890")
        self.assertTrue(guard("http://[fd00::1]:8890/x.ts"))
        self.assertFalse(guard("http://[fd00::2]:8890/x.ts"))

    def test_an_ipv4_literal_provider_is_lan_without_dns(self):
        guard = ssrf.lan_origin_guard("http://192.168.2.52:8890")
        self.assertTrue(guard("http://192.168.2.52:8890/live/1.ts"))
        self.assertFalse(guard("http://192.168.2.52:8096/Users"))
        self.assertFalse(guard("http://192.168.2.53:8890/live/1.ts"))

    def test_loopback_is_never_lan_even_though_it_is_private(self):
        for ip in ("127.0.0.1", "::1", "169.254.169.254", "0.0.0.0", "224.0.0.1"):
            self.assertFalse(ssrf._ip_is_lan(ip), ip)

    def test_the_exempt_ranges_are_exactly_the_documented_ones(self):
        for ip in ("10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.5",
                   "100.76.178.110", "fd00::1"):
            self.assertTrue(ssrf._ip_is_lan(ip), ip)
        for ip in ("172.15.0.1", "172.32.0.1", "100.63.255.255", "100.128.0.1",
                   "11.0.0.1", "fe80::1", "93.184.216.34"):
            self.assertFalse(ssrf._ip_is_lan(ip), ip)

    def test_ipv4_mapped_ipv6_cannot_smuggle_a_lan_address_past_is_safe_url(self):
        """::ffff:192.168.1.5 is the LAN address written differently."""
        self.assertTrue(ssrf._ip_is_lan("::ffff:192.168.1.5"))
        self.assertTrue(ssrf._ip_is_blocked("::ffff:192.168.1.5"))


if __name__ == "__main__":
    unittest.main()

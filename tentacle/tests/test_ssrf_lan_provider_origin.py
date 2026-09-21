"""#76: a provider the admin put on the LAN (tuliprox / xTeVe / Threadfin).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Stream URLs on the provider's own configured origin are allowed when that
origin resolves only to LAN addresses. Nothing else is loosened.
"""
import asyncio
import socket
import unittest
from unittest import mock

from services import ssrf

DNS = {
    "tuliprox.lan": ["192.168.2.52"],
    "192.168.2.52": ["192.168.2.52"],
    "100.101.102.103": ["100.101.102.103"],
    "provider.example": ["93.184.216.34"],
    "rebind.example": ["93.184.216.35"],
    "127.0.0.1": ["127.0.0.1"],
    "169.254.169.254": ["169.254.169.254"],
    "10.0.0.5": ["10.0.0.5"],
}


def _gai(host, *a, **kw):
    if host not in DNS:
        raise socket.gaierror("nx")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in DNS[host]]


class LanOriginGuard(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ssrf.socket, "getaddrinfo", _gai)
        p.start()
        self.addCleanup(p.stop)

    def test_tuliprox_stream_on_its_own_origin_is_allowed(self):
        g = ssrf.lan_origin_guard("http://192.168.2.52:8890")
        self.assertTrue(g("http://192.168.2.52:8890/live/u/p/220.ts"))
        self.assertTrue(g("http://192.168.2.52:8890/hls/abc/seg1.ts"))

    def test_a_hostname_provider_on_the_lan_works_too(self):
        self.assertTrue(ssrf.lan_origin_guard("http://tuliprox.lan:8890")("http://tuliprox.lan:8890/x.ts"))

    def test_tailscale_cgnat_provider_is_lan(self):
        self.assertTrue(ssrf.lan_origin_guard("http://100.101.102.103:8890")("http://100.101.102.103:8890/x.ts"))

    def test_another_port_on_the_same_box_is_still_blocked(self):
        g = ssrf.lan_origin_guard("http://192.168.2.52:8890")
        self.assertFalse(g("http://192.168.2.52:8096/Users"), "Jellyfin on the same host was reachable")

    def test_another_lan_host_is_still_blocked(self):
        self.assertFalse(ssrf.lan_origin_guard("http://192.168.2.52:8890")("http://10.0.0.5/"))

    def test_scheme_must_match_too(self):
        self.assertFalse(ssrf.lan_origin_guard("http://192.168.2.52:8890")("https://192.168.2.52:8890/x"))

    def test_loopback_and_metadata_providers_are_never_trusted(self):
        for url in ("http://127.0.0.1:8890", "http://169.254.169.254"):
            self.assertFalse(ssrf.lan_origin_guard(url)(url + "/x"), url)

    def test_a_public_provider_is_unchanged(self):
        g = ssrf.lan_origin_guard("http://provider.example")
        self.assertIs(g, ssrf.is_safe_url)
        self.assertTrue(g("http://provider.example/live/1.ts"))

    def test_a_public_provider_that_rebinds_to_the_lan_is_not_trusted(self):
        g = ssrf.lan_origin_guard("http://rebind.example")
        with mock.patch.dict(DNS, {"rebind.example": ["192.168.2.1"]}):
            self.assertFalse(g("http://rebind.example/live/1.ts"))


class StreamRouteUsesTheProviderOrigin(unittest.TestCase):
    """The public /api/live/stream route, end to end up to the fetch."""

    def setUp(self):
        import tempfile
        import models.database as mdb
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        p = mock.patch.object(ssrf.socket, "getaddrinfo", _gai)
        p.start()
        self.addCleanup(p.stop)
        engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def _channel(self, server_url, stream_url):
        from models.database import LiveChannel, Provider
        prov = Provider(name="P", server_url=server_url, username="u", password="p")
        self.db.add(prov)
        self.db.commit()
        ch = LiveChannel(provider_id=prov.id, name="C", stream_url=stream_url)
        self.db.add(ch)
        self.db.commit()
        return ch.id

    def _call(self, cid):
        import routers.livetv as livetv
        seen = {}

        async def _inner(channel_id, ua, url, release, guard=None):
            release()
            seen["url"], seen["guard"] = url, guard
            return "streamed"

        with mock.patch.object(livetv, "_stream_proxy_inner", _inner):
            try:
                return asyncio.run(livetv.stream_proxy(cid, self.db)), seen
            except Exception as e:           # HTTPException from the guard
                return e, seen

    def test_tuliprox_channel_is_served(self):
        cid = self._channel("http://192.168.2.52:8890", "http://192.168.2.52:8890/live/u/p/220.ts")
        result, seen = self._call(cid)
        self.assertEqual("streamed", result)
        self.assertFalse(seen["guard"]("http://192.168.2.52:8096/"), "the guard handed down was not scoped")

    def test_private_stream_url_under_a_public_provider_is_still_blocked(self):
        cid = self._channel("http://provider.example", "http://10.0.0.5/live/1.ts")
        result, _ = self._call(cid)
        self.assertEqual(502, getattr(result, "status_code", None))


if __name__ == "__main__":
    unittest.main()

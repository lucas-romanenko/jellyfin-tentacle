"""An edit to an Xtream provider's server, username or password reaches its
channels' stream URLs at once, without a channel sync (#469).

An Xtream channel's URL is {server}/live/{user}/{pass}/{id}.{ext}, written at
channel-sync time. Before the fix the Providers page (PUT /api/providers/{id})
and POST /api/live/provider changed only the provider row, so every tune and
recording kept sending the old login (401 -> 502) or went to the old host (a
LAN re-streamer on a new host: 502 "non-public host") until a channel sync.
"""
import asyncio
import socket
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv
import routers.providers as providers
from models.database import LiveChannel, LiveChannelGroup, Provider
from services import ssrf, xtream_client
from tmp_dirs import temp_dir

DNS = {"old-panel.example": "93.184.216.34", "new-panel.example": "93.184.216.35",
       "192.168.1.10": "192.168.1.10", "192.168.1.20": "192.168.1.20"}


def _gai(host, *a, **kw):
    if host not in DNS:
        raise socket.gaierror("nx")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (DNS[host], 0))]


class _Panel(xtream_client.XtreamClient):
    def get_live_streams(self, category_id=None):
        return [{"stream_id": 101, "name": "TSN 1", "category_id": "7"},
                {"stream_id": 102, "name": "TSN 2", "category_id": "7"}]

    def authenticate(self):
        return {}


class ProviderEditReachesChannels(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ssrf.socket, "getaddrinfo", _gai)
        p.start()
        self.addCleanup(p.stop)
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def _synced(self, server, ptype="xtream"):
        prov = Provider(name="Live", provider_type=ptype, server_url=server, username="u",
                        password="p1", live_tv_enabled=True, active=False)
        self.db.add(prov)
        self.db.commit()
        self.db.add(LiveChannelGroup(provider_id=prov.id, name="Sports", category_id="7", enabled=True))
        self.db.commit()
        with mock.patch.object(xtream_client, "XtreamClient", _Panel):
            livetv._sync_channels_from_xtream(livetv._snapshot_provider(prov, "xtream"), self.db)
        ch = self.db.query(LiveChannel).filter(LiveChannel.stream_id == "101").one()
        return prov.id, ch.id

    def _tune(self, cid):
        seen = {}

        async def inner(channel_id, ua, url, release, guard=None, **kw):
            release()
            seen["url"] = url
            return "streamed"

        self.db.expire_all()
        with mock.patch.object(livetv, "_stream_proxy_inner", inner):
            try:
                return asyncio.run(livetv.stream_proxy(cid, self.db)), seen.get("url")
            except Exception as e:
                return e, seen.get("url")

    def _urls(self):
        self.db.expire_all()
        return sorted((c.stream_id, c.stream_url) for c in self.db.query(LiveChannel).all())

    def test_providers_page_new_server_and_password(self):
        pid, cid = self._synced("http://old-panel.example:8080")
        providers.update_provider(pid, providers.ProviderUpdate(
            server_url="http://new-panel.example:8080", password="p2"), self.db)
        self.assertEqual(("streamed", "http://new-panel.example:8080/live/u/p2/101.m3u8"), self._tune(cid))
        self.assertEqual([("101", "http://new-panel.example:8080/live/u/p2/101.m3u8"),
                          ("102", "http://new-panel.example:8080/live/u/p2/102.m3u8")], self._urls())

    def test_providers_page_new_username(self):
        pid, cid = self._synced("http://old-panel.example:8080")
        providers.update_provider(pid, providers.ProviderUpdate(username="u2"), self.db)
        self.assertEqual(("streamed", "http://old-panel.example:8080/live/u2/p1/101.m3u8"), self._tune(cid))

    def test_live_provider_api_new_password(self):
        pid, cid = self._synced("http://old-panel.example:8080")
        livetv.save_live_provider(livetv.LiveProviderConfig(password="p2"), self.db)
        self.assertEqual(("streamed", "http://old-panel.example:8080/live/u/p2/101.m3u8"), self._tune(cid))

    def test_lan_restreamer_on_a_new_host(self):
        pid, cid = self._synced("http://192.168.1.10:8890")
        livetv.save_live_provider(livetv.LiveProviderConfig(server_url="http://192.168.1.20:8890"), self.db)
        self.assertEqual(("streamed", "http://192.168.1.20:8890/live/u/p1/101.m3u8"), self._tune(cid))

    def test_trailing_slash_and_extension_kept(self):
        """The URL reads what a channel sync would write: the server without
        its trailing slash, the stream format the channel already had."""
        pid, cid = self._synced("http://old-panel.example:8080")
        ch = self.db.get(LiveChannel, cid)
        ch.stream_url = "http://old-panel.example:8080/live/u/p1/101.ts"
        self.db.commit()
        providers.update_provider(pid, providers.ProviderUpdate(
            server_url="http://new-panel.example:8080/"), self.db)
        self.assertEqual(("streamed", "http://new-panel.example:8080/live/u/p1/101.ts"), self._tune(cid))

    def test_masked_password_changes_nothing(self):
        pid, cid = self._synced("http://old-panel.example:8080")
        before = self._urls()
        livetv.save_live_provider(livetv.LiveProviderConfig(password="••••••••"), self.db)
        self.assertEqual(before, self._urls())

    def test_other_edits_leave_urls_alone(self):
        pid, cid = self._synced("http://old-panel.example:8080")
        before = self._urls()
        providers.update_provider(pid, providers.ProviderUpdate(name="Renamed", priority=3), self.db)
        self.assertEqual(before, self._urls())

    def test_m3u_channel_urls_untouched(self):
        """M3U URLs come from the playlist, not from server/user/password."""
        prov = Provider(name="M3U", provider_type="m3u_url", server_url="", username="", password="",
                        m3u_url="http://old-panel.example/get.php", live_tv_enabled=True, active=False)
        self.db.add(prov)
        self.db.commit()
        self.db.add(LiveChannel(provider_id=prov.id, name="A", stream_id="abc",
                                stream_url="http://old-panel.example/abc.ts", enabled=True))
        self.db.commit()
        providers.update_provider(prov.id, providers.ProviderUpdate(
            server_url="http://new-panel.example", username="x", password="y"), self.db)
        self.assertEqual([("abc", "http://old-panel.example/abc.ts")], self._urls())


if __name__ == "__main__":
    unittest.main()

"""Saving a Live TV provider with a new server address, username or password
must reach its channels: the tuner opens the URL of the provider as it is now.

Run from the tentacle/ directory:  python -m unittest discover -s tests

An Xtream channel sync writes each channel's URL with the server and the login
in it ({server}/live/{user}/{pass}/{id}.{ext}), and stream_proxy opens that
URL. Saving the provider (PUT /api/providers/{id}, the Providers page, or
POST /api/live/provider, the Live TV provider API) changed only the provider
row, and no scheduled job re-syncs channels. After a routine change (the panel
moved to a new domain, a password reset, a LAN re-streamer on a new host)
every tune and every recording still went to the old server or login until
someone pressed "Sync channels"; a re-streamer's channels were even refused,
because the LAN guard follows the new address.
"""
import asyncio
import logging
import socket
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv
import routers.providers as providers
from models.database import LiveChannel, LiveChannelGroup, Provider, set_setting
from services import ssrf
from services import xtream_client
from tmp_dirs import temp_dir

DNS = {
    "old-panel.example": ["93.184.216.34"],
    "new-panel.example": ["93.184.216.35"],
    "192.168.1.10": ["192.168.1.10"],
    "192.168.1.20": ["192.168.1.20"],
}
UA = "TiviMate/4.7.0 (Linux; Android 12)"


def _gai(host, *a, **kw):
    if host not in DNS:
        raise socket.gaierror("nx")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in DNS[host]]


class _FakePanel(xtream_client.XtreamClient):
    """The real client (so live_stream_url is the real URL builder); only the
    two calls a channel sync makes are answered here."""

    def get_live_streams(self, category_id=None):
        return [
            {"stream_id": 101, "name": "TSN 1", "category_id": "7", "epg_channel_id": "tsn1.ca"},
            {"stream_id": 102, "name": "TSN 2", "category_id": "7", "epg_channel_id": "tsn2.ca"},
        ]

    def authenticate(self):
        return {}


class _NoPanel(xtream_client.XtreamClient):
    def __init__(self, *a, **kw):
        raise AssertionError("saving the provider must not contact it")


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        p = mock.patch.object(ssrf.socket, "getaddrinfo", _gai)
        p.start()
        self.addCleanup(p.stop)
        engine = create_engine(f"sqlite:///{tmp}/t.db", connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def _xtream(self, server_url, password="p1", name="Live", group="7"):
        prov = Provider(name=name, provider_type="xtream", server_url=server_url,
                        username="u", password=password, live_tv_enabled=True, active=False)
        self.db.add(prov)
        self.db.commit()
        self.db.add(LiveChannelGroup(provider_id=prov.id, name="Sports", category_id=group, enabled=True))
        self.db.commit()
        self._sync(prov.id)
        return prov.id

    def _sync(self, pid):
        """'Sync channels' with the provider as saved now."""
        self.db.expire_all()
        prov = self.db.get(Provider, pid)
        with mock.patch.object(xtream_client, "XtreamClient", _FakePanel):
            livetv._sync_channels_from_xtream(livetv._snapshot_provider(prov, "xtream"), self.db)

    def _urls(self, pid):
        self.db.expire_all()
        return {c.stream_id: c.stream_url for c in self.db.query(LiveChannel).filter_by(provider_id=pid)}

    def _rows(self, pid):
        self.db.expire_all()
        return {c.stream_id: (c.id, c.name, c.enabled, c.channel_number, c.sort_order)
                for c in self.db.query(LiveChannel).filter_by(provider_id=pid)}

    def _cid(self, pid, sid="101"):
        return self.db.query(LiveChannel).filter_by(provider_id=pid, stream_id=sid).one().id

    def _expected(self, server_url, password, sid=101, username="u", ext="m3u8"):
        """What 'Sync channels' writes for these settings (the real builder)."""
        client = _FakePanel(server_url, username, password)
        self.addCleanup(client.close)
        return client.live_stream_url(sid, extension=ext)

    def _put(self, pid, **fields):
        with mock.patch.object(xtream_client, "XtreamClient", _NoPanel):
            providers.update_provider(pid, providers.ProviderUpdate(**fields), self.db)

    def _save_live_provider(self, **fields):
        with mock.patch.object(xtream_client, "XtreamClient", _NoPanel):
            livetv.save_live_provider(livetv.LiveProviderConfig(**fields), self.db)

    def _tune(self, cid):
        """GET /api/live/stream/{cid}, up to the request to the provider."""
        seen = {}

        async def _inner(channel_id, ua, url, release, guard=None, **kw):
            release()
            seen["url"] = url
            return "streamed"

        self.db.expire_all()
        with mock.patch.object(livetv, "_stream_proxy_inner", _inner):
            try:
                return asyncio.run(livetv.stream_proxy(cid, self.db)), seen
            except Exception as e:  # HTTPException (guard refusal)
                return e, seen


class ProviderEditReachesTheTuner(_Base):
    def test_a_synced_channel_opens_the_provider_as_synced(self):
        pid = self._xtream("http://old-panel.example:8080")
        result, seen = self._tune(self._cid(pid))
        self.assertEqual("streamed", result)
        self.assertEqual(self._expected("http://old-panel.example:8080", "p1"), seen["url"])

    def test_providers_page_new_server_and_password(self):
        pid = self._xtream("http://old-panel.example:8080")
        self._put(pid, server_url="http://new-panel.example:8080", password="p2")
        result, seen = self._tune(self._cid(pid))
        self.assertEqual("streamed", result)
        self.assertEqual(self._expected("http://new-panel.example:8080", "p2"), seen.get("url"),
                         "after the provider was edited the tune still opened the old server/login")

    def test_live_provider_api_new_password(self):
        pid = self._xtream("http://old-panel.example:8080")
        self._save_live_provider(password="p2")
        result, seen = self._tune(self._cid(pid))
        self.assertEqual("streamed", result)
        self.assertEqual(self._expected("http://old-panel.example:8080", "p2"), seen.get("url"),
                         "after a password reset the tune still sent the old password")

    def test_live_provider_api_new_username(self):
        pid = self._xtream("http://old-panel.example:8080")
        self._save_live_provider(username="u2")
        self.assertEqual(self._expected("http://old-panel.example:8080", "p1", username="u2"),
                         self._urls(pid)["101"])

    def test_lan_restreamer_on_a_new_host_still_tunes(self):
        pid = self._xtream("http://192.168.1.10:8890")
        self._save_live_provider(server_url="http://192.168.1.20:8890")
        result, seen = self._tune(self._cid(pid))
        self.assertEqual("streamed", result,
                         f"tune refused ({getattr(result, 'status_code', result)} "
                         f"{getattr(result, 'detail', '')}): the channel still pointed at the old LAN host")
        self.assertEqual(self._expected("http://192.168.1.20:8890", "p1"), seen.get("url"))


class TheRewriteIsWhatASyncWrites(_Base):
    def test_every_channel_gets_the_url_a_sync_writes_and_keeps_its_row(self):
        pid = self._xtream("http://old-panel.example:8080/")
        rows = self._rows(pid)
        self._put(pid, server_url="http://new-panel.example:8080", password="p2")
        edited = self._urls(pid)
        self.assertEqual({"101": self._expected("http://new-panel.example:8080", "p2", sid=101),
                          "102": self._expected("http://new-panel.example:8080", "p2", sid=102)}, edited)
        self.assertEqual(rows, self._rows(pid), "ids, names, enabled flags and numbers are unchanged")
        self._sync(pid)
        self.assertEqual(self._urls(pid), edited, "a channel sync afterwards changes no URL")
        self.assertEqual({"101", "102"}, set(edited))

    def test_the_extension_stays_as_synced(self):
        set_setting(self.db, "livetv_stream_format", "ts")
        self.db.commit()
        pid = self._xtream("http://old-panel.example:8080")
        self._save_live_provider(password="p2")
        self.assertEqual(self._expected("http://old-panel.example:8080", "p2", ext="ts"), self._urls(pid)["101"])

    def test_edits_that_keep_server_and_login_change_no_url(self):
        pid = self._xtream("http://old-panel.example:8080")
        before = self._urls(pid)
        self._put(pid, name="Renamed", user_agent="VLC/3.0", epg_url="http://old-panel.example:8080/xmltv.php",
                  priority=2, active=False)
        # the masked password that GET /api/live/provider returns
        self._save_live_provider(password="••••••••", server_url="http://old-panel.example:8080", username="u")
        self.assertEqual(before, self._urls(pid))

    def test_another_providers_channels_are_left_alone(self):
        pid = self._xtream("http://old-panel.example:8080")
        other = self._xtream("http://old-panel.example:8080", name="Second")
        before = self._urls(other)
        self._put(pid, password="p2")
        self.assertEqual(before, self._urls(other))
        self.assertEqual(self._expected("http://old-panel.example:8080", "p2"), self._urls(pid)["101"])

    def test_a_url_the_old_settings_did_not_build_is_left_alone(self):
        pid = self._xtream("http://old-panel.example:8080")
        ch = self.db.query(LiveChannel).filter_by(provider_id=pid, stream_id="102").one()
        ch.stream_url = "http://elsewhere.example/hand/made.ts"
        self.db.commit()
        self._put(pid, server_url="http://new-panel.example:8080")
        urls = self._urls(pid)
        self.assertEqual("http://elsewhere.example/hand/made.ts", urls["102"])
        self.assertEqual(self._expected("http://new-panel.example:8080", "p1"), urls["101"])

    def test_m3u_channels_and_a_switch_to_m3u_are_left_alone(self):
        m3u = Provider(name="M3U", provider_type="m3u_url", m3u_url="http://old-panel.example/get.php?t=a",
                       server_url="", username="", password="", live_tv_enabled=False)
        self.db.add(m3u)
        self.db.commit()
        self.db.add(LiveChannel(provider_id=m3u.id, name="One", stream_id="abc",
                                stream_url="http://old-panel.example/live/x/y/1.ts"))
        self.db.commit()
        self._put(m3u.id, m3u_url="http://new-panel.example/get.php?t=b")
        self.assertEqual({"abc": "http://old-panel.example/live/x/y/1.ts"}, self._urls(m3u.id))

        pid = self._xtream("http://old-panel.example:8080")
        before = self._urls(pid)
        self._put(pid, provider_type="m3u_url", server_url="http://new-panel.example:8080")
        self.assertEqual(before, self._urls(pid))

    def test_a_failed_save_changes_neither_the_provider_nor_its_channels(self):
        pid = self._xtream("http://old-panel.example:8080")
        before = self._urls(pid)
        with mock.patch.object(self.db, "commit", side_effect=OperationalError("commit", {}, Exception("locked"))):
            with self.assertRaises(OperationalError):
                self._put(pid, server_url="http://new-panel.example:8080", password="p2")
        self.db.rollback()
        self.assertEqual(before, self._urls(pid))
        self.assertEqual(("http://old-panel.example:8080", "p1"),
                         (self.db.get(Provider, pid).server_url, self.db.get(Provider, pid).password))


if __name__ == "__main__":
    unittest.main()

"""Which stream an Xtream channel URL asks the provider for: auto (the
default: continuous MPEG-TS unless the account says it cannot serve it, with
HLS as a per-channel fallback), or the overrides .m3u8 (HLS) and .ts.

Run from the tentacle/ directory:  python -m unittest discover -s tests

HLS is a playlist reload every few seconds plus one request per segment --
about 3,600 requests over a three-hour game. On a connection-limited
account each one is a chance for the provider to answer 509 because
something else opened a connection meanwhile (seen live 2026-09-23: every
509 that split an NHL recording landed on a playlist refresh). The
continuous .ts stream is ONE connection for the whole programme, served
through the raw-TS path with its re-dial (#103). Seen live 2026-10-18 on a
1-connection account: three HLS channels at once, two died on playlist
refreshes (509) within 30 s.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tmp_dirs import temp_dir


class _Base(unittest.TestCase):
    def setUp(self):
        import models.database as mdb
        engine = create_engine(f"sqlite:///{temp_dir(self)}/t.db",
                               connect_args={"check_same_thread": False})
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def _set(self, key, value):
        from models.database import set_setting
        set_setting(self.db, key, value)
        self.db.commit()


class FormatSetting(_Base):
    def test_default_is_auto_which_is_ts_for_an_account_that_says_nothing(self):
        from routers import livetv
        self.assertEqual("auto", livetv._live_stream_format(self.db))
        self.assertEqual("ts", livetv._live_extension(self.db, 1))

    def test_ts_and_auto_are_accepted_and_garbage_keeps_the_default(self):
        from routers import livetv
        for v in ("ts", "TS ", "auto", "m3u8"):
            self._set("livetv_stream_format", v)
            self.assertEqual(v.strip().lower(), livetv._live_stream_format(self.db))
        self._set("livetv_stream_format", "rtmp")
        self.assertEqual("auto", livetv._live_stream_format(self.db))

    def test_explicit_ts_wins_regardless_of_what_the_account_advertises(self):
        from routers import livetv
        self._set("livetv_stream_format", "ts")
        self.assertEqual("ts", livetv._live_extension(self.db, 1))


class AutoUsesWhatTheAccountAdvertises(_Base):
    def test_auto_without_any_record_takes_ts(self):
        """Xtream panels almost always serve /live/.../<id>.ts; a channel
        whose ts open is refused falls back to HLS (OpenFallsBackToHls)."""
        from routers import livetv
        self._set("livetv_stream_format", "auto")
        self.assertEqual("ts", livetv._live_extension(self.db, 1))

    def test_auto_takes_ts_when_the_account_allows_it(self):
        from routers import livetv
        self._set("livetv_stream_format", "auto")
        livetv._remember_output_formats(self.db, 1, {"user_info": {"allowed_output_formats": ["m3u8", "ts", "rtmp"]}})
        self.db.commit()
        self.assertEqual(["m3u8", "ts", "rtmp"], livetv._remembered_output_formats(self.db, 1))
        self.assertEqual("ts", livetv._live_extension(self.db, 1))
        livetv._remember_output_formats(self.db, 2, {"user_info": {"allowed_output_formats": ["m3u8"]}})
        self.assertEqual("m3u8", livetv._live_extension(self.db, 2), "per provider")

    def test_auto_stays_hls_when_the_account_does_not_allow_ts(self):
        from routers import livetv
        self._set("livetv_stream_format", "auto")
        livetv._remember_output_formats(self.db, 1, {"user_info": {"allowed_output_formats": ["m3u8"]}})
        self.assertEqual("m3u8", livetv._live_extension(self.db, 1))

    def test_a_reply_without_formats_leaves_the_record_alone(self):
        from routers import livetv
        livetv._remember_output_formats(self.db, 1, {"user_info": {"allowed_output_formats": ["ts"]}})
        livetv._remember_output_formats(self.db, 1, {"user_info": {"status": "Active"}})
        livetv._remember_output_formats(self.db, 1, {})
        self.assertEqual(["ts"], livetv._remembered_output_formats(self.db, 1))

    def test_a_corrupt_record_reads_as_nothing(self):
        from routers import livetv
        self._set("livetv_output_formats_1", "{not json")
        self.assertEqual([], livetv._remembered_output_formats(self.db, 1))


class ChannelSyncWritesTheChosenFormat(_Base):
    def _client(self):
        from services.xtream_client import XtreamClient
        return XtreamClient(server="http://panel.test", username="u", password="p", user_agent="UA")

    def _streams(self):
        return [{"stream_id": 101, "name": "One", "category_id": "5"},
                {"stream_id": 102, "name": "Two", "category_id": "5"}]

    def test_ts_urls_are_written_and_a_change_rewrites_existing_rows_in_place(self):
        from routers import livetv
        from models.database import LiveChannel
        client = self._client()
        livetv._upsert_channels(1, self._streams(), {"5": "Sports"}, client, self.db, extension="m3u8")
        self.db.commit()
        rows = {r.stream_id: r for r in self.db.query(LiveChannel).all()}
        self.assertEqual("http://panel.test/live/u/p/101.m3u8", rows["101"].stream_url)
        rows["101"].enabled = True
        rows["101"].channel_number = 7
        self.db.commit()
        ids = {sid: r.id for sid, r in rows.items()}

        out = livetv._upsert_channels(1, self._streams(), {"5": "Sports"}, client, self.db, extension="ts")
        self.db.commit()
        rows = {r.stream_id: r for r in self.db.query(LiveChannel).all()}
        self.assertEqual({"new": 0, "updated": 2, "total": 2}, out)
        self.assertEqual("http://panel.test/live/u/p/101.ts", rows["101"].stream_url)
        self.assertEqual(ids["101"], rows["101"].id, "the row must be re-keyed in place, not replaced")
        self.assertTrue(rows["101"].enabled)
        self.assertEqual(7, rows["101"].channel_number)

    def test_the_default_call_writes_hls(self):
        from routers import livetv
        from models.database import LiveChannel
        livetv._upsert_channels(1, self._streams(), {}, self._client(), self.db)
        self.db.commit()
        self.assertTrue(all(r.stream_url.endswith(".m3u8") for r in self.db.query(LiveChannel).all()))


PANEL = "http://panel.test/live/u/p/101"


def _channel(url, cid=7):
    return SimpleNamespace(id=cid, stream_id="101", stream_url=url)


def _provider(ptype="xtream"):
    return SimpleNamespace(id=1, provider_type=ptype)


class OpenUsesTheFormatNow(_Base):
    """The format is applied when a channel is opened: an install whose
    channels were synced as .m3u8 streams ts right after the upgrade, with
    no channel sync."""

    def setUp(self):
        super().setUp()
        from routers import livetv
        livetv._ts_refused_channels.clear()
        self.addCleanup(livetv._ts_refused_channels.clear)

    def test_a_channel_synced_as_hls_opens_as_ts_with_hls_as_fallback(self):
        from routers import livetv
        self.assertEqual((PANEL + ".ts", PANEL + ".m3u8"),
                         livetv._live_open_urls(self.db, _provider(), _channel(PANEL + ".m3u8")))

    def test_an_override_is_used_as_is_with_no_fallback(self):
        from routers import livetv
        self._set("livetv_stream_format", "m3u8")
        self.assertEqual((PANEL + ".m3u8", None),
                         livetv._live_open_urls(self.db, _provider(), _channel(PANEL + ".ts")))
        self._set("livetv_stream_format", "ts")
        self.assertEqual((PANEL + ".ts", None),
                         livetv._live_open_urls(self.db, _provider(), _channel(PANEL + ".m3u8")))

    def test_an_account_without_ts_opens_hls(self):
        from routers import livetv
        livetv._remember_output_formats(self.db, 1, {"user_info": {"allowed_output_formats": ["m3u8"]}})
        self.assertEqual((PANEL + ".m3u8", None),
                         livetv._live_open_urls(self.db, _provider(), _channel(PANEL + ".ts")))

    def test_a_channel_whose_ts_was_refused_opens_hls_directly(self):
        from routers import livetv
        livetv._ts_refused_channels.add(7)
        self.assertEqual((PANEL + ".m3u8", None),
                         livetv._live_open_urls(self.db, _provider(), _channel(PANEL + ".ts")))

    def test_urls_a_sync_did_not_build_are_left_alone(self):
        from routers import livetv
        for prov, url in ((_provider("m3u_url"), "http://cdn.test/news/master.m3u8"),
                          (_provider(), "http://panel.test/play/abc.m3u8"),
                          (None, PANEL + ".m3u8")):
            self.assertEqual((url, None), livetv._live_open_urls(self.db, prov, _channel(url)))

    def test_an_m3u_of_a_panels_links_opens_like_xtream(self):
        # a get.php playlist: the same panel links, ts first (#700)
        from routers import livetv
        self.assertEqual((PANEL + ".ts", PANEL + ".m3u8"),
                         livetv._live_open_urls(self.db, _provider("m3u_url"), _channel(PANEL + ".m3u8")))


def _resp(status, url, content=b"", headers=None):
    return httpx.Response(status, headers=headers or {}, content=content,
                          request=httpx.Request("GET", url))


class _FakeClient:
    def __init__(self, script, log, **kwargs):
        self._script, self._log = script, log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aclose(self):
        pass

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        url = str(request.url)
        self._log.append(url)
        queue = self._script[url]
        return queue[0] if len(queue) == 1 else queue.pop(0)


class OpenFallsBackToHls(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from routers import livetv
        livetv._ts_refused_channels.clear()
        self.addCleanup(livetv._ts_refused_channels.clear)

    async def _open(self, script, fallback):
        from routers import livetv
        log = []
        real_sleep = asyncio.sleep

        async def fast_sleep(delay):
            await real_sleep(0)

        with patch("httpx.AsyncClient", lambda **kw: _FakeClient(script, log)), \
                patch("asyncio.sleep", fast_sleep):
            resp = await livetv._stream_proxy_inner(
                channel_id=7, user_agent="UA", stream_url=PANEL + ".ts", _release_sem=lambda: None,
                guard=lambda *a, **k: True, fallback_url=fallback)
            body = b""
            async for piece in resp.body_iterator:
                body += piece
                break
        return body, log

    def _hls(self):
        playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nc1.ts\n#EXT-X-ENDLIST\n"
        return {PANEL + ".m3u8": [_resp(200, PANEL + ".m3u8", playlist.encode(),
                                        {"content-type": "application/vnd.apple.mpegurl"})],
                "http://panel.test/live/u/p/c1.ts": [_resp(200, "http://panel.test/live/u/p/c1.ts", b"HLS")]}

    async def test_ts_refused_outright_opens_hls_and_is_remembered(self):
        from routers import livetv
        body, log = await self._open({PANEL + ".ts": [_resp(404, PANEL + ".ts")], **self._hls()},
                                     PANEL + ".m3u8")
        self.assertEqual(b"HLS", body)
        self.assertEqual([PANEL + ".ts", PANEL + ".m3u8"], log[:2])
        self.assertIn(7, livetv._ts_refused_channels)

    async def test_a_busy_account_is_waited_out_on_ts_not_switched_to_hls(self):
        """509 means the account is busy, not that ts is not served: HLS
        would only add requests."""
        from routers import livetv
        script = {PANEL + ".ts": [_resp(509, PANEL + ".ts"),
                                  _resp(200, PANEL + ".ts", b"TS", {"content-type": "video/mp2t"}),
                                  _resp(404, PANEL + ".ts")], **self._hls()}
        body, log = await self._open(script, PANEL + ".m3u8")
        self.assertEqual(b"TS", body)
        self.assertNotIn(PANEL + ".m3u8", log)
        self.assertNotIn(7, livetv._ts_refused_channels)

    async def test_without_a_fallback_a_refusal_still_fails(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            await self._open({PANEL + ".ts": [_resp(404, PANEL + ".ts")], **self._hls()}, None)


if __name__ == "__main__":
    unittest.main()

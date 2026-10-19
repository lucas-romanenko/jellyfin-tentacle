"""#244: a saved proxy that fails validation sent all YouTube traffic direct.

The proxy exists so YouTube and Google see its address, not the household's.
When the saved value could not be used (socks5://…, a bad port, a value the
generic settings endpoint stored unchecked), traffic._refresh_settings logged
one warning and used no proxy: yt-dlp, the shared HTTP client and ffmpeg all
went out direct, while the page kept showing the proxy as set.

Now nothing is sent to YouTube while the saved proxy can't be used, the page
says so, and POST /api/settings refuses such a value as the YouTube page does.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_proxy_never_direct.py
"""
import unittest
from unittest import mock

from models.database import get_setting, set_setting
from services.youtube import client, indexer, traffic
from services.youtube.errors import PausedByBotCheck
from test_youtube_traffic import _Db


class _SavedProxy(_Db):
    def save(self, value):
        set_setting(self.db, "youtube_proxy", value)
        traffic._settings["loaded_at"] = 0          # past the settings cache


class AnUnusableProxyHoldsYouTubeTraffic(_SavedProxy):
    def test_nothing_goes_out_direct(self):
        self.save("socks5://vpn:1080")
        self.assertTrue(traffic.paused())
        with mock.patch.object(client, "_ydl") as ydl:
            with self.assertRaises(PausedByBotCheck) as raised:
                client.extract("https://www.youtube.com/watch?v=aaaaaaaaaaa")
        ydl.assert_not_called()
        self.assertIn("proxy", str(raised.exception))
        with self.assertRaises(PausedByBotCheck):
            traffic.http_client()
        with self.assertRaises(PausedByBotCheck):
            traffic.ffmpeg_proxy_args()
        with self.assertRaises(PausedByBotCheck):
            traffic.ydl_options()

    def test_the_channel_check_stands_down(self):
        self.save("http://host:notaport")
        ch = self.channel()
        with mock.patch.object(indexer.client, "flat_listing") as listing:
            result = indexer.index_channel(self.db, ch)
        listing.assert_not_called()
        self.assertTrue(result["skipped"])

    def test_a_working_proxy_is_used(self):
        self.save("http://gluetun:8888")
        self.assertFalse(traffic.paused())
        self.assertEqual("http://gluetun:8888", traffic.ydl_options()["proxy"])
        self.assertEqual(["-http_proxy", "http://gluetun:8888"], traffic.ffmpeg_proxy_args())

    def test_no_proxy_is_still_direct(self):
        self.save("")
        self.assertFalse(traffic.paused())
        self.assertNotIn("proxy", traffic.ydl_options())


class ThePagesSayWhatIsInUse(_SavedProxy):
    def _app(self, router):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import models.database as mdb
        from routers.auth import require_admin
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[require_admin] = lambda: None
        return TestClient(app)

    def test_the_traffic_page_shows_the_proxy_is_not_used(self):
        from routers import youtube as yt_router
        self.save("socks5://vpn:1080")
        body = self._app(yt_router.router).get("/api/youtube/traffic").json()
        self.assertEqual("socks5://vpn:1080", body["proxy"])      # what is saved, to fix it
        self.assertEqual("", body["proxy_in_use"])
        self.assertIn("SOCKS", body["proxy_error"])

    def test_the_generic_settings_endpoint_refuses_an_unusable_proxy(self):
        from routers import settings as settings_router
        c = self._app(settings_router.router)
        r = c.post("/api/settings", json={"settings": {"youtube_proxy": "socks5://vpn:1080"}})
        self.assertEqual(400, r.status_code)
        self.assertEqual("", get_setting(self.db, "youtube_proxy", "") or "")
        r = c.post("/api/settings", json={"settings": {"youtube_proxy": "gluetun:8888"}})
        self.assertEqual(200, r.status_code)
        self.assertEqual("http://gluetun:8888", get_setting(self.db, "youtube_proxy"))
        self.assertEqual("http://gluetun:8888", traffic.proxy())


if __name__ == "__main__":
    unittest.main()

"""The YouTube page shows the proxy's address, never its password.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A proxy with a login (http://user:pass@host:port) came back in full from
GET /api/youtube/traffic, twice ("proxy" and "proxy_in_use"), was written to
the log when the traffic settings were saved, and an address with a bad port
was quoted whole in the error shown on the page, logged and repeated in
every held-request message. The password is now masked in the replies and
the log line, the error names the problem only, and a Save that sends the
masked form back keeps the stored password (the API key keeps working the
same way).
"""
import io
import logging
import unittest
from unittest import mock

from models.database import get_setting, set_setting
from services.youtube import traffic
import test_youtube_proxy_never_direct as _pages

PROXY = "http://someone:proxy-pw-5e1@gluetun:8888"


class TrafficPageKeepsTheLoginOut(_pages._SavedProxy):
    _app = _pages.ThePagesSayWhatIsInUse._app

    def setUp(self):
        super().setUp()
        # A Save reschedules the channel check on the app's scheduler; keep
        # that job out of the modules that run after this one.
        import main
        p = mock.patch.object(main, "reschedule_youtube_index")
        p.start()
        self.addCleanup(p.stop)

    def client(self):
        from routers import youtube as yt_router
        return self._app(yt_router.router)

    def body(self, c, **over):
        page = c.get("/api/youtube/traffic").json()
        body = {"background_checks": page["background_checks"], "interval_minutes": page["interval_minutes"],
                "api_key": page["api_key"], "proxy": page["proxy"]}
        body.update(over)
        return body

    def test_replies_show_the_address_not_the_password(self):
        self.save(PROXY)
        page = self.client().get("/api/youtube/traffic").json()
        self.assertEqual("http://someone:••••@gluetun:8888", page["proxy"])
        self.assertEqual("http://someone:••••@gluetun:8888", page["proxy_in_use"])
        self.assertNotIn("proxy-pw-5e1", repr(page))

    def test_saving_what_the_page_shows_keeps_the_password_and_key(self):
        self.save(PROXY)
        set_setting(self.db, "youtube_api_key", "AIza" + "k" * 35)
        c = self.client()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger("routers.youtube").addHandler(handler)
        try:
            r = c.post("/api/youtube/traffic", json=self.body(c))
        finally:
            logging.getLogger("routers.youtube").removeHandler(handler)
        self.assertEqual(200, r.status_code)
        self.assertEqual(PROXY, get_setting(self.db, "youtube_proxy"))
        self.assertEqual("AIza" + "k" * 35, get_setting(self.db, "youtube_api_key"))
        self.assertNotIn("proxy-pw-5e1", r.text)
        self.assertNotIn("proxy-pw-5e1", stream.getvalue())

    def test_a_new_port_with_the_masked_password_keeps_the_password(self):
        self.save(PROXY)
        c = self.client()
        r = c.post("/api/youtube/traffic", json=self.body(c, proxy="http://someone:••••@gluetun:8889"))
        self.assertEqual(200, r.status_code)
        self.assertEqual("http://someone:proxy-pw-5e1@gluetun:8889", get_setting(self.db, "youtube_proxy"))

    def test_a_new_key_and_a_new_proxy_are_saved_as_typed(self):
        self.save(PROXY)
        c = self.client()
        r = c.post("/api/youtube/traffic", json=self.body(c, api_key="AIza...new", proxy="http://gluetun:3128"))
        self.assertEqual(200, r.status_code)
        self.assertEqual("AIza...new", get_setting(self.db, "youtube_api_key"))
        self.assertEqual("http://gluetun:3128", get_setting(self.db, "youtube_proxy"))

    def test_a_bad_port_error_does_not_quote_the_address(self):
        with self.assertRaises(ValueError) as ctx:
            traffic.normalize_proxy("http://someone:proxy-pw-5e1@gluetun:99999")
        self.assertNotIn("proxy-pw-5e1", str(ctx.exception))
        self.save("http://someone:proxy-pw-5e1@gluetun:99999")
        page = self.client().get("/api/youtube/traffic").json()
        self.assertIn("port", page["proxy_error"])
        self.assertNotIn("proxy-pw-5e1", repr(page))


if __name__ == "__main__":
    unittest.main()

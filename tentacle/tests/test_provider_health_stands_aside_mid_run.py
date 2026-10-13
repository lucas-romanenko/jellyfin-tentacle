"""The hourly provider check stands aside for live TV and recordings for
its WHOLE run, not only at its start.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A check is one or more real connections on the account: the API login,
then up to five test streams (three films, two channels) with a pause
between them, then the same again for the next provider, and once more two
minutes later to confirm a failure. A one-connection account answers a
new connection by cutting the one already open -- usually a recording. The
run looks for live TV only once, before the first provider, so a recording
(or a film through /api/vod) that starts after that look is the stream
the next test connection cuts.
"""
from unittest import mock

from models.database import Provider
import tests.test_provider_health as tph
from tests.test_provider_health import API, Base_, FakeResponse, MOVIE_URLS, SERVER, api_ok


class StandsAsideMidRun(Base_):
    def _live_after(self, first_url):
        """live_streams_active() turns True once `first_url` has been requested."""
        started = {"live": False}

        def watch(url):
            if url.startswith(first_url):
                started["live"] = True
        return started, watch

    def run_with_live_start(self, routes, starts_after):
        started, watch = self._live_after(starts_after)
        orig = tph.Net.get

        def get(net, url, **kw):
            try:
                return orig(net, url, **kw)
            finally:
                watch(url)
        with mock.patch.object(tph.Net, "get", get), \
                mock.patch("services.provider_activity.live_streams_active",
                           side_effect=lambda: started["live"]):
            return self.run_check(routes)

    def test_a_recording_that_starts_between_test_streams_is_not_cut(self):
        # The first film left the provider (404), so the check would open the
        # next one 3 s later -- by then a recording has started.
        self.run_with_live_start({API: api_ok(), MOVIE_URLS[0]: FakeResponse(404, data=b""),
                                  SERVER + "/movie/": FakeResponse(206)}, starts_after=MOVIE_URLS[0])
        later = self.net.calls[self.net.calls.index(MOVIE_URLS[0]) + 1:]
        self.assertEqual(later, [], "no connection opened after live TV started")
        s = self.state()
        self.assertEqual(s["state"], "unknown", "no verdict from a check that stood aside")
        self.assertIsNotNone(s["skipped_at"])

    def test_a_recording_that_starts_after_the_login_gets_no_test_stream(self):
        self.run_with_live_start({API: api_ok(), SERVER + "/movie/": FakeResponse(206)}, starts_after=API)
        self.assertEqual([u for u in self.net.calls if "/movie/" in u], [])
        self.assertEqual(self.state()["state"], "unknown")

    def test_the_next_provider_is_not_checked_once_live_tv_started(self):
        other = Provider(name="Second IPTV", provider_type="xtream", server_url="http://two.example.test",
                         username="u2", password="p2", active=True)
        self.db.add(other)
        self.db.commit()
        self.run_with_live_start({API: api_ok(), SERVER + "/movie/": FakeResponse(206),
                                  "http://two.example.test/": api_ok()}, starts_after=MOVIE_URLS[0])
        self.assertFalse([u for u in self.net.calls if u.startswith("http://two.example.test/")],
                         "the second provider is left alone while live TV runs")
        self.assertEqual(self.state()["state"], "ok", "the first provider's finished check still counts")

    def test_without_live_tv_the_check_runs_as_before(self):
        self.run_check({API: api_ok(), MOVIE_URLS[0]: FakeResponse(404, data=b""),
                        SERVER + "/movie/": FakeResponse(206)})
        self.assertEqual(self.state()["state"], "ok")
        self.assertTrue(self.net.calls[0].startswith(API))
        self.assertEqual(self.net.calls[1:], [MOVIE_URLS[0], MOVIE_URLS[1]])


if __name__ == "__main__":
    import unittest
    unittest.main()

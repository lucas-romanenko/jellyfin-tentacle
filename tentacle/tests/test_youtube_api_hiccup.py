"""#246: with an API key, a Data API 5xx, 429 or network error made the
scheduled check scrape every tab instead of reading the RSS feed.

newest_uploads() fell back to RSS only on a 400/403 refusal. A 5xx, 429 or
connection error raised YouTubeUnavailable instead, the indexer read that as
"no feed", and listed every channel's tabs with yt-dlp (the heaviest, most
bot-like request) on every run of the outage, with the free feed working.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_api_hiccup.py
"""
import unittest
from unittest import mock

import httpx

from services.youtube import feeds, indexer, traffic
from test_youtube_traffic import FEED, _Db


class _Http:
    def __init__(self, api_status=503, api_error=None):
        self.urls, self.api_status, self.api_error = [], api_status, api_error

    def get(self, url, params=None, timeout=None, headers=None):
        self.urls.append(url)
        if "googleapis" in url:
            if self.api_error:
                raise self.api_error
            return httpx.Response(self.api_status, text="oops", request=httpx.Request("GET", url))
        return httpx.Response(200, text=FEED.format(a="a" * 11, b="b" * 11),
                              request=httpx.Request("GET", url))

    def api_calls(self):
        return sum("googleapis" in u for u in self.urls)


class AnApiHiccupFallsBackToTheFeed(_Db):
    def _uploads(self, http, times=1):
        traffic.configure(api_key="AIza-test")
        ch = self.channel()
        with mock.patch.object(traffic, "http_client", return_value=http):
            return [feeds.newest_uploads(ch) for _ in range(times)]

    def test_a_5xx_falls_back_to_the_feed(self):
        http = _Http(503)
        got = self._uploads(http)[0]
        self.assertEqual("a" * 11, got[0]["id"])

    def test_a_429_falls_back_to_the_feed(self):
        self.assertEqual("a" * 11, self._uploads(_Http(429))[0][0]["id"])

    def test_a_network_error_falls_back_to_the_feed(self):
        http = _Http(api_error=httpx.ConnectError("no route"))
        self.assertEqual("a" * 11, self._uploads(http)[0][0]["id"])

    def test_the_api_backs_off_for_a_while_after_a_hiccup(self):
        http = _Http(503)
        self._uploads(http, times=3)
        self.assertEqual(1, http.api_calls())
        self.assertGreater(feeds.api_state()["off_for_seconds"], 0)
        self.assertLessEqual(feeds.api_state()["off_for_seconds"], 30 * 60)

    def test_the_scheduled_check_reads_the_feed_not_the_tabs(self):
        traffic.configure(api_key="AIza-test")
        from datetime import datetime
        ch = self.channel(last_full_check=datetime.utcnow(), feed_ids=["a" * 11, "b" * 11])
        with mock.patch.object(traffic, "http_client", return_value=_Http(503)), \
             mock.patch.object(indexer.client, "flat_listing") as listing:
            result = indexer.index_channel(self.db, ch, light=True)
        listing.assert_not_called()
        self.assertTrue(result.get("light"))


class AnAnswerThatIsNotJson(_Db):
    """A 200 that is not the API's JSON (a proxy's or captive portal's page)
    raised JSONDecodeError, which no YouTube handler catches: the channel
    failed on every run instead of reading its feed, and "Test key" was a 500."""

    def test_uploads_fall_back_to_the_feed(self):
        traffic.configure(api_key="AIza-test")
        ch = self.channel()
        with mock.patch.object(traffic, "http_client", return_value=_Http(200)):
            got = feeds.newest_uploads(ch)
        self.assertEqual("a" * 11, got[0]["id"])
        self.assertGreater(feeds.api_state()["off_for_seconds"], 0)

    def test_details_fall_back_to_yt_dlp(self):
        traffic.configure(api_key="AIza-test")
        with mock.patch.object(traffic, "http_client", return_value=_Http(200)), \
             mock.patch.object(indexer.client, "video_details",
                               return_value={"id": "a" * 11, "title": "From yt-dlp"}) as ytdlp:
            self.assertEqual("From yt-dlp", indexer._details("a" * 11)["title"])
        ytdlp.assert_called_once()

    def test_json_that_is_not_an_object_falls_back_too(self):
        traffic.configure(api_key="AIza-test")
        http = _Http(200)
        http.get = lambda url, params=None, timeout=None, headers=None: httpx.Response(
            200, text="null" if "googleapis" in url else FEED.format(a="a" * 11, b="b" * 11),
            request=httpx.Request("GET", url))
        with mock.patch.object(traffic, "http_client", return_value=http), \
             mock.patch.object(indexer.client, "video_details",
                               return_value={"id": "a" * 11, "title": "From yt-dlp"}):
            self.assertEqual("From yt-dlp", indexer._details("a" * 11)["title"])

    def test_the_key_check_says_what_happened(self):
        traffic.configure(api_key="AIza-test")
        with mock.patch.object(traffic, "http_client", return_value=_Http(200)):
            ok, message = feeds.check_api_key()
        self.assertFalse(ok)
        self.assertIn("not JSON", message)


if __name__ == "__main__":
    unittest.main()

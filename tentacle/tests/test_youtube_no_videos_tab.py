"""#277: a channel that only streams live (or only posts Shorts) could not be added.

Such a channel has no Videos tab on YouTube. yt-dlp answers the /videos URL
Tentacle builds with "This channel does not have a videos tab", and the add was
refused with 400, although the add code means to handle exactly these channels
(keep their finished streams). An existing channel that lost its Videos tab
failed every refresh the same way, and its other tabs were never read.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_no_videos_tab.py
"""
import unittest
from unittest import mock

from models.database import TentacleUser, YouTubeChannel
from services.youtube import indexer
from services.youtube import sync as ysync
from services.youtube.errors import YouTubeUnavailable
from test_youtube_traffic import _Db

CID = "UC" + "q" * 22


def _no_tab(tab):
    return YouTubeUnavailable(f"ERROR: [youtube:tab] {CID}: This channel does not have a {tab} tab")


def _listing(tabs):
    """A channel with only these tabs; urls asked for are recorded on the function."""
    def listing(url, limit):
        listing.urls.append(url)
        tab = url.rsplit("/", 1)[-1]
        if tab not in tabs:
            raise _no_tab(tab)
        return {"channel": "Only Some Tabs", "channel_id": CID, "entries": tabs[tab]}
    listing.urls = []
    return listing


class AddingAChannelWithoutAVideosTab(unittest.TestCase):
    def _resolve(self, tabs):
        listing = _listing(tabs)
        with mock.patch.object(indexer.client, "flat_listing", side_effect=listing):
            return indexer.resolve_channel(f"https://www.youtube.com/channel/{CID}"), listing.urls

    def test_a_live_only_channel_resolves(self):
        info, urls = self._resolve({"streams": [{"id": "abcdefghijk", "live_status": "was_live"}]})
        self.assertFalse(info["has_uploads"])
        self.assertFalse(info["has_videos_tab"])
        self.assertEqual("Only Some Tabs", info["title"])
        self.assertTrue(urls[0].endswith("/videos"), "an ordinary channel still lists /videos first")

    def test_a_shorts_only_channel_resolves(self):
        info, _ = self._resolve({"shorts": [{"id": "abcdefghijk"}]})
        self.assertFalse(info["has_uploads"])
        self.assertEqual(CID, info["channel_id"])

    def test_an_ordinary_channel_is_unchanged(self):
        info, urls = self._resolve({"videos": [{"id": "abcdefghijk"}]})
        self.assertTrue(info["has_uploads"])
        self.assertTrue(info["has_videos_tab"])
        self.assertEqual(1, len(urls))

    def test_any_other_error_still_refuses(self):
        def listing(url, limit):
            raise YouTubeUnavailable("ERROR: Unable to download API page: HTTP Error 500")
        with mock.patch.object(indexer.client, "flat_listing", side_effect=listing):
            with self.assertRaises(YouTubeUnavailable):
                indexer.resolve_channel(f"https://www.youtube.com/channel/{CID}")


class TheAddStoresWhatTheChannelHas(_Db):
    def test_add_does_not_ask_for_a_videos_tab_again(self):
        from routers import youtube as yt_router
        self.db.add(TentacleUser(jellyfin_user_id="a" * 32, display_name="u", is_admin=True))
        self.db.commit()

        class _Req:
            headers = {"host": "192.168.2.10:8888"}
        listing = _listing({"streams": [{"id": "abcdefghijk", "live_status": "was_live"}]})
        with mock.patch.object(yt_router.client, "available", lambda: True), \
             mock.patch.object(indexer.client, "flat_listing", side_effect=listing), \
             mock.patch.object(yt_router, "_start_refresh", lambda **kw: True), \
             mock.patch.object(ysync, "detect_base_url",
                               lambda db, host=None, scheme="http": {"url": "http://192.168.2.10:8888", "tried": []}):
            yt_router.add_channel(yt_router.ChannelCreate(url=f"https://www.youtube.com/channel/{CID}"),
                                  request=_Req(), db=self.db)
        ch = self.db.query(YouTubeChannel).one()
        self.assertFalse(ch.include_videos)
        self.assertTrue(ch.include_streams)
        self.assertEqual([f"https://www.youtube.com/channel/{CID}/streams"], indexer._tab_urls(ch))


class RefreshingAChannelThatLostItsVideosTab(_Db):
    def test_its_other_tabs_are_still_read(self):
        ch = self.channel(channel_id=CID, include_streams=True, min_duration=0, live_enabled=False)
        listing = _listing({"streams": [{"id": "abcdefghijk", "live_status": "was_live"}]})
        details = {"id": "abcdefghijk", "title": "Last night", "availability": "public",
                   "live_status": "was_live", "duration": 3600, "timestamp": 1758412800}
        with mock.patch.object(indexer.client, "flat_listing", side_effect=listing), \
             mock.patch.object(indexer.client, "video_details", return_value=details):
            result = indexer.index_channel(self.db, ch)
        self.assertEqual(1, result["new"])
        self.assertEqual({"videos": 0, "streams": 1}, result["listing"])


if __name__ == "__main__":
    unittest.main()

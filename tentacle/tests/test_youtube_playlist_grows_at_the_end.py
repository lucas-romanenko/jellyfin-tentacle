"""A YouTube playlist source picks up videos added at the end of the playlist.

The indexer reads a source's listing as "newest first" and stops after the
newest N (keep_count, read as N + 5 entries with yt-dlp's playlistend). That
holds for a channel's tabs. A playlist lists in the playlist's own order, and
YouTube adds new videos at the BOTTOM of a playlist unless its owner changed
that setting. So a playlist longer than N + 5 is read from its oldest
entries, and anything added later is never listed, never indexed, never in
the row. The scheduled check reads the playlist's feed, which shows the same
first entries, so it never sees anything new either: with a Data API key it
has to notice the playlist grew (its item count) and list it (#544).

No network: the listing and the details are faked; the listing honours the
limit the indexer asks for, as yt-dlp's playlistend does.
Run from tentacle/:  python -m unittest discover -s tests -p "test_youtube_playlist_grows_at_the_end.py"
"""
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from tmp_dirs import temp_dir
from test_youtube_indexer_followup import _channel, _fresh_db

DAY0 = datetime(2026, 1, 1)


def _vid(i: int) -> str:
    return f"vid{i:08d}"   # 11 characters, a valid id


class TestPlaylistAppendedAtTheEnd(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        self.addCleanup(self.db.close)
        self.channel = _channel(self.db, kind="playlist", playlist_id="PLgrows", channel_id=None,
                                title="Grows", slug="grows", live_enabled=False, min_duration=0,
                                keep_count=3)
        # Playlist order = order added; each video was uploaded the day it was added.
        self.playlist = [_vid(i) for i in range(20)]

    def _details(self, video_id, *a, **k):
        i = int(video_id[3:])
        return {"id": video_id, "title": f"Video {i}", "duration": 600, "live_status": None,
                "availability": "public",
                "timestamp": int((DAY0 + timedelta(days=i)).timestamp())}

    def _sync(self, light=False, api=False):
        from services.youtube import indexer, library, sync

        def listing(url, limit):
            return {"entries": [{"id": v, "title": v} for v in self.playlist[:limit]]}

        # The playlist's feed (RSS or the API's first page) lists from the top.
        def feed(channel):
            return [{"id": v, "title": "", "published": "", "short": False} for v in self.playlist[:15]]

        def api_details(ids):
            return {v: self._details(v) for v in ids}

        root = Path(temp_dir(self))
        with mock.patch.object(indexer.client, "flat_listing", side_effect=listing), \
             mock.patch.object(indexer.client, "video_details", side_effect=self._details), \
             mock.patch.object(indexer.feeds, "api_available", return_value=api), \
             mock.patch.object(indexer.feeds, "api_details", side_effect=api_details), \
             mock.patch.object(indexer.feeds, "newest_uploads", side_effect=feed), \
             mock.patch.object(indexer.feeds, "api_playlist_size", create=True,
                               side_effect=lambda playlist_id: len(self.playlist)), \
             mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", root), \
             mock.patch.object(library, "fetch_artwork", return_value=0), \
             mock.patch.object(indexer.time, "sleep"):
            sync.sync_channel(self.db, self.channel, "http://192.0.2.20:8888", light=light)

    def _in_library(self):
        from models.database import YouTubeVideo
        return sorted(v.video_id for v in self.db.query(YouTubeVideo).filter(
            YouTubeVideo.channel_fk == self.channel.id, YouTubeVideo.removed_at.is_(None)))

    def test_the_newest_videos_of_a_long_playlist_are_kept(self):
        self._sync()
        self.assertEqual(self._in_library(), [_vid(17), _vid(18), _vid(19)])

    def test_a_video_added_to_the_end_later_reaches_the_library(self):
        self._sync()
        self.playlist.append(_vid(20))
        self._sync()
        self.assertIn(_vid(20), self._in_library())

    def test_the_hourly_check_with_an_api_key_sees_a_video_added_to_the_end(self):
        self._sync(light=True, api=True)        # never fully listed: lists it
        self.assertEqual(self._in_library(), [_vid(17), _vid(18), _vid(19)])
        self.playlist.append(_vid(20))
        self._sync(light=True, api=True)        # the feed's first 15 are all known
        self.assertIn(_vid(20), self._in_library())

    def test_the_hourly_check_does_not_list_a_playlist_that_did_not_change(self):
        self._sync(light=True, api=True)
        from services.youtube import indexer
        with mock.patch.object(indexer.client, "flat_listing",
                               side_effect=AssertionError("listed although nothing changed")):
            self._sync(light=True, api=True)

    def test_control_a_playlist_that_adds_at_the_top_works(self):
        # The owner's "add new videos to top" setting: newest first, like a channel tab.
        self.playlist.reverse()
        self._sync()
        self.assertEqual(self._in_library(), [_vid(17), _vid(18), _vid(19)])

    def test_a_short_playlist_that_adds_at_the_top_keeps_its_newest(self):
        # Shorter than N + 5: both ends cover the whole playlist. Read from the
        # bottom first, its oldest fill that end's N; the top end still has to
        # reach the newest ones.
        self.playlist = [_vid(i) for i in range(7, -1, -1)]
        self._sync()
        self.assertEqual(self._in_library(), [_vid(5), _vid(6), _vid(7)])

    def test_listing_it_again_fetches_nothing(self):
        # The top end holds the oldest videos, which retention retires: they
        # count as dealt with, or every run would fetch the next few old ones.
        self._sync()
        from services.youtube import indexer
        with mock.patch.object(indexer, "_details",
                               side_effect=AssertionError("fetched although nothing is new")):
            self._sync()
        self.assertEqual(self._in_library(), [_vid(17), _vid(18), _vid(19)])


class TestApiPlaylistSize(unittest.TestCase):
    def test_reads_the_item_count(self):
        from services.youtube import feeds
        answer = {"items": [{"id": "PLx", "contentDetails": {"itemCount": 42}}]}
        with mock.patch.object(feeds, "_api_get", return_value=answer) as get:
            self.assertEqual(feeds.api_playlist_size("PLx"), 42)
        get.assert_called_once_with("playlists", {"part": "contentDetails", "id": "PLx"})

    def test_a_playlist_the_api_does_not_return_has_no_size(self):
        from services.youtube import feeds
        with mock.patch.object(feeds, "_api_get", return_value={"items": []}):
            self.assertIsNone(feeds.api_playlist_size("PLgone"))


if __name__ == "__main__":
    unittest.main()

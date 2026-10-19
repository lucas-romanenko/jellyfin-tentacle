"""A YouTube playlist is a source of its own, named after itself.

resolve_channel() titled every source "channel or uploader or title", and a
playlist listing names its OWNER in "channel" (yt-dlp's _tab extractor fills
channel/channel_id from the playlist's owner) — so a playlist was titled as
its channel. add_channel() then refused it whenever that channel, or another
of its playlists, was already added: it matched on channel_id, which is the
owner's for a playlist. The indexer already handles a video listed by two
sources ("already indexed under another channel or playlist").

Run from tentacle/:  python -m unittest discover -s tests -p "test_youtube_playlist_source.py"
"""
import unittest
from unittest import mock

import test_youtube

OWNER = "UC" + "a" * 22


class TestPlaylistTitle(unittest.TestCase):
    def test_a_playlist_is_titled_by_its_own_name(self):
        from services.youtube import indexer
        listing = {"title": "Bluey Season 1", "channel": "Bluey - Official Channel",
                   "channel_id": OWNER, "entries": [{"id": "abcdefghijk"}], "thumbnails": []}
        with mock.patch.object(indexer.client, "flat_listing", return_value=listing):
            info = indexer.resolve_channel("https://www.youtube.com/playlist?list=PLbluey1")
        self.assertEqual(info["kind"], "playlist")
        self.assertEqual(info["title"], "Bluey Season 1")

    def test_a_channel_is_still_titled_by_the_channel(self):
        from services.youtube import indexer
        listing = {"title": "Bluey - Official Channel - Videos", "channel": "Bluey - Official Channel",
                   "channel_id": OWNER, "entries": [], "thumbnails": []}
        with mock.patch.object(indexer.client, "flat_listing", return_value=listing):
            info = indexer.resolve_channel("https://www.youtube.com/@bluey")
        self.assertEqual(info["title"], "Bluey - Official Channel")


class TestPlaylistNextToItsChannel(unittest.TestCase):
    """The add-channel harness of test_youtube.TestAddingAChannel, borrowed, not inherited."""
    setUp = test_youtube.TestAddingAChannel.setUp
    tearDown = test_youtube.TestAddingAChannel.tearDown
    _Req = test_youtube.TestAddingAChannel._Req
    _add = test_youtube.TestAddingAChannel._add

    def _as_playlist(self, playlist_id, title):
        self.info.update(kind="playlist", playlist_id=playlist_id, title=title,
                         channel_id=OWNER, canonical=f"https://www.youtube.com/playlist?list={playlist_id}")

    def test_a_playlist_can_be_added_next_to_its_channel(self):
        self.info.update(channel_id=OWNER)
        self._add()
        self._as_playlist("PLone", "Season 1")
        self._add()
        self.assertEqual(self.db.query(self.YouTubeChannel).count(), 2)

    def test_two_playlists_of_one_owner_can_both_be_added(self):
        self._as_playlist("PLone", "Season 1")
        self._add()
        self._as_playlist("PLtwo", "Season 2")
        self._add()
        self.assertEqual(self.db.query(self.YouTubeChannel).count(), 2)

    def test_the_same_playlist_twice_is_refused(self):
        from fastapi import HTTPException
        self._as_playlist("PLone", "Season 1")
        self._add()
        with self.assertRaises(HTTPException) as cm:
            self._add()
        self.assertEqual(cm.exception.status_code, 409)

    def test_the_same_channel_twice_is_still_refused(self):
        from fastapi import HTTPException
        self.info.update(channel_id=OWNER)
        self._add()
        self._as_playlist("PLone", "Season 1")
        self._add()
        self.info.update(kind="channel", playlist_id=None, title="A Channel", channel_id=OWNER)
        with self.assertRaises(HTTPException) as cm:
            self._add()
        self.assertEqual(cm.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()

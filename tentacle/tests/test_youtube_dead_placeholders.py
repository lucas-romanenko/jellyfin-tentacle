"""#275: library videos that are gone stayed in every user's row for ever as
"youtube video #<id>" with no picture.

An earlier build stored some videos under a placeholder title. When such a
video is no longer on YouTube, #131's repair only ever looked for a title: an
entry the listing names "[Private video]" was not read at all, and one with no
title was read, YouTube said "Video unavailable", and that answer was dropped.
The row stayed a library item with its .strm, and every full refresh spent a
detail read on it again.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_dead_placeholders.py
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from test_youtube_indexer_followup import _channel, _fresh_db, _tabs
from models.database import YouTubeVideo
from services.youtube import indexer, library, sync
from services.youtube.errors import VideoUnavailable, YouTubeBlocked

BASE = "http://192.0.2.20:8888"
VID = "abcdefghijk"
GONE = VideoUnavailable(f"ERROR: [youtube] {VID}: Video unavailable. This video has been removed by the uploader")


class DeadPlaceholderRowsLeave(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        self.channel = _channel(self.db, live_enabled=False, title="Playlist A", kind="playlist",
                                playlist_id="PLabcdefabcdefabcdef", keep_count=41, min_duration=0)
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        self.db.close()

    def _legacy_row(self):
        v = YouTubeVideo(channel_fk=self.channel.id, video_id=VID, title=f"youtube video #{VID}",
                         first_seen=datetime(2026, 9, 21), last_seen=datetime(2026, 9, 21))
        self.db.add(v)
        self.db.commit()
        with mock.patch.object(library, "fetch_artwork", return_value=0):
            library.write_video(v, self.channel, BASE, root=self.root)
        self.db.commit()
        return v

    def _sync(self, entry, details):
        reader = mock.Mock(side_effect=details) if isinstance(details, Exception) \
            else mock.Mock(return_value=details)
        with mock.patch.object(indexer.client, "flat_listing", side_effect=_tabs(videos_entries=[entry])), \
             mock.patch.object(indexer.client, "video_details", reader), \
             mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", self.root), \
             mock.patch.object(library, "fetch_artwork", return_value=0), \
             mock.patch.object(indexer.time, "sleep"):
            sync.sync_channel(self.db, self.channel, BASE)
        return reader

    def test_an_untitled_entry_whose_details_say_unavailable_leaves_the_library(self):
        v = self._legacy_row()
        reader = self._sync({"id": VID}, GONE)
        self.db.refresh(v)
        self.assertEqual(1, reader.call_count)
        self.assertIsNotNone(v.removed_at)
        self.assertEqual(indexer.UNAVAILABLE_REASON, v.skip_reason)
        self.assertIsNotNone(v.next_check_at, "read again later, restored if it plays")
        self.assertEqual([], list(self.root.rglob("*.strm")))

    def test_a_private_marker_entry_leaves_the_library(self):
        v = self._legacy_row()
        self._sync({"id": VID, "title": "[Private video]"}, GONE)
        self.db.refresh(v)
        self.assertIsNotNone(v.removed_at)

    def test_it_is_not_read_again_on_the_next_refresh(self):
        self._legacy_row()
        self._sync({"id": VID}, GONE)
        reader = self._sync({"id": VID}, GONE)
        self.assertEqual(0, reader.call_count)

    def test_a_private_marker_entry_whose_details_read_is_kept_and_retitled(self):
        v = self._legacy_row()
        self._sync({"id": VID, "title": "[Private video]"},
                   {"id": VID, "title": "Still here", "availability": "public"})
        self.db.refresh(v)
        self.assertIsNone(v.removed_at)
        self.assertEqual("Still here", v.title)

    def test_a_block_during_the_repair_retires_nothing(self):
        v = self._legacy_row()
        with self.assertRaises(YouTubeBlocked):
            self._sync({"id": VID}, YouTubeBlocked("Sign in to confirm you're not a bot"))
        self.db.refresh(v)
        self.assertIsNone(v.removed_at)


if __name__ == "__main__":
    unittest.main()

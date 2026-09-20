"""A video already indexed under one channel must not abort another channel.

Run from the tentacle/ directory:  python -m unittest discover -s tests

youtube_videos.video_id is unique across ALL channels, but index_channel's
`known` set only covers the channel being indexed. A second source that lists
the same video (a playlist source containing a subscribed channel's upload, a
collab, a re-upload) inserted it again; the IntegrityError aborted that
channel's whole run, and it recurred on every run.
"""
import unittest
from unittest import mock

from test_youtube_indexer_followup import _channel, _fresh_db, _tabs


class TestAVideoOwnedByAnotherChannel(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()

    def tearDown(self):
        self.db.close()

    def test_second_channel_listing_the_same_video_still_indexes(self):
        from models.database import YouTubeVideo
        from services.youtube import indexer
        a = _channel(self.db, live_enabled=False, channel_id="UC" + "a" * 22, slug="a", title="A")
        b = _channel(self.db, live_enabled=False, channel_id="UC" + "b" * 22, slug="b", title="B")
        shared = {"id": "sssssssssss"}
        own = {"id": "bbbbbbbbbbb"}
        details = {"title": "Upload", "availability": "public",
                   "duration": 600, "upload_date": "20260916"}
        with mock.patch.object(indexer.client, "video_details", return_value=details), \
             mock.patch.object(indexer.time, "sleep"):
            with mock.patch.object(indexer.client, "flat_listing",
                                   side_effect=_tabs((), [shared])):
                indexer.index_channel(self.db, a)
            with mock.patch.object(indexer.client, "flat_listing",
                                   side_effect=_tabs((), [shared, own])):
                indexer.index_channel(self.db, b)   # raised IntegrityError before
        ids = {v.video_id: v.channel_fk for v in self.db.query(YouTubeVideo).all()}
        self.assertEqual(ids.get("sssssssssss"), a.id)
        self.assertEqual(ids.get("bbbbbbbbbbb"), b.id,
                         "channel B's own upload was never indexed because the shared video aborted the run")


if __name__ == "__main__":
    unittest.main()

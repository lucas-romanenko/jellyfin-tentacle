"""#288 follow-up: a restart after a channel's listing, before its files.

The start-up resume (#288) queues channels whose first listing never finished
(last_full_check is NULL). But the listing sets last_full_check before any
file is written and before the playlists are made: a restart in that stretch
(writing files, warming streams, waiting for Jellyfin) left the channel exactly
as #288 describes, library videos with no files and no playlist, and it was
not queued, so it stayed so until the next scheduled check, or for good with
background checks off.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_resume_unwritten.py
"""
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from models.database import YouTubeChannel, YouTubeVideo, set_setting
from services.youtube import indexer, library
from services.youtube import sync as ysync
from test_youtube_traffic import _Db
from tmp_dirs import temp_dir

LISTED = datetime(2026, 9, 28)


def _details(vid, *a, **kw):
    n = int(vid[-2:])
    return {"id": vid, "title": f"Video {n}", "availability": "public", "live_status": "not_live",
            "duration": 600, "timestamp": 1758412800 - n * 3600}


class ResumeAfterTheListing(_Db):
    def setUp(self):
        super().setUp()
        set_setting(self.db, "youtube_enabled", "true")

    def _resume(self):
        from routers import youtube as yt_router
        with mock.patch.object(yt_router.client, "available", return_value=True), \
             mock.patch.object(yt_router, "_start_refresh") as start:
            queued = yt_router.resume_unfinished_channels()
        return queued, start

    def _video(self, ch, vid, **kw):
        self.db.add(YouTubeVideo(channel_fk=ch.id, video_id=vid, title=vid, **kw))
        self.db.commit()

    def test_a_listed_channel_whose_videos_have_no_files_is_queued(self):
        ch = self.channel(last_full_check=LISTED, last_checked=LISTED)
        self._video(ch, "a" * 11, strm_path="/media/youtube/Chan/a/a.strm")
        self._video(ch, "b" * 11)                      # listed, never written
        queued, start = self._resume()
        self.assertEqual([ch.id], queued)
        start.assert_called_once_with(channel_ids=[ch.id])

    def test_a_channel_with_every_file_written_is_not_queued(self):
        ch = self.channel(last_full_check=LISTED, last_checked=LISTED)
        self._video(ch, "a" * 11, strm_path="/media/youtube/Chan/a/a.strm")
        # Rows that never get files: skipped, retired, and a stream not on air yet.
        self._video(ch, "b" * 11, removed_at=LISTED, skip_reason="duration")
        self._video(ch, "c" * 11, live_status="is_upcoming")
        queued, start = self._resume()
        self.assertEqual([], queued)
        start.assert_not_called()

    def test_a_disabled_channel_is_not_queued(self):
        ch = self.channel(last_full_check=LISTED, last_checked=LISTED, enabled=False)
        self._video(ch, "b" * 11)
        self.assertEqual([], self._resume()[0])

    def test_the_resumed_refresh_writes_the_missing_files(self):
        from routers import youtube as yt_router
        root = Path(temp_dir(self))
        ch = self.channel(keep_count=3)
        listing = {"entries": [{"id": f"vid{i:08d}", "title": f"Video {i}"} for i in range(3)]}
        with mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", root), \
             mock.patch.object(indexer.client, "flat_listing", return_value=listing), \
             mock.patch.object(indexer.client, "video_details", side_effect=_details), \
             mock.patch.object(indexer.feeds, "api_available", return_value=False), \
             mock.patch.object(library, "fetch_artwork", return_value=0):
            # The listing finishes, then the restart: no file yet.
            indexer.index_channel(self.db, ch)
            self.assertEqual([], list(root.rglob("*.strm")))
            queued, _ = self._resume()
            self.assertEqual([ch.id], queued)
            with mock.patch.object(ysync, "base_url", return_value="http://192.0.2.20:8888"), \
                 mock.patch.object(ysync, "publish_to_jellyfin") as publish, \
                 mock.patch.object(ysync, "reconcile_playlists", return_value=0):
                yt_router._run_refresh_once(queued)
        self.assertEqual(3, len(list(root.rglob("*.strm"))))
        publish.assert_called_once()
        self.assertEqual([], self._resume()[0])


if __name__ == "__main__":
    unittest.main()

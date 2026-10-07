"""A channel removed while its files are being written leaves none behind.

A sync writes each new video's folder, .strm and NFO, then saves the folder
paths in one commit. Removing the channel in that window (the first index of a
channel just added is the likely moment) deleted the rows before the paths were
saved, so the removal found nothing to delete ("0 file(s) deleted"), the
commit then failed, and the files stayed: Jellyfin kept every one as a video
that answers 404 "Unknown video", tagged with a channel that no longer exists,
and with no row left nothing would ever remove them.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_removed_while_written.py
"""
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy.exc import OperationalError

from models.database import YouTubeVideo
from services.youtube import indexer, library
from services.youtube import sync as ysync
from test_youtube_traffic import _Db
from tmp_dirs import temp_dir

BASE = "http://192.0.2.20:8888"


def _details(vid, *a, **kw):
    n = int(vid[-2:])
    return {"id": vid, "title": f"Video {n}", "availability": "public", "live_status": "not_live",
            "duration": 600, "timestamp": 1758412800 - n * 3600}


LISTING = {"entries": [{"id": f"vid{i:08d}", "title": f"Video {i}"} for i in range(3)]}


class RemovedWhileFilesAreWritten(_Db):
    def setUp(self):
        super().setUp()
        self.root = Path(temp_dir(self))
        for p in (mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", self.root),
                  mock.patch.object(indexer.client, "flat_listing", return_value=LISTING),
                  mock.patch.object(indexer.client, "video_details", side_effect=_details),
                  mock.patch.object(indexer.feeds, "api_available", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def _files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())

    def _remove_from_the_page(self, channel_id):
        from routers import youtube as yt_router
        other = self.Session()
        try:
            with mock.patch.object(yt_router.threading, "Thread"):
                return yt_router.delete_channel(channel_id, db=other)
        finally:
            other.close()

    def test_files_written_before_and_after_the_removal_are_removed(self):
        ch = self.channel(keep_count=3)
        answers = []

        def artwork(video, folder):
            # Remove is pressed while the second video's files are written.
            if not answers and video.video_id.endswith("01"):
                answers.append(self._remove_from_the_page(ch.id))
            return 0

        with mock.patch.object(library, "fetch_artwork", side_effect=artwork):
            with self.assertRaises(Exception):
                ysync.sync_channel(self.db, ch, BASE)
        self.db.rollback()
        self.assertEqual(1, len(answers))
        self.assertEqual(0, self.db.query(YouTubeVideo).count())
        self.assertEqual([], self._files())
        self.assertEqual([], list(self.root.iterdir()))

    def test_a_failed_save_of_a_channel_still_there_keeps_its_files(self):
        # Nothing removed: the files stay for the next run to adopt (a .strm is
        # never rewritten, so nothing is lost by keeping it).
        ch = self.channel(keep_count=3)
        real_commit, calls = self.db.commit, []

        def flaky_commit():
            calls.append(1)
            if any(v.folder_path for v in self.db.dirty if isinstance(v, YouTubeVideo)):
                raise OperationalError("COMMIT", {}, Exception("database is locked"))
            return real_commit()

        with mock.patch.object(library, "fetch_artwork", return_value=0), \
             mock.patch.object(self.db, "commit", side_effect=flaky_commit):
            with self.assertRaises(OperationalError):
                ysync.sync_channel(self.db, ch, BASE)
        self.db.rollback()
        self.assertEqual(6, len(self._files()))
        self.assertEqual(3, self.db.query(YouTubeVideo).count())

    def test_the_next_run_after_such_a_failure_writes_nothing_twice(self):
        ch = self.channel(keep_count=3)
        with mock.patch.object(library, "fetch_artwork", return_value=0):
            ysync.sync_channel(self.db, ch, BASE)
            before = self._files()
            ysync.sync_channel(self.db, ch, BASE)
        self.assertEqual(6, len(before))
        self.assertEqual(before, self._files())

    def test_a_removal_after_the_save_is_left_to_the_removal(self):
        ch = self.channel(keep_count=3)
        with mock.patch.object(library, "fetch_artwork", return_value=0):
            ysync.sync_channel(self.db, ch, BASE)
        self.assertEqual(6, len(self._files()))
        self._remove_from_the_page(ch.id)
        self.assertEqual([], self._files())


if __name__ == "__main__":
    unittest.main()

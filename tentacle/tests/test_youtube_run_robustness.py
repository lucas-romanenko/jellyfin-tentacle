"""#245 and #278: one channel must not end a YouTube run, and two runs must not
index at once.

#245: the scheduled check and a refresh someone started were not serialised.
Both could index the same channel, both saw the same new upload, and the second
insert failed on UNIQUE(video_id). The scheduled loop only caught YouTubeError,
so that IntegrityError (or #253's "database is locked") ended the whole run:
the channels after it were not checked, nothing was published.

#278: removing a channel while a run was on it raised ObjectDeletedError from
the error bookkeeping itself (_note_channel_error read channel.title), which
ended the run the same way; and a row the run committed after the removal
outlived its channel, so the channel added again skipped that video for ever
("already indexed under another channel or playlist").

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_run_robustness.py
"""
import threading
import unittest
from datetime import datetime
from unittest import mock

from sqlalchemy.exc import IntegrityError, OperationalError

from models.database import YouTubeChannel, YouTubeVideo, set_setting
from services.youtube import indexer, library
from services.youtube import sync as ysync
from test_youtube_traffic import _Db

BASE = "http://192.0.2.20:8888"
DETAILS = {"title": "New upload", "availability": "public", "live_status": "not_live",
           "duration": 600, "timestamp": 1758412800}


class TwoRunsDoNotIndexAtOnce(_Db):
    def test_a_second_run_waits_for_the_first_and_nothing_is_inserted_twice(self):
        ch = self.channel(min_duration=0, live_enabled=False)
        other = self.Session()
        self.addCleanup(other.close)
        ch_other = other.get(YouTubeChannel, ch.id)
        inside, release = threading.Event(), threading.Event()
        calls, errors = [], []

        def details(video_id, *a, **kw):
            calls.append(threading.current_thread().name)
            inside.set()
            release.wait(5)
            return dict(DETAILS, id=video_id)

        listing = {"entries": [{"id": "n" * 11, "title": "New upload"}]}

        def run(db, channel):
            try:
                ysync.sync_channel(db, channel, BASE)
            except Exception as e:          # pragma: no cover - the failure being tested
                errors.append(e)

        with mock.patch.object(indexer.client, "flat_listing", return_value=listing), \
             mock.patch.object(indexer.client, "video_details", side_effect=details), \
             mock.patch.object(library, "write_video", return_value={}), \
             mock.patch.object(library, "fetch_artwork", return_value=0):
            first = threading.Thread(target=run, args=(self.db, ch), name="scheduled")
            second = threading.Thread(target=run, args=(other, ch_other), name="manual")
            first.start()
            self.assertTrue(inside.wait(5))
            second.start()
            second.join(0.5)
            # The manual run has not started reading while the scheduled one indexes.
            self.assertEqual(["scheduled"], calls)
            release.set()
            first.join(5)
            second.join(5)
        self.assertEqual([], errors)
        self.assertEqual(1, self.db.query(YouTubeVideo).filter(YouTubeVideo.video_id == "n" * 11).count())


class OneChannelDoesNotEndTheScheduledCheck(_Db):
    def _run(self, failure):
        set_setting(self.db, "youtube_enabled", "true")
        bad = self.channel(title="Bad", slug="bad", channel_id="UC" + "b" * 22)
        good = self.channel(title="Good", slug="good", channel_id="UC" + "g" * 22)
        seen = []

        def sync_channel(db, channel, base, light=False, **kw):
            seen.append(channel.title)
            if channel.title == "Bad":
                raise failure
            return {"new": 1, "written": 1}

        with mock.patch.object(ysync, "base_url", return_value=BASE), \
             mock.patch.object(ysync, "sync_channel", side_effect=sync_channel), \
             mock.patch.object(ysync, "publish_to_jellyfin") as publish, \
             mock.patch.object(ysync, "reconcile_playlists", return_value=0), \
             mock.patch("random.shuffle", lambda x: x.sort(key=lambda c: c.title)), \
             mock.patch.object(ysync.time, "sleep"):
            totals = ysync.run_youtube_sync()
        return seen, totals, publish, good

    def test_a_unique_constraint_failure_is_that_channels_error_only(self):
        failure = IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: youtube_videos.video_id"))
        seen, totals, publish, good = self._run(failure)
        self.assertEqual(["Bad", "Good"], seen)
        self.assertEqual(1, totals["errors"])
        self.assertEqual(1, totals["written"])
        publish.assert_called_once()
        self.assertEqual(["Good"], [c.title for c in publish.call_args[0][1]])

    def test_a_locked_database_is_that_channels_error_only(self):
        seen, totals, publish, _ = self._run(OperationalError("UPDATE", {}, Exception("database is locked")))
        self.assertEqual(["Bad", "Good"], seen)
        publish.assert_called_once()


class RemovingAChannelMidRun(_Db):
    def test_an_orphan_row_does_not_block_the_re_added_channel(self):
        self.db.add(YouTubeVideo(channel_fk=999, video_id="abcdefghijk", title="newest upload",
                                 first_seen=datetime(2026, 9, 28), last_seen=datetime(2026, 9, 28)))
        self.db.commit()
        ch = self.channel(live_enabled=False, min_duration=0, keep_count=5)
        listing = {"entries": [{"id": "abcdefghijk"}]}
        with mock.patch.object(indexer.client, "flat_listing", return_value=listing), \
             mock.patch.object(indexer.client, "video_details",
                               return_value=dict(DETAILS, id="abcdefghijk", title="newest upload")):
            result = indexer.index_channel(self.db, ch)
        self.assertEqual(1, result["new"])
        rows = self.db.query(YouTubeVideo).filter(YouTubeVideo.video_id == "abcdefghijk").all()
        self.assertEqual([ch.id], [r.channel_fk for r in rows])

    def test_recording_an_error_for_a_removed_channel_does_not_raise(self):
        from routers import youtube as yt_router
        ch = self.channel(live_enabled=False)
        other = self.Session()
        other.delete(other.get(YouTubeChannel, ch.id))
        other.commit()
        other.close()
        self.db.expire_all()
        yt_router._note_channel_error(self.db, ch, RuntimeError("boom"))
        self.assertIn("boom", yt_router._refresh_state["error_detail"])

    def test_a_channel_removed_during_a_refresh_only_ends_its_own_work(self):
        from routers import youtube as yt_router
        a = self.channel(title="A", slug="a", channel_id="UC" + "a" * 22)
        b = self.channel(title="B", slug="b", channel_id="UC" + "b" * 22)
        c = self.channel(title="C", slug="c", channel_id="UC" + "d" * 22)
        synced = []

        def sync_channel(db, channel, base, **kw):
            synced.append(channel.title)
            if channel.title == "A":
                # B is removed from the page while A is being indexed.
                other = self.Session()
                other.delete(other.get(YouTubeChannel, b.id))
                other.commit()
                other.close()
            db.commit()                          # as the real sync does
            return {"new": 1, "written": 1}

        yt_router._refresh_state.update(errors=0, error_detail=None, new=0, written=0, retired=0,
                                        channels_done=0)
        with mock.patch.object(ysync, "base_url", return_value=BASE), \
             mock.patch.object(ysync, "sync_channel", side_effect=sync_channel), \
             mock.patch.object(ysync, "publish_to_jellyfin") as publish, \
             mock.patch.object(ysync, "reconcile_playlists", return_value=0), \
             mock.patch.object(yt_router, "MANUAL_CHANNEL_GAP_SECONDS", (0, 0)):
            yt_router._run_refresh_once([a.id, b.id, c.id])
        self.assertEqual(["A", "C"], synced)
        self.assertEqual(0, yt_router._refresh_state["errors"])
        publish.assert_called_once()
        self.assertEqual(["A", "C"], sorted(ch.title for ch in publish.call_args[0][1]))

    def test_delete_leaves_no_row_of_the_channel(self):
        from routers import youtube as yt_router
        ch = self.channel()
        self.db.add(YouTubeVideo(channel_fk=ch.id, video_id="v" * 11, title="V"))
        self.db.commit()
        with mock.patch.object(yt_router.threading, "Thread"):
            yt_router.delete_channel(ch.id, db=self.db)
        self.assertEqual(0, self.db.query(YouTubeVideo).count())


if __name__ == "__main__":
    unittest.main()

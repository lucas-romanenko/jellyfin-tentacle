"""Indexer behaviour re-checked at 1633dd1.

1f3f559 + b97a8b9 added the finished-broadcast filter (was_live and post_live)
and _drop_disqualified(). TestFinishedBroadcastsAreFiltered passes now and is
kept as a regression guard.

What is still wrong:

* One video listed on two of a channel's tabs is queued twice in the same run,
  and youtube_videos.video_id is unique, so the run dies on an IntegrityError.
  1f3f559 made this MORE likely, not less: a Live TV channel now always polls
  /streams as well as /videos, which is exactly the pair of tabs that overlap.
* A video the filters reject is never recorded, so its details are re-extracted
  on every run for ever, against a ~300/hour budget.
* _drop_disqualified() matches only was_live/post_live. is_library_item()'s own
  docstring says "once a stream ends its live_status clears" — and a video
  whose re-check clears it to NULL is a library item by is_library_status(),
  so the broadcast the filter was written to exclude lands in the library
  anyway.

Needs sqlalchemy only (as CI has).
Run from tentacle/:  python -m unittest tests.test_youtube_indexer_followup
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _fresh_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models.database as mdb
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _channel(db, **kw):
    from models.database import YouTubeChannel
    fields = dict(
        input_url="https://www.youtube.com/@ch", kind="channel",
        channel_id="UC" + "z" * 22, title="Ch", slug="ch", enabled=True,
        live_enabled=True, include_videos=True, include_streams=False,
        min_duration=60, keep_count=30, extra_tags=[],
    )
    fields.update(kw)
    channel = YouTubeChannel(**fields)
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


def _tabs(streams_entries=(), videos_entries=()):
    def _listing(url, limit):
        if url.endswith("/streams"):
            return {"entries": list(streams_entries)}
        return {"entries": list(videos_entries)}
    return _listing


class _IndexerCase(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        self.channel = _channel(self.db)

    def tearDown(self):
        self.db.close()

    def _sync(self, details, streams=(), videos=()):
        """Full sync against a temporary media root; returns the .strm names."""
        from services.youtube import indexer, library, sync
        root = Path(tempfile.mkdtemp())
        with mock.patch.object(indexer.client, "flat_listing",
                               side_effect=_tabs(streams, videos)), \
             mock.patch.object(indexer.client, "video_details", return_value=details), \
             mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", root), \
             mock.patch.object(indexer.time, "sleep"):
            sync.sync_channel(self.db, self.channel, "http://192.0.2.20:8888")
        return sorted(p.name for p in root.rglob("*.strm"))


class TestFinishedBroadcastsAreFiltered(_IndexerCase):
    """FIXED by 1f3f559 + b97a8b9 — kept so a regression is caught."""

    def test_a_finished_broadcast_is_not_indexed_when_past_streams_are_off(self):
        details = {"title": "Yesterday's broadcast", "availability": "public",
                   "live_status": "was_live", "duration": 7200,
                   "upload_date": "20260916"}
        self.assertEqual([], self._sync(details, streams=[{"id": "aaaaaaaaaaa"}]),
                         "a past broadcast was written into the library")

    def test_post_live_is_treated_the_same(self):
        details = {"title": "Just ended", "availability": "public",
                   "live_status": "post_live", "duration": 7200,
                   "upload_date": "20260916"}
        self.assertEqual([], self._sync(details, streams=[{"id": "aaaaaaaaaaa"}]))

    def test_it_is_still_indexed_when_the_user_asked_for_it(self):
        self.channel.include_streams = True
        self.db.commit()
        details = {"title": "Yesterday's broadcast", "availability": "public",
                   "live_status": "was_live", "duration": 7200,
                   "upload_date": "20260916"}
        self.assertEqual(1, len(self._sync(details, streams=[{"id": "aaaaaaaaaaa"}])))

    def test_a_live_broadcast_is_still_indexed_for_the_guide(self):
        from models.database import YouTubeVideo
        from services.youtube import indexer
        details = {"title": "On now", "availability": "public",
                   "live_status": "is_live", "duration": None,
                   "release_timestamp": 1789574400}
        with mock.patch.object(indexer.client, "flat_listing",
                               side_effect=_tabs([{"id": "bbbbbbbbbbb"}])), \
             mock.patch.object(indexer.client, "video_details", return_value=details), \
             mock.patch.object(indexer.time, "sleep"):
            indexer.index_channel(self.db, self.channel)
        rows = self.db.query(YouTubeVideo).all()
        self.assertEqual(1, len(rows))
        self.assertEqual("is_live", rows[0].live_status)


class TestABroadcastWhoseStatusClearsIsStillFiltered(_IndexerCase):
    """STILL BROKEN — _drop_disqualified() matches was_live/post_live only.

    index_channel() re-checks every pending broadcast and writes back whatever
    live_status the details carry. When that is NULL the row is a library item
    by is_library_status(), _drop_disqualified() passes over it, and the next
    sync writes .strm + NFO for a broadcast the user said not to keep.
    """

    def test_a_stream_that_ends_with_a_null_status_does_not_enter_the_library(self):
        from models.database import YouTubeVideo
        from services.youtube import indexer, library, sync

        # Run one: the broadcast is on air, so it is a guide entry only.
        live = {"title": "Tonight's stream", "availability": "public",
                "live_status": "is_live", "duration": None,
                "release_timestamp": 1789574400}
        root = Path(tempfile.mkdtemp())
        with mock.patch.object(indexer.client, "flat_listing",
                               side_effect=_tabs([{"id": "aaaaaaaaaaa"}])), \
             mock.patch.object(indexer.client, "video_details", return_value=live), \
             mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", root), \
             mock.patch.object(indexer.time, "sleep"):
            sync.sync_channel(self.db, self.channel, "http://192.0.2.20:8888")
        self.assertEqual([], sorted(p.name for p in root.rglob("*.strm")))

        # Run two: it has ended and yt-dlp no longer reports any live marker —
        # the case is_library_item()'s docstring describes as "its live_status
        # clears". _should_index() never sees it again; only the pending
        # re-check and _drop_disqualified() apply.
        ended = {"title": "Tonight's stream", "availability": "public",
                 "live_status": None, "duration": 7200,
                 "upload_date": "20260916"}
        with mock.patch.object(indexer.client, "flat_listing",
                               side_effect=_tabs([{"id": "aaaaaaaaaaa"}])), \
             mock.patch.object(indexer.client, "video_details", return_value=ended), \
             mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", root), \
             mock.patch.object(indexer.time, "sleep"):
            sync.sync_channel(self.db, self.channel, "http://192.0.2.20:8888")

        row = self.db.query(YouTubeVideo).one()
        self.assertIsNotNone(
            row.removed_at,
            "the finished broadcast is still an active library row although "
            "'Past live streams' is off — its live_status cleared to NULL, "
            "which _drop_disqualified() does not match",
        )
        self.assertEqual(
            [], sorted(p.name for p in root.rglob("*.strm")),
            "a finished broadcast was written into the Movies library",
        )


class TestOneVideoOnTwoTabs(_IndexerCase):
    """STILL BROKEN — and 1f3f559 made the overlapping-tabs case the norm."""

    def test_one_video_listed_on_two_tabs_is_indexed_once(self):
        from models.database import YouTubeVideo
        from services.youtube import indexer
        details = {"title": "Broadcast", "availability": "public",
                   "live_status": "is_live", "duration": None,
                   "release_timestamp": 1789574400}
        entry = {"id": "ccccccccccc"}
        with mock.patch.object(indexer.client, "flat_listing",
                               side_effect=_tabs([entry], [entry])), \
             mock.patch.object(indexer.client, "video_details", return_value=details), \
             mock.patch.object(indexer.time, "sleep"):
            indexer.index_channel(self.db, self.channel)
        self.assertEqual(1, self.db.query(YouTubeVideo).count())


class TestRejectedVideosAreRemembered(unittest.TestCase):
    """STILL BROKEN — a rejected video is re-extracted on every run for ever."""

    def setUp(self):
        self.db = _fresh_db()
        self.channel = _channel(self.db, live_enabled=False)

    def tearDown(self):
        self.db.close()

    def test_a_rejected_video_is_not_re_fetched_on_the_next_run(self):
        from services.youtube import indexer
        listing = {"entries": [{"id": "aaaaaaaaaab"}]}
        # 20s clip, under the channel's 60s minimum: rejected every time.
        details = {"title": "Short clip", "availability": "public",
                   "duration": 20, "upload_date": "20260916"}
        calls = []

        def _details(video_id, js_runtime=None):
            calls.append(video_id)
            return details

        with mock.patch.object(indexer.client, "flat_listing", return_value=listing), \
             mock.patch.object(indexer.client, "video_details", side_effect=_details), \
             mock.patch.object(indexer.time, "sleep"):
            indexer.index_channel(self.db, self.channel)
            first = len(calls)
            indexer.index_channel(self.db, self.channel)
            indexer.index_channel(self.db, self.channel)

        self.assertEqual(1, first, "the first run should look the video up once")
        self.assertEqual(
            1, len(calls),
            f"a rejected video was looked up again on later runs ({len(calls)} "
            "extraction(s) for one video in three runs) — guest extraction is "
            "rate-limited at ~300 videos/hour and this repeats for ever",
        )

    def test_the_skip_is_still_counted_for_the_ui(self):
        """1633dd1's per-reason skip counts must survive the fix."""
        from services.youtube import indexer
        listing = {"entries": [{"id": "aaaaaaaaaab"}]}
        details = {"title": "Short clip", "availability": "public",
                   "duration": 20, "upload_date": "20260916"}
        with mock.patch.object(indexer.client, "flat_listing", return_value=listing), \
             mock.patch.object(indexer.client, "video_details", return_value=details), \
             mock.patch.object(indexer.time, "sleep"):
            result = indexer.index_channel(self.db, self.channel)
        self.assertEqual(1, result["filtered"])
        self.assertEqual({"duration Ns under minimum Ns": 1}, result["skips"])
        self.assertEqual({"duration Ns under minimum Ns": 1}, self.channel.last_skips)


if __name__ == "__main__":
    unittest.main()

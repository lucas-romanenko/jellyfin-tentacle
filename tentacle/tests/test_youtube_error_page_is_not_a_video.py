"""A video page YouTube refuses is not read as a video.

Every extraction runs with ignore_no_formats_error (an upcoming stream's details
need it). With it, yt-dlp 2026.8.19 does not raise when YouTube answers a video
page with an error: it reports the reason as a warning and returns a "video"
with no formats, no date, no length and the title "youtube video #<id>". So:

- a new upload read during a rate limit was stored as "youtube video #<id>"
  with no date, and with the channel at "keep newest N" retention deleted it at
  once (a dateless row sorts oldest), for good;
- a bot check, a captcha or the rate limit on a video page started no pause;
- a dead video was never known to be gone: the resolver saw "no usable HLS",
  never VideoUnavailable, so retiring it (#241) and taking a dead placeholder
  out (#275) could not happen on a real install.

These tests run the real yt-dlp offline: the watch page's player response is
canned in YouTube's shape, nothing is fetched.
Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_error_page_is_not_a_video.py
"""
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from yt_dlp.extractor.youtube._video import YoutubeIE

from models.database import YouTubeVideo
from services.youtube import client, indexer, library, resolver, traffic
from services.youtube import sync as ysync
from services.youtube.errors import VideoUnavailable, YouTubeBlocked, YouTubeUnavailable
from test_youtube_traffic import _Db
from tmp_dirs import temp_dir

BASE = "http://192.0.2.20:8888"


def _page(status, reason, subreason=None, details=None, captcha=False, extra=None):
    """A player response the way YouTube sends a refused (or upcoming) video."""
    screen = {"playerErrorMessageRenderer": {"reason": {"simpleText": reason}}}
    if subreason:
        screen["playerErrorMessageRenderer"]["subreason"] = {"simpleText": subreason}
    if captcha:
        screen["playerCaptchaViewModel"] = {"title": {"content": "Confirm you are not a robot"}}
    page = {"playabilityStatus": {"status": status, "reason": reason, "errorScreen": screen}}
    if details:
        page["videoDetails"] = details
    page.update(extra or {})
    return page


PAGES = {
    "rate limit": _page("UNPLAYABLE", "Video unavailable", "This content isn't available, try again later."),
    "captcha": _page("UNPLAYABLE", "Video unavailable", captcha=True),
    "bot check": _page("LOGIN_REQUIRED", "Sign in to confirm you’re not a bot",
                       "This helps protect our community. Learn more"),
    "removed": _page("ERROR", "Video unavailable", "This video has been removed by the uploader"),
    "private": _page("LOGIN_REQUIRED", "Private video", "Sign in if you've been granted access to this video"),
    "members-only": _page("LOGIN_REQUIRED", "Join this channel to get access to members-only content like "
                          "this video, and other exclusive perks.",
                          details={"title": "Members", "lengthSeconds": "600", "isLiveContent": False}),
    # About one video, though they say "try again later" like the rate limit does.
    "processing": _page("UNPLAYABLE", "This video is still being processed. Please try again later."),
    "something wrong": _page("UNPLAYABLE", "Something went wrong. Refresh or try again later."),
    "age gate": _page("LOGIN_REQUIRED", "Sign in to confirm your age",
                      "This video may be inappropriate for some users.",
                      details={"title": "Grown-ups", "lengthSeconds": "600", "isLiveContent": False}),
}


def _upcoming():
    start = str(int(datetime.utcnow().timestamp()) + 3 * 3600)
    return _page("LIVE_STREAM_OFFLINE", "This live event will begin in 3 hours.",
                 details={"title": "Tonight's show", "isLiveContent": True, "isUpcoming": True},
                 extra={"playabilityStatus": {
                     "status": "LIVE_STREAM_OFFLINE", "reason": "This live event will begin in 3 hours.",
                     "liveStreamability": {"liveStreamabilityRenderer": {"offlineSlate": {
                         "liveStreamOfflineSlateRenderer": {"scheduledStartTime": start}}}}}})


class _RealYtDlp(_Db):
    def setUp(self):
        super().setUp()
        self.pages = {}
        pages = self.pages

        def fake_initial(ie, url, smuggled, webpage_url, webpage_client, video_id):
            return "", {}, {}, False, [pages[video_id]], None
        p = mock.patch.object(YoutubeIE, "_initial_extract", fake_initial)
        p.start()
        self.addCleanup(p.stop)
        resolver._unplayable.clear()
        resolver._failures.clear()
        self.addCleanup(resolver._unplayable.clear)
        self.addCleanup(resolver._failures.clear)


class WhatAVideoPageRefusalIs(_RealYtDlp):
    def _details(self, name):
        self.pages["abcdefghijk"] = PAGES[name]
        with self.assertRaises(Exception) as caught:
            client.video_details("abcdefghijk")
        return caught.exception

    def test_rate_limit_captcha_and_bot_check_are_blocks_that_pause(self):
        for name in ("rate limit", "captcha", "bot check"):
            with self.subTest(name):
                traffic.clear_pause()
                self.assertIsInstance(self._details(name), YouTubeBlocked)
                self.assertTrue(traffic.pause_state()["paused"], name)

    def test_a_refused_video_is_unavailable_and_starts_no_pause(self):
        for name in ("removed", "private", "members-only", "age gate"):
            with self.subTest(name):
                error = self._details(name)
                self.assertIsInstance(error, VideoUnavailable)
                self.assertNotIsInstance(error, YouTubeBlocked)
                self.assertFalse(traffic.pause_state()["paused"], name)

    def test_try_again_later_about_one_video_is_a_hiccup_not_a_pause(self):
        for name in ("processing", "something wrong"):
            with self.subTest(name):
                error = self._details(name)
                self.assertIsInstance(error, YouTubeUnavailable)
                self.assertNotIsInstance(error, YouTubeBlocked)
                self.assertFalse(traffic.pause_state()["paused"], name)

    def test_such_a_video_backs_off_its_own_plays_and_is_not_retired(self):
        self.pages["processing1"] = PAGES["processing"]
        for _ in range(3):
            resolver._failures.clear()            # each play after the back-off
            with self.assertRaises(YouTubeUnavailable):
                resolver.resolve("processing1")
        self.assertFalse(traffic.pause_state()["paused"])
        with self.assertRaises(resolver.ResolveBackoff):
            resolver.resolve("processing1")       # the back-off is recorded
        self.assertNotIn("processing1", resolver.unplayable_ids())

    def test_an_upcoming_stream_still_gives_its_details(self):
        self.pages["upcomingvid"] = _upcoming()
        info = client.video_details("upcomingvid")
        self.assertEqual("is_upcoming", info["live_status"])
        self.assertEqual("Tonight's show", info["title"])

    def test_a_listing_is_not_checked(self):
        with mock.patch.object(client, "_ydl") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value = {"entries": []}
            self.assertEqual({"entries": []}, client.flat_listing("https://www.youtube.com/@x/videos", 5))


class ANewUploadDuringARateLimit(_RealYtDlp):
    def test_is_not_stored_as_a_placeholder_and_retention_keeps_the_library(self):
        ch = self.channel(include_streams=False, min_duration=0, keep_count=3, live_enabled=False)
        old = ["o%010d" % i for i in range(3)]
        for i, vid in enumerate(old):
            self.db.add(YouTubeVideo(channel_fk=ch.id, video_id=vid, title=f"Old {i}", duration=600,
                                     published_at=datetime(2026, 9, 1 + i), strm_path=f"/x/{vid}.strm",
                                     first_seen=datetime(2026, 9, 1), last_seen=datetime(2026, 9, 1)))
        self.db.commit()
        self.pages["newupload01"] = PAGES["rate limit"]
        listing = {"entries": [{"id": "newupload01", "title": "Brand new"}] + [{"id": v} for v in old]}
        with mock.patch.object(client, "flat_listing", return_value=listing), \
                mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", Path(temp_dir(self))), \
                mock.patch.object(library, "fetch_artwork", return_value=0), \
                self.assertRaises(YouTubeBlocked):
            ysync.sync_channel(self.db, ch, BASE)
        self.assertTrue(traffic.pause_state()["paused"])
        self.assertEqual([], self.db.query(YouTubeVideo).filter_by(video_id="newupload01").all(),
                         "nothing is recorded for a video read during a block")
        kept = self.db.query(YouTubeVideo).filter(YouTubeVideo.removed_at.is_(None)).count()
        self.assertEqual(3, kept)


class ANewUploadStillBeingProcessed(_RealYtDlp):
    def test_is_read_again_later_and_nothing_pauses(self):
        ch = self.channel(include_streams=False, min_duration=0, keep_count=3, live_enabled=False)
        self.pages["processing2"] = PAGES["processing"]
        with mock.patch.object(client, "flat_listing", return_value={"entries": [{"id": "processing2"}]}), \
                mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", Path(temp_dir(self))), \
                mock.patch.object(library, "fetch_artwork", return_value=0):
            ysync.sync_channel(self.db, ch, BASE)
        self.assertFalse(traffic.pause_state()["paused"])
        row = self.db.query(YouTubeVideo).filter_by(video_id="processing2").one()
        self.assertIsNotNone(row.removed_at, "not a library item")
        self.assertEqual(indexer.UNREADABLE_REASON, row.skip_reason)
        self.assertIsNotNone(row.next_check_at)


class ADeadVideoIsKnownToBeGone(_RealYtDlp):
    def test_the_resolver_counts_it_and_the_next_sync_retires_it_for_a_re_check(self):
        ch = self.channel(include_streams=False, min_duration=0, keep_count=5, live_enabled=False)
        video = YouTubeVideo(channel_fk=ch.id, video_id="deadvideo01", title="Was here",
                             strm_path="/x/d.strm", first_seen=datetime(2026, 9, 1),
                             last_seen=datetime(2026, 9, 1))
        self.db.add(video)
        self.db.commit()
        self.pages["deadvideo01"] = PAGES["removed"]
        for _ in range(2):
            resolver._failures.clear()          # the next probe comes after the back-off
            with self.assertRaises(VideoUnavailable):
                resolver.resolve("deadvideo01")
        self.assertIn("deadvideo01", resolver.unplayable_ids())
        with mock.patch.object(library, "remove_video"):
            self.assertEqual(1, ysync._retire_unplayable(self.db, ch))
        self.db.refresh(video)
        self.assertEqual(indexer.PLAYBACK_REASON, video.skip_reason)
        self.assertIsNotNone(video.next_check_at)

    def test_a_dead_placeholder_leaves_the_library(self):
        ch = self.channel(kind="playlist", playlist_id="PL" + "k" * 18, include_streams=False,
                          min_duration=0, keep_count=41, live_enabled=False)
        root = Path(temp_dir(self))
        video = YouTubeVideo(channel_fk=ch.id, video_id="deadvideo02", title="youtube video #deadvideo02",
                             first_seen=datetime(2026, 9, 21), last_seen=datetime(2026, 9, 21))
        self.db.add(video)
        self.db.commit()
        with mock.patch.object(library, "fetch_artwork", return_value=0):
            library.write_video(video, ch, BASE, root=root)
        self.db.commit()
        self.pages["deadvideo02"] = PAGES["removed"]
        with mock.patch.object(client, "flat_listing", return_value={"entries": [{"id": "deadvideo02"}]}), \
                mock.patch.object(library, "YOUTUBE_MEDIA_ROOT", root), \
                mock.patch.object(library, "fetch_artwork", return_value=0):
            ysync.sync_channel(self.db, ch, BASE)
        self.db.refresh(video)
        self.assertIsNotNone(video.removed_at)
        self.assertIsNotNone(video.next_check_at)


class ARetiredVideoComesBackOnlyWhenItPlays(_Db):
    """Its details can read fine (the Data API calls a members-only video public)
    while the player still can't open it: that must not bring it back."""

    def _retired(self, ch):
        video = YouTubeVideo(channel_fk=ch.id, video_id="m" * 11, title="Members", live_status=None,
                             strm_path="/x/m.strm", first_seen=datetime(2026, 9, 1),
                             last_seen=datetime(2026, 9, 1))
        self.db.add(video)
        self.db.commit()
        resolver._unplayable["m" * 11] = 2
        self.addCleanup(resolver._unplayable.clear)
        with mock.patch.object(library, "remove_video"):
            ysync._retire_unplayable(self.db, ch)
        self.db.refresh(video)
        return video

    def _retry(self, ch, video, plays):
        from datetime import timedelta
        video.next_check_at = datetime.utcnow() - timedelta(minutes=1)
        self.db.commit()
        details = {"id": "m" * 11, "title": "Members", "availability": None,
                   "live_status": None, "duration": 600, "timestamp": 1758412800}
        with mock.patch.object(client, "flat_listing", return_value={"entries": [{"id": "m" * 11}]}), \
                mock.patch.object(client, "video_details", return_value=details), \
                mock.patch.object(resolver, "resolve", side_effect=plays):
            indexer.index_channel(self.db, ch)
        self.db.refresh(video)

    def test_details_say_public_but_the_player_refuses(self):
        ch = self.channel(include_streams=False, min_duration=0, keep_count=5, live_enabled=False)
        video = self._retired(ch)
        first = video.check_failures
        self._retry(ch, video, VideoUnavailable("Join this channel to get access to members-only content"))
        self.assertIsNotNone(video.removed_at)
        self.assertEqual(indexer.PLAYBACK_REASON, video.skip_reason)
        self.assertEqual(first + 1, video.check_failures, "the re-check interval keeps doubling")

    def test_it_plays_again_and_stays(self):
        ch = self.channel(include_streams=False, min_duration=0, keep_count=5, live_enabled=False)
        video = self._retired(ch)
        self._retry(ch, video, None)
        self.assertIsNone(video.removed_at)
        with mock.patch.object(library, "remove_video"):
            self.assertEqual(0, ysync._retire_unplayable(self.db, ch), "retired again at once")


if __name__ == "__main__":
    unittest.main()

"""#241: YouTube's session rate limit ("This content isn't available, try again
later") read as "video unavailable".

yt-dlp words the limit as "Video unavailable. This content isn't available, try
again later. The current session has been rate-limited by YouTube for up to an
hour." classify() found "video unavailable" in it and blamed the one video, so:
nothing paused, the resolver counted it towards retiring the video, the next
sync deleted a playable video's files, and a retired row was never read again.

Run from tentacle/:  python -m unittest discover -s tests -p test_youtube_rate_limit_retire.py
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from models.database import YouTubeVideo
from services.youtube import client, indexer, library, resolver, traffic
from services.youtube import sync as ysync
from services.youtube.errors import VideoUnavailable, YouTubeBlocked, classify
from test_youtube_traffic import _Db

RATE_LIMIT = ("ERROR: [youtube] aaaaaaaaaaa: Video unavailable. This content isn't available, "
              "try again later. The current session has been rate-limited by YouTube for up to "
              "an hour. It is recommended to use `-t sleep` to add a delay between video requests "
              "to avoid exceeding the rate limit.")
CAPTCHA = ("ERROR: [youtube] aaaaaaaaaaa: Video unavailable. YouTube is requiring a captcha "
           "challenge before playback")


class _Raises:
    def __init__(self, text):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        raise Exception(self.text)


class TheRateLimitIsABlock(_Db):
    def test_the_rate_limit_text_is_a_block(self):
        self.assertIsInstance(classify(Exception(RATE_LIMIT)), YouTubeBlocked)

    def test_the_captcha_text_is_a_block(self):
        self.assertIsInstance(classify(Exception(CAPTCHA)), YouTubeBlocked)

    def test_a_removed_video_is_still_about_the_video(self):
        e = classify(Exception("ERROR: [youtube] x: Video unavailable. This video has been "
                               "removed by the uploader"))
        self.assertIsInstance(e, VideoUnavailable)

    def test_the_rate_limit_starts_the_pause(self):
        with mock.patch.object(client, "_ydl", return_value=_Raises(RATE_LIMIT)):
            with self.assertRaises(YouTubeBlocked):
                client.extract("https://www.youtube.com/watch?v=aaaaaaaaaaa")
        self.assertTrue(traffic.paused())


class OnlyAClearVerdictRetires(_Db):
    def _fail_twice(self, text):
        for _ in range(2):
            with mock.patch.object(client, "extract", side_effect=classify(Exception(text))):
                with self.assertRaises(Exception):
                    resolver.resolve("p" * 11)
            resolver._failures.clear()          # past its back-off
            traffic.clear_pause()

    def _video(self, ch):
        video = YouTubeVideo(channel_fk=ch.id, video_id="p" * 11, title="Plays fine",
                             live_status="not_live", strm_path="/x/p.strm")
        self.db.add(video)
        self.db.commit()
        return video

    def test_a_rate_limited_video_is_not_retired(self):
        ch = self.channel()
        video = self._video(ch)
        self._fail_twice(RATE_LIMIT)
        with mock.patch.object(library, "remove_video") as removed:
            self.assertEqual(0, ysync._retire_unplayable(self.db, ch))
        removed.assert_not_called()
        self.db.refresh(video)
        self.assertIsNone(video.removed_at)

    def test_a_bare_video_unavailable_is_not_enough(self):
        ch = self.channel()
        video = self._video(ch)
        self._fail_twice("ERROR: [youtube] ppppppppppp: Video unavailable")
        with mock.patch.object(library, "remove_video"):
            self.assertEqual(0, ysync._retire_unplayable(self.db, ch))
        self.db.refresh(video)
        self.assertIsNone(video.removed_at)

    def test_a_private_video_is_still_retired(self):
        ch = self.channel()
        video = self._video(ch)
        self._fail_twice("ERROR: [youtube] ppppppppppp: Private video. Sign in if you've been "
                         "granted access to this video")
        with mock.patch.object(library, "remove_video"):
            self.assertEqual(1, ysync._retire_unplayable(self.db, ch))
        self.db.refresh(video)
        self.assertIsNotNone(video.removed_at)


class ARetiredVideoIsReadAgain(_Db):
    def test_retirement_schedules_a_re_read_and_a_playable_video_comes_back(self):
        ch = self.channel(include_streams=False, min_duration=0, keep_count=5, live_enabled=False)
        video = YouTubeVideo(channel_fk=ch.id, video_id="p" * 11, title="Back again",
                             live_status=None, strm_path="/x/p.strm",
                             first_seen=datetime(2026, 9, 1), last_seen=datetime(2026, 9, 1))
        self.db.add(video)
        self.db.commit()
        resolver._unplayable["p" * 11] = 2
        with mock.patch.object(library, "remove_video"):
            self.assertEqual(1, ysync._retire_unplayable(self.db, ch))
        self.db.refresh(video)
        self.assertIsNotNone(video.next_check_at)
        # Forgotten by the resolver: the video must not be retired again at once
        # when a re-read brings it back.
        self.assertNotIn("p" * 11, resolver.unplayable_ids())

        # Later, the listing still shows it and its details read fine: it returns.
        video.next_check_at = datetime.utcnow() - timedelta(minutes=1)
        self.db.commit()
        details = {"id": "p" * 11, "title": "Back again", "availability": "public",
                   "live_status": "not_live", "duration": 600, "timestamp": 1758412800}
        listing = {"entries": [{"id": "p" * 11, "title": "Back again"}]}
        with mock.patch.object(indexer.client, "flat_listing", return_value=listing), \
             mock.patch.object(indexer.client, "video_details", return_value=details):
            indexer.index_channel(self.db, ch)
        self.db.refresh(video)
        self.assertIsNone(video.removed_at)
        self.assertIsNone(video.skip_reason)


if __name__ == "__main__":
    unittest.main()

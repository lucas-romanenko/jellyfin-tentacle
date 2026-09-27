"""Tentacle's YouTube traffic must not look like a bot, for any install.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A household's IP was captcha'd by Google Search. From its DNS log and the code:
every channel's tabs were listed with yt-dlp at the same minute every hour,
every live or upcoming stream got a full page load on each run, failed videos
were read again every run, a whole channel was re-resolved (four at a time)
when one video changed, a stream was looked up again after a fixed 4 h and
after every restart, the master playlist was downloaded again on every probe,
every segment opened a new connection, and a bot check stopped nothing.
"""
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import YouTubeChannel, YouTubeVideo, get_setting, set_setting
from services.youtube import client, feeds, indexer, resolver, traffic
from services.youtube import sync as ysync
from services.youtube.errors import PausedByBotCheck, VideoUnavailable, YouTubeBlocked, classify


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Db(unittest.TestCase):
    """A fresh database that SessionLocal also points at, and clean module state."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        p = mock.patch.object(mdb, "SessionLocal", self.Session)
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        traffic.reset_for_tests()
        feeds.reset_for_tests()
        resolver._cache.clear()
        resolver._failures.clear()
        resolver._unplayable.clear()
        resolver._tracks.clear()
        self.addCleanup(traffic.reset_for_tests)
        self.addCleanup(feeds.reset_for_tests)
        self.addCleanup(resolver._cache.clear)
        self.addCleanup(self.db.close)
        sleep = mock.patch.object(indexer, "DETAIL_SPACING_SECONDS", 0)
        sleep.start()
        self.addCleanup(sleep.stop)

    def channel(self, **kw):
        fields = dict(input_url="u", kind="channel", channel_id="UC" + "c" * 22, title="Chan",
                      slug="chan", enabled=True, include_videos=True, include_streams=False,
                      include_shorts=False, keep_count=3, extra_tags=[])
        fields.update(kw)
        ch = YouTubeChannel(**fields)
        self.db.add(ch)
        self.db.commit()
        self.db.refresh(ch)
        return ch


# ── The pause after a bot check ─────────────────────────────────────────────

class ThePause(_Db):
    def test_a_bot_check_pauses_every_request_and_persists(self):
        with mock.patch.object(traffic.time, "time", return_value=1_000_000):
            until = traffic.record_block("Sign in to confirm you're not a bot")
        self.assertAlmostEqual(until - 1_000_000, 3600, delta=360)
        self.db.expire_all()
        self.assertEqual(str(int(until)), get_setting(self.db, "youtube_pause_until"))
        with mock.patch.object(traffic.time, "time", return_value=1_000_100):
            self.assertTrue(traffic.paused())
            with self.assertRaises(PausedByBotCheck):
                traffic.ensure_allowed()

    def test_blocks_in_a_row_double_the_pause_up_to_a_day(self):
        now, lengths = 1_000_000.0, []
        for _ in range(7):
            with mock.patch.object(traffic.time, "time", return_value=now), \
                 mock.patch.object(traffic, "jitter", lambda s, f=0.2: s):
                until = traffic.record_block("429")
            lengths.append(round((until - now) / 3600))
            now = until + 1              # the pause ended; the next request is blocked again
        self.assertEqual([1, 2, 4, 8, 16, 24, 24], lengths)

    def test_a_request_in_flight_does_not_extend_the_pause(self):
        with mock.patch.object(traffic.time, "time", return_value=1_000_000):
            first = traffic.record_block("429")
        with mock.patch.object(traffic.time, "time", return_value=1_000_500):
            self.assertEqual(first, traffic.record_block("429 again"))
        self.assertEqual(1, traffic.pause_state()["blocks_in_a_row"])

    def test_a_success_after_the_pause_starts_the_next_one_short(self):
        with mock.patch.object(traffic.time, "time", return_value=1_000_000):
            until = traffic.record_block("429")
        with mock.patch.object(traffic.time, "time", return_value=until + 10):
            traffic.note_success()
            again = traffic.record_block("429")
        self.assertAlmostEqual(again - (until + 10), 3600, delta=360)

    def test_the_pause_survives_a_restart(self):
        with mock.patch.object(traffic.time, "time", return_value=1_000_000):
            traffic.record_block("429")
        traffic._pause.update(until=0.0, count=0, reason="", loaded=False)   # a new process
        with mock.patch.object(traffic.time, "time", return_value=1_000_100):
            self.assertTrue(traffic.paused())


class TheClientRespectsThePause(_Db):
    def test_nothing_is_sent_while_paused(self):
        traffic.record_block("429")
        with mock.patch.object(client, "_ydl") as ydl:
            with self.assertRaises(PausedByBotCheck):
                client.extract("https://www.youtube.com/watch?v=aaaaaaaaaaa")
        ydl.assert_not_called()

    def test_a_bot_check_from_yt_dlp_starts_the_pause(self):
        class _Boom:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                raise Exception("ERROR: Sign in to confirm you're not a bot")
        with mock.patch.object(client, "_ydl", return_value=_Boom()):
            with self.assertRaises(YouTubeBlocked):
                client.extract("https://www.youtube.com/watch?v=aaaaaaaaaaa")
        self.assertTrue(traffic.paused())

    def test_an_age_gate_is_not_a_bot_check(self):
        self.assertIsInstance(classify(Exception("Sign in to confirm your age")), VideoUnavailable)
        self.assertIsInstance(classify(Exception("blocked it in your country")), VideoUnavailable)
        self.assertIsInstance(classify(Exception("Our systems have detected unusual traffic")), YouTubeBlocked)


# ── Counting, proxy, jitter ─────────────────────────────────────────────────

class Counting(_Db):
    def test_requests_are_counted_by_purpose_and_host(self):
        traffic.count("https://www.youtube.com/feeds/videos.xml?channel_id=x")
        with traffic.purpose("playback"):
            traffic.count("https://rr3---sn-abc.googlevideo.com/videoplayback?x")
            traffic.count("https://rr3---sn-abc.googlevideo.com/videoplayback?y")
        self.assertEqual({"background": {"youtube.com": 1}, "playback": {"googlevideo": 2}},
                         traffic.counts())

    def test_the_hourly_line_reports_and_starts_a_new_hour(self):
        set_setting(self.db, "youtube_enabled", "true")
        traffic.count("https://www.youtube.com/feeds/videos.xml")
        with self.assertLogs("services.youtube.traffic", level="INFO") as logs:
            logging.disable(logging.NOTSET)
            try:
                line = traffic.hourly_report()
            finally:
                logging.disable(logging.CRITICAL)
        self.assertIn("1 request(s): background 1 (youtube.com 1)", line)
        self.assertEqual(1, len(logs.output))
        self.assertEqual({}, traffic.counts())

    def test_a_429_or_googles_sorry_page_starts_the_pause_but_the_api_does_not(self):
        import httpx
        api = httpx.Response(429, request=httpx.Request("GET", "https://www.googleapis.com/youtube/v3/videos"))
        traffic._on_response(api)
        self.assertFalse(traffic.paused())
        yt = httpx.Response(429, request=httpx.Request("GET", "https://www.youtube.com/feeds/videos.xml"))
        traffic._on_response(yt)
        self.assertTrue(traffic.paused())

    def test_the_proxy_reaches_yt_dlp_ffmpeg_and_the_http_client(self):
        traffic.configure(proxy="gluetun:8888")
        self.assertEqual("http://gluetun:8888", traffic.ydl_options()["proxy"])
        self.assertEqual(["-http_proxy", "http://gluetun:8888"], traffic.ffmpeg_proxy_args())
        first = traffic.http_client()
        self.assertIs(first, traffic.http_client(), "one shared client")
        traffic.configure(proxy="")
        self.assertIsNot(first, traffic.http_client(), "a new proxy builds a new client")
        self.assertEqual([], traffic.ffmpeg_proxy_args())

    def test_only_http_proxies_are_accepted(self):
        for bad in ("socks5://vpn:1080", "http://"):
            with self.assertRaises(ValueError):
                traffic.normalize_proxy(bad)

    def test_intervals_never_go_below_half_an_hour_and_jitter_stays_in_bounds(self):
        self.assertEqual(30, traffic.interval_minutes("7"))
        self.assertEqual(60, traffic.interval_minutes("garbage"))
        self.assertEqual(90, traffic.interval_minutes("90"))
        values = [traffic.jitter(100, 0.2) for _ in range(200)]
        self.assertTrue(all(80 <= v <= 120 for v in values))
        self.assertGreater(len({round(v, 3) for v in values}), 150, "never a fixed beat")


# ── Feeds and the YouTube Data API ──────────────────────────────────────────

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <entry><yt:videoId>{a}</yt:videoId><title>New upload</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v={a}"/><published>2026-09-27T10:00:00+00:00</published></entry>
 <entry><yt:videoId>{b}</yt:videoId><title>A short</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/{b}"/><published>2026-09-26T10:00:00+00:00</published></entry>
</feed>"""


class Feeds(_Db):
    def test_the_feed_gives_ids_newest_first_and_marks_shorts(self):
        entries = feeds.parse_feed(FEED.format(a="a" * 11, b="b" * 11))
        self.assertEqual(["a" * 11, "b" * 11], [e["id"] for e in entries])
        self.assertEqual([False, True], [e["short"] for e in entries])

    def test_channel_and_playlist_feeds(self):
        ch = self.channel()
        self.assertTrue(feeds.feed_url(ch).endswith("?channel_id=UC" + "c" * 22))
        pl = self.channel(kind="playlist", playlist_id="PLx", slug="pl", channel_id="UC" + "o" * 22)
        self.assertTrue(feeds.feed_url(pl).endswith("?playlist_id=PLx"))
        self.assertEqual("UU" + "c" * 22, feeds.uploads_playlist(ch))

    def test_the_api_maps_onto_the_indexers_details(self):
        upcoming = feeds.api_to_details({
            "id": "u" * 11,
            "snippet": {"title": "Soon", "liveBroadcastContent": "upcoming",
                        "publishedAt": "2026-09-27T10:00:00Z", "thumbnails": {"high": {"url": "h"}}},
            "contentDetails": {"duration": "P0D"},
            "status": {"privacyStatus": "public", "madeForKids": False},
            "liveStreamingDetails": {"scheduledStartTime": "2026-09-28T18:00:00Z"}})
        self.assertEqual("is_upcoming", upcoming["live_status"])
        self.assertIsNone(upcoming["availability"])
        from datetime import timezone
        self.assertEqual(int(datetime(2026, 9, 28, 18, tzinfo=timezone.utc).timestamp()),
                         upcoming["release_timestamp"])
        self.assertIs(False, upcoming["is_made_for_kids"])
        ended = feeds.api_to_details({"id": "e" * 11, "snippet": {"liveBroadcastContent": "none"},
                                      "contentDetails": {"duration": "PT1H2M3S"},
                                      "status": {"privacyStatus": "unlisted"},
                                      "liveStreamingDetails": {"actualEndTime": "2026-09-27T12:00:00Z"}})
        self.assertEqual(("was_live", 3723, "unlisted"),
                         (ended["live_status"], ended["duration"], ended["availability"]))

    def test_a_refusing_api_falls_back_to_the_feed_for_a_while(self):
        import httpx
        traffic.configure(api_key="AIza-test")
        ch = self.channel()

        class _Http:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, timeout=None, headers=None):
                self.urls.append(url)
                if "googleapis" in url:
                    return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}},
                                          request=httpx.Request("GET", url))
                return httpx.Response(200, text=FEED.format(a="a" * 11, b="b" * 11),
                                      request=httpx.Request("GET", url))
        http = _Http()
        with mock.patch.object(traffic, "http_client", return_value=http):
            first = feeds.newest_uploads(ch)
            second = feeds.newest_uploads(ch)
        self.assertEqual("a" * 11, first[0]["id"])
        self.assertEqual(1, sum("googleapis" in u for u in http.urls), "the API is not asked again")
        self.assertEqual(second, first)


# ── The scheduled check ─────────────────────────────────────────────────────

class _Indexing(_Db):
    def setUp(self):
        super().setUp()
        self.listings, self.details_read = [], []
        self.tabs = {"videos": [], "streams": []}
        self.details = {}
        self.feed = []

        def listing(url, limit):
            tab = url.rsplit("/", 1)[-1]
            self.listings.append(tab)
            return {"entries": [dict(e) for e in self.tabs.get(tab, [])][:limit]}

        def details(vid):
            self.details_read.append(vid)
            d = self.details.get(vid, {"title": f"Video {vid[0]}", "duration": 600, "availability": "public"})
            if isinstance(d, Exception):
                raise d
            return dict(d)
        for target, attr, fn in ((client, "flat_listing", listing), (client, "video_details", details),
                                 (feeds, "newest_uploads", lambda ch: [dict(e) for e in self.feed])):
            p = mock.patch.object(target, attr, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def listed(self, *ids, status=None):
        return [{"id": i, "title": f"Video {i[0]}", "live_status": status} for i in ids]

    def index(self, ch, light=True):
        self.listings.clear()
        self.details_read.clear()
        return indexer.index_channel(self.db, ch, light=light)


class TheLightCheck(_Indexing):
    def setUp(self):
        super().setUp()
        self.ch = self.channel()
        self.tabs["videos"] = self.listed("a" * 11, "b" * 11, "c" * 11)
        self.feed = [{"id": v, "short": False} for v in ("a" * 11, "b" * 11, "c" * 11)]
        self.index(self.ch)                           # first check: listed in full, feed ids stored

    def test_the_first_check_lists_the_channel_and_remembers_the_feed(self):
        self.assertEqual(["a" * 11, "b" * 11, "c" * 11], self.ch.feed_ids)
        self.assertIsNotNone(self.ch.last_full_check)

    def test_nothing_new_in_the_feed_means_no_listing_and_no_details(self):
        r = self.index(self.ch)
        self.assertTrue(r["light"])
        self.assertEqual([], self.listings)
        self.assertEqual([], self.details_read)

    def test_a_new_upload_in_the_feed_lists_the_channel(self):
        self.feed.insert(0, {"id": "z" * 11, "short": False})
        self.tabs["videos"].insert(0, self.listed("z" * 11)[0])
        r = self.index(self.ch)
        self.assertNotIn("light", r)
        self.assertEqual(["videos"], self.listings)
        self.assertEqual(["z" * 11], self.details_read)
        self.assertEqual("z" * 11, self.ch.feed_ids[0])

    def test_a_short_the_channel_skips_is_not_new(self):
        self.feed.insert(0, {"id": "s" * 11, "short": True})
        self.assertTrue(self.index(self.ch)["light"])

    def test_a_day_old_listing_is_refreshed_anyway(self):
        self.ch.last_full_check = datetime.utcnow() - timedelta(hours=30)
        self.db.commit()
        self.index(self.ch)
        self.assertEqual(["videos"], self.listings)

    def test_no_feed_means_the_tabs_are_listed_as_before(self):
        with mock.patch.object(feeds, "newest_uploads", side_effect=feeds.FeedUnavailable("no feed")):
            self.index(self.ch)
        self.assertEqual(["videos"], self.listings)

    def test_a_refused_feed_backs_the_channel_off(self):
        with mock.patch.object(feeds, "newest_uploads", side_effect=YouTubeBlocked("429")):
            with self.assertRaises(YouTubeBlocked):
                self.index(self.ch)
        self.assertIsNotNone(self.ch.blocked_until)

    def test_nothing_at_all_while_paused(self):
        traffic.record_block("429")
        before = feeds.newest_uploads.call_count
        r = self.index(self.ch)
        self.assertTrue(r["skipped"])
        self.assertEqual([], self.listings)
        self.assertEqual(before, feeds.newest_uploads.call_count, "the feed was read while paused")


class LiveStatus(_Indexing):
    def setUp(self):
        super().setUp()
        self.ch = self.channel(live_enabled=True)
        self.tabs["streams"] = self.listed("l" * 11, status="is_upcoming")
        self.details["l" * 11] = {"title": "Tonight", "live_status": "is_upcoming", "availability": "public"}
        self.feed = [{"id": "l" * 11, "short": False}]
        self.index(self.ch)
        self.video = self.db.query(YouTubeVideo).filter_by(video_id="l" * 11).one()

    def test_going_live_is_read_from_the_listing_without_a_page_load(self):
        self.tabs["streams"] = self.listed("l" * 11, status="is_live")
        self.index(self.ch)
        self.assertEqual(["streams"], self.listings, "only the streams tab, only because one is pending")
        self.assertEqual([], self.details_read)
        self.assertEqual("is_live", self.video.live_status)

    def test_a_live_channel_peeks_at_its_streams_and_lists_itself_for_a_new_broadcast(self):
        self.tabs["streams"] = self.listed("n" * 11, status="is_upcoming") + self.listed("l" * 11, status="is_upcoming")
        self.details["n" * 11] = {"title": "Tomorrow", "live_status": "is_upcoming", "availability": "public"}
        self.index(self.ch)
        self.assertEqual(["streams", "videos", "streams"], self.listings,
                         "the peek found a new broadcast the feed did not show, so the tabs were listed")
        self.assertTrue(self.db.query(YouTubeVideo).filter_by(video_id="n" * 11).one())

    def test_an_ended_stream_is_read_once_for_its_final_length(self):
        self.tabs["streams"] = self.listed("l" * 11, status="was_live")
        self.details["l" * 11] = {"title": "Tonight", "live_status": "was_live", "duration": 5400,
                                  "availability": "public"}
        self.index(self.ch)
        self.assertEqual(["l" * 11], self.details_read)
        self.assertEqual(("was_live", 5400), (self.video.live_status, self.video.duration))

    def test_a_stream_missing_from_the_listing_is_read_at_most_every_few_hours(self):
        self.tabs["streams"] = []
        self.index(self.ch)
        self.assertEqual(["l" * 11], self.details_read)
        self.index(self.ch)
        self.assertEqual([], self.details_read, "read again within PENDING_DETAIL_HOURS")

    def test_nothing_pending_still_one_small_peek_and_no_details(self):
        self.video.live_status = "was_live"
        self.db.commit()
        self.tabs["streams"] = self.listed("l" * 11, status="was_live")
        self.index(self.ch)
        self.assertEqual(["streams"], self.listings)
        self.assertEqual([], self.details_read)

    def test_without_live_tv_nothing_is_checked(self):
        self.ch.live_enabled = False
        self.db.commit()
        self.index(self.ch)
        self.assertEqual([], self.listings)
        self.assertEqual([], self.details_read)

    def test_with_an_api_key_the_api_answers_for_pending_streams(self):
        with mock.patch.object(feeds, "api_available", return_value=True), \
             mock.patch.object(feeds, "api_details", return_value={
                 "l" * 11: {"live_status": "is_live", "availability": None}}) as api:
            self.index(self.ch)
        api.assert_called_once()
        self.assertEqual([], self.listings)
        self.assertEqual("is_live", self.video.live_status)


class FailedVideosAreRetriedLater(_Indexing):
    def setUp(self):
        super().setUp()
        self.ch = self.channel()
        self.tabs["videos"] = self.listed("a" * 11, "m" * 11)
        self.details["m" * 11] = VideoUnavailable("members-only content")

    def test_a_failed_video_is_recorded_and_not_read_on_the_next_run(self):
        self.index(self.ch, light=False)
        self.assertIn("m" * 11, self.details_read)
        row = self.db.query(YouTubeVideo).filter_by(video_id="m" * 11).one()
        self.assertEqual(indexer.UNAVAILABLE_REASON, row.skip_reason)
        self.assertIsNotNone(row.removed_at)
        self.assertGreater(row.next_check_at, datetime.utcnow() + timedelta(hours=5))
        self.index(self.ch, light=False)
        self.assertNotIn("m" * 11, self.details_read)

    def test_once_due_it_is_read_again_and_can_join_the_library(self):
        self.index(self.ch, light=False)
        row = self.db.query(YouTubeVideo).filter_by(video_id="m" * 11).one()
        row.next_check_at = datetime.utcnow() - timedelta(minutes=1)
        self.db.commit()
        self.details["m" * 11] = {"title": "Now public", "duration": 600, "availability": "public"}
        self.index(self.ch, light=False)
        self.assertEqual(["m" * 11], self.details_read)
        self.db.refresh(row)
        self.assertIsNone(row.removed_at)
        self.assertEqual("Now public", row.title)

    def test_the_wait_doubles(self):
        self.index(self.ch, light=False)
        row = self.db.query(YouTubeVideo).filter_by(video_id="m" * 11).one()
        first = row.next_check_at - datetime.utcnow()
        row.next_check_at = datetime.utcnow() - timedelta(minutes=1)
        self.db.commit()
        self.index(self.ch, light=False)
        self.db.refresh(row)
        second = row.next_check_at - datetime.utcnow()
        self.assertAlmostEqual(second / first, 2, delta=0.5)


# ── Scheduling ──────────────────────────────────────────────────────────────

class Scheduling(_Db):
    def test_the_job_has_jitter_and_a_random_first_run(self):
        import main
        set_setting(self.db, "youtube_index_interval_minutes", "60")
        with mock.patch.object(main, "SessionLocal", self.Session):
            self.assertTrue(main.reschedule_youtube_index())
        job = main.scheduler.get_job("youtube_index")
        self.addCleanup(lambda: main.scheduler.remove_job("youtube_index"))
        self.assertEqual(timedelta(minutes=60), job.trigger.interval)
        self.assertEqual(720, job.trigger.jitter)
        delay = (job.trigger.start_date.replace(tzinfo=None) - datetime.now()).total_seconds()
        self.assertTrue(4 * 60 <= delay <= 60 * 60, delay)

    def test_background_checks_off_removes_the_job(self):
        import main
        set_setting(self.db, "youtube_background_checks", "false")
        with mock.patch.object(main, "SessionLocal", self.Session):
            self.assertFalse(main.reschedule_youtube_index())
        self.assertIsNone(main.scheduler.get_job("youtube_index"))

    def test_a_scheduled_run_checks_each_channel_lightly_with_random_gaps(self):
        set_setting(self.db, "youtube_enabled", "true")
        set_setting(self.db, "youtube_base_url", "http://t:8888")
        for n in range(3):
            self.channel(slug=f"c{n}", channel_id="UC" + str(n) * 22)
        calls, gaps = [], []
        with mock.patch.object(ysync, "sync_channel",
                               side_effect=lambda db, ch, base, light=False: calls.append(light) or {}), \
             mock.patch.object(ysync, "reconcile_playlists", return_value=0), \
             mock.patch.object(ysync.time, "sleep", side_effect=gaps.append):
            ysync.run_youtube_sync()
        self.assertEqual([True, True, True], calls)
        self.assertEqual(2, len(gaps))
        self.assertTrue(all(ysync.CHANNEL_GAP_SECONDS[0] <= g <= ysync.CHANNEL_GAP_SECONDS[1] for g in gaps))

    def test_off_or_paused_means_no_run(self):
        set_setting(self.db, "youtube_enabled", "true")
        set_setting(self.db, "youtube_background_checks", "false")
        self.channel()
        with mock.patch.object(ysync, "sync_channel") as sync:
            self.assertFalse(ysync.run_youtube_sync()["background"])
            set_setting(self.db, "youtube_background_checks", "true")
            traffic.record_block("429")
            self.assertTrue(ysync.run_youtube_sync()["paused"])
        sync.assert_not_called()

    def test_only_new_videos_are_warmed_one_at_a_time(self):
        ch = self.channel()
        old = datetime.utcnow() - timedelta(days=2)
        for i in range(15):
            self.db.add(YouTubeVideo(channel_fk=ch.id, video_id=f"{i:011d}", title=str(i),
                                     live_status="not_live", first_seen=old if i < 3 else datetime.utcnow()))
        self.db.commit()
        resolved, gaps = [], []
        with mock.patch.object(resolver, "resolve", side_effect=lambda vid, h=1080: resolved.append(vid)), \
             mock.patch.object(resolver, "is_cached", return_value=False), \
             mock.patch.object(ysync.time, "sleep", side_effect=gaps.append):
            ysync._warm_streams(self.db, [ch])
        self.assertEqual(ysync.WARM_MAX, len(resolved))
        self.assertFalse(any(v in resolved for v in ("00000000000", "00000000001", "00000000002")))
        self.assertEqual(ysync.WARM_MAX - 1, len(gaps))


# ── Finding and playing streams ─────────────────────────────────────────────

MASTER = "https://manifest.googlevideo.com/api/manifest/hls_variant/expire/{exp}/ei/x/file/index.m3u8"


class _FakeHttp:
    def __init__(self, status=200):
        self.status, self.gets = status, []

    def get(self, url, headers=None, timeout=None):
        import httpx
        self.gets.append(url)
        return httpx.Response(self.status, text="#EXTM3U\n", request=httpx.Request("GET", url))


class Resolving(_Db):
    def _extracted(self, exp):
        return {"formats": [{"protocol": "m3u8_native", "manifest_url": MASTER.format(exp=exp),
                             "height": 1080, "http_headers": {}}], "duration": 60}

    def test_a_resolve_lives_until_shortly_before_googles_expiry(self):
        import time
        exp = int(time.time()) + 5 * 3600
        with mock.patch.object(client, "extract", return_value=self._extracted(exp)):
            r = resolver.resolve("a" * 11)
        self.assertAlmostEqual(r.expires_at, exp - resolver.EXPIRY_MARGIN_SECONDS, delta=2)
        self.assertEqual(exp, resolver.url_expiry("https://x/videoplayback?expire=%d&x=1" % exp))

    def test_the_master_is_fetched_once_and_reused(self):
        import time
        http = _FakeHttp()
        with mock.patch.object(client, "extract", return_value=self._extracted(int(time.time()) + 6 * 3600)) as ex, \
             mock.patch.object(traffic, "http_client", return_value=http):
            resolver.master_text("a" * 11)
            resolver.master_text("a" * 11)
        self.assertEqual(1, ex.call_count)
        self.assertEqual(1, len(http.gets), "the master was downloaded again")

    def test_a_cached_stream_google_no_longer_honours_is_resolved_again_once(self):
        import time
        exp = int(time.time()) + 6 * 3600
        with mock.patch.object(client, "extract", return_value=self._extracted(exp)) as ex, \
             mock.patch.object(traffic, "http_client", side_effect=[_FakeHttp(403), _FakeHttp(200)]):
            resolver.master_text("a" * 11)
        self.assertEqual(2, ex.call_count)

    def test_while_paused_only_cached_streams_play(self):
        import time
        with mock.patch.object(client, "extract", return_value=self._extracted(int(time.time()) + 6 * 3600)):
            resolver.resolve("a" * 11)
        traffic.record_block("429")
        with mock.patch.object(client, "_ydl") as ydl:
            self.assertTrue(resolver.resolve("a" * 11))
            with self.assertRaises(PausedByBotCheck):
                resolver.resolve("b" * 11)
        ydl.assert_not_called()

    def test_a_bot_check_does_not_try_the_second_client(self):
        with mock.patch.object(client, "extract", side_effect=YouTubeBlocked("bot")) as ex:
            with self.assertRaises(YouTubeBlocked):
                resolver.resolve("a" * 11)
        self.assertEqual(1, ex.call_count)

    def test_resolves_survive_a_restart(self):
        import time
        with mock.patch.object(client, "extract", return_value=self._extracted(int(time.time()) + 6 * 3600)):
            resolver.resolve("a" * 11)
        saved = json.loads((resolver._persist_path()).read_text())
        self.assertIn("a" * 11, saved)
        resolver._cache.clear()
        resolver._persist_loaded = False                  # a new process
        self.assertTrue(resolver.is_cached("a" * 11))

    def test_live_tracks_are_reused_across_tunes(self):
        formats = {"formats": [{"protocol": "m3u8_native", "url": "https://x/v.m3u8", "height": 720,
                                "vcodec": "avc1"}]}
        with mock.patch.object(client, "extract", return_value=formats) as ex:
            resolver.pick_tracks("l" * 11, 1080)
            resolver.pick_tracks("l" * 11, 1080)
            resolver.forget_tracks("l" * 11)
            resolver.pick_tracks("l" * 11, 1080)
        self.assertEqual(2, ex.call_count)

    def test_a_video_that_twice_cannot_be_played_is_retired(self):
        ch = self.channel()
        video = YouTubeVideo(channel_fk=ch.id, video_id="p" * 11, title="Gone private", live_status="not_live")
        self.db.add(video)
        self.db.commit()
        for _ in range(2):
            with mock.patch.object(client, "extract", side_effect=VideoUnavailable("Private video")):
                with self.assertRaises(VideoUnavailable):
                    resolver.resolve("p" * 11)
                resolver._failures.clear()          # past its back-off
        self.assertEqual(1, ysync._retire_unplayable(self.db, ch))
        self.db.refresh(video)
        self.assertIsNotNone(video.removed_at)


class Routes(_Db):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import youtube as youtube_router
        from routers.auth import require_admin
        self.ch = self.channel()
        self.db.add(YouTubeVideo(channel_fk=self.ch.id, video_id="a" * 11, title="A", live_status="not_live"))
        self.db.commit()
        app = FastAPI()
        app.include_router(youtube_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[require_admin] = lambda: None
        self.client = TestClient(app)

    def test_a_head_probe_never_asks_youtube(self):
        with mock.patch.object(resolver, "resolve") as res, mock.patch.object(resolver, "master_text") as mt:
            r = self.client.head(f"/api/youtube/v/{'a' * 11}/master.m3u8")
        self.assertEqual(200, r.status_code)
        res.assert_not_called()
        mt.assert_not_called()

    def test_the_settings_save_masks_the_key_and_refuses_a_bad_proxy(self):
        import main
        with mock.patch.object(main, "reschedule_youtube_index") as resched, \
             mock.patch.object(feeds, "check_api_key", return_value=(True, "The key works")):
            r = self.client.post("/api/youtube/traffic", json={
                "background_checks": True, "interval_minutes": 10,
                "api_key": "AIzaSyExampleKey1234", "proxy": "http://gluetun:8888"})
            self.assertEqual(200, r.status_code, r.text)
            body = r.json()
            self.assertEqual(30, body["interval_minutes"])
            self.assertEqual("AIza...1234", body["api_key"])
            self.assertEqual({"ok": True, "detail": "The key works"}, body["api_check"])
            resched.assert_called_once()
            # Saving the masked value keeps the key.
            self.client.post("/api/youtube/traffic", json={
                "background_checks": False, "interval_minutes": 120,
                "api_key": body["api_key"], "proxy": "http://gluetun:8888"})
            self.db.expire_all()
            self.assertEqual("AIzaSyExampleKey1234", get_setting(self.db, "youtube_api_key"))
            self.assertEqual("false", get_setting(self.db, "youtube_background_checks"))
            bad = self.client.post("/api/youtube/traffic", json={"proxy": "socks5://vpn:1080"})
        self.assertEqual(400, bad.status_code)

    def test_a_new_proxy_ends_the_pause(self):
        import main
        traffic.record_block("429")
        with mock.patch.object(main, "reschedule_youtube_index"):
            self.client.post("/api/youtube/traffic", json={"proxy": "http://gluetun:8888"})
        self.assertFalse(traffic.paused())


if __name__ == "__main__":
    unittest.main()

"""#122: a client can ask for a lower height; #120 (5): unplayable videos back off.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import models.database as mdb
import routers.youtube as youtube_router
from services.youtube import resolver
from services.youtube.errors import (
    VideoUnavailable, YouTubeBlocked, YouTubeError, YouTubeUnavailable,
)

LADDER = """#EXTM3U
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:URI="https://r1.googlevideo.com/audio.m3u8",TYPE=AUDIO,GROUP-ID="233",NAME="Default"
#EXT-X-STREAM-INF:BANDWIDTH=352312,CODECS="avc1.4D4015",RESOLUTION=426x240,AUDIO="233"
https://r1.googlevideo.com/v240.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1100000,CODECS="avc1.4D401E",RESOLUTION=854x480,AUDIO="233"
https://r1.googlevideo.com/v480.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2600000,CODECS="avc1.4D401F",RESOLUTION=1280x720,AUDIO="233"
https://r1.googlevideo.com/v720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5200000,CODECS="avc1.640028",RESOLUTION=1920x1080,AUDIO="233"
https://r1.googlevideo.com/v1080.m3u8
"""

VID = "EXhAoxKXBcE"


def _heights(body: str) -> list:
    return [int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", body)]


class _FakeHttp:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        r = mock.Mock()
        r.text = LADDER
        r.raise_for_status = lambda: None
        return r


class _RouteBase(unittest.TestCase):
    cap = 1080

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models.database import YouTubeChannel, YouTubeVideo
        tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        ch = YouTubeChannel(input_url="https://www.youtube.com/@ch", kind="channel",
                            channel_id="UC" + "z" * 22, title="Chan", slug="chan",
                            enabled=True, max_height=self.cap, extra_tags=[])
        self.db.add(ch)
        self.db.commit()
        self.db.add(YouTubeVideo(channel_fk=ch.id, video_id=VID, title="A video",
                                 live_status="not_live", media_type="video"))
        self.db.commit()

        app = FastAPI()
        app.include_router(youtube_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        self.client = TestClient(app)
        resolver._cache.clear()
        resolver._failures.clear()
        self.addCleanup(resolver._cache.clear)
        self.addCleanup(resolver._failures.clear)

    def tearDown(self):
        self.db.close()

    def get(self, query=""):
        fake = resolver.ResolvedVideo(VID, "https://r1.googlevideo.com/master.m3u8", {})
        with mock.patch.object(youtube_router.resolver, "resolve", return_value=fake) as res, \
             mock.patch.object(youtube_router.httpx, "Client", _FakeHttp):
            r = self.client.get(f"/api/youtube/v/{VID}/master.m3u8{query}")
        self.resolve_calls = res.call_args_list
        return r


class TestRequestedHeight(_RouteBase):
    def test_no_parameter_serves_the_channel_setting(self):
        r = self.get()
        self.assertEqual(200, r.status_code)
        self.assertEqual([1080], _heights(r.text))

    def test_a_lower_height_is_honoured(self):
        self.assertEqual([720], _heights(self.get("?h=720").text))
        self.assertEqual([480], _heights(self.get("?h=480").text))

    def test_an_in_between_height_takes_the_next_rung_down(self):
        self.assertEqual([480], _heights(self.get("?h=600").text))

    def test_always_exactly_one_variant(self):
        for q in ("", "?h=720", "?h=480", "?h=240"):
            body = self.get(q).text
            self.assertEqual(1, body.count("#EXT-X-STREAM-INF"), q)
            self.assertEqual(1, body.count("TYPE=AUDIO"), q)

    def test_garbage_is_rejected(self):
        for q in ("?h=0", "?h=-5", "?h=abc", "?h=99999"):
            self.assertEqual(422, self.get(q).status_code, q)


class TestHeightIsClampedToTheChannelCap(_RouteBase):
    cap = 720

    def test_asking_above_the_cap_gets_the_cap(self):
        self.assertEqual([720], _heights(self.get("?h=2160").text))
        self.assertEqual([720], _heights(self.get("?h=1080").text))

    def test_below_the_cap_still_works(self):
        self.assertEqual([480], _heights(self.get("?h=480").text))

    def test_the_resolver_sees_the_effective_height(self):
        self.get("?h=2160")
        self.assertEqual(720, self.resolve_calls[0].args[1])


class TestEffectiveHeight(unittest.TestCase):
    def test_table(self):
        f = youtube_router.effective_height
        self.assertEqual(1080, f(1080, None))
        self.assertEqual(720, f(1080, 720))
        self.assertEqual(720, f(720, 1080))
        self.assertEqual(1080, f(None, None))
        self.assertEqual(480, f(None, 480))


# ── Failure back-off ────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class TestResolverBackoff(unittest.TestCase):
    def setUp(self):
        resolver._cache.clear()
        resolver._failures.clear()
        self.addCleanup(resolver._cache.clear)
        self.addCleanup(resolver._failures.clear)
        self.clock = _Clock()
        p = mock.patch.object(resolver.time, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def _extract_raising(self, exc):
        return mock.patch.object(resolver, "_extract", side_effect=exc)

    def test_a_failed_video_is_not_extracted_again_straight_away(self):
        with self._extract_raising(YouTubeError("web_embedded returned no usable HLS")) as ex:
            with self.assertRaises(YouTubeError):
                resolver.resolve(VID)
            for _ in range(6):   # Jellyfin re-probing
                with self.assertRaises(resolver.ResolveBackoff):
                    resolver.resolve(VID)
        self.assertEqual(1, ex.call_count)

    def test_the_back_off_doubles_and_is_capped(self):
        delays = []
        with self._extract_raising(VideoUnavailable("members-only")):
            for _ in range(8):
                with self.assertRaises(YouTubeError):
                    resolver.resolve(VID)
                retry_at = resolver._failures[VID][0]
                delays.append(int(retry_at - self.clock.now))
                self.clock.now = retry_at + 1
        self.assertEqual([600, 1200, 2400, 4800, 9600, 19200, 21600, 21600], delays)

    def test_after_the_back_off_it_tries_again(self):
        with self._extract_raising(YouTubeError("no HLS")) as ex:
            with self.assertRaises(YouTubeError):
                resolver.resolve(VID)
            self.clock.now += resolver.FAILURE_BACKOFF_START + 1
            with self.assertRaises(YouTubeError) as ctx:
                resolver.resolve(VID)
            self.assertNotIsInstance(ctx.exception, resolver.ResolveBackoff)
        self.assertEqual(2, ex.call_count)

    def test_a_network_hiccup_backs_off_only_briefly(self):
        with self._extract_raising(YouTubeUnavailable("timed out")):
            with self.assertRaises(YouTubeError):
                resolver.resolve(VID)
        self.assertEqual(resolver.TRANSIENT_BACKOFF,
                         int(resolver._failures[VID][0] - self.clock.now))

    def test_a_bot_check_is_not_pinned_on_the_video(self):
        with self._extract_raising(YouTubeBlocked("sign in to confirm")):
            with self.assertRaises(YouTubeBlocked):
                resolver.resolve(VID)
        self.assertNotIn(VID, resolver._failures)

    def test_success_clears_the_record(self):
        good = resolver.ResolvedVideo(VID, "https://m", {})
        with self._extract_raising(YouTubeError("no HLS")):
            with self.assertRaises(YouTubeError):
                resolver.resolve(VID)
        self.clock.now += resolver.FAILURE_BACKOFF_START + 1
        with mock.patch.object(resolver, "_extract", return_value=good):
            self.assertIs(good, resolver.resolve(VID))
        self.assertNotIn(VID, resolver._failures)

    def test_force_and_live_skip_the_back_off(self):
        good = resolver.ResolvedVideo(VID, "https://m", {})
        with self._extract_raising(YouTubeError("no HLS")):
            with self.assertRaises(YouTubeError):
                resolver.resolve(VID)
        with mock.patch.object(resolver, "_extract", return_value=good):
            self.assertIs(good, resolver.resolve(VID, backoff=False))
        resolver._cache.clear()
        resolver._failures[VID] = (self.clock.now + 999, 1, "x")
        with mock.patch.object(resolver, "_extract", return_value=good):
            self.assertIs(good, resolver.resolve(VID, force=True))

    def test_live_failures_are_not_recorded(self):
        with self._extract_raising(YouTubeError("not started")):
            with self.assertRaises(YouTubeError):
                resolver.resolve(VID, backoff=False)
        self.assertNotIn(VID, resolver._failures)

    def test_clear_and_count(self):
        resolver._failures[VID] = (self.clock.now + 60, 1, "x")
        resolver._failures["expired0000"] = (self.clock.now - 60, 1, "x")
        self.assertEqual(1, resolver.failure_count())
        self.assertEqual(2, resolver.clear_failures())
        self.assertEqual(0, resolver.failure_count())


class TestRouteDuringBackoff(_RouteBase):
    def test_master_is_a_502_with_retry_after_and_youtube_is_asked_once(self):
        with mock.patch.object(resolver, "_extract",
                               side_effect=YouTubeError("no usable HLS")) as ex:
            codes = [self.client.get(f"/api/youtube/v/{VID}/master.m3u8") for _ in range(7)]
        self.assertEqual([502] * 7, [r.status_code for r in codes])
        self.assertEqual(1, ex.call_count)
        self.assertIn("retry-after", {k.lower() for k in codes[-1].headers})

    def test_a_segment_request_is_a_502_not_a_500(self):
        from services.youtube import playlist
        token = playlist.register("https://r1.googlevideo.com/seg0")
        resolver._failures[VID] = (resolver.time.time() + 600, 1, "no HLS")
        r = self.client.get(f"/api/youtube/v/{VID}/r/{token}.ts")
        self.assertEqual(502, r.status_code)


if __name__ == "__main__":
    unittest.main()

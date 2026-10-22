"""One person watching a provider title is one provider connection, and
none once they stop.

Found on the system this was written for (2026-10-22): a film with embedded
subtitles played in Jellyfin Web. The browser direct-played the file from its
own address while Jellyfin's ffmpeg extracted the subtitles from Jellyfin's:
two playbacks of one title, two provider connections on an account that
allows one. The extraction reads the whole file (2.3 GB), and Jellyfin does
not stop it when the player stops: it went on pulling for minutes with
nobody watching, while a live channel someone opened next to it failed.

So the relay asks Jellyfin's sessions who is watching which title: a second
client of a title one person watches joins that playback (one connection,
time-shared), and a playback whose viewer has gone is closed within seconds.
A playback no session has reported yet, and everything while Jellyfin can't
say, is left alone.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import unittest
from unittest import mock

import httpx

from test_vod_proxy import _Base, _Client, _RangeFake, _request


def _endless(url):
    class _Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b"X" * 10
                await asyncio.sleep(0.001)

        async def aclose(self):
            pass
    return httpx.Response(206, headers={"content-type": "video/x-matroska", "accept-ranges": "bytes",
                                        "content-range": "bytes 0-999999999/1000000000",
                                        "content-length": "1000000000"},
                          stream=_Endless(), request=httpx.Request("GET", url))


class _ViewersBase(_Base):
    def setUp(self):
        super().setUp()
        from models.database import set_setting
        set_setting(self.db, "jellyfin_url", "http://jellyfin.test:8096")
        set_setting(self.db, "jellyfin_api_key", "k" * 32)
        self.db.commit()
        self.title = f"movie/{self.token_file}"
        self.watching = {}            # what "Jellyfin" says: title -> [session addresses]; None = can't say
        self.asked = 0
        vod = self.vod
        vod._viewer_watch = None
        vod._viewers.update(titles=None, at=0.0)

        def answer(url, key):
            self.asked += 1
            return None if self.watching is None else dict(self.watching)
        for patch in (mock.patch.object(vod, "_jellyfin_watched_titles", answer),
                      mock.patch.object(vod, "VIEWER_CHECK_SECONDS", 0.01),
                      mock.patch.object(vod, "VIEWER_GONE_SECONDS", 0.05)):
            patch.start()
            self.addCleanup(patch.stop)

    async def _open(self, client, range_header="bytes=0-"):
        return await self.vod.vod_stream("movie", self.token_file, _request(range_header, client=client), self.db)

    async def _until(self, check, seconds=2.0):
        for _ in range(int(seconds / 0.01)):
            if check():
                return True
            await asyncio.sleep(0.01)
        return False

    def _endless_run(self, scenario):
        script = {self.upstream: [_endless(self.upstream)]}

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                return await scenario()
        return asyncio.run(go())


class ClosedWhenTheViewerHasGone(_ViewersBase):
    def test_a_subtitle_extraction_still_reading_after_the_player_stopped_is_closed(self):
        self.watching = {self.title: ["10.0.0.9"]}

        async def scenario():
            extraction = await self._open("172.19.0.1")
            it = extraction.body_iterator.__aiter__()
            await it.__anext__()
            pb = next(iter(self.vod._playbacks.values()))
            self.assertTrue(await self._until(lambda: pb.watched_at is not None), "Jellyfin was asked")
            self.watching = {}                         # the player stopped
            got = 0
            try:
                while True:
                    await asyncio.wait_for(it.__anext__(), 2.0)
                    got += 1
            except StopAsyncIteration:
                pass
            return pb
        pb = self._endless_run(scenario)
        self.assertTrue(pb.stopped.is_set())
        self.assertEqual({}, self.vod._playbacks)
        self.assertEqual(0, self.livetv._stream_slots.active, "its connection slot is free")

    def test_a_viewer_who_paused_keeps_the_playback(self):
        """A paused session still lists the title it plays."""
        self.watching = {self.title: ["10.0.0.9"]}

        async def scenario():
            playing = await self._open("10.0.0.9")
            it = playing.body_iterator.__aiter__()
            await it.__anext__()
            before = self.asked
            self.assertTrue(await self._until(lambda: self.asked >= before + 20))
            await it.__anext__()
            open_now = len(self.vod._playbacks)
            await it.aclose()
            return open_now
        self.assertEqual(1, self._endless_run(scenario))

    def test_a_playback_no_session_has_reported_yet_is_left_alone(self):
        """The player reports playing only after Jellyfin probed the title and
        started it; a client that never reports is still a person."""
        self.watching = {}

        async def scenario():
            playing = await self._open("10.0.0.9")
            it = playing.body_iterator.__aiter__()
            await it.__anext__()
            before = self.asked
            self.assertTrue(await self._until(lambda: self.asked >= before + 20))
            await it.__anext__()
            open_now = len(self.vod._playbacks)
            await it.aclose()
            return open_now
        self.assertEqual(1, self._endless_run(scenario))

    def test_while_jellyfin_cannot_say_nothing_is_closed(self):
        self.watching = {self.title: ["10.0.0.9"]}

        async def scenario():
            playing = await self._open("10.0.0.9")
            it = playing.body_iterator.__aiter__()
            await it.__anext__()
            pb = next(iter(self.vod._playbacks.values()))
            self.assertTrue(await self._until(lambda: pb.watched_at is not None))
            self.watching = None                       # Jellyfin restarting
            before = self.asked
            self.assertTrue(await self._until(lambda: self.asked >= before + 20))
            await it.__anext__()
            stopped = pb.stopped.is_set()
            await it.aclose()
            return stopped
        self.assertFalse(self._endless_run(scenario))

    def test_without_a_jellyfin_connection_jellyfin_is_never_asked(self):
        from models.database import set_setting
        set_setting(self.db, "jellyfin_api_key", "")
        self.db.commit()

        async def scenario():
            playing = await self._open("10.0.0.9")
            it = playing.body_iterator.__aiter__()
            await it.__anext__()
            await asyncio.sleep(0.1)
            await it.__anext__()
            await it.aclose()
        self._endless_run(scenario)
        self.assertEqual(0, self.asked)


class OnePersonOneConnection(_ViewersBase):
    DATA = bytes(range(256)) * 4

    def _run(self, scenario):
        fake = _RangeFake(self.DATA, self.log)
        self.fake = fake

        async def go():
            with mock.patch("httpx.AsyncClient", fake), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch.object(self.vod, "QUANTUM_SECONDS", 0.02), \
                    mock.patch.object(self.vod, "PREEMPT_GRACE_SECONDS", 0.02):
                return await scenario()
        return asyncio.run(go())

    async def _read(self, resp):
        return b"".join([c async for c in resp.body_iterator])

    async def _browser_then_extraction(self):
        browser = await self._open("10.0.0.9")          # Jellyfin Web direct-plays the file itself
        it = browser.body_iterator.__aiter__()
        first = await it.__anext__()
        self.assertTrue(await self._until(lambda: self.vod._viewers["titles"] is not None))
        extraction = await self._open("172.19.0.1")     # Jellyfin's ffmpeg, from Jellyfin's address
        rest = b"".join([c async for c in it])
        return first + rest, await self._read(extraction)

    def test_the_browser_and_jellyfins_subtitle_extraction_share_one_connection(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_max_concurrent_streams", "2")
        self.db.commit()
        self.watching = {self.title: ["10.0.0.9"]}
        a, b = self._run(self._browser_then_extraction)
        self.assertEqual(self.DATA, a)
        self.assertEqual(self.DATA, b)
        self.assertEqual(1, self.fake.most_open, "never two provider connections for one viewer")
        self.assertEqual(1, len(self.vod._playbacks))
        self.assertEqual(1, self.livetv._stream_slots.active, "one viewer, one slot")

    def test_before_the_player_reports_the_extraction_still_joins(self):
        """Jellyfin lists the session a few seconds after the first bytes."""
        from models.database import set_setting
        set_setting(self.db, "livetv_max_concurrent_streams", "2")
        self.db.commit()
        self.watching = {}
        a, b = self._run(self._browser_then_extraction)
        self.assertEqual((self.DATA, self.DATA), (a, b))
        self.assertEqual(1, self.fake.most_open)
        self.assertEqual(1, self.livetv._stream_slots.active)

    def test_two_people_on_the_same_title_keep_two_playbacks(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_max_concurrent_streams", "2")
        self.db.commit()
        self.watching = {self.title: ["10.0.0.9", "10.0.0.6"]}
        self._run(self._browser_then_extraction)
        self.assertEqual(2, len(self.vod._playbacks))
        self.assertEqual(2, self.livetv._stream_slots.active)

    def test_while_jellyfin_cannot_say_each_client_has_its_own_playback(self):
        from models.database import set_setting
        set_setting(self.db, "livetv_max_concurrent_streams", "2")
        set_setting(self.db, "jellyfin_api_key", "")
        self.db.commit()

        async def scenario():
            a = await self._open("10.0.0.9")
            b = await self._open("172.19.0.1")
            return await asyncio.gather(self._read(a), self._read(b))
        self._run(scenario)
        self.assertEqual(2, len(self.vod._playbacks))

    def test_a_head_from_the_other_client_is_answered_from_the_playback(self):
        self.watching = {self.title: ["10.0.0.9"]}

        async def scenario():
            playing = await self._open("10.0.0.9")
            it = playing.body_iterator.__aiter__()
            await it.__anext__()
            self.assertTrue(await self._until(lambda: self.vod._viewers["titles"] is not None))
            before = len(self.log)
            head = await self.vod.vod_head("movie", self.token_file, _request(method="HEAD", client="172.19.0.1"),
                                           self.db)
            await it.aclose()
            return head, len(self.log) - before
        head, opened = self._run(scenario)
        self.assertEqual(0, opened)
        self.assertEqual(200, head.status_code)


class _PacedRangeFake(_RangeFake):
    """A provider that takes its time (1 ms per 10 bytes), as a real one
    does: a reader that holds the connection holds it for a while."""

    async def send(self, request, stream=False):
        resp = await super().send(request, stream)
        inner = resp.stream

        class _Paced(httpx.AsyncByteStream):
            async def __aiter__(self):
                async for piece in inner:
                    await asyncio.sleep(0.001)
                    yield piece

            async def aclose(self):
                pass
        resp.stream = _Paced()
        return resp


class TheViewerComesFirst(OnePersonOneConnection):
    """Within one shared connection, the player's own requests (from the
    address Jellyfin lists for the session) are never parked for Jellyfin's
    work while they read; that work reads while the player does not. Shared
    in equal turns, the web player stalled 18 s at the start of a film
    while the subtitle extraction had its turns (production, 2026-10-22)."""

    DATA = bytes(range(256)) * 16      # 4 KB: both readers span many quanta

    def _run(self, scenario):
        fake = _PacedRangeFake(self.DATA, self.log)
        self.fake = fake

        async def go():
            with mock.patch("httpx.AsyncClient", fake), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch.object(self.vod, "QUANTUM_SECONDS", 0.02), \
                    mock.patch.object(self.vod, "PREEMPT_GRACE_SECONDS", 0.02):
                return await scenario()
        return asyncio.run(go())

    def test_a_reading_viewer_is_never_parked_for_the_extraction(self):
        self.watching = {self.title: ["10.0.0.9"]}

        async def scenario():
            # its own range end, so its resumes can be told from the extraction's
            browser = await self._open("10.0.0.9", "bytes=0-4094")
            it = browser.body_iterator.__aiter__()
            first = await it.__anext__()
            self.assertTrue(await self._until(lambda: self.vod._viewers["titles"] is not None))
            extraction = await self._open("172.19.0.1")
            ex = asyncio.ensure_future(self._read(extraction))
            rest = b""
            async for c in it:                         # a player's pace: many quanta long
                rest += c
                await asyncio.sleep(0.002)
            return first + rest, await ex
        a, b = self._run(scenario)
        self.assertEqual(self.DATA[:4095], a)
        self.assertEqual(self.DATA, b)
        browser_ranges = [rng for _, rng, _ in self.log if rng and rng.endswith("-4094") and rng != "bytes=0-4094"]
        # the extraction's own open parks it once; it takes the connection back at the extraction's first chunk
        self.assertLessEqual(len(browser_ranges), 1, browser_ranges)
        self.assertEqual(1, self.fake.most_open)

    def test_the_extraction_reads_while_the_viewer_pauses_and_hands_back_when_it_reads_again(self):
        self.watching = {self.title: ["10.0.0.9"]}

        async def scenario():
            browser = await self._open("10.0.0.9")
            it = browser.body_iterator.__aiter__()
            head = await it.__anext__()
            self.assertTrue(await self._until(lambda: self.vod._viewers["titles"] is not None))
            extraction = await self._open("172.19.0.1")
            ex = asyncio.ensure_future(self._read(extraction))
            # the browser's buffer is full: it reads nothing while the extraction finishes
            done = await asyncio.wait_for(ex, 5)
            rest = b"".join([c async for c in it])     # and then the browser reads on
            return head + rest, done
        a, b = self._run(scenario)
        self.assertEqual(self.DATA, a, "the viewer ended early or skipped bytes")
        self.assertEqual(self.DATA, b, "the extraction did not get the idle connection")
        self.assertEqual(1, self.fake.most_open)

    def test_without_a_viewer_address_it_is_equal_turns_as_before(self):
        """A Jellyfin transcode and the extraction both come from Jellyfin's
        address: neither is the player's own, they share in turns."""
        self.watching = {self.title: ["192.0.2.99"]}

        async def scenario():
            transcode = await self._open("172.19.0.1")
            extraction = await self._open("172.19.0.1")
            return await asyncio.gather(self._read(transcode), self._read(extraction))
        a, b = self._run(scenario)
        self.assertEqual((self.DATA, self.DATA), (a, b))
        self.assertGreater(len(self.log), 2, "they took turns")
        self.assertEqual(1, self.fake.most_open)


class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.data


class AskingJellyfin(unittest.TestCase):
    """What a Jellyfin session list says about Tentacle's titles."""

    def setUp(self):
        import routers.vod as vod
        from services import vod_tokens
        self.vod = vod
        vod._item_titles.clear()
        self.addCleanup(vod._item_titles.clear)
        self.movie = vod_tokens.url("http://192.0.2.10:8888", "s" * 64, 1, "movie", 52300, "mkv")
        self.episode = vod_tokens.url("http://192.0.2.10:8888", "s" * 64, 1, "series", 800509, "mp4")
        self.calls = []

    def _jellyfin(self, sessions, items):
        def get(url, params=None, headers=None, timeout=None):
            self.calls.append((url.rsplit("/", 1)[1], dict(params or {})))
            if url.endswith("/Sessions"):
                return _Resp(sessions)
            ids = params["Ids"].split(",")
            return _Resp({"Items": [it for it in items if it["Id"] in ids]})
        return mock.patch("requests.get", get)

    def test_sessions_are_listed_per_title_and_items_are_looked_up_once(self):
        sessions = [{"NowPlayingItem": {"Id": "aa"}, "RemoteEndPoint": "192.0.2.21"},
                    {"NowPlayingItem": {"Id": "aa"}, "RemoteEndPoint": "::ffff:192.0.2.22"},
                    {"NowPlayingItem": {"Id": "bb"}, "RemoteEndPoint": "192.0.2.23"},
                    {"NowPlayingItem": {"Id": "cc"}, "RemoteEndPoint": "192.0.2.24"}, {"DeviceName": "idle"}]
        items = [{"Id": "aa", "MediaSources": [{"Path": self.movie}]},
                 {"Id": "bb", "MediaSources": [{"Path": self.episode}]},
                 {"Id": "cc", "MediaSources": [{"Path": "/media/Movies/Film (2020)/Film (2020).mkv"}]}]
        with self._jellyfin(sessions, items):
            got = self.vod._jellyfin_watched_titles("http://jf:8096", "k")
            again = self.vod._jellyfin_watched_titles("http://jf:8096", "k")
        movie = f"movie/{self.movie.rsplit('/', 1)[1]}"
        episode = f"series/{self.episode.rsplit('/', 1)[1]}"
        self.assertEqual({movie: ["192.0.2.21", "192.0.2.22"], episode: ["192.0.2.23"]}, got)
        self.assertEqual(got, again)
        self.assertEqual(["Sessions", "Items", "Sessions"], [c[0] for c in self.calls])
        self.assertEqual("aa,bb,cc", self.calls[1][1]["Ids"])

    def test_an_error_is_unknown_not_nobody(self):
        def boom(*a, **k):
            raise ConnectionError("refused")
        with mock.patch("requests.get", boom):
            self.assertIsNone(self.vod._jellyfin_watched_titles("http://jf:8096", "k"))
        with mock.patch("requests.get", lambda *a, **k: _Resp({}, 401)):
            self.assertIsNone(self.vod._jellyfin_watched_titles("http://jf:8096", "k"))

    def test_nobody_playing_is_an_empty_answer(self):
        with self._jellyfin([{"DeviceName": "TV"}], []):
            self.assertEqual({}, self.vod._jellyfin_watched_titles("http://jf:8096", "k"))

    def test_not_set_up_is_unknown_and_asks_nothing(self):
        with self._jellyfin([], []):
            self.assertIsNone(self.vod._jellyfin_watched_titles("", ""))
        self.assertEqual([], self.calls)

    def test_the_title_in_a_link_is_the_relays_own_name_for_it(self):
        from services import vod_tokens
        token_file = self.movie.rsplit("/", 1)[1]
        self.assertEqual(f"movie/{token_file}", vod_tokens.title_in_url(self.movie))
        self.assertEqual(f"movie/{token_file}", vod_tokens.title_in_url(self.movie + "?x=1"))
        self.assertIsNone(vod_tokens.title_in_url("http://provider.example/movie/u/p/52300.mkv"))
        self.assertIsNone(vod_tokens.title_in_url(None))


if __name__ == "__main__":
    unittest.main()

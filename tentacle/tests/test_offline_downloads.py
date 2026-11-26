"""Downloads to watch offline (Nov 2026): Jellyfin's Download of a
provider's title sends the video (the plugin's StrmDownloadFilter pulls it
from the relay with the offline header), and Tentacle's Offline page saves
titles on a phone or computer (routers/offline.py, static/js/offline.js).

Run from the tentacle/ directory:  python -m unittest discover -s tests

- A download is its own "download" lease, the lowest rank: it starts only
  on a free connection and never takes one from anyone.
- Someone who wants to watch takes its connection; the download keeps its
  client waiting (nothing sent, the connection open) and carries on from
  the byte it stopped at once a connection is free again.
- A dropped provider connection is resumed from its byte; a client's Range
  (a download that broke off) is passed on.
- A .strm with the provider's own address is downloaded through the relay
  only when it carries that provider's login (POST /api/vod/link).
- The Offline page asks Jellyfin, as the person, whether they may download
  the title (Can download, their libraries and age limit, not a recording
  still recording), and puts back where they stopped watching offline
  unless Jellyfin knows of a later watch.
"""
import asyncio
import unittest
from unittest import mock

import httpx

from test_livetv_raw_reconnect import _Body, _dropped
from test_vod_proxy import PANEL, _Base, _Client, _file, _fresh_db
from starlette.requests import Request


class _Held(httpx.AsyncByteStream):
    """A body that sends its pieces, then waits until it is closed (the relay
    closing the provider connection), then breaks like a closed socket."""

    def __init__(self, pieces):
        self._pieces = pieces
        self.closed = asyncio.Event()

    async def __aiter__(self):
        for piece in self._pieces:
            yield piece
        await self.closed.wait()
        raise httpx.ReadError("connection closed")

    async def aclose(self):
        self.closed.set()


def _download_request(range_header=None, client="10.0.0.9"):
    headers = [(b"host", b"tentacle"), (b"x-tentacle-offline", b"1")]
    if range_header:
        headers.append((b"range", range_header.encode()))

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": "GET", "headers": headers, "path": "/", "query_string": b"",
                    "scheme": "http", "server": ("tentacle", 8888), "client": (client, 1)}, receive)


class _DownloadBase(_Base):
    def setUp(self):
        super().setUp()
        self.vod._downloads.clear()
        self.addCleanup(self.vod._downloads.clear)
        patches = [mock.patch.object(self.vod, "DOWNLOAD_RETRY_SECONDS", 0.05),
                   mock.patch.object(self.livetv, "_SLOT_WAIT_SECONDS", 0.05)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_download(self, script, range_header=None, during=None):
        """Start a download, read it to the end; `during(first_piece)` runs
        after the first piece. Returns (status, headers, body)."""
        vod = self.vod
        real_sleep = asyncio.sleep

        async def fast_sleep(d):
            await real_sleep(0)

        async def go():
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client(script, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)), \
                    mock.patch("asyncio.sleep", fast_sleep):
                resp = await vod.vod_stream("movie", self.token_file, _download_request(range_header), self.db)
                it = resp.body_iterator
                out = b""
                async for piece in it:
                    if not out and during is not None:
                        out += piece
                        await during(piece)
                        continue
                    out += piece
                return resp.status_code, dict(resp.headers), out
        return asyncio.run(go())


class ADownloadIsTheLowestRank(_DownloadBase):
    def test_it_takes_a_free_connection_as_a_download_lease_and_gives_it_back(self):
        seen = {}

        async def during(_):
            leases = list(self.livetv._stream_slots.leases.values())
            seen["kinds"] = [l.kind for l in leases]
            seen["listed"] = [(s["kind"], s["state"]) for s in _snapshot(self.livetv)]
        status, headers, body = self.run_download({self.upstream: [_file(200, [b"AAAA", b"BBBB"])]}, during=during)
        self.assertEqual((200, b"AAAABBBB"), (status, body))
        self.assertEqual(["download"], seen["kinds"])
        self.assertEqual([("download", "streaming")], seen["listed"])
        self.assertEqual("8", headers["content-length"])
        self.assertEqual("bytes", headers["accept-ranges"])
        self.assertEqual({}, self.livetv._stream_slots.leases, "its slot was not given back")
        self.assertEqual([], self.vod._downloads)

    def test_it_never_takes_a_connection_from_someone_watching(self):
        async def go():
            viewer = await self.livetv._stream_slots.acquire_lease(1, 0.1, "vod", "someone watching")
            self.assertIsNotNone(viewer)
            from fastapi import HTTPException
            with mock.patch("httpx.AsyncClient", lambda **kw: _Client({}, self.log, **kw)), \
                    mock.patch("routers.vod.lan_origin_guard", lambda *a, **k: (lambda u: True)):
                with self.assertRaises(HTTPException) as cm:
                    await self.vod.vod_stream("movie", self.token_file, _download_request(), self.db)
            self.assertFalse(viewer.preempted)
            return cm.exception
        e = asyncio.run(go())
        self.assertEqual(503, e.status_code)
        self.assertIn("in use for watching", e.detail)
        self.assertEqual([], self.log, "it opened a provider connection")

    def test_someone_who_wants_to_watch_takes_its_connection_and_it_carries_on_afterwards(self):
        held = _Held([b"AAAA"])
        first = httpx.Response(200, headers={"content-length": "8", "content-type": "video/x-matroska"},
                               stream=held, request=httpx.Request("GET", self.upstream))
        rest = _file(206, [b"BBBB"], start=4, total=8)
        state = {}

        async def during(_):
            slots = self.livetv._stream_slots
            viewer = await slots.acquire_lease(1, 0.5, "vod", "someone watching")
            state["viewer_got_it"] = viewer is not None
            await asyncio.wait_for(held.closed.wait(), 2)
            state["closed_for_viewer"] = True
            # While they watch: the download holds, nothing is asked of the provider
            for _ in range(20):
                await asyncio.sleep(0)
            state["asked_while_watching"] = len(self.log)
            state["listed"] = [(s["kind"], s["state"]) for s in _snapshot(self.livetv)]
            slots.release_lease(viewer)

        status, _, body = self.run_download({self.upstream: [first, rest]}, during=during)
        self.assertTrue(state["viewer_got_it"])
        self.assertTrue(state["closed_for_viewer"])
        self.assertEqual(1, state["asked_while_watching"], "the download pulled while someone watched")
        self.assertIn(("download", "waiting for a free connection"), state["listed"])
        self.assertEqual(b"AAAABBBB", body, "the download did not carry on from where it stopped")
        self.assertEqual("bytes=4-", self.log[-1][2])
        self.assertEqual({}, self.livetv._stream_slots.leases)


    def test_a_finished_viewers_idle_slot_goes_back_to_the_waiting_download(self):
        # A playback keeps its slot for a while after its last request (a seek
        # may follow); a download waiting for one takes it back after
        # DOWNLOAD_RECLAIM_SECONDS, instead of the whole idle window.
        held = _Held([b"AAAA"])
        first = httpx.Response(200, headers={"content-length": "8", "content-type": "video/mp4"},
                               stream=held, request=httpx.Request("GET", self.upstream))
        rest = _file(206, [b"BBBB"], start=4, total=8)
        state = {}

        async def during(_):
            vod = self.vod
            lease = await self.livetv._stream_slots.acquire_lease(1, 0.5, "vod", "vod:movie:1:99")
            pb = vod._Playback("movie/x@10.0.0.5", "vod:movie:1:99", lease, 45.0, "10.0.0.5", "movie/x")
            lease.on_preempt = pb.stop
            vod._playbacks[pb.token] = pb
            await asyncio.wait_for(held.closed.wait(), 2)
            pb.last_used -= vod.DOWNLOAD_RECLAIM_SECONDS + 1     # its film ended a while ago
            state["pb"] = pb

        with mock.patch.object(self.vod, "DOWNLOAD_RECLAIM_SECONDS", 0.2):
            status, _, body = self.run_download({self.upstream: [first, rest]}, during=during)
        self.assertEqual(b"AAAABBBB", body)
        self.assertNotIn(state["pb"].token, self.vod._playbacks, "the idle playback kept its slot")

    def test_a_playback_still_sending_is_never_reclaimed(self):
        async def go():
            vod = self.vod
            lease = await self.livetv._stream_slots.acquire_lease(1, 0.5, "vod", "vod:movie:1:99")
            pb = vod._Playback("movie/x@c", "vod:movie:1:99", lease, 45.0, "c", "movie/x")
            vod._playbacks[pb.token] = pb
            pb.active_bodies = 1                     # a paused player: its request is still open
            pb.last_used -= 3600
            return vod._reclaim_idle_playback(1), pb.token in vod._playbacks
        self.assertEqual((False, True), asyncio.run(go()))


class RangesAndDrops(_DownloadBase):
    def test_a_dropped_provider_connection_is_resumed_from_its_byte(self):
        script = {self.upstream: [_file(200, [b"AAAA"], then=_dropped(), total=8),
                                  _file(206, [b"BBBB"], start=4, total=8)]}
        status, _, body = self.run_download(script)
        self.assertEqual(b"AAAABBBB", body)
        self.assertEqual([None, "bytes=4-"], [r for _, _, r in self.log])

    def test_a_download_that_broke_off_asks_for_the_rest(self):
        script = {self.upstream: [_file(206, [b"BBBB"], start=4, total=8)]}
        status, headers, body = self.run_download(script, range_header="bytes=4-")
        self.assertEqual((206, b"BBBB"), (status, body))
        self.assertEqual("bytes 4-7/8", headers["content-range"])
        self.assertEqual(["bytes=4-"], [r for _, _, r in self.log])

    def test_from_the_start_a_200_goes_out_as_the_206_that_was_asked_for(self):
        status, headers, body = self.run_download({self.upstream: [_file(200, [b"AAAABBBB"])]},
                                                  range_header="bytes=0-")
        self.assertEqual((206, "bytes 0-7/8"), (status, headers["content-range"]))


def _snapshot(livetv):
    """The live streams list (what /api/live/streams shows)."""
    return livetv._stream_snapshot(None)


class DirectProviderLinks(unittest.TestCase):
    def setUp(self):
        self.db = _fresh_db()
        from models.database import Provider, set_setting
        self.provider = Provider(name="P", server_url=PANEL, username="us er", password="p@ss",
                                 provider_type="xtream")
        self.db.add(self.provider)
        set_setting(self.db, "vod_token_secret", "s" * 64)
        self.db.commit()

    def test_a_strm_with_the_providers_own_address_goes_through_the_relay(self):
        from routers.vod import relay_path_for
        from services import vod_tokens
        path = relay_path_for(self.db, f"{PANEL}/movie/us%20er/p%40ss/52300.mkv")
        self.assertEqual(f"/api/vod/movie/{vod_tokens.mint('s' * 64, self.provider.id, 'movie', 52300, 'mkv')}.mkv",
                         path)
        self.assertTrue(path.startswith(f"/api/vod/movie/{self.provider.id}.52300."))
        self.assertTrue(relay_path_for(self.db, f"{PANEL}/series/us%20er/p%40ss/7.mp4").startswith("/api/vod/series/"))

    def test_only_with_that_providers_login(self):
        from routers.vod import relay_path_for
        self.assertIsNone(relay_path_for(self.db, f"{PANEL}/movie/us%20er/wrong/52300.mkv"))
        self.assertIsNone(relay_path_for(self.db, "http://other.test/movie/us%20er/p%40ss/52300.mkv"))
        self.assertIsNone(relay_path_for(self.db, "http://example.test/some/film.mkv"))

    def test_the_route_says_not_found_otherwise(self):
        from fastapi import HTTPException
        from routers.vod import _DirectLink, vod_link
        with self.assertRaises(HTTPException) as cm:
            vod_link(_DirectLink(url="http://example.test/film.mkv"), self.db)
        self.assertEqual(404, cm.exception.status_code)


class _FakeJellyfin:
    url = "http://jf.test"
    headers = {"X-Emby-Token": "k"}

    def __init__(self, policy=None, items=None):
        self.policy = {"EnableContentDownloading": True} if policy is None else policy
        self.items = items if items is not None else {}
        self.posted = []

    def get(self, path, **params):
        if path.startswith("/Users/"):
            return {"Policy": self.policy}
        if path == "/Items":
            self.asked_as = params.get("userId")
            item = self.items.get(params.get("ids"))
            return {"Items": [item] if item else []}
        raise AssertionError(path)

    def post(self, path, body=None, **params):
        self.posted.append((path, body, params))


ITEM = "0123456789abcdef0123456789abcdef"
FILM = {"Id": ITEM, "Name": "Paddington", "Type": "Movie", "MediaType": "Video", "ProductionYear": 2014,
        "CanDownload": True, "RunTimeTicks": 57_000_000_000,
        "MediaSources": [{"Path": "http://tentacle:8888/api/vod/movie/1.2.abc.mkv", "Container": "strm"}],
        "UserData": {"PlaybackPositionTicks": 0, "Played": False}}


class MayThisPersonDownloadIt(unittest.TestCase):
    def setUp(self):
        from services import offline
        offline.forget()
        self.addCleanup(offline.forget)
        self.offline = offline
        from models.database import TentacleUser
        self.user = TentacleUser(id=5, jellyfin_user_id="aa" * 16, display_name="Roman")
        self.db = None

    def check(self, jf, item_id=ITEM):
        with mock.patch.object(self.offline, "jellyfin", lambda db: jf):
            return self.offline.check(self.db, self.user, item_id, fresh=True)

    def refusal(self, jf, item_id=ITEM):
        from services.family import FamilyError
        with self.assertRaises(FamilyError) as cm:
            self.check(jf, item_id)
        return cm.exception

    def test_a_film_they_can_open_and_may_download(self):
        jf = _FakeJellyfin(items={ITEM: FILM})
        item = self.check(jf)
        self.assertEqual("Paddington", item["Name"])
        self.assertEqual("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", jf.asked_as, "Jellyfin was not asked as them")
        s = self.offline.summary(item)
        # An MKV is saved as the MP4 the server makes of it
        self.assertEqual(("movie", "mp4", "mkv", ITEM), (s["kind"], s["container"], s["source_container"], s["id"]))

    def test_can_download_off(self):
        e = self.refusal(_FakeJellyfin(policy={"EnableContentDownloading": False}, items={ITEM: FILM}))
        self.assertEqual(403, e.status)
        self.assertIn("Can save offline", e.message)

    def test_a_title_outside_their_libraries_or_age_limit(self):
        e = self.refusal(_FakeJellyfin(items={}))
        self.assertEqual((404, "That title isn't available to you."), (e.status, e.message))

    def test_a_recording_still_recording(self):
        e = self.refusal(_FakeJellyfin(items={ITEM: dict(FILM, Status="InProgress")}))
        self.assertEqual(409, e.status)

    def test_music_is_not_offered(self):
        e = self.refusal(_FakeJellyfin(items={ITEM: dict(FILM, MediaType="Audio", Type="Audio")}))
        self.assertEqual(400, e.status)

    def test_not_an_item_id(self):
        self.assertEqual(404, self.refusal(_FakeJellyfin(), item_id="../Users").status)

    def test_an_episode_is_named_by_its_show(self):
        ep = dict(FILM, Type="Episode", Name="Pilot", SeriesName="Bluey", ParentIndexNumber=1, IndexNumber=2,
                  MediaSources=[{"Path": "/media/tv/Bluey/S01E02.mp4", "Container": "mp4", "Size": 123_000_000}])
        s = self.offline.summary(ep)
        self.assertEqual(("episode", "Bluey", 1, 2, "mp4", 123_000_000), (s["kind"], s["series"], s["season"],
                                                                  s["episode"], s["container"], s["size"]))


class WatchedOfflineGoesBackToJellyfin(MayThisPersonDownloadIt):
    def sync(self, jf, entries):
        with mock.patch.object(self.offline, "jellyfin", lambda db: jf):
            return self.offline.sync_progress(self.db, self.user, entries)

    def test_where_they_stopped_is_saved_as_them(self):
        jf = _FakeJellyfin(items={ITEM: FILM})
        out = self.sync(jf, [{"id": ITEM, "position_ticks": 12_000_000_000, "played": False, "at": 1764072000000}])
        self.assertEqual(1, out["saved"])
        path, body, params = jf.posted[0]
        self.assertEqual("/UserItems/01234567-89ab-cdef-0123-456789abcdef/UserData", path)
        self.assertEqual({"userId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}, params)
        self.assertEqual((12_000_000_000, False), (body["PlaybackPositionTicks"], body["Played"]))
        self.assertTrue(body["LastPlayedDate"].startswith("2025-11-25T12:00:00"))

    def test_watched_to_the_end(self):
        jf = _FakeJellyfin(items={ITEM: FILM})
        self.sync(jf, [{"id": ITEM, "position_ticks": 56_000_000_000, "played": True, "at": 1764072000000}])
        self.assertEqual((0, True), (jf.posted[0][1]["PlaybackPositionTicks"], jf.posted[0][1]["Played"]))

    def test_a_later_watch_in_jellyfin_wins(self):
        later = dict(FILM, UserData={"PlaybackPositionTicks": 5, "LastPlayedDate": "2025-11-26T09:00:00.0000000Z"})
        jf = _FakeJellyfin(items={ITEM: later})
        out = self.sync(jf, [{"id": ITEM, "position_ticks": 1, "played": False, "at": 1764072000000}])
        self.assertEqual((0, 1), (out["saved"], out["newer_in_jellyfin"]))
        self.assertEqual([], jf.posted)

    def test_a_title_they_can_no_longer_open_is_skipped(self):
        jf = _FakeJellyfin(items={})
        out = self.sync(jf, [{"id": ITEM, "position_ticks": 1, "at": 1764072000000}, {"id": "nope", "at": 1}])
        self.assertEqual((0, 2), (out["saved"], out["skipped"]))


class CanDownloadOnPeople(unittest.TestCase):
    def test_it_is_jellyfins_allow_media_downloading(self):
        from services import person_access as PA
        self.assertTrue(PA.access_of({})["can_download"])
        self.assertFalse(PA.access_of({"EnableContentDownloading": False})["can_download"])
        self.assertEqual(False, PA.changed_policy({"EnableContentDownloading": True}, {"can_download": False},
                                                  "u")["EnableContentDownloading"])


if __name__ == "__main__":
    unittest.main()


# An MP4 a browser plays as it is (Jellyfin looked inside it)
MP4_FILM = dict(FILM, MediaSources=[{"Path": "/media/movies/Paddington.mp4", "Container": "mp4", "Size": 8,
                                     "MediaStreams": [{"Type": "Video", "Codec": "h264"},
                                                      {"Type": "Audio", "Codec": "aac"}]}])


class TheVideoIsJellyfinsDownload(unittest.TestCase):
    """GET /api/offline/items/<id>/video of an MP4 that plays as it is passes
    Jellyfin's Download on, byte ranges and all; a .strm text (a plugin older than the Download hook) is
    refused in words instead of being saved as a film."""

    def setUp(self):
        self.db = _fresh_db()
        from models.database import TentacleUser, set_setting
        set_setting(self.db, "jellyfin_url", "http://jf.test")
        set_setting(self.db, "jellyfin_api_key", "k" * 32)
        self.user = TentacleUser(id=5, jellyfin_user_id="aa" * 16, display_name="Roman")
        self.sent = []

    def call(self, answer, range_header=None):
        from routers import offline as R
        sent = self.sent

        class _JF:
            def __init__(self, **kw):
                pass

            def build_request(self, method, url, headers=None):
                sent.append((method, url, dict(headers or {})))
                return httpx.Request(method, url, headers=headers)

            async def send(self, request, stream=False):
                return answer

            async def aclose(self):
                pass

        headers = [(b"host", b"tentacle")] + ([(b"range", range_header.encode())] if range_header else [])

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        req = Request({"type": "http", "method": "GET", "headers": headers, "path": "/", "query_string": b"",
                       "scheme": "http", "server": ("tentacle", 8888), "client": ("10.0.0.7", 1)}, receive)

        async def go():
            with mock.patch.object(R.offline, "check", lambda db, user, item_id: MP4_FILM), \
                    mock.patch("httpx.AsyncClient", _JF):
                resp = await R.video(ITEM, req, can="", db=self.db, user=self.user)
                body = b"".join([c async for c in resp.body_iterator])
                return resp.status_code, dict(resp.headers), body
        return asyncio.run(go())

    def test_ranges_pass_through(self):
        answer = httpx.Response(206, headers={"content-range": "bytes 4-7/8", "content-length": "4",
                                              "content-type": "video/mp4", "accept-ranges": "bytes"},
                                stream=_Body([b"BBBB"]), request=httpx.Request("GET", "http://jf.test"))
        status, headers, body = self.call(answer, "bytes=4-")
        self.assertEqual((206, b"BBBB", "bytes 4-7/8"), (status, body, headers["content-range"]))
        method, url, sent = self.sent[0]
        self.assertEqual(f"http://jf.test/Items/{ITEM}/Download", url)
        self.assertEqual("bytes=4-", sent["range"])
        self.assertEqual("k" * 32, sent["X-Emby-Token"])

    def test_a_strm_text_is_refused_in_words(self):
        from fastapi import HTTPException
        answer = httpx.Response(200, headers={"content-length": "60", "content-type": "application/octet-stream",
                                              "content-disposition": 'attachment; filename="Coco (2017).strm"'},
                                stream=_Body([b"http://x\n"]), request=httpx.Request("GET", "http://jf.test"))
        with self.assertRaises(HTTPException) as cm:
            self.call(answer)
        self.assertEqual(409, cm.exception.status_code)
        self.assertIn("newer Tentacle plugin", cm.exception.detail)

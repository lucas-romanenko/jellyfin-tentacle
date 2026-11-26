"""Save offline works for every title on every device (Nov 2026, Lucas: "I
need this to work for everything"): what a browser can't play as it is (an
MKV, AC-3 sound on a PC, an old codec) is made into an MP4 by Tentacle as it
sends it (services/offline_convert.py), and a show or a season saves every
episode in one tap (services/offline.py episodes()).

Run from the tentacle/ directory:  python -m unittest discover -s tests

- An MP4 with H.264 and AAC goes as it is; an MKV is repackaged, the
  picture copied when the browser plays it (H.264; HEVC when it says so),
  the sound copied when it is AAC or MP3, else AAC stereo (AC-3 too,
  whatever the browser claims); an old codec's picture becomes H.264 (the GPU when there is one).
- The browser reads the MP4 while it is being made, from any byte; once
  finished it is a normal file with its size, and its first bytes (the size
  of its data, filled in at the end) are what the browser asks for again.
- A making that failed isn't started again at once; nobody reading it for
  an hour stops it; a finished one is deleted hours after its last read.
- A show's or a season's episodes are asked of Jellyfin as the person,
  leaving out missing ones and recordings still recording.
"""
import asyncio
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

from starlette.requests import Request

from tmp_dirs import temp_dir

ITEM = "0123456789abcdef0123456789abcdef"


def _probe(video="h264", audio=("ac3",), fmt="matroska,webm", duration="120.0", default=None):
    streams = [{"index": 0, "codec_type": "video", "codec_name": video, "disposition": {"default": 1}}]
    for i, a in enumerate(audio, start=1):
        streams.append({"index": i, "codec_type": "audio", "codec_name": a,
                        "disposition": {"default": 1 if default == i else 0}})
    return {"streams": streams, "format": {"format_name": fmt, "duration": duration}}


class WhatPlaysAsItIs(unittest.TestCase):
    def setUp(self):
        from services import offline_convert as C
        self.C = C

    def test_an_mp4_with_h264_and_aac_goes_as_it_is(self):
        C = self.C
        self.assertTrue(C.fits("mp4", {"video": "h264", "audio": ["aac"]}, frozenset()))
        self.assertTrue(C.fits("m4v", {"video": "h264", "audio": []}, frozenset()))

    def test_an_mkv_never_does(self):
        self.assertFalse(self.C.fits("mkv", {"video": "h264", "audio": ["aac"]}, frozenset()))
        self.assertFalse(self.C.fits("avi", None, frozenset()))

    def test_hevc_only_where_the_browser_says_it_plays_it_and_ac3_never(self):
        C = self.C
        self.assertFalse(C.fits("mp4", {"video": "hevc", "audio": ["aac"]}, frozenset()))
        self.assertTrue(C.fits("mp4", {"video": "hevc", "audio": ["aac"]}, C.caps_of("hevc")))
        self.assertFalse(C.fits("mp4", {"video": "h264", "audio": ["eac3"]}, C.caps_of("hevc,eac3")))

    def test_an_mp4_jellyfin_never_looked_inside_needs_a_look(self):
        self.assertIsNone(self.C.fits("mp4", None, frozenset()))

    def test_only_known_codecs_count_from_what_the_browser_says(self):
        self.assertEqual(frozenset({"hevc"}), self.C.caps_of("HEVC, ac3,vp9,../x"))
        self.assertEqual("base", self.C.caps_key(frozenset()))

    def test_jellyfins_streams(self):
        item = {"MediaSources": [{"MediaStreams": [{"Type": "Video", "Codec": "h265"},
                                                   {"Type": "Audio", "Codec": "ac3"},
                                                   {"Type": "Subtitle", "Codec": "srt"}]}]}
        self.assertEqual({"video": "hevc", "audio": ["ac3"]}, self.C.streams_from_item(item))
        self.assertIsNone(self.C.streams_from_item({"MediaSources": [{"Path": "x.strm"}]}))


class WhatTheServerDoes(unittest.TestCase):
    def setUp(self):
        from services import offline_convert as C
        self.C = C

    def test_an_mkv_with_h264_and_ac3_copies_the_picture_and_makes_aac(self):
        p = self.C.plan(_probe(), frozenset())
        self.assertEqual(("copy", "aac", 0, 1, True), (p["video"], p["audio"], p["video_index"],
                                                        p["audio_index"], p["matroska"]))
        args = self.C.ffmpeg_args(p, "http://jf/Items/x/Download", "tok", "/out.mp4", "x264")
        self.assertIn("-seekable", args)       # an MKV read straight through
        self.assertEqual(["-c:v", "copy"], args[args.index("-c:v"):args.index("-c:v") + 2])
        self.assertEqual(["-c:a", "aac", "-b:a", "192k", "-ac", "2"], args[args.index("-c:a"):args.index("-c:a") + 6])
        self.assertEqual("/out.mp4", args[-1])
        self.assertIn("X-Emby-Token: tok\r\n", args)        # the key in a header, never the URL

    def test_ac3_becomes_aac_even_for_a_browser_that_claims_it_plays_it(self):
        p = self.C.plan(_probe(), self.C.caps_of("ac3,eac3"))
        self.assertEqual("aac", p["audio"])

    def test_hevc_is_copied_tagged_for_apple_where_it_plays(self):
        p = self.C.plan(_probe(video="hevc", audio=("aac",)), self.C.caps_of("hevc"))
        args = self.C.ffmpeg_args(p, "u", "t", "/o.mp4", "x264")
        self.assertEqual(("copy", "copy"), (p["video"], p["audio"]))
        self.assertIn("hvc1", args)

    def test_hevc_becomes_h264_where_it_doesnt(self):
        p = self.C.plan(_probe(video="hevc", audio=("aac",)), frozenset())
        self.assertEqual("encode", p["video"])

    def test_an_old_codec_is_converted_on_the_gpu_when_there_is_one(self):
        p = self.C.plan(_probe(video="mpeg4", audio=("mp3",), fmt="avi"), frozenset())
        self.assertEqual(("encode", "copy", False), (p["video"], p["audio"], p["matroska"]))
        gpu = self.C.ffmpeg_args(p, "u", "t", "/o.mp4", "vaapi")
        self.assertIn("h264_vaapi", gpu)
        self.assertIn("-vaapi_device", gpu)
        cpu = self.C.ffmpeg_args(p, "u", "t", "/o.mp4", "x264")
        self.assertIn("libx264", cpu)
        self.assertNotIn("-vaapi_device", cpu)

    def test_the_default_sound_track_only(self):
        p = self.C.plan(_probe(audio=("aac", "ac3", "dts"), default=2), frozenset())
        self.assertEqual((2, "aac"), (p["audio_index"], p["audio"]))
        p = self.C.plan(_probe(audio=("dts", "aac")), frozenset())
        self.assertEqual((1, "aac"), (p["audio_index"], p["audio"]))

    def test_no_sound_is_fine_and_no_picture_is_refused(self):
        p = self.C.plan(_probe(audio=()), frozenset())
        self.assertEqual((None, None), (p["audio"], p["audio_index"]))
        args = self.C.ffmpeg_args(p, "u", "t", "/o.mp4", "x264")
        self.assertNotIn("-c:a", args)
        with self.assertRaises(ValueError):
            self.C.plan({"streams": [{"index": 0, "codec_type": "audio", "codec_name": "aac"}]}, frozenset())


class HowBigItWillBe(unittest.TestCase):
    """The page checks the device has room before saving: the MP4 leaves out
    an MKV's other (often lossless) sound tracks, so its size comes from the
    bit rates of what it keeps, not the source's size."""

    def setUp(self):
        from services import offline_convert as C
        self.C = C

    def test_from_the_kept_streams_bit_rates(self):
        probe = _probe(audio=("truehd", "ac3"), duration="3600")
        probe["streams"][0]["tags"] = {"BPS": "8000000"}
        p = self.C.plan(probe, frozenset())
        est = self.C.output_estimate(probe, p)
        self.assertAlmostEqual((8_000_000 + 192_000) * 3600 / 8, est, delta=est * 0.03)

    def test_unknown_when_the_source_doesnt_say(self):
        p = self.C.plan(_probe(), frozenset())
        self.assertEqual(0, self.C.output_estimate(_probe(), p))
        enc = self.C.plan(_probe(video="mpeg4", audio=("mp3",), fmt="avi"), frozenset())
        self.assertEqual(0, self.C.output_estimate(_probe(video="mpeg4"), enc))


class _Spool(unittest.TestCase):
    def setUp(self):
        from services import offline_convert as C
        self.C = C
        self.dir = temp_dir(self)
        patcher = mock.patch.dict(os.environ, {"DATA_DIR": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        C.forget_all()
        self.addCleanup(C.forget_all)


class Cleaning(_Spool):
    def job(self, **kw):
        job = self.C.Job(f"{ITEM}-base", ITEM, 0)
        self.C.spool_dir().mkdir(parents=True, exist_ok=True)
        job.path.write_bytes(b"x" * 10)
        for k, v in kw.items():
            setattr(job, k, v)
        self.C._jobs[job.key] = job
        return job

    def test_a_finished_one_stays_hours_after_its_last_read_then_goes(self):
        job = self.job(done=True, size=10)
        now = time.monotonic()
        self.C.sweep(now + 3600)
        self.assertTrue(job.path.exists())
        self.C.sweep(now + self.C.KEEP_DONE_S + 1)
        self.assertFalse(job.path.exists())
        self.assertNotIn(job.key, self.C._jobs)

    def test_one_nobody_reads_while_it_is_made_is_stopped_after_an_hour(self):
        job = self.job()
        now = time.monotonic()
        self.C.sweep(now + 600)
        self.assertIn(job.key, self.C._jobs)
        self.C.sweep(now + self.C.IDLE_STOP_S + 1)
        self.assertNotIn(job.key, self.C._jobs)
        self.assertFalse(job.path.exists())

    def test_one_being_read_is_never_swept(self):
        job = self.job(readers=1)
        self.C.sweep(time.monotonic() + 10 * self.C.KEEP_DONE_S)
        self.assertIn(job.key, self.C._jobs)

    def test_a_failed_one_is_not_started_again_at_once(self):
        job = self.job(error=self.C.BROKE, finished=time.time())
        with mock.patch.object(self.C.shutil, "which", lambda n: "/usr/bin/" + n):
            again = self.C.get_or_start(ITEM, frozenset(), "http://jf/x", "t", 0, "Roman")
        self.assertIs(job, again)

    def test_no_room_on_the_server_is_said_plainly(self):
        from services.family import FamilyError
        with mock.patch.object(self.C.shutil, "which", lambda n: "/usr/bin/" + n), \
                mock.patch.object(self.C.shutil, "disk_usage", lambda p: mock.Mock(free=3 << 30)):
            with self.assertRaises(FamilyError) as cm:
                self.C.get_or_start(ITEM, frozenset(), "http://jf/x", "t", 8 << 30, "Roman")
        self.assertEqual((507, self.C.NO_ROOM), (cm.exception.status, cm.exception.message))

    def test_no_ffmpeg_is_said_plainly(self):
        from services.family import FamilyError
        with mock.patch.object(self.C.shutil, "which", lambda n: None):
            with self.assertRaises(FamilyError) as cm:
                self.C.get_or_start(ITEM, frozenset(), "http://jf/x", "t", 0, "Roman")
        self.assertEqual(409, cm.exception.status)

    def test_finished_before_a_restart_is_used_again(self):
        job = self.C.Job(f"{ITEM}-base", ITEM, 0)
        self.C.spool_dir().mkdir(parents=True, exist_ok=True)
        job.path.write_bytes(b"y" * 7)
        job.marker.write_text(json.dumps({"gen": "abc", "size": 7}))
        with mock.patch.object(self.C.shutil, "which", lambda n: "/usr/bin/" + n):
            got = self.C.get_or_start(ITEM, frozenset(), "http://jf/x", "t", 0, "Roman")
        self.assertEqual((True, 7, "abc"), (got.done, got.size, got.gen))


# ── With ffmpeg: a real MKV served as Jellyfin's Download ─────────────────────

class _Download(http.server.BaseHTTPRequestHandler):
    """Jellyfin's /Items/<id>/Download: the file, byte ranges, the key checked;
    `pause` holds the body after its first MB (a provider giving way to a viewer)."""
    path_on_disk = ""
    token = "k" * 32
    gate = None
    opened = []

    def do_GET(self):
        if self.headers.get("X-Emby-Token") != self.token:
            self.send_response(401)
            self.end_headers()
            return
        with open(self.path_on_disk, "rb") as f:
            data = f.read()
        start = 0
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0] or 0)
        type(self).opened.append(start)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(data) - start))
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        try:
            gate = type(self).gate
            if gate is not None and start == 0:
                self.wfile.write(data[:1 << 20])
                self.wfile.flush()
                gate.wait(30)
                self.wfile.write(data[1 << 20:])
            else:
                self.wfile.write(data[start:])
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def _request(range_header=None):
    headers = [(b"host", b"tentacle")] + ([(b"range", range_header.encode())] if range_header else [])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": "GET", "headers": headers, "path": "/", "query_string": b"",
                    "scheme": "http", "server": ("tentacle", 8888), "client": ("10.0.0.7", 1)}, receive)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg (in the image)")
class AnMkvBecomesAnMp4AsItIsSent(_Spool):
    @classmethod
    def setUpClass(cls):
        cls.src_dir = temp_dir()
        cls.src = os.path.join(cls.src_dir, "film.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "20",
                        "-c:v", "libx264", "-preset", "ultrafast", "-g", "24", "-b:v", "1500k",
                        "-c:a", "ac3", "-ac", "6", "-b:a", "384k", cls.src], check=True)
        _Download.path_on_disk = cls.src
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Download)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/Items/{ITEM}/Download"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.src_dir, ignore_errors=True)

    def setUp(self):
        super().setUp()
        _Download.gate = None
        _Download.opened = []
        self.out = os.path.join(self.dir, "saved.mp4")

    async def video(self, range_header=None, caps=""):
        """The Offline route's answer for the film, made by a real ffmpeg."""
        from routers import offline as R
        from models.database import TentacleUser
        user = TentacleUser(id=5, jellyfin_user_id="aa" * 16, display_name="Roman")
        db = mock.Mock()
        item = {"Id": ITEM, "Name": "Film", "Type": "Movie", "MediaType": "Video", "RunTimeTicks": 200_000_000,
                "MediaSources": [{"Path": "http://tentacle/api/vod/movie/1.2.abc.mkv", "Container": "strm"}]}
        settings = {"jellyfin_url": self.url.split("/Items/")[0], "jellyfin_api_key": _Download.token}
        with mock.patch.object(R.offline, "check", lambda db, user, item_id: item), \
                mock.patch.object(R, "get_setting", lambda db, k, d=None: settings.get(k, d)):
            return await R.video(ITEM, _request(range_header), can=caps, db=db, user=user)

    async def read_all(self, resp):
        return b"".join([c async for c in resp.body_iterator])

    def test_saved_in_two_goes_it_is_an_mp4_with_h264_and_aac_that_ffprobe_reads(self):
        async def go():
            _Download.gate = threading.Event()
            first = await self.video()
            self.assertEqual(200, first.status_code)
            self.assertNotIn("content-length", first.headers)      # no size while it is made
            job = first.headers["x-offline-job"]
            it = first.body_iterator
            got = await it.__anext__()                              # some of it, then the phone loses signal
            await it.aclose()
            _Download.gate.set()
            second = await self.video(f"bytes={len(got)}-")
            self.assertEqual((206, str(len(got)), job), (second.status_code, second.headers["x-offline-from"],
                                                          second.headers["x-offline-job"]))
            got += await self.read_all(second)
            head = await self.video("bytes=0-65535")                 # finished: its first bytes again
            self.assertEqual(206, head.status_code)
            total = int(head.headers["content-range"].rsplit("/", 1)[1])
            first_bytes = await self.read_all(head)
            return got, first_bytes, total
        got, first_bytes, total = asyncio.run(go())
        saved = first_bytes + got[len(first_bytes):]
        self.assertEqual(total, len(saved))
        with open(self.out, "wb") as f:
            f.write(saved)
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                          "-show_format", self.out], capture_output=True, check=True).stdout)
        codecs = [(s["codec_type"], s["codec_name"]) for s in info["streams"]]
        self.assertEqual([("video", "h264"), ("audio", "aac")], codecs)
        self.assertIn("mp4", info["format"]["format_name"])
        self.assertAlmostEqual(20.0, float(info["format"]["duration"]), delta=0.5)
        self.assertEqual(2, info["streams"][1]["channels"])
        # The whole file decodes (a cut in the middle would show here)
        dec = subprocess.run(["ffmpeg", "-v", "error", "-i", self.out, "-f", "null", "-"], capture_output=True)
        self.assertEqual(b"", dec.stderr.strip())
        # Its picture is the source's own, copied (not converted)
        src_v = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                                "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", self.src],
                               capture_output=True, check=True).stdout.strip()
        out_v = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                                "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", self.out],
                               capture_output=True, check=True).stdout.strip()
        self.assertEqual(src_v, out_v)

    def test_once_made_it_is_a_normal_file_with_its_size_and_ranges(self):
        async def go():
            whole = await self.read_all(await self.video())
            again = await self.video()
            self.assertEqual(str(len(whole)), again.headers["content-length"])
            body = await self.read_all(again)
            part = await self.video("bytes=100-199")
            self.assertEqual(f"bytes 100-199/{len(whole)}", part.headers["content-range"])
            return body, await self.read_all(part)
        body, part = asyncio.run(go())
        self.assertEqual(body[100:200], part)
        # One making for both reads: Jellyfin's Download was read once by ffprobe, once by ffmpeg
        self.assertLessEqual(len(_Download.opened), 3)

    def test_a_browser_that_plays_hevc_gets_its_own_copy(self):
        async def go():
            await self.read_all(await self.video(caps="hevc,ac3"))
            return os.path.join(self.dir, "offline-ready", f"{ITEM}-hevc.mp4")
        path = asyncio.run(go())
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path],
                                         capture_output=True, check=True).stdout)
        self.assertEqual(["h264", "aac"], [s["codec_name"] for s in info["streams"]])


# ── A show or a season ───────────────────────────────────────────────────────

SHOW = "11111111111111111111111111111111"
SEASON = "22222222222222222222222222222222"


def _ep(n, season=1, **kw):
    ep = {"Id": f"{n:032x}", "Name": f"Episode {n}", "Type": "Episode", "MediaType": "Video",
          "SeriesName": "Bluey", "ParentIndexNumber": season, "IndexNumber": n, "CanDownload": True,
          "MediaSources": [{"Path": f"/media/tv/Bluey/S0{season}E0{n}.mkv", "Container": "mkv", "Size": 100}]}
    ep.update(kw)
    return ep


class _ShowJellyfin:
    def __init__(self, episodes, policy=None):
        self.episodes = episodes
        self.policy = policy or {"EnableContentDownloading": True}
        self.asked = []
        self.folders = {SHOW: {"Id": SHOW, "Name": "Bluey", "Type": "Series", "IsFolder": True},
                        SEASON: {"Id": SEASON, "Name": "Season 2", "Type": "Season", "IsFolder": True,
                                 "SeriesId": SHOW, "SeriesName": "Bluey", "IndexNumber": 2}}

    def get(self, path, **params):
        if path.startswith("/Users/"):
            return {"Policy": self.policy}
        if path == "/Items":
            item = self.folders.get(params.get("ids"))
            return {"Items": [item] if item else []}
        if path == f"/Shows/{SHOW}/Episodes":
            self.asked.append(params)
            return {"Items": self.episodes}
        raise AssertionError(path)


class AShowOrASeasonInOneTap(unittest.TestCase):
    def setUp(self):
        from services import offline
        offline.forget()
        self.addCleanup(offline.forget)
        self.offline = offline
        from models.database import TentacleUser
        self.user = TentacleUser(id=5, jellyfin_user_id="aa" * 16, display_name="Roman")

    def ask(self, jf, item_id, season=None):
        from routers import offline as R
        with mock.patch.object(self.offline, "jellyfin", lambda db: jf):
            return R.item(item_id, season=season, db=None, user=self.user)

    def test_a_show_saves_every_episode_it_has_in_order(self):
        jf = _ShowJellyfin([_ep(2, 2), _ep(1, 1), _ep(3, 1)])
        got = self.ask(jf, SHOW)
        self.assertEqual(("show", "Bluey"), (got["kind"], got["name"]))
        self.assertEqual([(1, 1), (1, 3), (2, 2)], [(e["season"], e["episode"]) for e in got["episodes"]])
        self.assertEqual("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", jf.asked[0]["userId"], "not asked as them")
        self.assertEqual("mp4", got["episodes"][0]["container"])     # MKVs come as MP4s

    def test_a_season_page_saves_that_season(self):
        jf = _ShowJellyfin([_ep(1, 2), _ep(2, 2)])
        got = self.ask(jf, SEASON)
        self.assertEqual(("season", 2), (got["kind"], got["season"]))
        self.assertEqual(SEASON, jf.asked[0]["seasonId"])

    def test_a_season_from_tentacles_show_page(self):
        jf = _ShowJellyfin([_ep(1, 3)])
        got = self.ask(jf, SHOW, season=3)
        self.assertEqual(("season", 3), (got["kind"], got["season"]))
        self.assertEqual(3, jf.asked[0]["season"])

    def test_missing_episodes_and_recordings_still_recording_are_left_out(self):
        jf = _ShowJellyfin([_ep(1), _ep(2, LocationType="Virtual"), _ep(3, Status="InProgress"),
                            _ep(4, CanDownload=False), _ep(5)])
        got = self.ask(jf, SHOW)
        self.assertEqual([1, 5], [e["episode"] for e in got["episodes"]])

    def test_nothing_to_save_is_said_plainly(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self.ask(_ShowJellyfin([_ep(1, LocationType="Virtual")]), SHOW)
        self.assertEqual((404, "There are no episodes here to save."), (cm.exception.status_code, cm.exception.detail))

    def test_someone_who_may_not_save_offline_gets_no_list(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self.ask(_ShowJellyfin([_ep(1)], policy={"EnableContentDownloading": False}), SHOW)
        self.assertEqual(403, cm.exception.status_code)

    def test_a_shows_video_is_never_asked_for(self):
        from services.family import FamilyError
        jf = _ShowJellyfin([_ep(1)])
        with mock.patch.object(self.offline, "jellyfin", lambda db: jf):
            with self.assertRaises(FamilyError) as cm:
                self.offline.check(None, self.user, SHOW, fresh=True)
        self.assertEqual(400, cm.exception.status)


if __name__ == "__main__":
    unittest.main()

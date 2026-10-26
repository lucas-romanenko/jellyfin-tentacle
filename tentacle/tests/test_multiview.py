"""Multi-view (services/multiview.py, routers/multiview.py): 2-4 channels in
one picture, sound per tile as its own audio track, connections only when
free and given back at the end.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import asyncio
import json
import shutil
import subprocess
import types
import unittest
from unittest import mock

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from tmp_dirs import temp_dir


def _fresh_db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{temp_dir()}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


class Layout(unittest.TestCase):
    def test_two_side_by_side_centred(self):
        from services.multiview import layout
        self.assertEqual(layout(2), [(0, 270, 960, 540), (960, 270, 960, 540)])

    def test_three_and_four_in_a_grid(self):
        from services.multiview import layout
        self.assertEqual(layout(4), [(0, 0, 960, 540), (960, 0, 960, 540), (0, 540, 960, 540), (960, 540, 960, 540)])
        self.assertEqual(layout(3), layout(4)[:3])


class FfmpegArgs(unittest.TestCase):
    """The encoder: raw tiles from the clock in, one picture with a sound
    track per tile out."""

    def _args(self, n=4, enc="x264"):
        from services.multiview import ffmpeg_args
        return ffmpeg_args([f"pipe:{10 + i}" for i in range(n)], [f"pipe:{20 + i}" for i in range(n)], enc,
                           [f"Ch {i}" for i in range(n)])

    def test_one_audio_track_per_tile_in_order(self):
        a = self._args(3)
        maps = [a[i + 1] for i, x in enumerate(a) if x == "-map"]
        self.assertEqual(maps, ["[v]", "3:a", "4:a", "5:a"])
        self.assertIn("title=Ch 2", a)

    def test_raw_tiles_are_read_without_probing(self):
        # ffmpeg probes its inputs one after the other; probing a tile would
        # wait for frames the clock hands out only with every other tile's.
        a = self._args(2)
        ins = [i for i, x in enumerate(a) if x == "-i"]
        self.assertEqual([a[i + 1] for i in ins], ["pipe:10", "pipe:11", "pipe:20", "pipe:21"])
        for i in ins:
            self.assertIn("-nofind_stream_info", a[i - 12:i])
        self.assertIn("960x540", a)
        self.assertIn("s16le", a)

    def test_two_tiles_are_padded_to_1080(self):
        a = self._args(2)
        g = a[a.index("-filter_complex") + 1]
        self.assertIn("xstack=inputs=2:layout=0_0|960_0", g)
        self.assertIn("pad=1920:1080:0:270", g)

    def test_cpu_encode_without_gpu(self):
        a = self._args(4)
        self.assertIn("libx264", a)
        self.assertNotIn("-init_hw_device", a)

    def test_gpu_encode(self):
        a = self._args(4, enc="vaapi")
        self.assertIn("h264_vaapi", a)
        self.assertTrue(a[a.index("-filter_complex") + 1].endswith("format=nv12,hwupload[v]"))

    def test_titles_are_cleaned(self):
        from services.multiview import _clean_title
        self.assertEqual(_clean_title('TSN 2 "HD"; rm -rf'), "TSN 2 HD rm -rf")
        self.assertEqual(_clean_title(""), "Channel")


class DecoderArgs(unittest.TestCase):
    """A tile's decoder: the channel in, raw frames and sound out, both from t=0."""

    def test_on_the_gpu(self):
        from services.multiview import decoder_args
        a = decoder_args(True, True, "pipe:5", "pipe:6")
        self.assertLess(a.index("-hwaccel"), a.index("-i"))
        g = a[a.index("-filter_complex") + 1]
        self.assertIn("[0:v:0]deinterlace_vaapi", g)
        self.assertIn("fps=30000/1001:start_time=0", g)
        self.assertIn("first_pts=0", g)
        self.assertEqual(a[-1], "pipe:6")
        self.assertEqual(a[a.index("rawvideo") + 3], "pipe:5")

    def test_on_the_cpu(self):
        from services.multiview import decoder_args
        a = decoder_args(False, True, "pipe:5", "pipe:6")
        self.assertNotIn("-hwaccel", a)
        self.assertIn("[0:v:0]yadif", a[a.index("-filter_complex") + 1])

    def test_a_silent_channel_has_no_sound_out(self):
        from services.multiview import decoder_args
        a = decoder_args(False, False, "pipe:5", None)
        self.assertEqual(a.count("-map"), 1)
        self.assertNotIn("s16le", a)
        self.assertNotIn("[0:a:0]", a[a.index("-filter_complex") + 1])


class Probe(unittest.TestCase):
    def test_video_and_audio(self):
        from services.multiview import gpu_decodes, parse_probe
        p = parse_probe("h264,video,yuv420p\naac,audio\nac3,audio\n")
        self.assertEqual(p, {"video": True, "audio": True, "codec": "h264", "pix_fmt": "yuv420p"})
        self.assertTrue(gpu_decodes(p))

    def test_no_audio(self):
        from services.multiview import parse_probe
        self.assertFalse(parse_probe("mpeg2video,video,yuv420p\n")["audio"])

    def test_ten_bit_or_odd_codecs_are_decoded_on_the_cpu(self):
        from services.multiview import gpu_decodes, parse_probe
        self.assertFalse(gpu_decodes(parse_probe("h264,video,yuv420p10le\naac,audio")))
        self.assertFalse(gpu_decodes(parse_probe("vc1,video,yuv420p\naac,audio")))

    def test_nothing_read_counts_as_both(self):
        from services.multiview import parse_probe
        self.assertEqual(parse_probe(""), {"video": True, "audio": True, "codec": None, "pix_fmt": None})


class Base(unittest.TestCase):
    def setUp(self):
        import routers.livetv as livetv
        import services.multiview as MV
        self.livetv, self.MV = livetv, MV
        livetv._stream_slots = livetv._StreamSlots()
        self.addCleanup(lambda: setattr(livetv, "_stream_slots", livetv._StreamSlots()))
        livetv._shared_lock = None
        livetv._shared_streams.clear()
        livetv._pending_opens.clear()
        MV._sessions.clear()
        MV._lock = None
        MV._reaper = None
        self.Session = _fresh_db()
        self.db = self.Session()
        self.addCleanup(self.db.close)

    def _provider(self, limit=None, name="P"):
        from models.database import Provider
        p = Provider(name=name, server_url="http://provider.test", username="u", password="p",
                     live_tv_enabled=True, live_max_streams=limit)
        self.db.add(p)
        self.db.commit()
        return p

    def _channel(self, prov, name="C"):
        from models.database import LiveChannel
        ch = LiveChannel(provider_id=prov.id, name=name,
                         stream_url=f"http://provider.test/live/u/p/{name}.m3u8", enabled=True)
        self.db.add(ch)
        self.db.commit()
        return ch


class Connections(Base):
    def _lease(self, pid, kind="live", key=None):
        slots = self.livetv._stream_slots
        lease = types.SimpleNamespace(id=len(slots.leases) + 1000, kind=kind, provider_id=pid, preempted=False)
        slots.leases[lease.id] = lease
        return lease

    def test_enough_free(self):
        p = self._provider(limit=8)
        chans = [self._channel(p, f"C{i}") for i in range(4)]
        for _ in range(3):
            self._lease(p.id)
        r = self.MV.free_connections(self.db, chans)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["needed"], r["free"]), (4, 5))

    def test_refused_in_plain_words(self):
        p = self._provider(limit=8)
        chans = [self._channel(p, f"C{i}") for i in range(4)]
        for _ in range(6):
            self._lease(p.id)
        r = self.MV.free_connections(self.db, chans)
        self.assertFalse(r["ok"])
        self.assertEqual(r["message"], "Multi-view needs 4 free connections; 2 are free right now.")

    def test_a_recording_about_to_start_keeps_its_connection(self):
        p = self._provider(limit=8)
        chans = [self._channel(p, f"C{i}") for i in range(4)]
        for _ in range(3):
            self._lease(p.id, kind="recording")
        r = self.MV.free_connections(self.db, chans, soon=2)
        self.assertFalse(r["ok"])
        self.assertIn("3 are free right now (2 kept for recordings that start soon)", r["message"])

    def test_a_channel_already_streaming_needs_no_connection(self):
        p = self._provider(limit=4)
        chans = [self._channel(p, f"C{i}") for i in range(4)]
        for _ in range(2):
            self._lease(p.id)
        shared = mock.Mock(_closed=False)
        self.livetv._shared_streams[chans[0].id] = shared
        self.livetv._shared_streams[chans[1].id] = shared
        r = self.MV.free_connections(self.db, chans)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["needed"], 2)

    def test_each_provider_counts_on_its_own(self):
        a, b = self._provider(limit=6, name="A"), self._provider(limit=2, name="B")
        chans = [self._channel(b, "B1"), self._channel(b, "B2"), self._channel(a, "A1")]
        self._lease(b.id)
        r = self.MV.free_connections(self.db, chans)
        self.assertFalse(r["ok"])
        self.assertEqual(r["message"], "Multi-view needs 2 free connections on B; 1 is free.")


def _make_ts(path: str, seconds: int, freq: int, size: str = "640x360"):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=s={size}:r=25",
                    "-f", "lavfi", "-i", f"sine=f={freq}:r=48000",
                    "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-g", "25", "-c:a", "aac", "-f", "mpegts", path], check=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Picture(unittest.TestCase):
    """The real ffmpeg graph puts each tile where layout() says (two tiles:
    the strip in the middle, black above and below)."""

    def _luma_rows(self, n: int) -> list:
        from services.multiview import HEIGHT, WIDTH, decoder_args, ffmpeg_args
        d = temp_dir(self)
        ins = []
        for i in range(n):
            p = f"{d}/c{i}.ts"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "color=c=white:s=640x360:r=25",
                            "-f", "lavfi", "-i", "sine=f=440:r=48000", "-t", "2",
                            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-f", "mpegts", p], check=True)
            ins.append(p)
        raws, pcms = [], []
        for i, src in enumerate(ins):
            raws.append(f"{d}/v{i}.raw")
            pcms.append(f"{d}/a{i}.raw")
            subprocess.run(["timeout", "60"] + decoder_args(False, True, raws[-1], pcms[-1], source=src), check=True)
        args = ffmpeg_args(raws, pcms, "x264", [f"Ch {i}" for i in range(n)])
        args[-1] = f"{d}/out.ts"
        subprocess.run(["timeout", "60"] + args, check=True)
        raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", f"{d}/out.ts",
                              "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "pipe:1"],
                             capture_output=True, check=True).stdout
        self.assertEqual(len(raw), WIDTH * HEIGHT)
        # mean brightness of each row's left and right half
        return [(sum(raw[y * WIDTH:y * WIDTH + WIDTH // 2]) // (WIDTH // 2),
                 sum(raw[y * WIDTH + WIDTH // 2:(y + 1) * WIDTH]) // (WIDTH // 2)) for y in range(HEIGHT)]

    def test_two_tiles_sit_in_the_middle(self):
        rows = self._luma_rows(2)
        for y in (100, 260, 820, 1000):
            self.assertLess(max(rows[y]), 40, f"row {y} should be black")
        for y in (300, 540, 780):
            self.assertGreater(min(rows[y]), 120, f"row {y} should be the tiles")

    def test_three_tiles_fill_the_grid_with_the_fourth_place_black(self):
        rows = self._luma_rows(3)
        for y in (100, 400):
            self.assertGreater(min(rows[y]), 120)
        self.assertGreater(rows[800][0], 120)
        self.assertLess(rows[800][1], 40)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class EndToEnd(Base):
    """Channels through the real stream_proxy (shared upstreams, slots) with
    a provider that sends real MPEG-TS, and the real ffmpeg (CPU encode)."""

    @classmethod
    def setUpClass(cls):
        d = temp_dir(cls)
        cls.ts = []
        for i, f in enumerate((300, 500, 700)):
            p = f"{d}/in{i}.ts"
            _make_ts(p, 6, f)
            with open(p, "rb") as f:
                cls.ts.append(f.read())

    def setUp(self):
        super().setUp()
        self.MV._encoder = "x264"
        self.addCleanup(setattr, self.MV, "_encoder", None)
        self.opens = []
        self.closed = []

    def _run(self, body):
        livetv = self.livetv
        ts = self.ts

        async def fake_inner(channel_id, ua, url, release, guard=None, **kw):
            self.opens.append(channel_id)
            data = ts[(channel_id - 1) % len(ts)]

            async def gen():
                try:
                    step = 188 * 200
                    for _ in range(3):        # ~18 s of a live channel, paced roughly in real time
                        for i in range(0, len(data), step):
                            yield data[i:i + step]
                            await asyncio.sleep(0.02)
                finally:
                    self.closed.append(channel_id)
                    release()
            return StreamingResponse(gen(), media_type="video/mp2t")

        async def no_recordings(*a, **k):
            return set()

        async def drive():
            with mock.patch.object(livetv, "_stream_proxy_inner", fake_inner), \
                    mock.patch.object(livetv, "is_safe_url", lambda *a, **k: True), \
                    mock.patch.object(livetv, "_recording_channel_ids", no_recordings), \
                    mock.patch.object(livetv, "lan_origin_guard", lambda *a, **k: (lambda url: True)):
                try:
                    return await asyncio.wait_for(body(), timeout=90)
                finally:
                    await self.MV.close_all()
        return asyncio.run(drive())

    async def _read(self, session, nbytes=600_000):
        out = bytearray()
        agen = session.read()
        try:
            async for piece in agen:
                out += piece
                if len(out) >= nbytes:
                    break
        finally:
            await agen.aclose()
        return bytes(out)

    def _streams(self, data: bytes) -> list:
        d = temp_dir(self)
        with open(f"{d}/out.ts", "wb") as f:
            f.write(data)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                              "-of", "json", f"{d}/out.ts"], capture_output=True, text=True).stdout
        return [",".join(str(x) for x in (st.get("codec_type"), st.get("width"), st.get("height")) if x)
                for st in json.loads(out or "{}").get("streams", [])]

    def test_three_channels_one_picture_three_sound_tracks_then_all_released(self):
        p = self._provider(limit=8)
        chans = [self._channel(p, f"C{i}") for i in range(3)]
        picks = [(c.id, c.name, f"c{c.id}", f"item{c.id}") for c in chans]

        async def body():
            s = await self.MV.start("u1", picks, self.Session)
            data = await self._read(s)
            held = self.livetv._stream_slots.active
            desc = s.describe()
            await self.MV.close(s.id)
            await asyncio.sleep(0.1)
            return data, held, desc, self.livetv._stream_slots.active, dict(self.livetv._shared_streams)

        data, held, desc, after, shared = self._run(body)
        self.assertEqual(self._streams(data), ["video,1920,1080", "audio", "audio", "audio"])
        self.assertEqual(held, 3)
        self.assertEqual(sorted(self.opens), sorted(c.id for c in chans), "one connection per channel")
        self.assertEqual([t["audio_track"] for t in desc["tiles"]], [0, 1, 2])
        self.assertEqual([(t["x"], t["y"]) for t in desc["tiles"]], [(0, 0), (960, 0), (0, 540)])
        self.assertEqual(after, 0, "connections not given back when multi-view closed")
        self.assertEqual(shared, {})

    def test_a_channel_someone_watches_costs_nothing_and_stays_on_for_them(self):
        p = self._provider(limit=8)
        chans = [self._channel(p, f"C{i}") for i in range(2)]
        picks = [(c.id, c.name, f"c{c.id}", "") for c in chans]

        async def body():
            viewer = await self.livetv.stream_proxy(chans[0].id, self.db)
            first = await viewer.body_iterator.__anext__()
            s = await self.MV.start("u1", picks, self.Session)
            await self._read(s, 200_000)
            await self.MV.close(s.id)
            await asyncio.sleep(0.1)
            still = chans[0].id in self.livetv._shared_streams
            more = await viewer.body_iterator.__anext__()
            await viewer.body_iterator.aclose()
            return first, still, more

        first, still, more = self._run(body)
        self.assertEqual(self.opens.count(chans[0].id), 1, "the watched channel was opened twice")
        self.assertTrue(still, "closing multi-view closed a channel someone else watches")
        self.assertTrue(first and more)

    def test_swap_keeps_the_others_open_and_leaves_the_removed_one(self):
        p = self._provider(limit=8)
        a, b, c = (self._channel(p, n) for n in "ABC")

        async def body():
            s = await self.MV.start("u1", [(a.id, "A", "ca", ""), (b.id, "B", "cb", "")], self.Session)
            await self._read(s, 200_000)
            await s.change([(a.id, "A", "ca", ""), (c.id, "C", "cc", "")], self.Session)
            data = await self._read(s, 300_000)
            return data, [t.channel_id for t in s.tiles], s.alive

        data, tiles, alive = self._run(body)
        self.assertEqual(tiles, [a.id, c.id])
        self.assertTrue(alive)
        self.assertEqual(self.opens.count(a.id), 1, "the kept channel was reopened")
        self.assertIn(b.id, self.closed, "the removed channel kept its connection")
        self.assertEqual(self._streams(data)[:1], ["video,1920,1080"])

    def test_a_new_one_replaces_the_persons_running_one(self):
        p = self._provider(limit=8)
        a, b, c = (self._channel(p, n) for n in "ABC")

        async def body():
            s1 = await self.MV.start("u1", [(a.id, "A", "ca", ""), (b.id, "B", "cb", "")], self.Session)
            s2 = await self.MV.start("u1", [(a.id, "A", "ca", ""), (c.id, "C", "cc", "")], self.Session)
            return self.MV.get(s1.id), self.MV.get(s2.id) is s2

        old, new = self._run(body)
        self.assertIsNone(old)
        self.assertTrue(new)
        self.assertEqual(self.opens.count(a.id), 1, "a channel in both was closed in between")

    def test_a_channel_that_does_not_open_refuses_with_its_name(self):
        p = self._provider(limit=8)
        a, b = self._channel(p, "A"), self._channel(p, "B")

        async def body():
            real = self.livetv._tuner_channel
            with mock.patch.object(self.livetv, "_tuner_channel",
                                   lambda db, cid: None if cid == b.id else real(db, cid)):
                with self.assertRaises(self.MV.MultiviewError) as e:
                    await self.MV.start("u1", [(a.id, "A", "ca", ""), (b.id, "Bee", "cb", "")], self.Session)
            await asyncio.sleep(0.1)
            return str(e.exception), self.livetv._stream_slots.active

        msg, active = self._run(body)
        self.assertIn("Bee didn't open", msg)
        self.assertEqual(active, 0, "the channel that did open kept its connection")


class Picks(Base):
    """The router takes channels from the person's own lineup only."""

    def _user(self, uid="u1"):
        from models.database import TentacleUser
        u = TentacleUser(jellyfin_user_id=uid, display_name=uid)
        self.db.add(u)
        self.db.commit()
        return u

    def _lineup(self, entries):
        return mock.patch("routers.my_livetv.my_lineup", lambda db, user: entries)

    def test_item_ids_to_channels(self):
        from routers import multiview as R
        entries = [{"key": "c5", "name": "TSN", "stream_path": "/api/live/stream/5"},
                   {"key": "c6", "name": "CBC", "stream_path": "/api/live/stream/6"}]
        with self._lineup(entries), \
                mock.patch("services.livetv_tuners.all_tuner_url", lambda db: "http://t/api/live/tuner/all.m3u"), \
                mock.patch("services.livetv_lineups.item_ids_for", lambda url, e: {"aa": "c5", "bb": "c6"}):
            picks = R._picks(self.db, self._user(), R.StartBody(item_ids=["bb", "AA"]))
        self.assertEqual(picks, [(6, "CBC", "c6", "bb"), (5, "TSN", "c5", "AA".lower())])

    def test_a_channel_not_in_their_lineup_is_refused(self):
        from routers import multiview as R
        with self._lineup([{"key": "c5", "name": "TSN", "stream_path": "/api/live/stream/5"}]), \
                mock.patch("services.livetv_tuners.all_tuner_url", lambda db: ""):
            with self.assertRaises(HTTPException) as e:
                R._picks(self.db, self._user(), R.StartBody(keys=["c5", "c9"]))
        self.assertEqual(e.exception.status_code, 404)

    def test_youtube_live_cannot_be_in_it(self):
        from routers import multiview as R
        entries = [{"key": "c5", "name": "TSN", "stream_path": "/api/live/stream/5"},
                   {"key": "y2", "name": "NASA", "stream_path": "/api/youtube/live/2/stream.ts"}]
        with self._lineup(entries), mock.patch("services.livetv_tuners.all_tuner_url", lambda db: ""):
            with self.assertRaises(HTTPException) as e:
                R._picks(self.db, self._user(), R.StartBody(keys=["c5", "y2"]))
        self.assertIn("NASA can't be in multi-view", e.exception.detail)

    def test_two_to_four(self):
        from routers import multiview as R
        entries = [{"key": f"c{i}", "name": f"C{i}", "stream_path": f"/api/live/stream/{i}"} for i in range(1, 7)]
        with self._lineup(entries), mock.patch("services.livetv_tuners.all_tuner_url", lambda db: ""):
            for keys in (["c1"], ["c1", "c1"], [f"c{i}" for i in range(1, 6)]):
                with self.assertRaises(HTTPException):
                    R._picks(self.db, self._user(f"u{len(keys)}{keys[-1]}"), R.StartBody(keys=keys))

    def test_network_keys_play_their_variant(self):
        from services import multiview as MV
        with mock.patch("services.livetv_catalog.network_channel", lambda db, nid: 40 + nid), \
                mock.patch("services.livetv_catalog.place_channel", lambda db, nid, place: 70 + nid):
            self.assertEqual(MV.channel_id_of_path(self.db, "/api/live/net/2"), 42)
            self.assertEqual(MV.channel_id_of_path(self.db, "/api/live/net/2/at/vancouver"), 72)
            self.assertEqual(MV.channel_id_of_path(self.db, "/api/live/stream/9"), 9)
            self.assertIsNone(MV.channel_id_of_path(self.db, "/api/youtube/live/3/stream.ts"))


if __name__ == "__main__":
    unittest.main()


def _tiles_from(MV, chunks_of: list, has_audio=True) -> list:
    """Tiles fed by async generators (as their channel's stream), opened
    without a provider: what _Tile.open leaves behind."""
    tiles = []
    for i, gen in enumerate(chunks_of):
        t = MV._Tile(i + 1, f"C{i}", f"c{i + 1}", "")
        t.arrived = asyncio.Event()
        t.reader = asyncio.ensure_future(t._read(gen))
        t.response = object()
        t.has_audio = has_audio
        tiles.append(t)
    return tiles


def _paced(data: bytes, seconds: float, burst: float = 2.0, pause_at=None, pause=0.0):
    """A live channel: `burst` seconds at once, then real time; optionally
    nothing at all for `pause` seconds from `pause_at` (a provider cut)."""
    async def gen():
        rate = len(data) / seconds
        step = 188 * 50
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        paused = False
        for k in range(0, len(data), step):
            if pause_at is not None and not paused and loop.time() - t0 > pause_at:
                paused = True
                await asyncio.sleep(pause)
                t0 += pause
            due = t0 + max(0.0, k / rate - burst)
            if due > loop.time():
                await asyncio.sleep(due - loop.time())
            yield data[k:k + step]
    return gen()


def _freezes(path: str, box: tuple) -> list:
    """Still stretches (>= 0.5 s) of one tile of a picture."""
    x, y = box
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", path, "-map", "0:v",
                          "-vf", f"crop=960:540:{x}:{y},freezedetect=n=-50dB:d=0.5", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    import re
    return [float(v) for v in re.findall(r"freeze_duration: ([\d.]+)", err)]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class Smooth(unittest.TestCase):
    """The picture goes on at its pace whatever one channel does: the
    provider cuts a channel for seconds at a time (each tile 4-5 times a
    minute on a one-connection account), and the old single ffmpeg stopped
    the whole picture each time."""

    @classmethod
    def setUpClass(cls):
        d = temp_dir(cls)
        cls.ts = []
        for i, f in enumerate((300, 500, 700)):
            p = f"{d}/in{i}.ts"
            _make_ts(p, 14, f)
            with open(p, "rb") as f:
                cls.ts.append(f.read())

    def setUp(self):
        import services.multiview as MV
        self.MV = MV
        MV._encoder = "x264"
        self.addCleanup(setattr, MV, "_encoder", None)
        MV._stuck.clear()
        self.addCleanup(MV._stuck.clear)

    def _run(self, body):
        async def drive():
            return await asyncio.wait_for(body(), timeout=90)
        return asyncio.run(drive())

    async def _start(self, tiles):
        for t in tiles:
            t.head = b""
            info = await self.MV.probe(b"".join(p for _, p in t.buf) or b"")
            t.gpu = False
        s = self.MV.Session("u1", tiles)
        await s.start()
        return s

    async def _read_for(self, s, seconds: float) -> bytes:
        out = bytearray()
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        agen = s.read()
        try:
            async for piece in agen:
                out += piece
                if loop.time() > end:
                    break
        finally:
            await agen.aclose()
        return bytes(out)

    def _durations(self, path: str) -> list:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
                              "-show_entries", "stream=duration", "-of", "json", path],
                             capture_output=True, text=True).stdout
        return [float(st.get("duration") or 0) for st in json.loads(out or "{}").get("streams", [])]

    def test_a_channel_cut_for_seconds_holds_its_last_frame_and_nothing_else_stops(self):
        MV = self.MV

        async def body():
            tiles = _tiles_from(MV, [_paced(self.ts[0], 14), _paced(self.ts[1], 14, pause_at=3.0, pause=4.0),
                                     _paced(self.ts[2], 14)])
            await asyncio.sleep(0.5)
            s = await self._start(tiles)
            try:
                loop = asyncio.get_running_loop()
                t0 = loop.time()
                data = await self._read_for(s, 9.0)
                return data, loop.time() - t0, [(t.held * MV.TICK, t.stalls) for t in s.tiles], s.clock.late
            finally:
                await s.close()

        data, wall, held, late = self._run(body)
        d = temp_dir(self)
        with open(f"{d}/out.ts", "wb") as f:
            f.write(data)
        durations = self._durations(f"{d}/out.ts")
        video, sound = durations[0], durations[1:]
        # The picture kept its pace (and started its lead ahead): never behind the clock.
        self.assertGreaterEqual(video, wall - 0.5, f"picture {video:.1f}s in {wall:.1f}s: it stopped")
        self.assertEqual(late, 0)
        # Sound for every tile, as long as the picture: they stay together.
        self.assertEqual(len(sound), 3)
        for a in sound:
            self.assertAlmostEqual(a, video, delta=0.3)
        # Only the cut channel held, about as long as the cut outlasted its buffer.
        # The others: never a stall; a repeated frame here and there at most (a busy host).
        for other in (held[0], held[2]):
            self.assertLessEqual(other[0], 3 * MV.TICK + 1e-9)
            self.assertEqual(other[1], 0)
        self.assertGreater(held[1][0], 1.0)
        self.assertLess(held[1][0], 4.5)
        self.assertEqual(held[1][1], 1)
        self.assertEqual(_freezes(f"{d}/out.ts", (0, 0)), [])
        self.assertEqual(_freezes(f"{d}/out.ts", (0, 540)), [])
        frozen = _freezes(f"{d}/out.ts", (960, 0))
        self.assertEqual(len(frozen), 1)
        self.assertGreater(frozen[0], 1.0)

    def test_a_decoder_that_stops_is_started_again(self):
        MV = self.MV

        async def body():
            tiles = _tiles_from(MV, [_paced(self.ts[0], 14, burst=6), _paced(self.ts[1], 14, burst=6)])
            await asyncio.sleep(0.5)
            s = await self._start(tiles)
            try:
                await self._read_for(s, 2.0)
                first = s.tiles[0].decoder
                first.proc.kill()
                data = await self._read_for(s, 4.0)
                now = s.tiles[0].decoder
                return first, now, now.exited, s.tiles[0].decoder_restarts, len(data), s.alive
            finally:
                await s.close()

        first, now, exited, restarts, size, alive = self._run(body)
        self.assertIsNot(now, first)
        self.assertFalse(exited)
        self.assertEqual(restarts, 1)
        self.assertTrue(alive)
        self.assertGreater(size, 100_000)


class StuckFeed(unittest.TestCase):
    """A feed whose picture stands still (a broken 4K feed, 25 Oct: the same
    frame for 4.5 s at a time, broken sound) moves to another of its network."""

    def setUp(self):
        import services.multiview as MV
        self.MV = MV
        MV._stuck.clear()
        self.addCleanup(MV._stuck.clear)

    def _judge(self, frames: list):
        MV = self.MV
        clock = MV._Clock.__new__(MV._Clock)
        slot, tile = MV._Slot(), MV._Tile(1, "AMC", "n18", "")
        for f in frames:
            slot.frame = f
            clock._judge(slot, tile)
        return tile.stuck

    def test_the_same_frame_again_and_again_is_stuck(self):
        still = [bytes([1]) * 4000] * (self.MV.STUCK_WINDOW + 10)
        self.assertTrue(self._judge(still))

    def test_a_short_clip_looping_is_stuck(self):
        # That feed: 4.5 s of one frame, 1.8 s moving, the same again (34
        # different frames in 30 s); only 65-70 % equal their predecessor.
        clip = [bytes([200]) * 4000] * 135 + [bytes([i]) * 4000 for i in range(54)]
        self.assertTrue(self._judge(clip * 4))

    def test_a_moving_picture_or_a_short_still_is_not(self):
        moving = [bytes([i % 250]) + bytes([i // 250]) * 3999 for i in range(900)]
        self.assertFalse(self._judge(moving))
        still_5s = moving[:300] + [moving[0]] * 150 + moving[300:600]
        self.assertFalse(self._judge(still_5s))

    def test_the_feed_a_place_moved_to_is_judged_on_its_own_frames(self):
        # 25 Oct on production: the window was the place's, so the feed AMC
        # moved to inherited AMC 4K's repeated frames and moved on at once.
        MV = self.MV
        clock = MV._Clock.__new__(MV._Clock)
        slot = MV._Slot()
        stuck, new = MV._Tile(27, "AMC", "n18", ""), MV._Tile(223, "AMC", "n18", "")
        for _ in range(MV.STUCK_WINDOW):
            slot.frame = bytes([7]) * 4000
            clock._judge(slot, stuck)
        self.assertTrue(stuck.stuck)
        for i in range(60):
            slot.frame = bytes([i % 250]) * 4000
            clock._judge(slot, new)
        self.assertFalse(new.stuck)

    def test_another_feed_of_the_network_best_first(self):
        MV = self.MV
        from services import livetv_catalog as C
        with mock.patch.object(C, "_network_members", return_value={18: {"members": [27, 223, 1148]}}), \
                mock.patch.object(C, "_Scorer", side_effect=RuntimeError("no db")):
            self.assertEqual(MV.other_feeds(None, "n18", 27), [223, 1148])
            self.assertEqual(MV.other_feeds(None, "n18@toronto", 223), [27, 1148])
            self.assertEqual(MV.other_feeds(None, "c27", 27), [])
            self.assertEqual(MV.other_feeds(None, "n99", 27), [])

    def test_the_place_moves_and_the_next_start_skips_the_stuck_feed(self):
        MV = self.MV
        opened, closed = [], []

        async def fake_open(tile, db_factory):
            if tile.channel_id == 223:
                raise MV.MultiviewError("CA: AMC didn't open", 503)
            opened.append(tile.channel_id)
            tile.response = object()

        async def fake_close(tile):
            closed.append(tile.channel_id)

        async def no_decoder(tile, vaapi):
            return None

        async def body():
            old = MV._Tile(27, "AMC", "n18", "item18")
            other = MV._Tile(1388, "Boomerang", "n126", "")
            s = MV.Session("u1", [other, old])
            s.db_factory = lambda: mock.MagicMock()
            old.stuck = True
            await s._move(1, old, "its picture stands still")
            return s

        with mock.patch.object(MV, "other_feeds", return_value=[223, 1148]), \
                mock.patch.object(MV._Tile, "open", fake_open), \
                mock.patch.object(MV._Tile, "close", fake_close), \
                mock.patch.object(MV._Tile, "start_decoder", no_decoder):
            s = asyncio.run(body())
            self.assertEqual([t.channel_id for t in s.tiles], [1388, 1148])
            self.assertEqual((s.tiles[1].name, s.tiles[1].key, s.tiles[1].item_id), ("AMC", "n18", "item18"))
            self.assertEqual(opened, [1148])
            self.assertEqual(closed, [27])
            self.assertEqual(s.describe()["moved"], [{"place": 1, "from": 27, "to": 1148,
                                                      "why": "its picture stands still"}])
            self.assertTrue(MV.is_stuck(27))
            # The next multi-view starts AMC on the other feed at once.
            self.assertEqual(MV.first_feed(None, (27, "AMC", "n18", "item18")), (223, "AMC", "n18", "item18"))
            self.assertEqual(MV.first_feed(None, (1388, "Boomerang", "n126", "")), (1388, "Boomerang", "n126", ""))


    def test_no_other_feed_gives_up_once(self):
        MV = self.MV

        async def body():
            old = MV._Tile(5, "One", "c5", "")
            s = MV.Session("u1", [MV._Tile(6, "Two", "c6", ""), old])
            s.db_factory = lambda: mock.MagicMock()
            old.stuck = True
            await s._move(1, old, "its picture stands still")
            return s, old

        with mock.patch.object(MV, "other_feeds", return_value=[]):
            s, old = asyncio.run(body())
        self.assertIs(s.tiles[1], old)
        self.assertTrue(old.gave_up, "it would be judged again every half second")
        self.assertEqual(s.moves, [])

    def test_an_ended_channel_is_tried_again_less_and_less_often(self):
        MV = self.MV
        tries = []

        async def refused(tile, db_factory):
            tries.append(tile.channel_id)
            raise MV.MultiviewError("Boomerang didn't open: refused", 503)

        async def body():
            old = MV._Tile(1388, "Boomerang", "n126", "")
            old.ended, old.ended_at = True, 0.0
            s = MV.Session("u1", [old, MV._Tile(1392, "PBS Kids", "n183", "")])
            s.db_factory = lambda: mock.MagicMock()
            waits = []
            for _ in range(6):
                await s._move(0, old, "its stream ended", same_first=True)
                waits.append(old.reopen_after)
            return s, old, waits

        with mock.patch.object(MV, "other_feeds", return_value=[]), \
                mock.patch.object(MV._Tile, "open", refused):
            s, old, waits = asyncio.run(body())
        self.assertEqual(tries, [1388] * 6)
        self.assertIs(s.tiles[0], old)
        self.assertEqual(waits, [40.0, 80.0, 160.0, 300.0, 300.0, 300.0])

    def test_the_network_stream_plays_another_feed_while_its_own_is_stuck(self):
        # Full screen from the grid (and anyone tuning AMC) gets the working feed.
        MV = self.MV
        from routers import livetv_catalog as R
        with mock.patch.object(MV, "other_feeds", return_value=[223, 1148]):
            self.assertEqual(R._not_stuck(None, 18, 27), 27)
            MV.mark_stuck(27)
            self.assertEqual(R._not_stuck(None, 18, 27), 223)
            MV.mark_stuck(223)
            self.assertEqual(R._not_stuck(None, 18, 27), 1148)
        with mock.patch.object(MV.time, "monotonic", return_value=MV.time.monotonic() + MV.STUCK_MEMORY_SECONDS + 1):
            self.assertFalse(MV.is_stuck(27), "a feed stays stuck only for a while")


class CatchUp(unittest.TestCase):
    """A tile far behind live jumps forward, onto a keyframe."""

    def _pkt(self, pes_video=False, rai=False, fill=0):
        if pes_video:
            af = bytes([1, 0x40 if rai else 0x00])
            body = af + b"\x00\x00\x01\xe0" + bytes(188 - 4 - len(af) - 4)
            return bytes([0x47, 0x41, 0x00, 0x30]) + body
        return bytes([0x47, 0x01, 0x00, 0x10]) + bytes([fill]) * 184

    def test_keyframe_offset(self):
        from services.multiview import _video_key_offset
        data = self._pkt() + self._pkt(pes_video=True) + self._pkt() + self._pkt(pes_video=True, rai=True)
        self.assertEqual(_video_key_offset(data), 3 * 188)
        self.assertEqual(_video_key_offset(self._pkt() + self._pkt(pes_video=True)), 188)
        self.assertIsNone(_video_key_offset(self._pkt() * 3))

    def test_drop_to_keeps_what_arrived_last_from_a_keyframe(self):
        import services.multiview as MV
        t = MV._Tile(1, "C", "c1", "")
        now = MV.time.monotonic()
        for k in range(50):                                    # one piece a second, 50 s waiting
            pkts = [self._pkt(fill=k)] * 10
            if k == 40:
                pkts[3] = self._pkt(pes_video=True, rai=True)
            piece = b"".join(pkts)
            t.buf.append((now - 50 + k, piece))
            t.buf_bytes += len(piece)
        self.assertAlmostEqual(t.seconds_behind(), 50, delta=0.5)
        dropped = t.drop_to(15)
        self.assertGreater(dropped, 0)
        rest = b"".join(p for _, p in t.buf)
        self.assertEqual(len(rest), t.buf_bytes)
        self.assertEqual(rest[:188], self._pkt(pes_video=True, rai=True))
        self.assertLessEqual(t.seconds_behind(), 15)

    def test_a_resent_burst_is_not_behind(self):
        # A re-dial not joined resends seconds already sent: many bytes, just arrived.
        import services.multiview as MV
        t = MV._Tile(1, "C", "c1", "")
        t.buf.append((MV.time.monotonic(), b"G" * 30_000_000))
        t.buf_bytes = 30_000_000
        self.assertLess(t.seconds_behind(), 1)
        self.assertEqual(t.drop_to(15), 0)

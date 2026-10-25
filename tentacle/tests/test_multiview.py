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
    def _args(self, n=4, enc="x264", audio=None, hw=None):
        from services.multiview import ffmpeg_args
        return ffmpeg_args([f"pipe:{10 + i}" for i in range(n)], enc, [f"Ch {i}" for i in range(n)],
                           audio or [True] * n, hw)

    def test_one_audio_track_per_tile_in_order(self):
        a = self._args(3)
        maps = [a[i + 1] for i, x in enumerate(a) if x == "-map"]
        self.assertEqual(maps, ["[v]", "[a0]", "[a1]", "[a2]"])
        self.assertIn("title=Ch 2", a)

    def test_a_silent_channel_gets_a_silent_track(self):
        g = self._args(2, audio=[True, False])[self._args(2, audio=[True, False]).index("-filter_complex") + 1]
        self.assertIn("[0:a:0]", g)
        self.assertNotIn("[1:a:0]", g)
        self.assertIn("anullsrc=r=48000:cl=stereo[a1]", g)

    def test_two_tiles_are_padded_to_1080(self):
        a = self._args(2)
        g = a[a.index("-filter_complex") + 1]
        self.assertIn("xstack=inputs=2:layout=0_0|960_0", g)
        self.assertIn("pad=1920:1080:0:270", g)

    def test_cpu_encode_without_gpu(self):
        a = self._args(4)
        self.assertIn("libx264", a)
        self.assertNotIn("-hwaccel", a)
        self.assertNotIn("-init_hw_device", a)

    def test_gpu_decodes_only_the_tiles_it_can(self):
        a = self._args(2, enc="vaapi", hw=[True, False])
        self.assertIn("h264_vaapi", a)
        self.assertEqual(a.count("-hwaccel"), 1)
        # The GPU input comes first, before its -i.
        self.assertLess(a.index("-hwaccel"), a.index("pipe:10"))
        g = a[a.index("-filter_complex") + 1]
        self.assertIn("[0:v:0]deinterlace_vaapi", g)
        self.assertIn("[1:v:0]yadif", g)
        self.assertTrue(g.endswith("format=nv12,hwupload[v]"))

    def test_no_gpu_decode_with_a_cpu_encoder(self):
        a = self._args(2, enc="x264", hw=[True, True])
        self.assertNotIn("-hwaccel", a)

    def test_titles_are_cleaned(self):
        from services.multiview import _clean_title
        self.assertEqual(_clean_title('TSN 2 "HD"; rm -rf'), "TSN 2 HD rm -rf")
        self.assertEqual(_clean_title(""), "Channel")


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
        from services.multiview import HEIGHT, WIDTH, ffmpeg_args
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
        args = ffmpeg_args(ins, "x264", [f"Ch {i}" for i in range(n)], [True] * n)
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

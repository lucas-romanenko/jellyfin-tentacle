"""Live pictures for web browsers (services/web_stream.py, routers/multiview.py):
Jellyfin Web's multi-view and its guide's muted preview, as fragmented MP4
(a browser plays neither MPEG-TS nor switchable sound tracks).

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

from services import web_stream as WS


class Args(unittest.TestCase):
    def test_multiview_copies_the_picture_with_one_sound_track(self):
        args = WS.multiview_args("ffmpeg", 2)
        self.assertIn("0:a:2?", args)
        self.assertEqual(args[args.index("-c") + 1], "copy")
        self.assertEqual(1, sum(1 for a in args if a.startswith("0:a")))
        # copied AAC (ADTS in MPEG-TS) needs its config in the MP4 header, which
        # is written once the first fragment is cut: browsers refuse it otherwise
        self.assertIn("aac_adtstoasc", args)
        self.assertIn("delay_moov", args[args.index("-movflags") + 1])

    def test_preview_is_small_and_silent(self):
        args = WS.preview_args("ffmpeg")
        self.assertIn("-an", args)
        vf = args[args.index("-vf") + 1]
        self.assertIn("scale=-2:360", vf)
        self.assertIn("yadif", vf)
        self.assertEqual(args[args.index("-g") + 1], "30")

    def test_fragmented_mp4_sent_as_made(self):
        for args in (WS.multiview_args("ffmpeg", 0), WS.preview_args("ffmpeg")):
            flags = args[args.index("-movflags") + 1]
            for f in ("frag_keyframe", "empty_moov", "default_base_moof"):
                self.assertIn(f, flags)
            self.assertEqual(args[-1], "pipe:1")


def _make_ts(path: str, seconds: int, tracks: int = 2):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30000/1001"]
    for i in range(tracks):
        cmd += ["-f", "lavfi", "-i", f"sine=f={300 + 200 * i}:r=48000"]
    cmd += ["-t", str(seconds)]
    for i in range(tracks + 1):
        cmd += ["-map", str(i)]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30",
            "-c:a", "aac", "-ac", "2", "-f", "mpegts", path]
    subprocess.run(cmd, check=True)


def _probe(path: str) -> list:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,height,profile",
                          "-of", "json", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class Real(unittest.TestCase):
    """The real ffmpeg, fed the way the routes feed it (an async source)."""

    @classmethod
    def setUpClass(cls):
        cls.dir = temp_dir(cls)
        _make_ts(f"{cls.dir}/in.ts", 4)
        with open(f"{cls.dir}/in.ts", "rb") as f:
            cls.ts = f.read()

    def _source(self, closed: list):
        data = self.ts

        async def gen():
            try:
                for i in range(0, len(data), 188 * 100):
                    yield data[i:i + 188 * 100]
                    await asyncio.sleep(0)
            finally:
                closed.append(True)
        return gen()

    def _run(self, args, limit=None):
        closed, done = [], []

        async def on_done():
            done.append(True)

        async def main():
            out = bytearray()
            stream = WS.fmp4(self._source(closed), args, on_done)
            async for piece in stream:
                out += piece
                if limit and len(out) >= limit:
                    break
            await stream.aclose()
            return bytes(out)
        return asyncio.run(main()), closed, done

    def test_multiview_for_a_browser(self):
        out, closed, done = self._run(WS.multiview_args("ffmpeg", 1))
        path = f"{self.dir}/mv.mp4"
        with open(path, "wb") as f:
            f.write(out)
        streams = _probe(path)
        self.assertEqual(["audio", "video"], sorted(s["codec_type"] for s in streams))
        self.assertIn(b"moof", out)
        aac = [s for s in streams if s["codec_type"] == "audio"][0]
        self.assertEqual("LC", aac["profile"])  # the AAC config made it into the header
        self.assertEqual([True], closed)
        self.assertEqual([True], done)

    def test_preview_for_a_browser(self):
        out, _, done = self._run(WS.preview_args("ffmpeg"))
        path = f"{self.dir}/pv.mp4"
        with open(path, "wb") as f:
            f.write(out)
        streams = _probe(path)
        self.assertEqual(1, len(streams))
        self.assertEqual(("h264", "video", 360), (streams[0]["codec_name"], streams[0]["codec_type"], streams[0]["height"]))
        self.assertEqual([True], done)

    def test_the_browser_leaving_stops_everything(self):
        out, closed, done = self._run(WS.multiview_args("ffmpeg", 0), limit=1)
        self.assertTrue(out)
        self.assertEqual([True], closed)      # the channel's read ends (its connection goes back)
        self.assertEqual([True], done)


class Routes(unittest.TestCase):
    def setUp(self):
        from routers import multiview as R
        from routers import livetv as LT
        self.R, self.LT = R, LT
        self.user = types.SimpleNamespace(jellyfin_user_id="u-1")
        p = mock.patch.object(R.MV, "_ffmpeg", lambda: "ffmpeg")
        p.start()
        self.addCleanup(p.stop)

    def test_an_ended_multiview_is_404(self):
        with self.assertRaises(HTTPException) as e:
            asyncio.run(self.R.multiview_web("nope-nope-nope-nope"))
        self.assertEqual(404, e.exception.status_code)

    def test_multiview_reads_the_session_with_one_sound(self):
        read = []

        async def reader():
            read.append(True)
            yield b""
        s = types.SimpleNamespace(tiles=[1, 2, 3], read=reader)
        with mock.patch.object(self.R.MV, "get", lambda sid: s), \
                mock.patch.object(self.R.WS, "fmp4", lambda src, args, *a: (args, src)) as _:
            resp = None
            with mock.patch.object(self.R, "StreamingResponse", lambda body, **kw: body):
                resp = asyncio.run(self.R.multiview_web("x" * 20, audio=9))
        args, _src = resp
        self.assertIn("0:a:2?", args)          # clamped to the last tile

    def _preview(self, previews: bool, open_already: bool):
        opened = []

        async def stream_proxy(cid, db):
            opened.append(cid)

            async def body():
                yield b"ts"
            return types.SimpleNamespace(body_iterator=body(), headers={})

        async def live_preview(previewing=False, user=None, db=None):
            return {"previews": previews}
        shared = {7: types.SimpleNamespace(_closed=False)} if open_already else {}
        with mock.patch.object(self.R, "_picks", lambda db, user, body, count=True: [(7, "PBS Kids", "k", "i")]), \
                mock.patch.object(self.LT, "live_preview", live_preview), \
                mock.patch.object(self.LT, "_shared_streams", shared), \
                mock.patch.object(self.LT, "stream_proxy", stream_proxy):
            resp = asyncio.run(self.R.preview_stream("ab" * 16, user=self.user, db=None))
        return resp, opened

    def test_preview_refused_without_a_spare_connection(self):
        with self.assertRaises(HTTPException) as e:
            self._preview(previews=False, open_already=False)
        self.assertEqual(409, e.exception.status_code)

    def test_preview_of_a_channel_already_open_costs_nothing(self):
        resp, opened = self._preview(previews=False, open_already=True)
        self.assertIsInstance(resp, StreamingResponse)
        self.assertEqual("video/mp4", resp.media_type)
        self.assertEqual([7], opened)

    def test_preview_with_a_spare_connection(self):
        resp, opened = self._preview(previews=True, open_already=False)
        self.assertEqual([7], opened)


if __name__ == "__main__":
    unittest.main()

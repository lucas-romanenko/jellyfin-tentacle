"""The YouTube Live tuner URL (/api/youtube/live/{id}/stream.ts) must not
start work it cannot finish.

Jellyfin HEADs a tuner URL before it plays it. The same handler served HEAD
and GET, so every HEAD resolved the stream with yt-dlp and started an ffmpeg
whose output was thrown away. And when a client disconnected, Starlette
abandoned the stream generator without closing it: ffmpeg sat blocked on a
full pipe until the garbage collector happened to run.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import gc
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, YouTubeChannel, YouTubeVideo, get_db
from routers import youtube as yt_router


def _fake_ffmpeg(dirname):
    """A stand-in ffmpeg that writes forever, like a live remux does."""
    path = os.path.join(dirname, "ffmpeg")
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\nimport sys\nwhile True:\n    sys.stdout.buffer.write(b'G' * 188)\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class TestYouTubeLiveStreamLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        engine = create_engine(f"sqlite:///{self.tmp.name}/t.db",
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        db = self.Session()
        ch = YouTubeChannel(input_url="https://www.youtube.com/@x", slug="x", title="X",
                            channel_id="UCxxxxxxxxxxxxxxxxxxxxxx", live_enabled=True)
        db.add(ch); db.commit()
        db.add(YouTubeVideo(video_id="abcdefghijk", channel_fk=ch.id, title="live",
                            live_status="is_live"))
        db.commit()
        self.channel_id = ch.id
        db.close()

        app = FastAPI()
        app.include_router(yt_router.router)
        app.dependency_overrides[get_db] = self._db
        self.client = TestClient(app)

        self.ffmpeg = _fake_ffmpeg(self.tmp.name)
        self.pick = mock.patch.object(
            yt_router.resolver, "pick_tracks",
            return_value=("http://example.invalid/v.m3u8", None, {"User-Agent": "x"}))
        self.which = mock.patch("shutil.which", return_value=self.ffmpeg)
        self.pick_mock = self.pick.start()
        self.which.start()

    def tearDown(self):
        self.pick.stop()
        self.which.stop()
        self.tmp.cleanup()

    def _db(self):
        db = self.Session()
        try:
            yield db
        finally:
            db.close()

    def test_head_answers_without_resolving_or_starting_ffmpeg(self):
        # Raises rather than returning a mock: a mocked pipe would stream
        # forever and hang the test instead of failing it.
        with mock.patch("subprocess.Popen",
                        side_effect=AssertionError("ffmpeg started for a HEAD")) as popen:
            try:
                r = self.client.head(f"/api/youtube/live/{self.channel_id}/stream.ts")
            except AssertionError as e:
                self.fail(str(e))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"].split(";")[0], "video/mp2t")
        popen.assert_not_called()
        self.pick_mock.assert_not_called()

    def test_head_on_a_channel_with_nothing_live_is_503(self):
        db = self.Session()
        db.query(YouTubeVideo).update({"live_status": "was_live"})
        db.commit(); db.close()
        r = self.client.head(f"/api/youtube/live/{self.channel_id}/stream.ts")
        self.assertEqual(r.status_code, 503)

    def test_head_on_an_unknown_channel_is_404(self):
        r = self.client.head("/api/youtube/live/999/stream.ts")
        self.assertEqual(r.status_code, 404)

    def test_a_client_that_disconnects_stops_ffmpeg_without_the_garbage_collector(self):
        """Through a real server, since what matters is what Starlette does
        with the generator when the client goes away."""
        import socket
        import threading
        import httpx
        import uvicorn

        started = []
        real_popen = __import__("subprocess").Popen

        def spy(*a, **k):
            p = real_popen(*a, **k)
            started.append(p)
            return p

        sock = socket.socket(); sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]; sock.close()
        server = uvicorn.Server(uvicorn.Config(self.client.app, host="127.0.0.1",
                                               port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)

        gc.disable()
        try:
            with mock.patch("subprocess.Popen", side_effect=spy):
                url = f"http://127.0.0.1:{port}/api/youtube/live/{self.channel_id}/stream.ts"
                with httpx.stream("GET", url, timeout=10) as r:
                    for _ in r.iter_bytes():
                        break
                proc = started[0]
                deadline = time.time() + 5
                while proc.poll() is None and time.time() < deadline:
                    time.sleep(0.05)
                alive = proc.poll() is None
                if alive:
                    proc.kill(); proc.wait()
            self.assertFalse(alive, "ffmpeg is still running 5 s after the client left")
        finally:
            gc.enable()
            server.should_exit = True


if __name__ == "__main__":
    unittest.main()

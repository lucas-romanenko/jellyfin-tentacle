"""What a YouTube Live TV channel hands the tuner, re-checked at 1633dd1.

9aa3977 ("Serve Live TV as MPEG-TS") replaced the lineup's master.m3u8 URL with
/live/{id}/stream.ts and added an ffmpeg stream-copy remux. That closes the
headline complaint, and TestTunerServesMpegTs proves it.

Four things in that path are still wrong:

* /api/live/playlist.m3u — the M3U tuner, the other supported way to attach
  Tentacle to Jellyfin — still lists only LiveChannel rows, so the same
  channels that are in the HDHomeRun lineup are absent from it.
* The stream.ts handler keeps ffmpeg's stderr on a pipe it only reads AFTER the
  stdout loop has finished. A pipe holds 64 KiB; once ffmpeg fills it the
  process blocks writing stderr, stops producing stdout, and the generator
  blocks for ever on a read that will never be satisfied. Live TV hangs with no
  error anywhere.
* HEAD and GET share one handler, so a HEAD probe runs a full yt-dlp
  extraction. Extraction is the rate-limited resource this whole feature is
  paced around (DETAIL_SPACING_SECONDS, BLOCK_BACKOFF_HOURS).
* A missing ffmpeg is an unhandled FileNotFoundError out of Popen rather than
  a reported status — and /diagnose, which walks the whole chain, checks
  yt-dlp but not the ffmpeg that every Live TV tune now depends on.

Needs fastapi + httpx + sqlalchemy. Run from tentacle/:
    python -m unittest tests.test_youtube_livetv_tuner
"""
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import models.database as mdb
import routers.livetv as livetv_router
import routers.youtube as youtube_router

DEADLINE = 15.0


def _fake_ffmpeg(tmpdir: str, stderr_bytes: int = 0, ts_packets: int = 2000) -> str:
    """An executable that behaves like the ffmpeg the handler spawns.

    Writes `stderr_bytes` to stderr FIRST, then MPEG-TS to stdout — the order a
    real ffmpeg uses when it reports input warnings before the first packet —
    then exits, so a request that never finishes is a stall and nothing else.
    """
    path = os.path.join(tmpdir, "fake-ffmpeg")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "#!" + sys.executable + "\n"
            "import sys\n"
            "noise = %d\n"
            "if noise:\n"
            "    sys.stderr.buffer.write(b'warning: discarding a segment\\n' * (noise // 28))\n"
            "    sys.stderr.buffer.flush()\n"
            "pkt = b'\\x47' + b'\\xff' * 187\n"
            "for _ in range(%d):\n"
            "    sys.stdout.buffer.write(pkt)\n"
            "sys.stdout.buffer.flush()\n" % (stderr_bytes, ts_packets)
        )
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def _seed(db):
    from models.database import LiveChannel, Provider, YouTubeChannel, YouTubeVideo
    provider = Provider(name="P", server_url="http://192.0.2.10",
                        username="u", password="p")
    db.add(provider)
    db.commit()
    db.add(LiveChannel(provider_id=provider.id, name="IPTV One", stream_id="277108",
                       stream_url="http://192.0.2.10/live/1.ts", enabled=True))
    channel = YouTubeChannel(input_url="https://www.youtube.com/@ch", kind="channel",
                             channel_id="UC" + "z" * 22, title="Sports Channel",
                             slug="sports-channel", enabled=True, live_enabled=True,
                             max_height=1080, extra_tags=[])
    db.add(channel)
    db.commit()
    db.refresh(channel)
    db.add(YouTubeVideo(channel_fk=channel.id, video_id="aaaaaaaaaaa", title="On now",
                        live_status="is_live", media_type="livestream"))
    db.commit()
    return channel


class _Base(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        self.tmp = tempfile.mkdtemp()
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.channel = _seed(self.db)

        app = FastAPI()
        app.include_router(livetv_router.router)
        app.include_router(youtube_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()


class TestTunerServesMpegTs(_Base):
    """FIXED by 9aa3977 — kept so a regression is caught."""

    def test_the_lineup_points_at_a_ts_endpoint(self):
        lineup = self.client.get("/hdhr/lineup.json").json()
        yt = [e for e in lineup if e["GuideName"] == "Sports Channel"]
        self.assertEqual(1, len(yt), lineup)
        self.assertTrue(yt[0]["URL"].endswith("/stream.ts"),
                        f"tuner URL is not MPEG-TS: {yt[0]['URL']}")

    def test_the_ts_endpoint_answers_with_mpegts_bytes(self):
        ffmpeg = _fake_ffmpeg(self.tmp)
        with mock.patch.object(youtube_router.resolver, "pick_tracks",
                               return_value=("https://r1.example.invalid/v.m3u8",
                                             "https://r1.example.invalid/a.m3u8",
                                             {"User-Agent": "Mozilla/5.0"})), \
             mock.patch("shutil.which", return_value=ffmpeg):
            r = self.client.get(f"/api/youtube/live/{self.channel.id}/stream.ts")
        self.assertEqual(200, r.status_code)
        self.assertEqual("video/mp2t", r.headers["content-type"].split(";")[0])
        self.assertEqual(0x47, r.content[0], "not an MPEG-TS packet")
        self.assertEqual(0, len(r.content) % 188, "not whole MPEG-TS packets")

    def test_nothing_on_air_is_a_503(self):
        from models.database import YouTubeVideo
        self.db.query(YouTubeVideo).delete()
        self.db.commit()
        r = self.client.get(f"/api/youtube/live/{self.channel.id}/stream.ts")
        self.assertEqual(503, r.status_code)


class TestM3uTunerListsYouTubeChannels(_Base):
    """STILL BROKEN — the M3U tuner never learned about YouTube channels."""

    def test_youtube_channels_are_in_the_m3u_tuner_playlist(self):
        body = self.client.get("/api/live/playlist.m3u").text
        self.assertIn("IPTV One", body)
        self.assertIn("Sports Channel", body,
                      "a Live TV channel the HDHomeRun lineup carries is missing "
                      "from the M3U tuner playlist")

    def test_the_m3u_entry_points_at_the_ts_endpoint(self):
        body = self.client.get("/api/live/playlist.m3u").text
        self.assertIn(f"/api/youtube/live/{self.channel.id}/stream.ts", body)


class TestFfmpegStderrDoesNotStallTheStream(_Base):
    """NEW at 9aa3977 — stderr is piped but only drained after stdout ends."""

    def test_a_chatty_ffmpeg_does_not_block_the_stream(self):
        # 512 KiB of stderr: eight times a 64 KiB pipe buffer. A live stream
        # carried over hours emits far more than this at -loglevel error.
        # The fake exits once it has written, so a request that never returns
        # is a stall and nothing else.
        ffmpeg = _fake_ffmpeg(self.tmp, stderr_bytes=512 * 1024)
        got = {}

        def _pull():
            try:
                r = self.client.get(f"/api/youtube/live/{self.channel.id}/stream.ts")
                got["body"] = r.content
            except BaseException as e:      # noqa: BLE001 - recorded, not raised
                got["error"] = e

        with mock.patch.object(youtube_router.resolver, "pick_tracks",
                               return_value=("https://r1.example.invalid/v.m3u8", None,
                                             {"User-Agent": "Mozilla/5.0"})), \
             mock.patch("shutil.which", return_value=ffmpeg):
            worker = threading.Thread(target=_pull, daemon=True)
            worker.start()
            worker.join(DEADLINE)

        self.assertFalse(
            worker.is_alive(),
            f"no MPEG-TS after {DEADLINE}s: ffmpeg filled the 64 KiB stderr pipe "
            f"that nobody reads until the stdout loop ends, so it can no longer "
            f"write stdout, and the stdout loop never ends — Live TV hangs with "
            f"no error logged anywhere",
        )
        self.assertIsNone(got.get("error"), got.get("error"))
        self.assertEqual(0x47, got.get("body", b"\x00")[0])


class TestMissingFfmpegIsReported(_Base):
    """NEW at 9aa3977 — Live TV now depends on a binary nothing checks for.

    The image installs ffmpeg, so this is about the failure mode rather than
    the common case: 8178cf7's /diagnose walks the whole chain and checks
    yt-dlp, the mount, the settings, the index, the files, Jellyfin and the
    home rows — but not ffmpeg, which every Live TV tune now goes through.
    """

    def test_a_missing_ffmpeg_is_not_an_unhandled_error(self):
        with mock.patch.object(youtube_router.resolver, "pick_tracks",
                               return_value=("https://r1.example.invalid/v.m3u8", None, {})), \
             mock.patch("shutil.which", return_value=None):
            try:
                resp = self.client.get(
                    f"/api/youtube/live/{self.channel.id}/stream.ts")
            except FileNotFoundError as e:
                self.fail(f"FileNotFoundError out of the handler: {e}")
        self.assertEqual(
            503, resp.status_code,
            "a missing ffmpeg should be a reported 503, not a traceback",
        )


class TestHeadProbeIsCheap(_Base):
    """NEW at 9aa3977 — HEAD shares the GET handler, so it costs an extraction.

    Extraction is the rate-limited resource the rest of the feature is paced
    around (DETAIL_SPACING_SECONDS = 5s, BLOCK_BACKOFF_HOURS = 6). The IPTV
    tuner's HEAD handler answers from headers alone.
    """

    def test_head_does_not_run_an_extraction(self):
        calls = []

        def _pick(video_id, max_height=1080):
            calls.append(video_id)
            return ("https://r1.example.invalid/v.m3u8", None, {})

        ffmpeg = _fake_ffmpeg(self.tmp)
        with mock.patch.object(youtube_router.resolver, "pick_tracks", _pick), \
             mock.patch("shutil.which", return_value=ffmpeg):
            resp = self.client.head(f"/api/youtube/live/{self.channel.id}/stream.ts")

        self.assertEqual(200, resp.status_code)
        self.assertEqual("video/mp2t", resp.headers.get("content-type", "").split(";")[0])
        self.assertEqual(
            [], calls,
            "a HEAD probe ran a yt-dlp extraction; the tuner probes before every "
            "tune, and extraction is rate-limited at roughly 300/hour",
        )


if __name__ == "__main__":
    unittest.main()

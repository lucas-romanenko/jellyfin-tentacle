"""Save offline keeps going with the page closed, and a phone gets a smaller
copy (26 Nov 2026, Lucas: "No way to have it keep going in background
either?"; "Any way we can automatically tell if users triggers save offline
from phone and if they do, do a smaller copy").

Run from the tentacle/ directory:  python -m unittest discover -s tests

- The server makes the copy first (services/offline_saves.py): a provider's
  title always, a file on the server only when the device can't take it as
  it is; one row per person, title and kind of device; ready → the row says
  so, the person gets Tentacle's message (Jellyfin shows it; the TV app
  doesn't ask for it) and a push message on the device that asked; a show's
  episodes share one message; a copy someone still wants is never stopped;
  removed before it was ready → the making stops.
- A phone's copy: at most 1080p either way round, HEVC where the browser
  plays it (HDR kept), else H.264 with HDR tone-mapped, about 6 / 4 Mb/s at
  1080p and never more than the source; a picture already small enough is
  copied; sound AAC stereo. On the GPU (VAAPI) when there is one.
- Web Push (services/web_push.py): only to a browser's push service, the
  message encrypted for that browser (RFC 8291, checked by decrypting it),
  signed with the server's key (RFC 8292, checked), a gone subscription
  removed.
"""
import base64
import json
import os
import shutil
import subprocess
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir

ITEM = "0123456789abcdef0123456789abcdef"


def _db(test):
    import models.database as mdb
    engine = create_engine(f"sqlite:///{temp_dir(test)}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _probe(w=3840, h=2160, codec="hevc", transfer=None, vbps=None, audio="eac3", channels=6, fbps=None):
    v = {"index": 0, "codec_type": "video", "codec_name": codec, "width": w, "height": h,
         "disposition": {"default": 1}}
    if transfer:
        v["color_transfer"] = transfer
    if vbps:
        v["bit_rate"] = str(vbps)
    a = {"index": 1, "codec_type": "audio", "codec_name": audio, "channels": channels,
         "disposition": {"default": 1}, "bit_rate": "640000"}
    fmt = {"format_name": "matroska,webm", "duration": "6705.0"}
    if fbps:
        fmt["bit_rate"] = str(fbps)
    return {"streams": [v, a], "format": fmt}


class APhonesCopy(unittest.TestCase):
    def setUp(self):
        from services import offline_convert as C
        self.C = C

    def test_a_4k_hdr_film_becomes_1080p_hevc_keeping_hdr_where_hevc_plays(self):
        p = self.C.plan(_probe(transfer="smpte2084", vbps=40_000_000), frozenset({"hevc"}), phone=True)
        self.assertEqual(("encode", "hevc", 1920, 1080, 4_000_000, True, False),
                         (p["video"], p["codec"], p["width"], p["height"], p["bps"], p["hdr"], p["tonemap"]))
        self.assertEqual("aac", p["audio"])

    def test_without_hevc_it_is_h264_with_hdr_made_normal_colours(self):
        p = self.C.plan(_probe(transfer="smpte2084", vbps=40_000_000), frozenset(), phone=True)
        self.assertEqual(("h264", 6_000_000, False, True), (p["codec"], p["bps"], p["hdr"], p["tonemap"]))

    def test_a_1080p_film_small_enough_is_copied_and_a_big_one_made_smaller(self):
        small = self.C.plan(_probe(1920, 1080, "h264", vbps=5_000_000, audio="aac", channels=2), frozenset(), phone=True)
        self.assertEqual(("copy", "copy"), (small["video"], small["audio"]))
        big = self.C.plan(_probe(1920, 1080, "h264", vbps=15_000_000), frozenset(), phone=True)
        self.assertEqual(("encode", 1920, 1080, 6_000_000), (big["video"], big["width"], big["height"], big["bps"]))

    def test_the_rate_follows_the_picture_and_never_tops_the_source(self):
        p = self.C.plan(_probe(1280, 720, "hevc", vbps=8_000_000), frozenset(), phone=True)
        self.assertEqual(("h264", 1280, 720), (p["codec"], p["width"], p["height"]))
        self.assertEqual(2_666_666, p["bps"])                   # 6 Mb/s for 1080p, by the picture's size
        low = self.C.plan(_probe(1280, 720, "hevc", vbps=2_000_000), frozenset(), phone=True)
        self.assertEqual(1_800_000, low["bps"])                 # 90% of the source's 2 Mb/s
        self.assertEqual((1080, 1920), self.C.phone_size(2160, 3840))   # upright keeps its shape
        self.assertEqual((1920, 800), self.C.phone_size(3840, 1600))    # scope keeps its shape

    def test_no_bit_rate_of_its_own_comes_from_the_whole_files(self):
        p = self.C.plan(_probe(1920, 1080, "h264", fbps=20_000_000, audio="aac", channels=2), frozenset(), phone=True)
        self.assertEqual("encode", p["video"])                    # ~19 Mb/s picture: too big for a phone

    def test_surround_aac_becomes_stereo_on_a_phone_and_a_computer_keeps_it(self):
        self.assertEqual("aac", self.C.plan(_probe(1280, 720, "h264", vbps=2_000_000, audio="aac", channels=6),
                                            frozenset(), phone=True)["audio"])
        self.assertEqual("copy", self.C.plan(_probe(1280, 720, "h264", vbps=2_000_000, audio="aac", channels=6),
                                             frozenset())["audio"])

    def test_a_computer_still_gets_the_picture_copied(self):
        p = self.C.plan(_probe(transfer="smpte2084"), frozenset({"hevc"}))
        self.assertEqual(("copy", False), (p["video"], p["phone"]))

    def test_its_size_is_known_before_it_is_made(self):
        p = self.C.plan(_probe(transfer="smpte2084", vbps=40_000_000), frozenset({"hevc"}), phone=True)
        est = self.C.output_estimate(_probe(transfer="smpte2084", vbps=40_000_000), p)
        self.assertAlmostEqual((4_000_000 + 160_000) * 6705 / 8, est, delta=est * 0.03)

    def test_its_own_file_name(self):
        self.assertEqual(f"{ITEM}-phone-hevc", self.C.job_key(ITEM, frozenset({"hevc"}), True))
        self.assertEqual(f"{ITEM}-base", self.C.job_key(ITEM, frozenset()))


def _dovi(w=3840, h=2160, profile=5, compat=0):
    probe = _probe(w, h, "hevc", vbps=20_000_000)
    probe["streams"][0]["pix_fmt"] = "yuv420p10le"
    probe["streams"][0]["side_data_list"] = [{"side_data_type": "DOVI configuration record", "dv_profile": profile,
                                              "dv_bl_signal_compatibility_id": compat}]
    return probe


class DolbyVisionOnly(unittest.TestCase):
    """Madden (2026) is Dolby Vision profile 5: decoded as plain HEVC it is green
    and purple. Its colours come right only through its Dolby Vision data."""

    def setUp(self):
        from services import offline_convert as C
        self.C = C

    def test_profile_5_is_told_from_8(self):
        self.assertTrue(self.C.dolby_vision_only(_dovi()["streams"][0]))
        self.assertFalse(self.C.dolby_vision_only(_dovi(profile=8, compat=1)["streams"][0]))
        self.assertFalse(self.C.dolby_vision_only(_probe()["streams"][0]))

    def test_always_converted_even_for_a_computer_that_plays_hevc(self):
        pc = self.C.plan(_dovi(), frozenset({"hevc"}))
        self.assertEqual(("encode", "hevc", 3840, 2160, True), (pc["video"], pc["codec"], pc["width"], pc["height"], pc["hdr"]))
        phone = self.C.plan(_dovi(), frozenset(), phone=True)
        self.assertEqual(("encode", "h264", 1920, 1080, True, False), (phone["video"], phone["codec"], phone["width"],
                                                                      phone["height"], phone["tonemap"], phone["hdr"]))

    def test_libplacebo_applies_it_on_the_gpu(self):
        p = self.C.plan(_dovi(), frozenset({"hevc"}), phone=True)
        with mock.patch.object(self.C, "placebo_ok", True):
            a = " ".join(self.C.ffmpeg_args(p, "http://jf/x", "t", "/o.mp4", "vaapi"))
        for part in ("-init_hw_device vaapi=va:/dev/dri/renderD128 -init_hw_device vulkan=vk@va", "-hwaccel vaapi -hwaccel_device va",
                     "libplacebo=w=1920:h=1080:format=p010:colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084"
                     ":range=tv:apply_dolbyvision=1,hwupload", "-c:v hevc_vaapi", "-profile:v main10", "-color_trc smpte2084"):
            self.assertIn(part, a)
        p = self.C.plan(_dovi(), frozenset(), phone=True)
        with mock.patch.object(self.C, "placebo_ok", True):
            a = " ".join(self.C.ffmpeg_args(p, "http://jf/x", "t", "/o.mp4", "x264"))
        self.assertIn("libplacebo=w=1920:h=1080:format=yuv420p:colorspace=bt709", a)
        self.assertIn("-c:v libx264", a)

    def test_a_local_dolby_vision_file_is_never_taken_as_it_is(self):
        from services import offline_saves as S
        item = _item(path="/media/movies/F/F.mp4", container="mp4", w=1920, h=1080, bps=5_000_000, vcodec="h264", acodec="aac")
        self.assertTrue(S.as_is(item, False, frozenset()))
        item["MediaSources"][0]["MediaStreams"][0]["DvProfile"] = 5
        self.assertFalse(S.as_is(item, False, frozenset()))


class OnTheGpu(unittest.TestCase):
    def setUp(self):
        from services import offline_convert as C
        self.C = C

    def args(self, probe, caps=frozenset(), enc="vaapi"):
        p = self.C.plan(probe, caps, phone=True)
        return " ".join(self.C.ffmpeg_args(p, "http://jf/Items/x/Download", "t", "/out.mp4", enc))

    def test_hdr_hevc_is_decoded_scaled_and_encoded_on_the_gpu_keeping_hdr(self):
        a = self.args(_probe(transfer="smpte2084", vbps=40_000_000), frozenset({"hevc"}))
        for part in ("-hwaccel vaapi", "-hwaccel_output_format vaapi", "scale_vaapi=w=1920:h=1080:format=p010",
                     "-c:v hevc_vaapi", "-rc_mode VBR -b:v 4000000", "-tag:v hvc1", "-profile:v main10",
                     "-color_trc smpte2084", "-c:a aac -b:a 160k -ac 2"):
            self.assertIn(part, a)
        self.assertNotIn("tonemap", a)

    def test_hdr_to_h264_is_tone_mapped_on_the_gpu(self):
        a = self.args(_probe(transfer="smpte2084", vbps=40_000_000))
        self.assertIn("scale_vaapi=w=1920:h=1080,tonemap_vaapi=format=nv12:t=bt709:m=bt709:p=bt709", a)
        self.assertIn("-c:v h264_vaapi", a)
        self.assertNotIn("-color_trc", a)          # set by tonemap_vaapi; here it would break the GPU chain

    def test_a_codec_the_gpu_cant_read_is_read_on_the_cpu_and_uploaded(self):
        a = self.args(_probe(1920, 1080, "mpeg4"))
        self.assertIn("-vaapi_device /dev/dri/renderD128", a)
        self.assertIn("-vf format=nv12,hwupload,scale_vaapi=w=1920:h=1080:format=nv12", a)
        self.assertNotIn("-hwaccel ", a)

    def test_no_gpu_means_x264_at_the_same_size(self):
        a = self.args(_probe(transfer="smpte2084", vbps=40_000_000), enc="x264")
        self.assertIn("-vf scale=1920:1080,format=yuv420p -c:v libx264", a)
        self.assertIn("-b:v 6000000", a)

    def test_a_computers_copy_is_unchanged(self):
        p = self.C.plan({"streams": [{"index": 0, "codec_type": "video", "codec_name": "mpeg4"}],
                         "format": {"format_name": "avi"}}, frozenset())
        a = " ".join(self.C.ffmpeg_args(p, "u", "t", "/o.mp4", "vaapi"))
        self.assertIn("-vaapi_device /dev/dri/renderD128", a)
        self.assertIn("-vf format=nv12,hwupload -c:v h264_vaapi -qp 22", a)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg (in the image)")
class APhoneCopyMadeForReal(unittest.TestCase):
    """A 1440p MKV with 5.1 sound, made into a phone's copy by a real ffmpeg
    (no GPU here: x264), read from a file the way Jellyfin's Download is."""

    def test_it_is_1080p_h264_with_stereo_aac(self):
        import asyncio
        from services import offline_convert as C
        d = temp_dir(self)
        src = os.path.join(d, "film.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=2560x1440:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3",
                        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "8M",
                        "-c:a", "ac3", "-ac", "6", src], check=True)
        out = os.path.join(d, "phone.mp4")
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                          "-show_format", src], capture_output=True, check=True).stdout)
        p = C.plan(info, frozenset(), phone=True)
        args = C.ffmpeg_args(p, src, "t", out, "x264")
        args = [a for a in args if a not in ("-headers", "X-Emby-Token: t\r\n")]
        for flag in ("-reconnect", "-reconnect_streamed", "-reconnect_on_network_error", "-reconnect_delay_max"):
            i = args.index(flag)
            del args[i:i + 2]
        subprocess.run(args, check=True, capture_output=True)
        got = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", out],
                                        capture_output=True, check=True).stdout)["streams"]
        self.assertEqual([("h264", 1920, 1080), ("aac", 2)],
                         [(got[0]["codec_name"], got[0]["width"], got[0]["height"]),
                          (got[1]["codec_name"], got[1]["channels"])])
        del asyncio


# ── The server's part first ──────────────────────────────────────────────────

def _item(path="http://tentacle/api/vod/movie/1.2.abc.mkv", container="strm", w=3840, h=2160, bps=40_000_000,
          vcodec="hevc", acodec="eac3", **kw):
    item = {"Id": ITEM, "Name": "Madden", "ProductionYear": 2026, "Type": "Movie", "MediaType": "Video",
            "RunTimeTicks": 67_050_000_000, "MediaSources": [{
                "Path": path, "Container": container, "Size": 27 << 30,
                "MediaStreams": [{"Type": "Video", "Codec": vcodec, "Width": w, "Height": h, "BitRate": bps},
                                 {"Type": "Audio", "Codec": acodec}]}]}
    item.update(kw)
    return item


class WhatTheDeviceTakesAsItIs(unittest.TestCase):
    def setUp(self):
        from services import offline_saves as S
        self.S = S

    def test_a_providers_title_never(self):
        mp4 = _item(path="http://tentacle/api/vod/movie/1.2.abc.mp4", w=1280, h=720, bps=2_000_000,
                    vcodec="h264", acodec="aac")
        self.assertFalse(self.S.as_is(mp4, False, frozenset()))

    def test_a_file_on_the_server_its_browser_plays(self):
        local = _item(path="/media/movies/Film/Film.mp4", container="mp4", w=1920, h=1080, bps=5_000_000,
                      vcodec="h264", acodec="aac")
        self.assertTrue(self.S.as_is(local, False, frozenset()))
        self.assertTrue(self.S.as_is(local, True, frozenset()))

    def test_a_4k_file_goes_as_it_is_to_a_computer_not_to_a_phone(self):
        local = _item(path="/media/movies/Film/Film.mp4", container="mp4", vcodec="h264", acodec="aac")
        self.assertTrue(self.S.as_is(local, False, frozenset()))
        self.assertFalse(self.S.as_is(local, True, frozenset()))

    def test_an_mkv_on_the_server_never(self):
        mkv = _item(path="/media/movies/Film/Film.mkv", container="mkv", w=1920, h=1080, vcodec="h264", acodec="aac")
        self.assertFalse(self.S.as_is(mkv, False, frozenset()))

    def test_how_the_message_names_it(self):
        self.assertEqual("Madden (2026)", self.S.label(_item()))
        ep = {"Type": "Episode", "SeriesName": "Bluey", "Name": "Magic Xylophone", "ParentIndexNumber": 1,
              "IndexNumber": 2}
        self.assertEqual("Bluey S1E2", self.S.label(ep))


class _Saves(unittest.TestCase):
    def setUp(self):
        from models.database import TentacleUser
        from services import offline_convert as C
        from services import offline_saves as S
        self.C, self.S = C, S
        self.Session = _db(self)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        p = mock.patch.object(S, "SessionLocal", self.Session)
        p.start()
        self.addCleanup(p.stop)
        self.dir = temp_dir(self)
        env = mock.patch.dict(os.environ, {"OFFLINE_READY_DIR": os.path.join(self.dir, "ready")})
        env.start()
        self.addCleanup(env.stop)
        C.forget_all()
        self.addCleanup(C.forget_all)
        S._forget_wanted()
        self.me = TentacleUser(jellyfin_user_id="aa" * 16, display_name="Roman")
        self.db.add(self.me)
        self.db.commit()
        self.items = {ITEM: _item()}
        p = mock.patch.object(S.offline, "check", lambda db, user, iid, **kw: self.items[iid.replace("-", "")])
        p.start()
        self.addCleanup(p.stop)
        self.pushed = []
        p = mock.patch("services.web_push.send", lambda db, uid, msg, endpoint=None: self.pushed.append((uid, msg, endpoint)) or 1)
        p.start()
        self.addCleanup(p.stop)
        self.started = []
        p = mock.patch.object(S, "start_job", self._start_job)
        p.start()
        self.addCleanup(p.stop)

    def _start_job(self, db, item, phone, caps, who):
        key = self.C.job_key(item["Id"], caps, phone)
        job = self.C._jobs.get(key)
        if job is None:
            job = self.C.Job(key, item["Id"], 0, phone)
            self.C._jobs[key] = job
            self.started.append(key)
        return job

    def finish(self, key, size=1234):
        job = self.C._jobs[key]
        job.done, job.size = True, size
        self.S._finished_rows(key, True, size, "")


class TheServerGetsItReadyFirst(_Saves):
    def test_save_offline_on_a_phone_starts_the_phones_copy_and_remembers_who_asked(self):
        got = self.S.request(self.db, self.me, ITEM, True, frozenset({"hevc"}), "https://fcm.googleapis.com/x")
        self.assertEqual("preparing", got["state"])
        self.assertEqual([f"{ITEM}-phone-hevc"], self.started)
        from models.database import OfflineSave
        row = self.db.query(OfflineSave).one()
        self.assertEqual(("phone", "hevc", "preparing", "Madden (2026)"), (row.device, row.caps, row.state, row.name))
        self.assertIn(f"{ITEM}-phone-hevc", self.S.wanted_keys())

    def test_ready_tells_them_once_in_jellyfin_and_on_the_device_that_asked(self):
        self.S.request(self.db, self.me, ITEM, True, frozenset(), "https://fcm.googleapis.com/x")
        self.finish(f"{ITEM}-phone-base", 4 << 30)
        from models.database import Notification, OfflineSave
        self.db.expire_all()
        row = self.db.query(OfflineSave).one()
        self.assertEqual(("ready", 4 << 30), (row.state, row.size))
        note = self.db.query(Notification).one()
        self.assertEqual(("offline", "Madden (2026) is ready to save"), (note.kind, note.message))
        self.assertEqual(1, len(self.pushed))
        uid, msg, endpoint = self.pushed[0]
        self.assertEqual((self.me.id, "https://fcm.googleapis.com/x", ITEM, 4 << 30),
                         (uid, endpoint, msg["save"]["id"], msg["save"]["size"]))
        self.assertEqual([{"id": ITEM, "state": "ready", "size": 4 << 30}],
                         self.S.status(self.db, self.me, [ITEM], True, frozenset()))
        self.S._finished_rows(f"{ITEM}-phone-base", True, 4 << 30, "")      # said once only
        self.assertEqual(1, len(self.pushed))

    def test_a_shows_episodes_share_one_message(self):
        eps = []
        for n in (1, 2):
            iid = f"{n:032x}"
            eps.append(iid)
            self.items[iid] = {"Id": iid, "Type": "Episode", "SeriesName": "Bluey", "Name": f"Ep {n}",
                               "ParentIndexNumber": 1, "IndexNumber": n, "MediaType": "Video",
                               "MediaSources": [{"Path": "http://t/api/vod/series/1.2.x.mkv", "Container": "strm"}]}
            self.S.request(self.db, self.me, iid, True, frozenset())
        for iid in eps:
            self.finish(f"{iid}-phone-base")
        from models.database import Notification
        self.db.expire_all()
        notes = self.db.query(Notification).all()
        self.assertEqual(["2 episodes of Bluey are ready to save"], [n.message for n in notes])
        self.assertEqual("offline-Bluey", self.pushed[-1][1]["tag"])

    def test_one_made_before_is_ready_at_once_with_nothing_to_tell(self):
        self.S.request(self.db, self.me, ITEM, False, frozenset())
        self.C._jobs[f"{ITEM}-base"].done = True
        self.C._jobs[f"{ITEM}-base"].size = 99
        got = self.S.request(self.db, self.me, ITEM, False, frozenset())
        self.assertEqual({"id": ITEM, "state": "ready", "size": 99}, got)
        self.assertEqual([], self.pushed)

    def test_a_file_the_device_takes_as_it_is_needs_nothing_from_the_server(self):
        self.items[ITEM] = _item(path="/media/movies/F/F.mp4", container="mp4", w=1920, h=1080, bps=5_000_000,
                                 vcodec="h264", acodec="aac")
        self.assertEqual({"id": ITEM, "state": "ready", "direct": True},
                         self.S.request(self.db, self.me, ITEM, True, frozenset()))
        self.assertEqual([], self.started)

    def test_the_device_has_it_now_so_the_server_lets_go(self):
        self.S.request(self.db, self.me, ITEM, True, frozenset())
        self.finish(f"{ITEM}-phone-base")
        self.assertTrue(self.S.forget(self.db, self.me, ITEM, True, frozenset()))
        self.assertNotIn(f"{ITEM}-phone-base", self.S.wanted_keys())

    def test_removed_before_it_was_ready_stops_the_making(self):
        self.S.request(self.db, self.me, ITEM, True, frozenset())
        with mock.patch.object(self.C, "stop") as stop:
            self.S.forget(self.db, self.me, ITEM, True, frozenset())
        stop.assert_called_once()

    def test_a_copy_someone_waits_for_is_never_stopped_for_want_of_readers(self):
        self.S.request(self.db, self.me, ITEM, False, frozenset())
        self.C.wanted = self.S.wanted_keys
        self.addCleanup(setattr, self.C, "wanted", lambda: set())
        job = self.C._jobs[f"{ITEM}-base"]
        self.C.sweep(time.monotonic() + 10 * self.C.IDLE_STOP_S)
        self.assertIn(job.key, self.C._jobs)
        self.S.forget(self.db, self.me, ITEM, False, frozenset())
        self.C.sweep(time.monotonic() + 10 * self.C.IDLE_STOP_S)
        self.assertNotIn(job.key, self.C._jobs)

    def test_a_ready_copy_nobody_came_for_is_let_go_after_days(self):
        self.S.request(self.db, self.me, ITEM, False, frozenset())
        self.finish(f"{ITEM}-base")
        from models.database import OfflineSave
        self.assertEqual(0, self.S.sweep())
        self.db.query(OfflineSave).update({"ready_at": datetime.utcnow() - timedelta(days=4)})
        self.db.commit()
        self.assertEqual(1, self.S.sweep())

    def test_failed_is_said_in_words(self):
        self.S.request(self.db, self.me, ITEM, False, frozenset())
        self.C._jobs[f"{ITEM}-base"].error = self.C.BROKE
        self.S._finished_rows(f"{ITEM}-base", False, 0, self.C.BROKE)
        self.assertEqual("failed", self.S.status(self.db, self.me, [ITEM], False, frozenset())[0]["state"])
        self.assertEqual([], self.pushed)

    def test_someone_elses_titles_are_not_shown(self):
        from models.database import TentacleUser
        other = TentacleUser(jellyfin_user_id="bb" * 16, display_name="Ben")
        self.db.add(other)
        self.db.commit()
        self.S.request(self.db, self.me, ITEM, False, frozenset())
        self.C._jobs.clear()
        self.assertEqual([{"id": ITEM, "state": "unknown"}], self.S.status(self.db, other, [ITEM], False, frozenset()))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg (in the image)")
class AMakingThatGetsStuck(unittest.TestCase):
    def C_BROKE(self):
        from services import offline_convert as C
        return C.BROKE

    """A read that goes silent (ffmpeg waits for ever) with nobody watching:
    the making is started again; held back for a viewer, it waits."""

    def run_with(self, watching, stall_s=0.3):
        import asyncio
        from services import offline_convert as C
        d = temp_dir(self)
        job = C.Job(f"{ITEM}-base", ITEM, 0)
        job.path = __import__("pathlib").Path(d) / "x.mp4"
        runs = []

        async def fake_ffmpeg(job, p, url, token, label, enc):
            runs.append(1)
            # a "process" that never ends by itself, writes nothing
            job.proc = await asyncio.create_subprocess_exec("sleep", "30")
            watchdog = asyncio.get_running_loop().create_task(C._watch_stall(job))
            await job.proc.wait()
            watchdog.cancel()

        async def go():
            with mock.patch.object(C, "_ffmpeg", fake_ffmpeg), mock.patch.object(C, "STALL_S", stall_s), \
                    mock.patch.object(C, "_someone_watching", lambda: watching):
                try:
                    await asyncio.wait_for(C._ffmpeg_until_made(job, {}, "u", "t", "Roman", "x264"), 3)
                except asyncio.TimeoutError:
                    job.proc.kill()
        asyncio.run(go())
        return runs, job

    def test_stuck_with_nobody_watching_is_started_again_then_given_up(self):
        runs, job = self.run_with(False)
        self.assertEqual(1 + 3, len(runs))
        self.assertTrue(job.stalled)
        self.assertEqual(self.C_BROKE(), job.error)         # said plainly, the page offers Try again

    def test_held_back_for_a_viewer_it_just_waits(self):
        runs, job = self.run_with(True)
        self.assertEqual(1, len(runs))
        self.assertFalse(job.stalled)


class StartedFromARoutesThread(unittest.TestCase):
    def test_the_making_runs_on_tentacles_own_loop(self):
        import asyncio
        import threading
        from services import offline_convert as C
        ran = []

        async def work():
            ran.append(threading.current_thread().name)

        async def main():
            C.set_loop(asyncio.get_running_loop())
            fut = await asyncio.to_thread(C._spawn, work())     # as a sync route's thread does
            for _ in range(50):
                if fut.done():
                    break
                await asyncio.sleep(0.01)
            return fut.done()
        self.assertTrue(asyncio.run(main()))
        self.assertEqual([threading.main_thread().name], ran)
        C.set_loop(None)


class OnlyBrowsersAskForTheMessage(_Saves):
    def test_the_tv_app_never_gets_ready_to_save(self):
        from models.database import Notification
        from routers import notifications as R
        self.db.add(Notification(user_id=self.me.id, tmdb_id=0, title="Madden", message="Madden is ready to save",
                                 kind="offline", jellyfin_item_id=ITEM))
        self.db.add(Notification(user_id=self.me.id, tmdb_id=5, title="Dune", message="Dune is ready", kind="download"))
        self.db.commit()
        with mock.patch("services.family.addresses", lambda db: {"tentacle": "https://tentacle.example"}):
            tv = R.get_notifications(offline=False, db=self.db, user=self.me)
            web = R.get_notifications(offline=True, db=self.db, user=self.me)
        self.assertEqual(["download"], [n["kind"] for n in tv["notifications"]])
        self.assertEqual({"download", "offline"}, {n["kind"] for n in web["notifications"]})
        offline = next(n for n in web["notifications"] if n["kind"] == "offline")
        self.assertEqual("https://tentacle.example/#offline", offline["url"])


# ── Web Push ─────────────────────────────────────────────────────────────────

def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class _Browser:
    """A browser's side of a push subscription: its keys, and decrypting."""

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                         serialization.PublicFormat.UncompressedPoint)
        self.auth = os.urandom(16)
        self.p256dh = base64.urlsafe_b64encode(self.public).rstrip(b"=").decode()
        self.auth_b64 = base64.urlsafe_b64encode(self.auth).rstrip(b"=").decode()

    def decrypt(self, body):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        salt, idlen = body[:16], body[20]
        as_public = body[21:21 + idlen]
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
        ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=self.auth,
                   info=b"WebPush: info\x00" + self.public + as_public).derive(shared)
        cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
        plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
        return plain.rstrip(b"\x00")[:-1]


class WebPush(unittest.TestCase):
    def setUp(self):
        from models.database import TentacleUser
        from services import web_push as W
        self.W = W
        self.db = _db(self)()
        self.addCleanup(self.db.close)
        self.me = TentacleUser(jellyfin_user_id="aa" * 16, display_name="Roman")
        self.db.add(self.me)
        self.db.commit()
        p = mock.patch("services.family.addresses", lambda db: {"tentacle": "https://tentacle.example"})
        p.start()
        self.addCleanup(p.stop)

    def test_only_a_browsers_push_service(self):
        ok = self.W.allowed_endpoint
        self.assertTrue(ok("https://fcm.googleapis.com/fcm/send/abc"))
        self.assertTrue(ok("https://web.push.apple.com/QGx"))
        self.assertTrue(ok("https://updates.push.services.mozilla.com/wpush/v2/x"))
        self.assertTrue(ok("https://wns2-bl2p.notify.windows.com/w/?token=x"))
        for bad in ("http://fcm.googleapis.com/x", "https://fcm.googleapis.com.evil.example/x",
                    "https://192.168.2.77/x", "https://user@fcm.googleapis.com/x", "file:///etc/passwd", ""):
            self.assertFalse(ok(bad), bad)
        with self.assertRaises(ValueError):
            self.W.subscribe(self.db, self.me.id, "https://router.lan/x", "a", "b")

    def test_the_key_is_made_once_and_kept_as_a_secret(self):
        from models.database import get_setting
        from routers.settings import NEVER_SERVED, SENSITIVE_KEYS
        a = self.W.public_key(self.db)
        self.assertEqual(a, self.W.public_key(self.db))
        self.assertEqual(65, len(_b64d(a)))
        self.assertIn("PRIVATE KEY", get_setting(self.db, self.W.KEY_SETTING))
        self.assertIn(self.W.KEY_SETTING, NEVER_SERVED & SENSITIVE_KEYS)

    def test_the_message_is_for_that_browser_only_and_signed_by_the_server(self):
        browser = _Browser()
        endpoint = "https://fcm.googleapis.com/fcm/send/abc"
        self.W.subscribe(self.db, self.me.id, endpoint, browser.p256dh, browser.auth_b64)
        sent = []

        def post(url, data=None, headers=None, timeout=None):
            sent.append((url, data, headers))
            return mock.Mock(status_code=201, text="")
        with mock.patch.object(self.W.requests, "post", post):
            self.assertEqual(1, self.W.send(self.db, self.me.id, {"title": "Madden is ready to save"}))
        url, body, headers = sent[0]
        self.assertEqual(endpoint, url)
        self.assertEqual("aes128gcm", headers["Content-Encoding"])
        self.assertEqual({"title": "Madden is ready to save"}, json.loads(browser.decrypt(body)))
        # The signature checks out with the server's public key, for that push service
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        t = headers["Authorization"].split("t=")[1].split(",")[0]
        k = headers["Authorization"].split("k=")[1]
        head, claims, sig = t.split(".")
        raw = _b64d(sig)
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _b64d(k))
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        c = json.loads(_b64d(claims))
        self.assertEqual(("https://fcm.googleapis.com", "https://tentacle.example"), (c["aud"], c["sub"]))
        self.assertEqual(k, self.W.public_key(self.db))

    def test_a_gone_browser_is_forgotten(self):
        from models.database import PushSubscription
        b = _Browser()
        self.W.subscribe(self.db, self.me.id, "https://fcm.googleapis.com/fcm/send/gone", b.p256dh, b.auth_b64)
        with mock.patch.object(self.W.requests, "post", lambda *a, **k: mock.Mock(status_code=410, text="")):
            self.assertEqual(0, self.W.send(self.db, self.me.id, {"title": "x"}))
        self.assertEqual(0, self.db.query(PushSubscription).count())

    def test_only_the_device_that_asked(self):
        a, b = _Browser(), _Browser()
        self.W.subscribe(self.db, self.me.id, "https://fcm.googleapis.com/fcm/send/phone", a.p256dh, a.auth_b64)
        self.W.subscribe(self.db, self.me.id, "https://fcm.googleapis.com/fcm/send/pc", b.p256dh, b.auth_b64)
        sent = []
        with mock.patch.object(self.W.requests, "post",
                               lambda url, **k: sent.append(url) or mock.Mock(status_code=201, text="")):
            self.W.send(self.db, self.me.id, {"title": "x"}, endpoint="https://fcm.googleapis.com/fcm/send/phone")
            self.W.send(self.db, self.me.id, {"title": "x"})
        self.assertEqual(["https://fcm.googleapis.com/fcm/send/phone", "https://fcm.googleapis.com/fcm/send/phone",
                          "https://fcm.googleapis.com/fcm/send/pc"], sent)


if __name__ == "__main__":
    unittest.main()

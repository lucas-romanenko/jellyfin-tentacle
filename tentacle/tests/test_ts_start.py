"""A viewer's live stream starts on a keyframe that has its sound with it.

Run from the tentacle/ directory:  python -m unittest tests.test_ts_start

2026-10-24: TSN 2's encoder muxed its audio ~9 s ahead of the video. Every
client joining it got 9 s of picture with no sound first; Jellyfin's remux
wrote the first HLS segments without audio and Jellyfin Web (hls.js) froze at
~6 s, every time (bufferAppendError: no audio SourceBuffer). services/ts_start
holds a viewer's first bytes until a keyframe with sound, and for a stream
whose audio runs ahead sends each audio frame next to the video it plays with
(sent first as a block, Jellyfin's 3 s probe saw only audio and remuxed
without the video).
"""
import random
import unittest

from services import ts_start
from services.ts_start import AvStart

V, A = 0x100, 0x101
HZ = 90000


def _ts(t):
    t = int(t) % (1 << 33)
    return bytes([0x21 | (t >> 29 & 0x0E), t >> 22 & 0xFF, (t >> 14 & 0xFE) | 1,
                  t >> 7 & 0xFF, (t << 1 & 0xFE) | 1])


def _crc_less_section(table_id, body):
    n = len(body) + 5 + 4
    return bytes([table_id, 0xB0 | (n >> 8), n & 0xFF, 0, 1, 0xC1, 0, 0]) + body + b"\0\0\0\0"


def _packet(pid, payload, pusi=False, rai=False, cc=0):
    head = bytes([0x47, (0x40 if pusi else 0) | pid >> 8, pid & 0xFF])
    room = 184
    if rai or len(payload) < room:
        af_len = max(1, room - 1 - len(payload))
        stuffing = af_len - 1
        af = bytes([af_len, 0x40 if rai else 0]) + b"\xff" * stuffing
        payload = payload[:184 - len(af)]
        return head + bytes([0x30 | cc & 0xF]) + af + payload
    return head + bytes([0x10 | cc & 0xF]) + payload[:184]


def pat():
    return _packet(0, b"\0" + _crc_less_section(0, bytes([0, 1, 0xE0, 0x20])), pusi=True)


def pmt(audio_type=0x0F, with_audio=True):
    es = bytes([0x1B, 0xE1, 0x00, 0xF0, 0x00])
    if with_audio:
        es += bytes([audio_type, 0xE1, 0x01, 0xF0, 0x00])
    return _packet(0x20, b"\0" + _crc_less_section(2, bytes([0xE1, 0x00, 0xF0, 0x00]) + es), pusi=True)


def pes(stream_id, pts, dts=None):
    if dts is None:
        return b"\0\0\1" + bytes([stream_id, 0, 0, 0x80, 0x80, 5]) + _ts(pts)
    return b"\0\0\1" + bytes([stream_id, 0, 0, 0x80, 0xC0, 10]) + _ts(pts) + _ts(dts)


def frame(pid, sid, pts, packets, key=False, tag=b""):
    """A PES of `packets` packets; its payload names it (pid, pts) to find it again."""
    name = tag or b"%d:%d;" % (pid, pts)
    out = [_packet(pid, pes(sid, pts) + name, pusi=True, rai=key)]
    for k in range(1, packets):
        out.append(_packet(pid, name + b"+%d" % k, cc=k))
    return out


def stream(seconds=30.0, skew=9.0, v0=1000.0, gop=1.0, fps=25, keyflags=True, audio=True):
    """A TS whose audio at each byte position plays `skew` s after its video."""
    out = [pat(), pmt(with_audio=audio)]
    a_next = v0 + skew
    t = v0
    step = 1.0 / fps
    n = 0
    while t < v0 + seconds:
        key = n % int(gop * fps) == 0
        out += frame(V, 0xE0, t * HZ, 6 if key else 3, key=key and keyflags)
        while audio and a_next < t + skew + step:
            out += frame(A, 0xC0, a_next * HZ, 1)
            a_next += 0.024
        if n % fps == 0:
            out += [pat(), pmt(with_audio=audio)]
        t += step
        n += 1
    return b"".join(out)


def packets(data):
    return [data[i:i + 188] for i in range(0, len(data) - 187, 188)]


def pes_starts(data):
    """(pid, pts in s) of every PES start, in order."""
    out = []
    for p in packets(data):
        pid = (p[1] & 0x1F) << 8 | p[2]
        if pid in (V, A) and p[1] & 0x40:
            i = 4 + (1 + p[4] if p[3] & 0x20 else 0)
            t = p[i + 9:i + 14]
            pts = ((t[0] >> 1 & 7) << 30 | t[1] << 22 | (t[2] >> 1) << 15 | t[3] << 7 | t[4] >> 1)
            out.append((pid, pts / HZ))
    return out


def run(data, piece=131072, rng=None):
    g = AvStart()
    out = bytearray()
    i = 0
    while i < len(data):
        n = rng.randint(1, 3 * piece) if rng else piece
        out += g.feed(data[i:i + n])
        i += n
    out += g.flush()
    return bytes(out), g


class SkewedStream(unittest.TestCase):
    """Audio 9 s ahead, as TSN 2 on 2026-10-24."""

    def setUp(self):
        self.src = stream(skew=9.0)
        self.out, self.g = run(self.src)

    def test_it_starts_with_the_tables_then_a_keyframe_that_has_sound(self):
        ps = packets(self.out)
        self.assertEqual(ps[0], pat())
        self.assertEqual(ps[1], pmt())
        first_v = next(t for pid, t in pes_starts(self.out) if pid == V)
        first_a = next(t for pid, t in pes_starts(self.out) if pid == A)
        self.assertGreaterEqual(first_v, 1009.0 - ts_start.START_TOLERANCE,
                                "it started on picture that has no sound")
        self.assertLessEqual(first_v, 1010.0, "it skipped more than it had to")
        self.assertAlmostEqual(first_a, first_v, delta=ts_start.START_TOLERANCE)
        v_pkt = next(p for p in ps if (p[1] & 0x1F) << 8 | p[2] == V)
        self.assertTrue(v_pkt[5] & 0x40, "the first video packet is not a keyframe")

    def test_every_audio_frame_goes_out_next_to_its_video(self):
        # Jellyfin probes 3 s: if the first seconds are audio only, it remuxes
        # the channel without its video (seen: audio-only HLS segments).
        last_v = None
        starts = pes_starts(self.out)
        last_video = max(i for i, (pid, _) in enumerate(starts) if pid == V)
        for pid, t in starts[:last_video]:      # the end of the stream flushes the rest
            if pid == V:
                last_v = t
            elif last_v is not None:
                self.assertLessEqual(t - last_v, ts_start.AUDIO_LEAD + 0.05,
                                     f"audio for {t:.2f}s went out with the video at {last_v:.2f}s")
        first = next(i for i, (pid, _) in enumerate(starts) if pid == V)
        before = [t for _, t in starts[:first]]
        self.assertLessEqual(max(before) - min(before), ts_start.START_TOLERANCE + ts_start.AUDIO_LEAD + 0.05,
                             "the stream opens with a block of audio")

    def test_nothing_from_the_start_on_is_lost_or_reordered(self):
        start = next(t for pid, t in pes_starts(self.out) if pid == V)
        src_a = [t for pid, t in pes_starts(self.src) if pid == A and t >= start - ts_start.START_TOLERANCE]
        out_a = [t for pid, t in pes_starts(self.out) if pid == A]
        self.assertEqual(out_a, src_a[len(src_a) - len(out_a):])
        self.assertGreaterEqual(out_a[0], start - ts_start.START_TOLERANCE)
        self.assertLessEqual(out_a[0], start + 0.05, "audio for the first seconds was dropped")
        src_v = [t for pid, t in pes_starts(self.src) if pid == V and t >= start]
        out_v = [t for pid, t in pes_starts(self.out) if pid == V]
        self.assertEqual(out_v, src_v)
        # Every packet of a kept frame, not only its start.
        src_pkts = [p for p in packets(self.src) if (p[1] & 0x1F) << 8 | p[2] == V]
        out_pkts = [p for p in packets(self.out) if (p[1] & 0x1F) << 8 | p[2] == V]
        self.assertEqual(out_pkts, src_pkts[len(src_pkts) - len(out_pkts):])

    def test_any_piece_sizes_give_the_same_stream(self):
        for seed in (1, 2, 3):
            out, _ = run(self.src, piece=4000, rng=random.Random(seed))
            self.assertEqual(out, self.out, f"seed {seed}")

    def test_the_log_says_what_it_did(self):
        self.assertIn("audio +9.0s", self.g.result)
        self.assertIn("moved", self.g.result)


class NormalStream(unittest.TestCase):
    def test_a_stream_in_step_is_passed_on_untouched_from_its_first_keyframe(self):
        src = stream(skew=0.0)
        out, g = run(src)
        ps = packets(src)
        key = next(i for i, p in enumerate(ps) if (p[1] & 0x1F) << 8 | p[2] == V and p[5] & 0x40)
        tail = b"".join(ps[key:])
        self.assertTrue(out.endswith(tail), "a normal stream was changed after its start")
        self.assertEqual(out[:376], pat() + pmt())
        self.assertNotIn("moved", g.result)

    def test_a_stream_joined_mid_gop_starts_on_the_next_keyframe(self):
        src = stream(skew=0.0)
        ps = packets(src)
        mid = b"".join(ps[7:])       # after the first keyframe's start
        out, _ = run(mid)
        first_v = next(t for pid, t in pes_starts(out) if pid == V)
        self.assertAlmostEqual(first_v, 1001.0, delta=0.01)

    def test_a_keyframe_before_the_tables_gets_them_in_front(self):
        # The provider's panels open with the keyframe and the PAT/PMT a few
        # packets later (seen on every channel, 2026-10-24): a demuxer drops
        # what comes before the PMT, so the first picture waited a GOP.
        ps = packets(stream(skew=0.0))
        tables = ps[:2]
        src = b"".join(ps[2:6] + tables + ps[6:])
        out, _ = run(src)
        self.assertEqual(out[:376], pat() + pmt())
        self.assertEqual(out[376:], src, "the stream after the tables changed")

    def test_audio_slightly_late_in_the_mux_still_starts_at_once(self):
        src = stream(skew=-0.5)
        out, _ = run(src)
        first_v = next(t for pid, t in pes_starts(out) if pid == V)
        self.assertAlmostEqual(first_v, 1000.0, delta=0.01)


class AsItCame(unittest.TestCase):
    """What it can't judge goes out unchanged, and never waits long."""

    def test_not_ts(self):
        out, g = run(b"SEG1SEG2")
        self.assertEqual(out, b"SEG1SEG2")
        g = AvStart()
        self.assertEqual(g.feed(b"SEG1"), b"SEG1", "a non-TS first piece was held")

    def test_no_audio_in_the_programme(self):
        src = stream(audio=False)
        g = AvStart()
        first = g.feed(src[:188 * 10])
        self.assertEqual(first, src[:188 * 10], "a video-only channel was held")
        self.assertIn("no video and audio", g.result)

    def test_keyframes_never_flagged(self):
        src = stream(keyflags=False, seconds=10)
        out, g = run(src, piece=188 * 50)
        self.assertEqual(out, src)
        self.assertIn("not flagged", g.result)

    def test_no_start_within_the_limit(self):
        src = stream(skew=30.0, seconds=40)
        g = AvStart()
        out = bytearray()
        for i in range(0, len(src), 188 * 100):
            out += g.feed(src[i:i + 188 * 100])
            if g.done:
                break
        self.assertTrue(g.done, "held past START_HOLD_SECONDS")
        self.assertIn("no keyframe with sound", g.result)

    def test_a_stream_that_ends_while_held(self):
        src = stream(skew=9.0, seconds=3)
        out, g = run(src)
        self.assertEqual(out, src)

    def test_a_video_jump_sends_the_waiting_audio(self):
        a = stream(skew=9.0, seconds=15)
        b = stream(skew=9.0, seconds=15, v0=5000.0)   # a re-dial far elsewhere
        out, _ = run(a + b)
        src_a = [t for pid, t in pes_starts(a + b) if pid == A]
        out_a = [t for pid, t in pes_starts(out) if pid == A]
        self.assertEqual(out_a, src_a[len(src_a) - len(out_a):], "audio lost at a break")


if __name__ == "__main__":
    unittest.main()

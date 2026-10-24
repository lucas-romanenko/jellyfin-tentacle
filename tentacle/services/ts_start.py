"""Where a viewer's MPEG-TS stream starts: on a video keyframe that has its
sound with it.

Some provider encoders mux the audio seconds ahead of the video (2026-10-24,
TSN 2: at the same byte position the audio's timestamps were ~9 s past the
video's, all day). A client that joins such a stream gets seconds of picture
with no sound first. Jellyfin's remux (-copyts, MPEG-TS -> HLS) then writes
the first segments without any audio, hls.js builds a video-only source
buffer from the first one and fails on the first segment that has audio
("bufferAppendError", fatal): Jellyfin Web froze such a channel at ~6 s every
time. The Android TV app (ExoPlayer) copes, the web player never does.

AvStart holds the first bytes of one client's stream until it can start it
where both exist: the first video keyframe whose timestamp is at or after the
first audio timestamp seen, less START_TOLERANCE (so a normal stream starts
on its first keyframe, as a player would anyway). The client gets the PAT
and PMT (also when they came after the keyframe, as the panels send them),
then everything from that keyframe on, and nothing is re-timed.

- A normal stream (audio held before the keyframe plays at most
  START_TOLERANCE after it) is passed on untouched from the keyframe.
- A stream whose audio runs ahead: the audio held before the keyframe that
  plays from it on, and all audio after it, goes out next to the video it
  plays with (AUDIO_LEAD before it), for the rest of the stream. Sent first
  as one block, its seconds of audio would be all a prober sees (Jellyfin
  probes 3 s: -analyzeduration 3000000), and Jellyfin remuxed the channel
  without its video. When the video jumps (a re-dial) or too much audio is
  waiting, what waits goes out at once and the stream goes on as it comes.

Panels open a stream with ~20-30 s of their buffer at once, so the hold costs
a viewer nothing noticeable (TSN 2: 0.05 s); a client joining a running
stream starts inside its recent bytes (the shared upstream's backlog).

Anything it can't judge goes out as it came, never held past its limits: not
TS (first byte not the sync byte), no PAT/PMT, a programme without both video
and audio, no keyframe flag, or no start found within START_HOLD_BYTES /
START_HOLD_SECONDS of video.
"""
from collections import deque

START_TOLERANCE = 1.0            # seconds of picture without sound accepted at the start
START_HOLD_BYTES = 48 * 1024 * 1024
START_HOLD_SECONDS = 20.0        # of video timestamps
NO_KEYFRAME_FLAG_SECONDS = 4.0   # of video without one random-access flag: not judged
AUDIO_LEAD = 0.5                 # how far ahead of the video moved audio goes out
AUDIO_WAIT_BYTES = 8 * 1024 * 1024   # the most audio kept waiting for its video
VIDEO_JUMP = 5.0                 # a video timestamp step this large is a break

_VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24, 0x42, 0xEA}
_AUDIO_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x81, 0x82, 0x84, 0x87}
_AUDIO_DESCRIPTORS = {0x6A, 0x7A, 0x7B, 0x7C}   # AC-3, E-AC-3, DTS, AAC in a 0x06 stream
_WRAP = 1 << 33


def _later(a: int, b: int) -> int:
    """a - b in 90 kHz ticks across the 33-bit wrap."""
    d = (a - b) % _WRAP
    return d - _WRAP if d >= _WRAP // 2 else d


def _pid(p, at: int) -> int:
    return (p[at + 1] & 0x1F) << 8 | p[at + 2]


def _payload(p, at: int):
    """Where the packet's payload starts, or None; and its random-access flag."""
    afc = p[at + 3] >> 4 & 3
    i, rai = at + 4, False
    if afc & 2:
        alen = p[at + 4]
        rai = alen > 0 and bool(p[at + 5] & 0x40)
        i = at + 5 + alen
    if not afc & 1 or i >= at + 188:
        return None, rai
    return i, rai


def _ts(p, i: int) -> int:
    return ((p[i] >> 1 & 7) << 30 | p[i + 1] << 22 | (p[i + 2] >> 1) << 15
            | p[i + 3] << 7 | p[i + 4] >> 1)


def _pes_times(p, at: int, i: int):
    """(pts, dts) of the PES starting in this packet, or None."""
    if p[i:i + 3] != b"\x00\x00\x01" or i + 14 > at + 188 or not p[i + 7] & 0x80:
        return None
    pts = _ts(p, i + 9)
    dts = _ts(p, i + 14) if p[i + 7] & 0x40 and i + 19 <= at + 188 else pts
    return pts, dts


class AvStart:
    """One client's stream start. feed() returns what goes out now: b"" while
    it holds, then the start, then the stream."""

    def __init__(self):
        self.done = False            # started (or given up): feed() no longer holds
        self.result = None           # where it started and why (for the log)
        self._buf = bytearray()
        self._scan = 0               # bytes of _buf parsed
        self._pmt_pid = None
        self._pat = None             # offset of the latest PAT packet
        self._pmt = []               # offsets of the latest PMT section's packets
        self._kinds = None           # pid -> "v" | "a" | "x"
        self._a0 = None              # first audio PTS
        self._v0 = None              # first video PTS
        self._vlast = None
        self._keys = []              # (offset, pts) of video keyframes
        self._audio_pes = []         # (offset, pid, pts) of audio PES starts
        self._flagged = False        # any random-access flag seen
        self._early = []             # PES starts before the PMT said whose they are
        self._mover = None           # _AudioMover once the audio runs ahead

    def feed(self, piece: bytes) -> bytes:
        if self._mover is not None:
            return self._mover.feed(piece)
        if self.done:
            return piece
        if not self._buf and piece[:1] != b"G":
            return self._release("not MPEG-TS", piece)
        self._buf += piece
        return self._judge()

    def flush(self) -> bytes:
        """The stream ended: everything still held goes out."""
        if self._mover is not None:
            return self._mover.flush()
        if self.done:
            return b""
        return self._release("the stream ended first")

    def _release(self, why: str, extra: bytes = b"") -> bytes:
        out = bytes(self._buf) + extra
        self._buf = bytearray()
        self.done, self.result = True, f"as it came ({why})"
        return out

    def _judge(self) -> bytes:
        buf = self._buf
        while self._scan + 188 <= len(buf):
            at = self._scan
            self._scan += 188
            if buf[at] != 0x47:
                return self._release("lost packet sync")
            self._packet(at)
            if self._kinds is not None:
                if "v" not in self._kinds.values() or "a" not in self._kinds.values():
                    return self._release("no video and audio programme")
                start = self._start()
                if start is not None:
                    return self._begin(*start)
                if self._v0 is not None:
                    shown = _later(self._vlast, self._v0) / 90000
                    if shown > START_HOLD_SECONDS:
                        return self._release(f"no keyframe with sound in {START_HOLD_SECONDS:.0f}s")
                    if not self._flagged and shown > NO_KEYFRAME_FLAG_SECONDS:
                        return self._release("keyframes are not flagged")
            if len(buf) > START_HOLD_BYTES:
                return self._release("too much held")
        return b""

    def _packet(self, at: int) -> None:
        p = self._buf
        pid = _pid(p, at)
        pusi = p[at + 1] & 0x40
        i, rai = _payload(p, at)
        if i is None:
            return
        if pid == 0:
            if pusi:
                self._pat = at
                self._parse_pat(at, i)
            return
        if pid == self._pmt_pid:
            if pusi:
                self._pmt = [at]
                self._parse_pmt(at, i)
            elif self._pmt:
                self._pmt.append(at)
            return
        if not pusi:
            return
        times = _pes_times(p, at, i)
        if times is None:
            return
        if self._kinds is None:
            # The panels open with a keyframe and send the tables after it.
            self._early.append((at, pid, times[0], rai))
            return
        self._pes(at, pid, times[0], rai)

    def _pes(self, at: int, pid: int, pts: int, rai: bool) -> None:
        kind = self._kinds.get(pid)
        if kind == "a":
            if self._a0 is None:
                self._a0 = pts
            self._audio_pes.append((at, pid, pts))
        elif kind == "v":
            if self._v0 is None:
                self._v0 = pts
            self._vlast = pts
            if rai:
                self._flagged = True
                self._keys.append((at, pts))

    def _parse_pat(self, at: int, i: int) -> None:
        p = self._buf
        j = i + 1 + p[i]
        if j + 12 > at + 188 or p[j] != 0x00:
            return
        k, end = j + 8, min(at + 188, j + 3 + ((p[j + 1] & 0x0F) << 8 | p[j + 2]) - 4)
        while k + 4 <= end:
            if (p[k] << 8 | p[k + 1]) != 0:       # programme 0 is the network PID
                self._pmt_pid = (p[k + 2] & 0x1F) << 8 | p[k + 3]
                return
            k += 4

    def _parse_pmt(self, at: int, i: int) -> None:
        p = self._buf
        j = i + 1 + p[i]
        if j + 12 > at + 188 or p[j] != 0x02 or self._kinds is not None:
            return
        end = min(at + 188, j + 3 + ((p[j + 1] & 0x0F) << 8 | p[j + 2]) - 4)
        k = j + 12 + ((p[j + 10] & 0x0F) << 8 | p[j + 11])
        kinds = {}
        while k + 5 <= end:
            st = p[k]
            epid = (p[k + 1] & 0x1F) << 8 | p[k + 2]
            elen = (p[k + 3] & 0x0F) << 8 | p[k + 4]
            if st in _VIDEO_TYPES:
                kinds[epid] = "v"
            elif st in _AUDIO_TYPES:
                kinds[epid] = "a"
            elif st == 0x06:
                d, dend, tags = k + 5, min(end, k + 5 + elen), set()
                while d + 2 <= dend:
                    tags.add(p[d])
                    d += 2 + p[d + 1]
                kinds[epid] = "a" if tags & _AUDIO_DESCRIPTORS else "x"
            else:
                kinds[epid] = "x"
            k += 5 + elen
        if kinds:
            self._kinds = kinds
            for early in self._early:
                self._pes(*early)
            self._early = []

    def _start(self):
        """The keyframe to start on, once there is one with sound."""
        if self._a0 is None or self._pat is None or not self._pmt:
            return None
        tol = int(START_TOLERANCE * 90000)
        for at, pts in self._keys:
            if _later(pts, self._a0) >= -tol:
                return at, pts
        return None

    def _begin(self, key_at: int, key_pts: int) -> bytes:
        buf = self._buf
        tol = int(START_TOLERANCE * 90000)
        # Audio PES held before the keyframe that play from it on: every packet
        # of its pid from the first such PES start up to the keyframe.
        kept = {}
        for at, pid, pts in self._audio_pes:
            if at >= key_at:
                break
            if pid not in kept and _later(pts, key_pts) >= -tol:
                kept[pid] = at
        # The tables go first, also when they came after the keyframe (the
        # panels open with it): a demuxer drops what precedes the PMT, and the
        # first picture would wait a GOP. Repeated where they came: harmless.
        out = bytearray()
        for at in [self._pat] + self._pmt:
            out += buf[at:at + 188]
        skew = _later(self._a0, self._v0) / 90000
        where = (f"on a keyframe {_later(key_pts, self._v0) / 90000:.1f}s in "
                 f"(audio {skew:+.1f}s from video), {key_at / 1e6:.1f} MB held")
        ahead = [at for at, pid, pts in self._audio_pes
                 if at < key_at and kept.get(pid, key_at) <= at and _later(pts, key_pts) > tol]
        self.done = True
        rest = bytes(buf[key_at:])
        self._buf = bytearray()
        if not ahead:
            # Normal: whatever little audio precedes the keyframe goes first.
            for at in range(min(kept.values(), default=key_at), key_at, 188):
                pid = _pid(buf, at)
                if pid in kept and at >= kept[pid]:
                    out += buf[at:at + 188]
            self.result = where
            return bytes(out) + rest
        self.result = where + ", audio moved next to its video"
        mover = _AudioMover(self._kinds)
        for at in range(min(kept.values()), key_at, 188):
            pid = _pid(buf, at)
            if pid in kept and at >= kept[pid]:
                mover.audio(buf[at:at + 188])
        self._mover = mover
        return bytes(out) + mover.feed(rest)


class _AudioMover:
    """The rest of a stream whose audio runs ahead of its video: each audio
    PES waits until the video is AUDIO_LEAD before its timestamp. Order per
    pid stays as it came; video and everything else pass at once."""

    def __init__(self, kinds: dict):
        self._kinds = kinds
        self._wait = deque()         # [pts, pid, bytearray] in arrival order
        self._open = {}              # pid -> its newest PES entry (None once sent)
        self._bytes = 0
        self._vlast = None
        self._rest = b""             # a torn packet at the end of a piece
        self._lead = int(AUDIO_LEAD * 90000)
        self._jump = int(VIDEO_JUMP * 90000)

    def audio(self, pkt, out=None) -> None:
        pid = _pid(pkt, 0)
        i, _ = _payload(pkt, 0)
        times = _pes_times(pkt, 0, i) if i is not None and pkt[1] & 0x40 else None
        if times is not None:
            entry = [times[0], pid, bytearray(pkt)]
            self._wait.append(entry)
            self._open[pid] = entry
        else:
            entry = self._open.get(pid)
            if entry is None:
                # Its PES went out already (or never had a start): it follows it.
                if out is not None:
                    out += pkt
                return
            entry[2] += pkt
        self._bytes += len(pkt)

    def _send(self, out, upto) -> None:
        """Waiting audio that plays before `upto` (None: all of it)."""
        wait = self._wait
        while wait and (upto is None or _later(wait[0][0], upto) <= self._lead):
            pts, pid, data = wait.popleft()
            out += data
            self._bytes -= len(data)
            if self._open.get(pid) is not None and self._open[pid][2] is data:
                self._open[pid] = None

    def feed(self, piece: bytes) -> bytes:
        data = self._rest + piece if self._rest else piece
        n = len(data) - len(data) % 188
        self._rest = data[n:]
        out = bytearray()
        kinds = self._kinds
        run = 0                       # start of the run of packets passed as they are
        for at in range(0, n, 188):
            pid = (data[at + 1] & 0x1F) << 8 | data[at + 2]
            kind = kinds.get(pid)
            if kind == "a":
                out += data[run:at]
                run = at + 188
                self.audio(data[at:at + 188], out)
            elif kind == "v" and data[at + 1] & 0x40:
                i, _ = _payload(data, at)
                times = _pes_times(data, at, i) if i is not None else None
                if times is None:
                    continue
                dts = times[1]
                out += data[run:at]
                run = at
                if self._vlast is not None and abs(_later(dts, self._vlast)) > self._jump:
                    self._send(out, None)          # a break: nothing to line up with
                self._vlast = dts
                self._send(out, dts)
        out += data[run:n]
        if self._bytes > AUDIO_WAIT_BYTES:
            self._send(out, None)
        return bytes(out)

    def flush(self) -> bytes:
        out = bytearray()
        self._send(out, None)
        out += self._rest
        self._rest = b""
        return bytes(out)

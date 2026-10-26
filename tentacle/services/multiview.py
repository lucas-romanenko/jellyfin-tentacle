"""Multi-view: two to four Live TV channels in one picture.

The TV app asks for 2-4 channels; Tentacle opens each the way the tuner
does (routers.livetv.stream_proxy: one shared upstream per channel, so a
channel someone already watches or records costs no extra connection) and
puts them side by side (2) or in a 2x2 grid (3-4) as one 1080p MPEG-TS
stream with every channel's sound as its own audio track, in tile order.
The player shows the one picture and picks the track of the tile that is
highlighted: switching sound is a track switch, no new stream.

Full screen is the channel itself, tuned through Jellyfin as always. Its
upstream is already open (the mosaic is one of its clients), so Jellyfin's
tuner attaches to it without a new provider connection, and the mosaic
keeps running behind it so Back to the grid is instant.

Smooth although the provider cuts. Accounts that carry one connection well
close the others every 10-15 s (measured: with 4 channels open, 3 of them
cut in the same second, each tile 4-5 times a minute). The shared upstream
re-dials and joins the provider's replay where it stopped, so nothing of
the channel is lost, but its bytes pause for 2-9 s each time. One ffmpeg
reading all four would wait for the paused one and stop the whole picture.
So each tile is decoded on its own (a decoder ffmpeg per tile: raw frames
and sound), the bytes in front of it are a buffer (the provider's burst on
every connect fills it: seconds of the channel), and a clock takes one
frame and 1/30 s of sound from every tile 29.97 times a second and hands
them to the encoder. A tile with nothing ready shows its last frame and is
silent for that tick; the others and the picture go on at the same pace.
Behind live by what its buffer holds: a tile that paused is that much
further behind, which is the buffer the next cut needs. A tile more than
CATCH_UP_SECONDS behind jumps forward to CATCH_UP_TO_SECONDS.

A channel whose picture stands still or loops (a broken feed: the same
few frames over and over) moves to another feed of the same network when
there is one; the
feed is remembered as stuck for STUCK_MEMORY_SECONDS, so the next
multi-view starts on the other one. A channel whose stream ended is opened
again after REOPEN_SECONDS, then less and less often while it won't (each
try is a new connection, and on such an account it cuts another channel).

Connections. A session needs a free provider connection for every channel
not already open; it is refused with a plain message otherwise
(free_connections). Recordings keep their priority: a recording at capacity
takes a viewer's slot (routers.livetv._StreamSlots), and a multi-view tile
is a viewer. Recordings starting within RECORDING_SOON_MINUTES count as
taken. Everything is released when the session closes: DELETE, a newer
session of the same person, or nobody reading the picture and no keepalive
for IDLE_SECONDS (the app went away).

Video: VAAPI (Intel Quick Sync, /dev/dri) when ffmpeg can open it, else
libx264, for the encode; a tile's decoder decodes, deinterlaces and scales
on the GPU too when its codec allows, else on the CPU.
"""
import asyncio
import fcntl
import logging
import os
import re
import secrets
import select
import shutil
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

MAX_TILES = 4
MIN_TILES = 2
WIDTH, HEIGHT = 1920, 1080
TILE_W, TILE_H = WIDTH // 2, HEIGHT // 2
FPS = "30000/1001"
TICK = 1001 / 30000            # one frame of the picture
GOP = 30                       # a keyframe every second: a reader starts within 1 s
VIDEO_BITRATE = "8M"
AUDIO_BITRATE = "128k"
IDLE_SECONDS = 45.0            # no reader and no keepalive this long: closed
RECORDING_SOON_MINUTES = 30    # a recording starting this soon keeps its connection
PROBE_BYTES = 2 * 1024 * 1024  # of each channel before its decoder starts (it sees its audio and video)
PROBE_SECONDS = 12.0
MAX_RESTARTS = 3
BACKLOG_SECONDS = 3.0          # of the picture a reader joining starts in (a keyframe within it)
READER_SLACK = 64              # pieces a slow reader may lag before its oldest go
RENDER_DEVICE = "/dev/dri/renderD128"

FRAME_BYTES = TILE_W * TILE_H * 3 // 2   # one NV12 tile
RATE = 48000                             # sound: s16 stereo
SAMPLE_BYTES = 4
LEAD_SECONDS = 1.5             # the picture starts this far ahead: the player's buffer
FIRST_FRAME_SECONDS = 8.0      # the clock waits this long at most for every tile's first frame
QUEUE_MAX_BYTES = 96 * 1024 * 1024   # of a channel waiting for its decoder (minutes of it)
CATCH_UP_SECONDS = 45.0        # a tile whose next bytes arrived longer ago than this jumps forward ...
CATCH_UP_TO_SECONDS = 15.0     # ... to what arrived in the last this
STUCK_WINDOW = 450             # frames (15 s) a feed is judged over ...
STUCK_SHARE = 0.85             # ... and how many of them shown already in it make it stuck
STUCK_MEMORY_SECONDS = 3 * 3600
REOPEN_SECONDS = 20.0          # a channel whose stream ended is opened again after this, ...
REOPEN_MAX_SECONDS = 300.0     # ... doubling up to this while it won't (each try is a new connection,
                               # and on a one-connection account a new one cuts another channel)
PENDING_FRAMES = 4             # ticks the clock may get ahead of the encoder on one input
READ_AHEAD_FRAMES = 4          # decoded frames the clock holds of each tile (the decoder's slack)
PIPE_BYTES = 1024 * 1024
F_SETPIPE_SZ = 1031


class MultiviewError(Exception):
    """Something a person can read: refused, not set up, a channel that can't be in it."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


# ── Layout ──────────────────────────────────────────────────────────────────

def layout(n: int) -> list:
    """Where each tile sits on the 1920x1080 picture: [(x, y, w, h)].
    2: side by side, centred; 3-4: a 2x2 grid (the 4th place stays black)."""
    if n == 2:
        top = (HEIGHT - TILE_H) // 2
        return [(0, top, TILE_W, TILE_H), (TILE_W, top, TILE_W, TILE_H)]
    return [((i % 2) * TILE_W, (i // 2) * TILE_H, TILE_W, TILE_H) for i in range(n)]


# ── ffmpeg ──────────────────────────────────────────────────────────────────

_encoder: Optional[str] = None


def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


async def encoder() -> str:
    """"vaapi" when ffmpeg can encode on the GPU here, else "x264". Asked
    once (a one-frame test encode) and remembered."""
    global _encoder
    if _encoder is not None:
        return _encoder
    found = "x264"
    if _ffmpeg() and os.path.exists(RENDER_DEVICE):
        try:
            proc = await asyncio.create_subprocess_exec(
                _ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                "-init_hw_device", f"vaapi=va:{RENDER_DEVICE}", "-filter_hw_device", "va",
                "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30", "-frames:v", "3",
                "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-f", "null", "-",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
            if proc.returncode == 0:
                found = "vaapi"
            else:
                logger.info(f"[Multiview] GPU encode not available, using x264: "
                            f"{(err or b'').decode(errors='replace').strip()[-200:]}")
        except Exception as e:
            logger.info(f"[Multiview] GPU encode test failed, using x264: {e}")
    _encoder = found
    return found


HW_DECODE_CODECS = {"h264", "hevc", "mpeg2video"}
HW_DECODE_PIX = {"yuv420p", "yuvj420p", "nv12"}


def gpu_decodes(info: dict) -> bool:
    """The GPU decodes this channel's video (8-bit 4:2:0 H.264, HEVC, MPEG-2)."""
    return info.get("codec") in HW_DECODE_CODECS and info.get("pix_fmt") in HW_DECODE_PIX


def decoder_args(gpu: bool, has_audio: bool, video_out: str, audio_out: Optional[str], source: str = "pipe:0") -> list:
    """The ffmpeg that decodes one tile: the channel's MPEG-TS from `source`
    (its stdin), its picture as raw NV12 960x540 frames at 29.97/s to
    `video_out` (deinterlaced, fitted inside the tile: 4:3 gets bars) and its
    sound as s16 stereo 48 kHz to `audio_out` (ffmpeg URLs: pipe:<fd>), both
    from the channel's first moment (t=0), so frame k and sample k * 1601.6
    are the same instant. `gpu`: decoded, deinterlaced and
    scaled on the GPU (the codec allows it and the encoder is "vaapi")."""
    args = [_ffmpeg() or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if gpu:
        args += ["-init_hw_device", f"vaapi=va:{RENDER_DEVICE}", "-filter_hw_device", "va",
                 "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-hwaccel_device", "va"]
    args += ["-thread_queue_size", "1024", "-fflags", "+genpts+discardcorrupt",
             "-analyzeduration", "3000000", "-probesize", "5000000", "-f", "mpegts", "-i", source]
    fit = f"pad={TILE_W}:{TILE_H}:-1:-1,setsar=1,fps={FPS}:start_time=0"
    if gpu:
        video = (f"[0:v:0]deinterlace_vaapi=auto=1,"
                 f"scale_vaapi=w={TILE_W}:h={TILE_H}:format=nv12:force_original_aspect_ratio=decrease,"
                 f"hwdownload,format=nv12,{fit}[v]")
    else:
        video = (f"[0:v:0]yadif=mode=0:parity=-1:deint=1,"
                 f"scale={TILE_W}:{TILE_H}:force_original_aspect_ratio=decrease,format=nv12,{fit}[v]")
    graph = [video]
    if has_audio and audio_out is not None:
        graph.append(f"[0:a:0]aresample={RATE}:async=1:first_pts=0,"
                     f"aformat=sample_fmts=s16:channel_layouts=stereo[a]")
    args += ["-filter_complex", ";".join(graph),
             "-map", "[v]", "-f", "rawvideo", "-flush_packets", "1", video_out]
    if has_audio and audio_out is not None:
        args += ["-map", "[a]", "-f", "s16le", "-flush_packets", "1", audio_out]
    return args


def ffmpeg_args(video_ins: list, audio_ins: list, enc: str, titles: list) -> list:
    """The encoder: every tile's raw frames (`video_ins`, NV12 960x540 at
    29.97/s, one per tick from the clock) and sound (`audio_ins`, s16 stereo
    48 kHz), as ffmpeg URLs (pipe:<fd>), into one 1080p picture with one audio track per tile, in order.
    Its inputs never wait for a provider: the clock feeds them evenly."""
    n = len(video_ins)
    places = layout(n)
    args = [_ffmpeg() or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if enc == "vaapi":
        args += ["-init_hw_device", f"vaapi=va:{RENDER_DEVICE}", "-filter_hw_device", "va"]
    for src in video_ins:
        args += ["-thread_queue_size", "64", "-nofind_stream_info", "-f", "rawvideo", "-pix_fmt", "nv12",
                 "-video_size", f"{TILE_W}x{TILE_H}", "-framerate", FPS, "-i", src]
    for src in audio_ins:
        args += ["-thread_queue_size", "256", "-nofind_stream_info", "-f", "s16le", "-ar", str(RATE),
                 "-ac", "2", "-i", src]
    stack = "".join(f"[{i}:v]" for i in range(n))
    # Two tiles: xstack makes the 1920x540 strip (its own y is 0), and pad
    # centres it; xstack at y=270 and then pad would push it down twice.
    top = places[0][1] if n == 2 else 0
    spots = "|".join(f"{x}_{y - top}" for x, y, _, _ in places)
    tail = f"{stack}xstack=inputs={n}:layout={spots}:fill=black,setsar=1"
    if n == 2:
        tail += f",pad={WIDTH}:{HEIGHT}:0:{top}"
    if enc == "vaapi":
        tail += ",format=nv12,hwupload"
    args += ["-filter_complex", tail + "[v]", "-map", "[v]"]
    if enc == "vaapi":
        args += ["-c:v", "h264_vaapi", "-rc_mode", "VBR"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"]
    args += ["-b:v", VIDEO_BITRATE, "-maxrate", "10M", "-bufsize", "8M", "-g", str(GOP)]
    for i in range(n):
        args += ["-map", f"{n + i}:a"]
    args += ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ac", "2"]
    for i, title in enumerate(titles):
        args += [f"-metadata:s:a:{i}", f"title={_clean_title(title)}"]
    args += ["-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-flush_packets", "1",
             "pipe:1"]
    return args


def _clean_title(title: str) -> str:
    return re.sub(r"[^\w .&'·:-]", "", title or "")[:60] or "Channel"


def _pipe() -> tuple:
    """(read fd, write fd), as large as Linux lets it be (a frame and a half)."""
    r, w = os.pipe()
    try:
        fcntl.fcntl(w, F_SETPIPE_SZ, PIPE_BYTES)
    except OSError:
        pass
    return r, w


def _close_fd(fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


async def probe(data: bytes) -> dict:
    """What ffprobe sees in a channel's first bytes: {"video", "audio",
    "codec", "pix_fmt"}. Unknown (no ffprobe, a failure) counts as video
    and audio there, decoded in software."""
    unknown = {"video": True, "audio": True, "codec": None, "pix_fmt": None}
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return unknown
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-f", "mpegts", "-analyzeduration", "3000000", "-probesize", str(len(data)),
            "-show_entries", "stream=codec_type,codec_name,pix_fmt", "-of", "csv=p=0", "-i", "pipe:0",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(data), timeout=10)
        return parse_probe((out or b"").decode(errors="replace"))
    except Exception as e:
        logger.info(f"[Multiview] probe failed: {e}")
        return unknown


def parse_probe(text: str) -> dict:
    """ffprobe's csv lines (codec_name,codec_type[,pix_fmt]) -> probe()'s answer."""
    found = {"video": False, "audio": False, "codec": None, "pix_fmt": None}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        name, kind = parts[0], parts[1]
        if kind == "video" and not found["video"]:
            found.update(video=True, codec=name, pix_fmt=parts[2] if len(parts) > 2 and parts[2] else None)
        elif kind == "audio":
            found["audio"] = True
    if not found["video"] and not found["audio"]:
        return {"video": True, "audio": True, "codec": None, "pix_fmt": None}
    return found


# ── Connections ─────────────────────────────────────────────────────────────

def channel_id_of_path(db, stream_path: str) -> Optional[int]:
    """The LiveChannel a lineup entry's stream path plays now; None for
    what multi-view can't open (YouTube live)."""
    m = re.fullmatch(r"/api/live/stream/(\d+)", stream_path or "")
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"/api/live/net/(\d+)(?:/at/([^/]+))?", stream_path or "")
    if m:
        from services import livetv_catalog as C
        nid = int(m.group(1))
        return C.place_channel(db, nid, m.group(2)) if m.group(2) else C.network_channel(db, nid)
    return None


def upcoming_recordings(db) -> int:
    """Recordings that start (with pre-padding) within RECORDING_SOON_MINUTES
    and aren't pulling yet: each will want a connection. Unknown: 0 (a
    recording takes a viewer's slot anyway when it starts)."""
    from datetime import datetime
    from models.database import get_setting
    from routers.livetv import _jellyfin_timers
    from routers.recordings import busy_from_timers
    url = (get_setting(db, "jellyfin_url", "") or "").strip()
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    if not url or not key:
        return 0
    try:
        items = (_jellyfin_timers(url, key) or {}).get("Items") or []
    except Exception as e:
        logger.info(f"[Multiview] Jellyfin's timers not read: {e}")
        return 0
    return len(busy_from_timers(items, datetime.utcnow(), RECORDING_SOON_MINUTES)["starting_soon"])


def free_connections(db, channels: list, soon: int = 0, keep: "set[int]" = frozenset()) -> dict:
    """Whether `channels` (LiveChannel rows) can be opened now. A channel
    already streaming (someone watches or records it, or it is in `keep`:
    the session being changed) needs nothing new. Per provider and overall,
    with `soon` connections set aside for recordings about to start.
    {"ok", "needed", "free", "message"}."""
    from routers import livetv as LT
    from models.database import Provider
    open_now = {cid for cid, s in LT._shared_streams.items() if not s._closed} | set(LT._pending_opens) | set(keep)
    new = [ch for ch in channels if ch.id not in open_now]
    slots = LT._stream_slots
    active = sum(1 for l in slots.leases.values() if not l.preempted)
    limit = LT.overall_stream_limit(db)
    free = (limit - active - soon) if limit > 0 else None
    out = {"ok": True, "needed": len(new), "free": free, "limit": limit, "active": active,
           "recordings_soon": soon, "message": ""}
    if free is not None and len(new) > free:
        out["ok"] = False
        out["message"] = refusal_message(len(new), max(free, 0), soon)
        return out
    by_provider = {}
    for ch in new:
        by_provider[ch.provider_id] = by_provider.get(ch.provider_id, 0) + 1
    for pid, wanted in by_provider.items():
        p = db.get(Provider, pid) if pid is not None else None
        plimit = LT.provider_stream_limit(db, p) if p is not None else 0
        if plimit <= 0:
            continue
        pfree = plimit - slots.provider_active(pid)
        if wanted > pfree:
            out["ok"] = False
            name = getattr(p, "name", None) or "the provider"
            out["message"] = (f"Multi-view needs {wanted} free connection{'s' if wanted != 1 else ''} on "
                              f"{name}; {max(pfree, 0)} {'is' if pfree == 1 else 'are'} free.")
            return out
    return out


def refusal_message(needed: int, free: int, soon: int = 0) -> str:
    msg = (f"Multi-view needs {needed} free connection{'s' if needed != 1 else ''}; "
           f"{free} {'is' if free == 1 else 'are'} free right now")
    if soon:
        msg += f" ({soon} kept for a recording that starts soon)" if soon == 1 else \
            f" ({soon} kept for recordings that start soon)"
    return msg + "."


# ── Another feed for a stuck channel ────────────────────────────────────────

_stuck: "dict[int, float]" = {}   # channel id -> when its picture was found standing still


def mark_stuck(channel_id: int) -> None:
    _stuck[channel_id] = time.monotonic()


def is_stuck(channel_id: int) -> bool:
    at = _stuck.get(channel_id)
    if at is None:
        return False
    if time.monotonic() - at > STUCK_MEMORY_SECONDS:
        _stuck.pop(channel_id, None)
        return False
    return True


def other_feeds(db, key: str, current: int) -> list:
    """The other switched-on feeds of the network a pool key ("n18", a
    place's "n18@city") plays, best first as the network ranks its variants;
    [] for a single channel ("c123") or a network with one feed."""
    m = re.fullmatch(r"n(\d+)", str(key or "").split("@", 1)[0])
    if not m:
        return []
    from services import livetv_catalog as C
    info = C._network_members(db).get(int(m.group(1)))
    if not info:
        return []
    members = list(info["members"])
    others = [c for c in members if c != current]
    if not others:
        return []
    try:
        by_id = C._member_rows(db, members)
        score = C._Scorer(db).network(members, by_id)
        country, tiers = C._place(db)
        others.sort(key=lambda c: score.score(c, country, tiers), reverse=True)
    except Exception as e:
        logger.info(f"[Multiview] {key}: feeds not ranked ({e}), in catalog order")
    return others


def first_feed(db, pick: tuple) -> tuple:
    """`pick` (channel_id, name, key, item_id), on another feed of its network
    when its own was found stuck lately and another isn't."""
    cid, name, key, item_id = pick
    if not is_stuck(cid):
        return pick
    for other in other_feeds(db, key, cid):
        if not is_stuck(other):
            logger.info(f"[Multiview] {name}: channel {cid} was stuck lately, starting on {other}")
            return (other, name, key, item_id)
    return pick


# ── A tile: one channel, the bytes in front of its decoder, the decoder ─────

def _video_key_offset(data: bytes) -> Optional[int]:
    """Where in `data` (MPEG-TS) the first video frame start marked as a
    random access point (a keyframe) is; else the first video frame start;
    None without either."""
    sync = next((i for i in range(min(188, len(data) - 188)) if data[i] == 0x47 and data[i + 188] == 0x47), 0)
    first = None
    for i in range(sync, len(data) - 187, 188):
        if data[i] != 0x47 or not data[i + 1] & 0x40:
            continue
        has_af = data[i + 3] & 0x20
        k = 4 + (1 + data[i + 4] if has_af else 0)
        if k + 4 > 188 or data[i + k:i + k + 3] != b"\x00\x00\x01" or not 0xE0 <= data[i + k + 3] <= 0xEF:
            continue
        if has_af and data[i + 4] > 0 and data[i + 5] & 0x40:
            return i
        if first is None:
            first = i
    return first


class _Decoder:
    """One tile's decoder ffmpeg: the channel in on a pipe (fed by
    _Tile.feed), raw frames and sound out on two more, read by the clock
    thread under `lock` (the fds are closed under it, never while read)."""

    def __init__(self, tile: "_Tile", gpu: bool):
        self.tile = tile
        self.gpu = gpu
        self.proc = None
        self.lock = threading.Lock()
        self.in_fd = None
        self.video_fd = None
        self.audio_fd = None
        self.started = time.monotonic()
        self.feed_task = None
        self.errors_task = None
        self.stderr_tail = deque(maxlen=5)

    async def start(self) -> None:
        in_r, in_w = _pipe()
        v_r, v_w = _pipe()
        a_r, a_w = _pipe() if self.tile.has_audio else (None, None)
        theirs = [fd for fd in (in_r, v_w, a_w) if fd is not None]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *decoder_args(self.gpu, self.tile.has_audio, f"pipe:{v_w}",
                              f"pipe:{a_w}" if a_w is not None else None),
                stdin=in_r, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                pass_fds=[fd for fd in (v_w, a_w) if fd is not None], preexec_fn=_lower_priority)
        except BaseException:
            for fd in (in_w, v_r, a_r):
                _close_fd(fd)
            raise
        finally:
            for fd in theirs:
                _close_fd(fd)
        for fd in (in_w, v_r, a_r):
            if fd is not None:
                os.set_blocking(fd, False)
        self.in_fd, self.video_fd, self.audio_fd = in_w, v_r, a_r
        self.started = time.monotonic()
        self.feed_task = asyncio.create_task(self.tile.feed(self))
        self.errors_task = asyncio.create_task(self._errors())

    async def _errors(self):
        try:
            async for line in self.proc.stderr:
                text = line.decode(errors="replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
        except Exception:
            pass

    @property
    def exited(self) -> bool:
        return self.proc is None or self.proc.returncode is not None

    def close_input(self) -> None:
        with self.lock:
            _close_fd(self.in_fd)
            self.in_fd = None

    async def stop(self) -> None:
        task, self.feed_task = self.feed_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        with self.lock:
            for fd in (self.in_fd, self.video_fd, self.audio_fd):
                _close_fd(fd)
            self.in_fd = self.video_fd = self.audio_fd = None
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                pass
        if self.errors_task is not None:
            self.errors_task.cancel()


class _Tile:
    """One channel: joined like the tuner joins it and read by one task for
    as long as the session has it, into `buf` (the bytes waiting for the
    decoder: the tile's buffer against the provider's pauses). Its decoder
    is restarted when it stops; the read never is (cancelling it would close
    the channel's subscription)."""

    def __init__(self, channel_id: int, name: str, key: str, item_id: str):
        self.channel_id = channel_id
        self.name = name
        self.key = key
        self.item_id = item_id
        self.response = None      # the stream_proxy response we read
        self.reader = None
        self.buf = deque()        # (when it arrived, bytes)
        self.buf_bytes = 0
        self.arrived = None       # asyncio.Event: bytes (or the end) arrived
        self.head = b""           # the first bytes, for the first decoder
        self.has_audio = True
        self.gpu = False          # the GPU can decode its video
        self.ended = False        # its stream ended (stopped for a recording, the provider gave up)
        self.ended_at = None
        self.decoder: Optional[_Decoder] = None
        # Kept by the clock thread (plain counters):
        self.frames = 0           # frames shown from the decoder
        self.held = 0             # ticks it showed its last frame again (nothing ready)
        self.stalls = 0           # times it held for half a second or more
        self.stuck = False        # its picture stood still for STUCK_WINDOW
        # Kept by the session:
        self.fed = 0              # bytes given to decoders
        self.decoder_restarts = 0
        self.catch_ups = 0
        self.switching = False
        self.gave_up = False      # stuck, and no other feed would open
        self.reopen_after = REOPEN_SECONDS

    async def open(self, db_factory) -> None:
        """Join the channel and read its first bytes (what it has: video, sound)."""
        from routers.livetv import stream_proxy
        self.arrived = asyncio.Event()
        db = db_factory()
        try:
            try:
                resp = await stream_proxy(self.channel_id, db)
            except Exception as e:
                detail = getattr(e, "detail", None) or str(e)
                raise MultiviewError(f"{self.name} didn't open: {detail}", 503)
        finally:
            db.close()
        self.response = resp
        reason = (getattr(resp, "headers", {}) or {}).get("x-tentacle-reason")
        if reason or not hasattr(resp, "body_iterator"):
            await self.close()
            raise MultiviewError(f"{self.name} didn't open: {reason or 'no stream'}", 503)
        self.reader = asyncio.create_task(self._read(resp.body_iterator))
        head = bytearray()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PROBE_SECONDS
        while len(head) < PROBE_BYTES:
            left = deadline - loop.time()
            if left <= 0:
                break
            try:
                piece = await asyncio.wait_for(self.next_piece(), timeout=left)
            except asyncio.TimeoutError:
                break
            if piece is None:
                break
            head += piece
        if not head:
            await self.close()
            raise MultiviewError(f"{self.name} sent nothing", 503)
        self.head = bytes(head)
        info = await probe(self.head)
        self.has_audio = info["audio"]
        self.gpu = gpu_decodes(info)

    async def _read(self, body) -> None:
        try:
            async for piece in body:
                self.buf.append((time.monotonic(), piece))
                self.buf_bytes += len(piece)
                while self.buf_bytes > QUEUE_MAX_BYTES and len(self.buf) > 1:
                    self.buf_bytes -= len(self.buf.popleft()[1])
                self.arrived.set()
            logger.info(f"[Multiview] {self.name}: its stream ended")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"[Multiview] {self.name}: its stream ended: {e}")
        finally:
            self.ended = True
            self.ended_at = time.monotonic()
            self.arrived.set()

    async def next_piece(self) -> Optional[bytes]:
        """The next bytes of the channel; None once it ended and all were taken."""
        while not self.buf:
            if self.ended:
                return None
            self.arrived.clear()
            await self.arrived.wait()
        _, piece = self.buf.popleft()
        self.buf_bytes -= len(piece)
        return piece

    def seconds_behind(self) -> float:
        """How long the bytes the decoder gets next have waited: how far the
        tile is behind what arrived. By time, not bytes: a re-dial the
        stream proxy couldn't join resends seconds already sent, which the
        decoder gets through at once (their frames are behind its clock)."""
        return time.monotonic() - self.buf[0][0] if self.buf else 0.0

    def drop_to(self, seconds: float) -> int:
        """Leave only what arrived in the last `seconds`, from a keyframe:
        the tile jumps forward. Bytes dropped."""
        now = time.monotonic()
        dropped = 0
        while self.buf and now - self.buf[0][0] > seconds:
            dropped += len(self.buf.popleft()[1])
        if not dropped:
            return 0
        self.buf_bytes -= dropped
        self.head = b""
        return dropped + self.from_keyframe()

    def from_keyframe(self) -> int:
        """Start what waits at its first keyframe (a decoder started mid-way
        shows broken pictures until one): bytes skipped. Looks at the first
        few MB only."""
        head, first = bytearray(), None
        while self.buf and len(head) < 8 * 1024 * 1024:
            at, piece = self.buf.popleft()
            first = at if first is None else first
            head += piece
        skip = _video_key_offset(bytes(head)) or 0
        if len(head) > skip:
            self.buf.appendleft((first, bytes(head[skip:])))
        self.buf_bytes -= skip
        return skip

    async def feed(self, dec: _Decoder) -> None:
        """Copy the channel into `dec`'s input until either side ends; at the
        end of the channel its input closes (the decoder finishes)."""
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()

        async def write_all(data: bytes):
            view = memoryview(data)
            while view:
                fd = dec.in_fd
                if fd is None:
                    raise BrokenPipeError
                try:
                    n = os.write(fd, view)
                    view = view[n:]
                except BlockingIOError:
                    ready.clear()
                    loop.add_writer(fd, ready.set)
                    try:
                        await ready.wait()
                    finally:
                        loop.remove_writer(fd)
        try:
            if self.head:
                head, self.head = self.head, b""
                self.fed += len(head)
                await write_all(head)
            while True:
                piece = await self.next_piece()
                if piece is None:
                    break
                self.fed += len(piece)
                await write_all(piece)
        except (BrokenPipeError, OSError, asyncio.CancelledError):
            return
        except Exception as e:
            logger.info(f"[Multiview] {self.name}: feed ended: {e}")
        dec.close_input()

    async def start_decoder(self, vaapi: bool) -> None:
        old, self.decoder = self.decoder, None
        if old is not None:
            await old.stop()
            if not self.head:
                self.from_keyframe()
        dec = _Decoder(self, gpu=vaapi and self.gpu)
        await dec.start()
        self.decoder = dec

    async def close(self) -> None:
        """Leave the channel (its upstream closes when it was the last client)."""
        dec, self.decoder = self.decoder, None
        if dec is not None:
            await dec.stop()
        reader, self.reader = self.reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except BaseException:
                pass
        resp, self.response = self.response, None
        shared = getattr(resp, "_shared", None)
        if shared is not None:
            try:
                await shared.unsubscribe(resp._q)
            except Exception as e:
                logger.info(f"[Multiview] {self.name}: unsubscribe: {e}")

    @property
    def opened(self) -> bool:
        return self.response is not None

    def describe(self, index: int, place: tuple) -> dict:
        x, y, w, h = place
        return {"index": index, "audio_track": index, "item_id": self.item_id, "key": self.key,
                "channel_id": self.channel_id, "name": self.name, "has_audio": self.has_audio,
                "live": not self.ended, "x": x, "y": y, "width": w, "height": h,
                "buffer_seconds": round(self.seconds_behind(), 1),
                "stalls": self.stalls, "held_seconds": round(self.held * TICK, 1)}


# ── The clock: one frame of every tile per tick ─────────────────────────────

_BLACK = bytes([16]) * (TILE_W * TILE_H) + bytes([128]) * (TILE_W * TILE_H // 2)
_AUDIO_CAP = RATE * SAMPLE_BYTES * 2      # sound read ahead of its frames, at most
_SILENCE = bytes(SAMPLE_BYTES * (RATE // 29 + 1))   # a tick of it, and more
# Frame buffers per place, reused in turn: read ahead, the one on screen,
# the ones waiting for the encoder (PENDING_FRAMES and the newest) and two
# more, so one is never read into while it is still to be written.
RING_FRAMES = READ_AHEAD_FRAMES + PENDING_FRAMES + 4


def _fill(fd: int, buf: bytearray, upto: int) -> None:
    while len(buf) < upto:
        try:
            got = os.read(fd, upto - len(buf))
        except OSError:      # nothing ready (BlockingIOError) or the decoder gone
            return
        if not got:
            return
        buf += got


class _Slot:
    """What the clock keeps of one place on the picture."""

    def __init__(self):
        self.decoder = None
        self.frame = _BLACK
        self.ring = [bytearray(FRAME_BYTES) for _ in range(RING_FRAMES)]
        self.next = 0             # the ring buffer the next frame is read into
        self.got = 0              # bytes of it read so far
        self.ready = deque()      # frames read ahead
        self.abuf = bytearray()
        self.apos = 0             # sound in abuf before this was taken
        self.started = False      # a frame was shown
        self.held_run = 0
        self.judged = None        # the tile the window below is of
        self.window = deque()     # (frame signature, seen before in the window)
        self.counts = {}          # signature -> times in the window
        self.seen = 0


class _Clock(threading.Thread):
    """Hands the encoder one frame and one tick of sound of every tile,
    29.97 times a second, whatever the channels do: a tile whose decoder has
    nothing ready shows its last frame and is silent. A thread of its own,
    so a busy event loop doesn't make the picture late. Reading one frame a
    tick is also what paces each decoder (it waits on its full pipe), and so
    the channel's bytes wait in the tile's buffer. Frames are read into the
    place's ring of buffers and written from them: four tiles are ~90 MB/s."""

    def __init__(self, session: "Session", video_fds: list, audio_fds: list):
        super().__init__(daemon=True, name=f"multiview-{session.id[:6]}")
        self.session = session
        self.video_fds = video_fds
        self.audio_fds = audio_fds
        self.slots = [_Slot() for _ in video_fds]
        # The encoder's inputs, a tile's picture then its sound, and what
        # waits to be written to each (non-blocking).
        self.fds = [fd for pair in zip(video_fds, audio_fds) for fd in pair]
        self.pending = [deque() for _ in self.fds]
        self.pending_bytes = [0] * len(self.fds)
        self.limits = [PENDING_FRAMES * FRAME_BYTES, PENDING_FRAMES * RATE * SAMPLE_BYTES // 30] * len(video_fds)
        self.halt = threading.Event()
        self.ticks = 0
        self.late = 0             # ticks handed over more than 0.25 s late
        self.error = None

    def run(self):
        try:
            self._first_frames()
            start = time.monotonic() - LEAD_SECONDS
            k = 0
            samples = 0
            while not self.halt.is_set():
                now = time.monotonic()
                due = start + k * TICK
                if due > now:
                    if self.halt.wait(due - now):
                        break
                elif now - due > 0.25 and k * TICK > LEAD_SECONDS + 1:
                    self.late += 1
                    if now - due > 2.0:   # the host stood still: go on from now, don't race
                        start = now - k * TICK
                if k * TICK < LEAD_SECONDS:
                    self._lead_frames()
                upto = round((k + 1) * TICK * RATE)
                need, samples = upto - samples, upto
                tiles = self.session.tiles
                for i, slot in enumerate(self.slots):
                    frame, sound = self._take(slot, tiles[i] if i < len(tiles) else None, need)
                    self._queue(2 * i, memoryview(frame))
                    self._queue(2 * i + 1, sound)
                self._flush()
                k += 1
                self.ticks = k
        except OSError as e:
            if not self.halt.is_set():
                self.error = e
                logger.info(f"[Multiview] {self.session.id[:6]}: clock stopped: {e}")

    def _queue(self, i: int, data) -> None:
        self.pending[i].append(data)
        self.pending_bytes[i] += len(data)

    def _flush(self) -> None:
        """Hand the encoder what waits for it, never waiting on one input
        while another starves (ffmpeg reads its inputs in its own order):
        until no input has more than PENDING_FRAMES ticks waiting."""
        while True:
            waiting = []
            for i, fd in enumerate(self.fds):
                queue = self.pending[i]
                while queue:
                    try:
                        n = os.write(fd, queue[0])
                    except BlockingIOError:
                        break
                    self.pending_bytes[i] -= n
                    if n < len(queue[0]):
                        queue[0] = memoryview(queue[0])[n:]
                        break
                    queue.popleft()
                if queue:
                    waiting.append(fd)
            if not waiting or self.halt.is_set():
                return
            if all(b <= lim for b, lim in zip(self.pending_bytes, self.limits)):
                return
            select.select([], waiting, [], 0.5)

    def _first_frames(self):
        """Wait (FIRST_FRAME_SECONDS at most) until every tile has a frame, so
        the picture doesn't start with black tiles."""
        end = time.monotonic() + FIRST_FRAME_SECONDS
        while time.monotonic() < end and not self.halt.is_set():
            tiles = self.session.tiles
            ready = 0
            for i, slot in enumerate(self.slots):
                tile = tiles[i] if i < len(tiles) else None
                dec = tile.decoder if tile is not None else None
                self._read_ahead(slot, dec)
                ready += bool(slot.ready)
            if ready == len(self.slots):
                return
            self.halt.wait(0.02)

    def _lead_frames(self) -> None:
        """While the picture runs ahead at the start (LEAD_SECONDS, no tick
        due yet) a decoder busy starting has a moment for its next frame
        (0.2 s at most) instead of a repeated one."""
        end = time.monotonic() + 0.2
        tiles = self.session.tiles
        while not self.halt.is_set():
            missing = 0
            for i, slot in enumerate(self.slots):
                tile = tiles[i] if i < len(tiles) else None
                dec = tile.decoder if tile is not None else None
                if dec is not None and not dec.exited:
                    self._read_ahead(slot, dec)
                    missing += not slot.ready
            if not missing or time.monotonic() > end:
                return
            self.halt.wait(0.005)

    def _take(self, slot: _Slot, tile: Optional[_Tile], need: int) -> tuple:
        """This tick's frame and `need` samples of sound for one place."""
        dec = tile.decoder if tile is not None else None
        self._read_ahead(slot, dec)
        nbytes = need * SAMPLE_BYTES
        if not slot.ready:
            # Nothing ready: the last frame again, silent. Its sound waits
            # with it (taken with its frames), so they stay together.
            if slot.started and tile is not None:
                tile.held += 1
                slot.held_run += 1
                if slot.held_run == 15:
                    tile.stalls += 1
            return slot.frame, _SILENCE[:nbytes]
        slot.frame = slot.ready.popleft()
        slot.started = True
        slot.held_run = 0
        tile.frames += 1
        self._judge(slot, tile)
        have = len(slot.abuf) - slot.apos
        if have >= nbytes:
            sound = bytes(slot.abuf[slot.apos:slot.apos + nbytes])
            slot.apos += nbytes
        else:
            sound = bytes(slot.abuf[slot.apos:]) + _SILENCE[:nbytes - have]
            slot.apos = len(slot.abuf)
        if slot.apos > _AUDIO_CAP // 2:
            del slot.abuf[:slot.apos]
            slot.apos = 0
        return slot.frame, sound

    def _read_ahead(self, slot: _Slot, dec: Optional[_Decoder]) -> None:
        """Up to READ_AHEAD_FRAMES of the tile's frames (and their sound) from
        its decoder, without waiting. A new decoder starts afresh."""
        if dec is not slot.decoder:
            slot.decoder, slot.got, slot.abuf, slot.apos = dec, 0, bytearray(), 0
            slot.ready.clear()
        if dec is None:
            return
        with dec.lock:
            if dec.video_fd is not None:
                while len(slot.ready) < READ_AHEAD_FRAMES:
                    buf = slot.ring[slot.next]
                    try:
                        while slot.got < FRAME_BYTES:
                            n = os.readv(dec.video_fd, [memoryview(buf)[slot.got:]])
                            if not n:
                                break
                            slot.got += n
                    except OSError:
                        pass
                    if slot.got < FRAME_BYTES:
                        break
                    slot.ready.append(buf)
                    slot.next = (slot.next + 1) % RING_FRAMES
                    slot.got = 0
            if dec.audio_fd is not None:
                _fill(dec.audio_fd, slot.abuf, slot.apos + _AUDIO_CAP)

    def _judge(self, slot: _Slot, tile: _Tile) -> None:
        """A feed that shows the same few frames over and over (a still
        picture, or a clip looping: a broken 4K feed, 25 Oct, 34 different
        frames in 30 s) is stuck. Judged by how many of the last STUCK_WINDOW
        frames of this tile had been shown already within them: 92-100 %
        there, 0 % on a live cartoon channel. A place that moved to another
        feed starts a new window."""
        if slot.judged is not tile:
            slot.judged, slot.window, slot.counts, slot.seen = tile, deque(), {}, 0
        sig = hash(bytes(slot.frame[::1021]))
        seen = slot.counts.get(sig, 0) > 0
        slot.window.append((sig, seen))
        slot.counts[sig] = slot.counts.get(sig, 0) + 1
        slot.seen += seen
        if len(slot.window) > STUCK_WINDOW:
            old, was_seen = slot.window.popleft()
            slot.seen -= was_seen
            left = slot.counts[old] - 1
            if left:
                slot.counts[old] = left
            else:
                del slot.counts[old]
        if len(slot.window) == STUCK_WINDOW and slot.seen >= STUCK_SHARE * STUCK_WINDOW:
            tile.stuck = True


# ── A session ───────────────────────────────────────────────────────────────

class Session:
    def __init__(self, user_id: str, tiles: list, device_id: str = ""):
        self.id = secrets.token_urlsafe(24)
        self.user_id = user_id
        self.device_id = device_id        # the Jellyfin device it plays on ("" = not told)
        self.tiles = tiles
        self.proc = None
        self.encoder = None
        self.readers = []                 # asyncio.Queue per reader
        self.backlog = deque()            # (loop time, bytes)
        self.pump_task = None
        self.errors_task = None
        self.monitor_task = None
        self.clock: Optional[_Clock] = None
        self.video_fds = []
        self.audio_fds = []
        self.db_factory = None
        self.touched = time.monotonic()
        self.started_at = time.monotonic()
        self.first_bytes_seconds = None
        self.closed = False
        self.stderr_tail = deque(maxlen=20)
        self.changing = False
        self.restarts = 0
        self.tried = [{t.channel_id} for t in tiles]   # feeds each place has had
        self.moves = []                   # (place, from channel, to channel, why)

    def touch(self):
        self.touched = time.monotonic()

    def idle(self, now: float) -> bool:
        return not self.readers and now - self.touched > IDLE_SECONDS

    async def start(self) -> None:
        """The picture: each tile's decoder, the encoder, the clock. Decoders
        start afresh with every clock: a clock stopped part-way through a
        frame would leave the next one reading the raw pipe out of step."""
        self.encoder = self.encoder or await encoder()
        for t in self.tiles:
            await t.start_decoder(self.encoder == "vaapi")
        video = [_pipe() for _ in self.tiles]
        audio = [_pipe() for _ in self.tiles]
        theirs = [r for r, _ in video + audio]
        args = ffmpeg_args([f"pipe:{r}" for r, _ in video], [f"pipe:{r}" for r, _ in audio], self.encoder,
                           [t.name for t in self.tiles])
        self.stderr_tail.clear()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, pass_fds=theirs, preexec_fn=_lower_priority)
        except BaseException:
            for _, w in video + audio:
                _close_fd(w)
            raise
        finally:
            for fd in theirs:
                _close_fd(fd)
        self.video_fds = [w for _, w in video]
        self.audio_fds = [w for _, w in audio]
        for fd in self.video_fds + self.audio_fds:
            os.set_blocking(fd, False)
        self.clock = _Clock(self, self.video_fds, self.audio_fds)
        self.clock.start()
        self.pump_task = asyncio.create_task(self._pump())
        self.errors_task = asyncio.create_task(self._errors())
        if self.monitor_task is None:
            self.monitor_task = asyncio.create_task(self._monitor())

    async def _stop_picture(self) -> None:
        """Stop the encoder and the clock (the tiles and their decoders go on)."""
        clock, self.clock = self.clock, None
        if clock is not None:
            clock.halt.set()
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        if clock is not None:
            await asyncio.to_thread(clock.join, 5)
        for fd in self.video_fds + self.audio_fds:
            _close_fd(fd)
        self.video_fds, self.audio_fds = [], []
        if self.proc is not None:
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                pass
        if self.pump_task is not None and self.pump_task is not asyncio.current_task():
            try:
                await asyncio.wait_for(self.pump_task, timeout=5)
            except BaseException:
                pass

    async def _errors(self):
        try:
            async for line in self.proc.stderr:
                text = line.decode(errors="replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
        except Exception:
            pass

    async def _pump(self):
        """The encoder's picture to every reader, keeping the last seconds for the next one."""
        loop = asyncio.get_running_loop()
        proc = self.proc
        try:
            while True:
                piece = await proc.stdout.read(188 * 512)
                if not piece:
                    break
                now = loop.time()
                if self.first_bytes_seconds is None:
                    self.first_bytes_seconds = round(time.monotonic() - self.started_at, 2)
                self.backlog.append((now, piece))
                while self.backlog and now - self.backlog[0][0] > BACKLOG_SECONDS:
                    self.backlog.popleft()
                for q in list(self.readers):
                    while q.qsize() > READER_SLACK:
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    q.put_nowait(piece)
        except Exception as e:
            logger.info(f"[Multiview] {self.id[:6]}: picture ended: {e}")
        if self.closed or self.changing or proc is not self.proc:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        tail = " | ".join(list(self.stderr_tail)[-3:])
        logger.warning(f"[Multiview] {self.id[:6]}: encoder stopped ({proc.returncode}) {tail}")
        # Once more (the channels stay open, the readers wait).
        if self.restarts < MAX_RESTARTS and any(t.opened for t in self.tiles):
            self.restarts += 1
            try:
                await self._stop_picture()
                await self.start()
                return
            except Exception as e:
                logger.warning(f"[Multiview] {self.id[:6]}: restart failed: {e}")
        for q in list(self.readers):
            q.put_nowait(None)

    async def _monitor(self):
        """Every half second: a decoder that stopped is started again (on the
        CPU if the GPU gave up on it at once), a tile too far behind jumps
        forward, a stuck feed moves to another, an ended channel is opened again."""
        while not self.closed:
            await asyncio.sleep(0.5)
            if self.changing or self.clock is None:
                continue
            now = time.monotonic()
            for i, t in enumerate(list(self.tiles)):
                if t.switching:
                    continue
                try:
                    dec = t.decoder
                    if dec is not None and dec.exited and now - dec.started > 2.0 \
                            and not (t.ended and not t.buf):
                        if dec.gpu and now - dec.started < 15:
                            t.gpu = False
                        t.decoder_restarts += 1
                        if t.decoder_restarts <= 5 or t.decoder_restarts % 50 == 0:
                            logger.info(f"[Multiview] {self.id[:6]} {t.name}: decoder stopped "
                                        f"({dec.proc.returncode if dec.proc else '-'}) "
                                        f"{' | '.join(dec.stderr_tail)[-200:]}; started again")
                        await t.start_decoder(self.encoder == "vaapi")
                        continue
                    behind = t.seconds_behind()
                    if behind > CATCH_UP_SECONDS:
                        dropped = t.drop_to(CATCH_UP_TO_SECONDS)
                        if dropped:
                            t.catch_ups += 1
                            logger.info(f"[Multiview] {self.id[:6]} {t.name}: {behind:.0f}s behind, "
                                        f"jumped forward ({dropped / 1e6:.1f} MB)")
                            await t.start_decoder(self.encoder == "vaapi")
                            continue
                    if t.stuck and not t.gave_up:
                        t.switching = True
                        asyncio.create_task(self._move(i, t, "its picture stands still"))
                    elif t.ended and not t.buf and t.ended_at and now - t.ended_at > t.reopen_after:
                        t.switching = True
                        asyncio.create_task(self._move(i, t, "its stream ended", same_first=True))
                except Exception as e:
                    logger.warning(f"[Multiview] {self.id[:6]} {t.name}: {e}")

    async def _move(self, i: int, old: _Tile, why: str, same_first: bool = False) -> None:
        """Place `i` onto another feed of its network (or, `same_first`, its
        own channel again first). The old one stays on screen until the new
        one opened; nothing changes if none opens."""
        try:
            if why.startswith("its picture"):
                mark_stuck(old.channel_id)

            def feeds():
                if self.db_factory is None:
                    return []
                db = self.db_factory()
                try:
                    return other_feeds(db, old.key, old.channel_id)
                finally:
                    db.close()
            others = [c for c in await asyncio.to_thread(feeds)
                      if c not in self.tried[i] and not is_stuck(c)]
            candidates = ([old.channel_id] if same_first else []) + others
            for cid in candidates:
                self.tried[i].add(cid)
                new = _Tile(cid, old.name, old.key, old.item_id)
                try:
                    await new.open(self.db_factory)
                except MultiviewError as e:
                    logger.info(f"[Multiview] {self.id[:6]} {old.name}: channel {cid} didn't open: {e}")
                    continue
                if self.closed or self.changing or i >= len(self.tiles) or self.tiles[i] is not old:
                    await new.close()
                    return
                await new.start_decoder(self.encoder == "vaapi")
                # The place's counts go on (what was on screen there, whichever feed).
                new.frames, new.held, new.stalls = old.frames, old.held, old.stalls
                new.decoder_restarts, new.catch_ups = old.decoder_restarts, old.catch_ups
                self.tiles[i] = new
                self.moves.append((i, old.channel_id, cid, why))
                logger.info(f"[Multiview] {self.id[:6]} {old.name}: {why} (channel {old.channel_id}), "
                            f"now channel {cid}")
                await old.close()
                return
            if why.startswith("its picture"):
                old.gave_up = True
                logger.info(f"[Multiview] {self.id[:6]} {old.name}: {why}, no other feed to move to")
        except Exception as e:
            logger.warning(f"[Multiview] {self.id[:6]} {old.name}: move failed: {e}")
        finally:
            old.switching = False
            old.stuck = False
            if same_first:
                old.ended_at = time.monotonic()   # tried: again later, and later
                old.reopen_after = min(old.reopen_after * 2, REOPEN_MAX_SECONDS)

    async def read(self):
        """A reader's stream: from a keyframe in the last seconds, then live."""
        from services.ts_start import AvStart
        q = asyncio.Queue()
        start = AvStart()
        backlog = b"".join(p for _, p in self.backlog)
        self.readers.append(q)
        self.touch()
        try:
            if backlog:
                out = start.feed(backlog)
                if out:
                    yield out
            while True:
                piece = await q.get()
                if piece is None:
                    rest = start.flush()
                    if rest:
                        yield rest
                    return
                if not start.done:
                    piece = start.feed(piece)
                    if not piece:
                        continue
                yield piece
        finally:
            if q in self.readers:
                self.readers.remove(q)
            self.touch()

    async def change(self, picks: list, db_factory) -> None:
        """Other channels in the same session (one swapped or removed, or
        reordered): channels it no longer has are left first (their
        connections free up for the new ones), new ones are opened, and the
        picture restarts with the new layout. A place that moved to another
        feed of its network keeps it. Readers get the new picture on their
        next connection."""
        keep = {t.key: t for t in self.tiles}
        wanted = {p[2] for p in picks}
        gone = [t for t in self.tiles if t.key not in wanted]
        self.changing = True
        try:
            await self._stop_picture()
            await asyncio.gather(*(t.close() for t in gone), return_exceptions=True)
            db = db_factory()
            try:
                fresh = [first_feed(db, p) for p in picks if p[2] not in keep]
            finally:
                db.close()
            new = [_Tile(*p) for p in fresh]
            results = await asyncio.gather(*(t.open(db_factory) for t in new), return_exceptions=True)
            opened = {t.key: t for t, r in zip(new, results) if not isinstance(r, BaseException)}
            failed = [r for r in results if isinstance(r, BaseException)]
            tiles = [keep.get(p[2]) or opened.get(p[2]) for p in picks]
            self.tiles = [t for t in tiles if t is not None]
            self.tried = [{t.channel_id} for t in self.tiles]
            self.backlog.clear()
            for q in list(self.readers):
                q.put_nowait(None)
            if len(self.tiles) < MIN_TILES:
                raise failed[0] if failed and isinstance(failed[0], MultiviewError) else \
                    MultiviewError("Multi-view needs at least two channels", 409)
            self.restarts = 0
            self.started_at = time.monotonic()
            self.first_bytes_seconds = None
            await self.start()
            if failed:
                raise failed[0] if isinstance(failed[0], MultiviewError) else MultiviewError(str(failed[0]), 503)
        finally:
            self.changing = False
            self.touch()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.monitor_task is not None:
            self.monitor_task.cancel()
        await self._stop_picture()
        await asyncio.gather(*(t.close() for t in self.tiles), return_exceptions=True)
        if self.pump_task is not None:
            self.pump_task.cancel()
        for q in list(self.readers):
            q.put_nowait(None)

    @property
    def alive(self) -> bool:
        return not self.closed and self.proc is not None and self.proc.returncode is None

    def describe(self) -> dict:
        places = layout(len(self.tiles))
        clock = self.clock
        return {"id": self.id, "stream_path": f"/api/live/multiview/{self.id}/stream.ts",
                "encoder": self.encoder, "width": WIDTH, "height": HEIGHT,
                "gpu_decode": self.encoder == "vaapi" and any(t.decoder and t.decoder.gpu for t in self.tiles),
                "tiles": [t.describe(i, places[i]) for i, t in enumerate(self.tiles)],
                "first_bytes_seconds": self.first_bytes_seconds,
                "late_ticks": clock.late if clock else 0,
                "moved": [{"place": i, "from": a, "to": b, "why": w} for i, a, b, w in self.moves]}


def _lower_priority():
    try:
        os.nice(5)
    except OSError:
        pass


# ── The sessions ────────────────────────────────────────────────────────────

_sessions: "dict[str, Session]" = {}
_lock: Optional[asyncio.Lock] = None
_reaper: Optional[asyncio.Task] = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def get(session_id: str) -> Optional[Session]:
    s = _sessions.get(session_id)
    return s if s is not None and not s.closed else None


def of_user(user_id: str, device_id: Optional[str] = None) -> list:
    """This person's running multi-views; with `device_id`, only the one on
    that device (one per device: a new one there replaces it, another
    device's keeps playing)."""
    return [s for s in _sessions.values() if s.user_id == user_id and not s.closed
            and (device_id is None or s.device_id == device_id)]


def own_connections(user_id: str, only: Optional[set] = None, device_id: Optional[str] = None) -> int:
    """Connections only this person's multi-view (on `device_id`) holds
    (channels nobody else reads): what closing it (or, with `only`, leaving
    those channels) gives back."""
    from routers import livetv as LT
    held = set()
    for s in of_user(user_id, device_id):
        for t in s.tiles:
            if only is not None and t.channel_id not in only:
                continue
            shared = LT._shared_streams.get(t.channel_id)
            if shared is not None and not shared._closed and len(shared.subscribers) <= 1:
                held.add(t.channel_id)
    return len(held)


async def start(user_id: str, picks: list, db_factory, device_id: str = "") -> Session:
    """A new session on `picks` ([(channel_id, name, key, item_id)]),
    started and producing. It replaces the person's running one on the same
    device (a swap, a removal, a new pick), which is closed only once the new
    one has its channels, so a channel in both never closes in between. Their
    multi-view on another device plays on (Lucas, 26 Oct: starting one on the
    web ended the TV's)."""
    db = db_factory()
    try:
        picks = [first_feed(db, p) for p in picks]
    finally:
        db.close()
    tiles = [_Tile(*p) for p in picks]
    session = Session(user_id, tiles, device_id)
    session.db_factory = db_factory
    t0 = time.monotonic()
    try:
        results = await asyncio.gather(*(t.open(db_factory) for t in tiles), return_exceptions=True)
        failed = [r for r in results if isinstance(r, BaseException)]
        if failed:
            raise failed[0] if isinstance(failed[0], MultiviewError) else MultiviewError(str(failed[0]), 503)
        opened = round(time.monotonic() - t0, 2)
        await session.start()
    except BaseException:
        await session.close()
        raise
    logger.info(f"[Multiview] {session.id[:6]} for {user_id[:8]}: {len(tiles)} channels "
                f"({', '.join(t.name for t in tiles)}) opened in {opened}s, encoder {session.encoder}")
    async with _get_lock():
        _sessions[session.id] = session
        old = [s for s in of_user(user_id, device_id) if s is not session]
    for s in old:     # one multi-view per device; the replaced one last, after the new one has its channels
        await close(s.id)
    _ensure_reaper()
    return session


async def close(session_id: str) -> bool:
    async with _get_lock():
        s = _sessions.pop(session_id, None)
    if s is None:
        return False
    await s.close()
    logger.info(f"[Multiview] {s.id[:6]} closed after {round(time.monotonic() - s.started_at)}s")
    return True


async def close_all() -> None:
    for sid in list(_sessions):
        await close(sid)


def _ensure_reaper():
    global _reaper
    if _reaper is None or _reaper.done():
        _reaper = asyncio.create_task(_reap())


async def _reap():
    """Close sessions nobody reads or keeps alive, and ones whose ffmpeg died."""
    while _sessions:
        await asyncio.sleep(5)
        now = time.monotonic()
        for s in list(_sessions.values()):
            if s.idle(now):
                logger.info(f"[Multiview] {s.id[:6]}: nobody watching for {IDLE_SECONDS:.0f}s, closing")
                await close(s.id)
            elif s.proc is not None and s.proc.returncode is not None and not s.readers \
                    and now - s.touched > 5:
                await close(s.id)

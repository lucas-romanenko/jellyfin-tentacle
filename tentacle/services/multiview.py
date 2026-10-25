"""Multi-view: two to four Live TV channels in one picture.

The TV app asks for 2-4 channels; Tentacle opens each the way the tuner
does (routers.livetv.stream_proxy: one shared upstream per channel, so a
channel someone already watches or records costs no extra connection) and
ffmpeg puts them side by side (2) or in a 2x2 grid (3-4) as one 1080p
MPEG-TS stream with every channel's sound as its own audio track, in tile
order. The player shows the one picture and picks the track of the tile
that is highlighted: switching sound is a track switch, no new stream.

Full screen is the channel itself, tuned through Jellyfin as always. Its
upstream is already open (the mosaic is one of its clients), so Jellyfin's
tuner attaches to it without a new provider connection, and the mosaic
keeps running behind it so Back to the grid is instant.

Connections. A session needs a free provider connection for every channel
not already open; it is refused with a plain message otherwise
(free_connections). Recordings keep their priority: a recording at capacity
takes a viewer's slot (routers.livetv._StreamSlots), and a multi-view tile
is a viewer. Recordings starting within RECORDING_SOON_MINUTES count as
taken. Everything is released when the session closes: DELETE, a newer
session of the same person, or nobody reading the picture and no keepalive
for IDLE_SECONDS (the app went away).

Video: VAAPI (Intel Quick Sync, /dev/dri) when ffmpeg can open it, else
libx264. The tiles are decoded and scaled in software either way (four
small pictures; a live channel's odd streams are decoded wherever they
are), only the 1080p encode goes to the GPU.
"""
import asyncio
import logging
import os
import re
import secrets
import shutil
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

MAX_TILES = 4
MIN_TILES = 2
WIDTH, HEIGHT = 1920, 1080
TILE_W, TILE_H = WIDTH // 2, HEIGHT // 2
FPS = "30000/1001"
GOP = 30                       # a keyframe every second: a reader starts within 1 s
VIDEO_BITRATE = "8M"
AUDIO_BITRATE = "128k"
IDLE_SECONDS = 45.0            # no reader and no keepalive this long: closed
RECORDING_SOON_MINUTES = 30    # a recording starting this soon keeps its connection
PROBE_BYTES = 2 * 1024 * 1024  # of each channel before ffmpeg starts (it sees its audio and video)
PROBE_SECONDS = 12.0
MAX_RESTARTS = 3
BACKLOG_SECONDS = 3.0          # of the picture a reader joining starts in (a keyframe within it)
READER_SLACK = 64              # pieces a slow reader may lag before its oldest go
RENDER_DEVICE = "/dev/dri/renderD128"


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


def ffmpeg_args(inputs: list, enc: str, titles: list, has_audio: list, hw_decode: Optional[list] = None) -> list:
    """The ffmpeg command for one mosaic. `inputs`: an ffmpeg input per tile
    (pipe:<fd>); `has_audio`: whether each has sound (a silent track stands
    in otherwise, so track N is always tile N); `hw_decode`: the tiles the
    GPU decodes, deinterlaces and scales (only with enc "vaapi")."""
    n = len(inputs)
    places = layout(n)
    hw_decode = [bool(h) and enc == "vaapi" for h in (hw_decode or [False] * n)]
    args = [_ffmpeg() or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if enc == "vaapi":
        args += ["-init_hw_device", f"vaapi=va:{RENDER_DEVICE}", "-filter_hw_device", "va"]
    for i, src in enumerate(inputs):
        if hw_decode[i]:
            args += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-hwaccel_device", "va"]
        args += ["-thread_queue_size", "1024", "-fflags", "+genpts+discardcorrupt",
                 "-analyzeduration", "3000000", "-probesize", "5000000",
                 "-f", "mpegts", "-i", src]
    graph = []
    fit = f"pad={TILE_W}:{TILE_H}:-1:-1,setsar=1,fps={FPS}"
    for i in range(n):
        # Interlaced channels (1080i) are deinterlaced first; the picture is
        # fitted inside its tile (4:3 gets bars), at one frame rate for all.
        if hw_decode[i]:
            graph.append(f"[{i}:v:0]deinterlace_vaapi=auto=1,"
                         f"scale_vaapi=w={TILE_W}:h={TILE_H}:format=nv12:force_original_aspect_ratio=decrease,"
                         f"hwdownload,format=nv12,{fit}[v{i}]")
        else:
            graph.append(f"[{i}:v:0]yadif=mode=0:parity=-1:deint=1,"
                         f"scale={TILE_W}:{TILE_H}:force_original_aspect_ratio=decrease,format=nv12,{fit}[v{i}]")
        if has_audio[i]:
            graph.append(f"[{i}:a:0]aresample=48000:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        else:
            graph.append(f"anullsrc=r=48000:cl=stereo[a{i}]")
    stack = "".join(f"[v{i}]" for i in range(n))
    # Two tiles: xstack makes the 1920x540 strip (its own y is 0), and pad
    # centres it; xstack at y=270 and then pad would push it down twice.
    top = places[0][1] if n == 2 else 0
    spots = "|".join(f"{x}_{y - top}" for x, y, _, _ in places)
    tail = f"{stack}xstack=inputs={n}:layout={spots}:fill=black"
    if n == 2:
        tail += f",pad={WIDTH}:{HEIGHT}:0:{top}"
    if enc == "vaapi":
        tail += ",format=nv12,hwupload"
    graph.append(tail + "[v]")
    args += ["-filter_complex", ";".join(graph), "-map", "[v]"]
    if enc == "vaapi":
        args += ["-c:v", "h264_vaapi", "-rc_mode", "VBR"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"]
    args += ["-b:v", VIDEO_BITRATE, "-maxrate", "10M", "-bufsize", "8M", "-g", str(GOP)]
    for i in range(n):
        args += ["-map", f"[a{i}]"]
    args += ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ac", "2"]
    for i, title in enumerate(titles):
        args += [f"-metadata:s:a:{i}", f"title={_clean_title(title)}"]
    args += ["-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-flush_packets", "1",
             "pipe:1"]
    return args


def _clean_title(title: str) -> str:
    return re.sub(r"[^\w .&'·:-]", "", title or "")[:60] or "Channel"


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


# ── A tile: one channel feeding ffmpeg ──────────────────────────────────────

class _Tile:
    """One channel: joined like the tuner joins it, read by one task for as
    long as the session has it (into `pieces`), and copied into whichever
    ffmpeg runs now. Only the copying is restarted with ffmpeg; the read
    never is (cancelling it would close the channel's subscription)."""

    def __init__(self, channel_id: int, name: str, key: str, item_id: str):
        self.channel_id = channel_id
        self.name = name
        self.key = key
        self.item_id = item_id
        self.response = None      # the stream_proxy response we read
        self.pieces = asyncio.Queue()
        self.reader = None
        self.head = b""           # the first bytes, for the first ffmpeg
        self.has_audio = True
        self.gpu = False          # the GPU can decode its video
        self.ended = False        # its stream ended (stopped for a recording, the provider gave up)
        self.read_fd = None
        self.write_fd = None
        self.task = None

    async def open(self, db_factory) -> None:
        """Join the channel and read its first bytes (what it has: video, sound)."""
        from routers.livetv import stream_proxy
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
                piece = await asyncio.wait_for(self.pieces.get(), timeout=left)
            except asyncio.TimeoutError:
                break
            if piece is None:
                self.pieces.put_nowait(None)
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
                self.pieces.put_nowait(piece)
                while self.pieces.qsize() > READER_SLACK:   # no ffmpeg reading (a restart): keep it recent
                    try:
                        self.pieces.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            self.ended = True
            logger.info(f"[Multiview] {self.name}: its stream ended")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.ended = True
            logger.info(f"[Multiview] {self.name}: its stream ended: {e}")
        finally:
            self.pieces.put_nowait(None)

    def pipe(self) -> int:
        """The fd ffmpeg reads this tile from (inherited)."""
        self.read_fd, self.write_fd = os.pipe()
        os.set_inheritable(self.read_fd, True)
        os.set_blocking(self.write_fd, False)
        return self.read_fd

    async def feed(self) -> None:
        """Copy the channel into ffmpeg's pipe until either side ends."""
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()

        async def write_all(data: bytes):
            view = memoryview(data)
            while view:
                try:
                    n = os.write(self.write_fd, view)
                    view = view[n:]
                except BlockingIOError:
                    ready.clear()
                    loop.add_writer(self.write_fd, ready.set)
                    try:
                        await ready.wait()
                    finally:
                        loop.remove_writer(self.write_fd)
        try:
            if self.head:
                head, self.head = self.head, b""
                await write_all(head)
            while True:
                piece = await self.pieces.get()
                if piece is None:
                    self.pieces.put_nowait(None)   # for the next ffmpeg too: this channel has ended
                    break
                await write_all(piece)
        except (BrokenPipeError, OSError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.info(f"[Multiview] {self.name}: feed ended: {e}")
        finally:
            self._close_write()

    async def stop_feed(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except BaseException:
                pass
        self.task = None
        self._close_write()

    def _close_write(self):
        if self.write_fd is not None:
            try:
                os.close(self.write_fd)
            except OSError:
                pass
            self.write_fd = None

    async def close(self) -> None:
        """Leave the channel (its upstream closes when it was the last client)."""
        await self.stop_feed()
        if self.read_fd is not None:
            try:
                os.close(self.read_fd)
            except OSError:
                pass
            self.read_fd = None
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
                "live": not self.ended, "x": x, "y": y, "width": w, "height": h}


# ── A session ───────────────────────────────────────────────────────────────

class Session:
    def __init__(self, user_id: str, tiles: list):
        self.id = secrets.token_urlsafe(24)
        self.user_id = user_id
        self.tiles = tiles
        self.proc = None
        self.encoder = None
        self.readers = []                 # asyncio.Queue per reader
        self.backlog = deque()            # (loop time, bytes)
        self.pump_task = None
        self.feeds = []
        self.touched = time.monotonic()
        self.started_at = time.monotonic()
        self.first_bytes_seconds = None
        self.closed = False
        self.stderr_tail = deque(maxlen=20)
        self.gpu_decode = True       # off after ffmpeg stopped on it once
        self.changing = False
        self.restarts = 0

    def touch(self):
        self.touched = time.monotonic()

    def idle(self, now: float) -> bool:
        return not self.readers and now - self.touched > IDLE_SECONDS

    async def start(self) -> None:
        self.encoder = self.encoder or await encoder()
        for t in self.tiles:
            await t.stop_feed()
        fds = [t.pipe() for t in self.tiles]
        args = ffmpeg_args([f"pipe:{fd}" for fd in fds], self.encoder,
                           [t.name for t in self.tiles], [t.has_audio for t in self.tiles],
                           [t.gpu and self.gpu_decode for t in self.tiles])
        self.stderr_tail.clear()
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, pass_fds=fds, preexec_fn=_lower_priority)
        for t in self.tiles:       # ffmpeg has its copies
            os.close(t.read_fd)
            t.read_fd = None
            t.task = asyncio.create_task(t.feed())
        self.pump_task = asyncio.create_task(self._pump())
        self.errors_task = asyncio.create_task(self._errors())

    async def _errors(self):
        try:
            async for line in self.proc.stderr:
                text = line.decode(errors="replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
        except Exception:
            pass

    async def _pump(self):
        """ffmpeg's picture to every reader, keeping the last seconds for the next one."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                piece = await self.proc.stdout.read(188 * 512)
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
        if self.closed or self.changing:
            return
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except Exception:
            pass
        tail = " | ".join(list(self.stderr_tail)[-3:])
        logger.warning(f"[Multiview] {self.id[:6]}: ffmpeg stopped ({self.proc.returncode}) {tail}")
        # Once more on the CPU (a channel the GPU wouldn't decode after all,
        # a stream that changed): the channels stay open, the readers wait.
        if self.restarts < MAX_RESTARTS and any(t.opened and not t.ended for t in self.tiles):
            self.restarts += 1
            if self.gpu_decode and any(t.gpu for t in self.tiles):
                self.gpu_decode = False
            try:
                await self.start()
                return
            except Exception as e:
                logger.warning(f"[Multiview] {self.id[:6]}: restart failed: {e}")
        for q in list(self.readers):
            q.put_nowait(None)

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
        picture restarts with the new layout. Readers get the new picture on
        their next connection."""
        keep = {t.channel_id: t for t in self.tiles}
        wanted = {p[0] for p in picks}
        gone = [t for t in self.tiles if t.channel_id not in wanted]
        self.changing = True
        try:
            if self.proc is not None and self.proc.returncode is None:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=5)
                except Exception:
                    pass
            if self.pump_task is not None:
                try:
                    await asyncio.wait_for(self.pump_task, timeout=5)
                except BaseException:
                    pass
            await asyncio.gather(*(t.close() for t in gone), return_exceptions=True)
            new = [_Tile(*p) for p in picks if p[0] not in keep]
            results = await asyncio.gather(*(t.open(db_factory) for t in new), return_exceptions=True)
            opened = {t.channel_id: t for t, r in zip(new, results) if not isinstance(r, BaseException)}
            failed = [r for r in results if isinstance(r, BaseException)]
            self.tiles = [keep.get(p[0]) or opened.get(p[0]) for p in picks]
            self.tiles = [t for t in self.tiles if t is not None]
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
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        await asyncio.gather(*(t.close() for t in self.tiles), return_exceptions=True)
        if self.proc is not None:
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                pass
        if self.pump_task is not None:
            self.pump_task.cancel()
        for q in list(self.readers):
            q.put_nowait(None)

    @property
    def alive(self) -> bool:
        return not self.closed and self.proc is not None and self.proc.returncode is None

    def describe(self) -> dict:
        places = layout(len(self.tiles))
        return {"id": self.id, "stream_path": f"/api/live/multiview/{self.id}/stream.ts",
                "encoder": self.encoder, "width": WIDTH, "height": HEIGHT,
                "gpu_decode": self.encoder == "vaapi" and self.gpu_decode and any(t.gpu for t in self.tiles),
                "tiles": [t.describe(i, places[i]) for i, t in enumerate(self.tiles)],
                "first_bytes_seconds": self.first_bytes_seconds}


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


def of_user(user_id: str) -> list:
    return [s for s in _sessions.values() if s.user_id == user_id and not s.closed]


def own_connections(user_id: str, only: Optional[set] = None) -> int:
    """Connections only this person's multi-view holds (channels nobody else
    reads): what closing it (or, with `only`, leaving those channels) gives back."""
    from routers import livetv as LT
    held = set()
    for s in of_user(user_id):
        for t in s.tiles:
            if only is not None and t.channel_id not in only:
                continue
            shared = LT._shared_streams.get(t.channel_id)
            if shared is not None and not shared._closed and len(shared.subscribers) <= 1:
                held.add(t.channel_id)
    return len(held)


async def start(user_id: str, picks: list, db_factory) -> Session:
    """A new session on `picks` ([(channel_id, name, key, item_id)]),
    started and producing. It replaces the person's running one (a swap, a
    removal, a new pick), which is closed only once the new one has its
    channels, so a channel in both never closes in between."""
    tiles = [_Tile(*p) for p in picks]
    session = Session(user_id, tiles)
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
        old = [s for s in of_user(user_id) if s is not session]
    for s in old:     # one multi-view per person; the replaced one last, after the new one has its channels
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

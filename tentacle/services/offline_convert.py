"""Save offline for every title: what a phone's browser can't play as it is
(an MKV, AC-3 sound on a PC, an old codec) is made into an MP4 by Tentacle
while it sends it (Nov 2026, Lucas: "I need this to work for everything";
"the server could also do that part").

A browser plays an MP4 with H.264 (or HEVC, where it can) and AAC or MP3
sound. So:

- an MP4/M4V/MOV whose streams fit goes as it is (routers/offline.py passes
  Jellyfin's Download through, byte ranges and all);
- anything else is repackaged by ffmpeg into an MP4: the picture copied
  when the device plays it (H.264 always, HEVC when the browser says so),
  the sound copied when it is AAC or MP3, else converted, the sound to AAC
  stereo and the picture to H.264 (on the GPU when there is one, as
  multi-view does). Only the default (else first) sound track; no
  subtitles. AC-3/E-AC-3 always become AAC, whatever a browser claims: no
  sound on a phone that said it plays them is worse than a little CPU.

ffmpeg reads Jellyfin's Download of the title (for a provider's title the
real video through Tentacle's relay on a "download" slot, which gives way
to anyone who wants to watch: ffmpeg then simply waits) and writes the MP4
into Tentacle's data folder (SPOOL). The browser reads that file as it
grows, from any byte (a break resumes where it stopped), several devices
from the same file. A plain MP4 is written with its index at the end and
the size of its data in the first bytes, filled in when ffmpeg finishes:
the browser asks for the first FIX_HEAD bytes again at the end (offline-
worker.js). A file whose making started over (Tentacle restarted, it was
cleaned away) has a new `gen`: a browser holding part of the old one
starts again from 0.

Kept KEEP_DONE_S after the last read once finished, so a second device or
a resume doesn't make it again; a file nobody has read for IDLE_STOP_S
while it is being made is stopped and deleted.
"""
import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from services.family import FamilyError

logger = logging.getLogger(__name__)

# What the browser plays inside an MP4 without help.
AS_IS_CONTAINERS = {"mp4", "m4v", "mov"}
ALWAYS_VIDEO = {"h264"}
ALWAYS_AUDIO = {"aac", "mp3"}
# What some browsers also play: they say so (offline.js, `can=`).
OPTIONAL = {"hevc"}

FIX_HEAD = 65536
KEEP_DONE_S = 3 * 3600
IDLE_STOP_S = 3600
MAX_JOBS = 2               # made at once; the rest wait their turn
ROOM_SPARE = 2 << 30       # left free on the server's disk besides the file
RETRY_AFTER_S = 600        # a making that failed is tried again only after this

NO_ROOM = "The server has no room to get this one ready right now. Try again later."
CANT = "The server can't get this one ready to save (no ffmpeg)."
BROKE = "The server couldn't get this one ready. Try again later."


def spool_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "/data")) / "offline-ready"


def caps_of(text: Optional[str]) -> frozenset:
    """The optional codecs a browser said it plays (`can=hevc`)."""
    return frozenset(c for c in (text or "").lower().replace(" ", "").split(",") if c in OPTIONAL)


def caps_key(caps: frozenset) -> str:
    return "-".join(sorted(caps)) or "base"


def _norm_codec(name: str) -> str:
    n = (name or "").lower()
    return {"h265": "hevc", "avc": "h264", "avc1": "h264", "hvc1": "hevc", "hev1": "hevc",
            "mp4a": "aac", "ac-3": "ac3", "e-ac-3": "eac3", "ec-3": "eac3", "mp3float": "mp3"}.get(n, n)


def streams_from_item(item: dict) -> Optional[dict]:
    """{"video": codec, "audio": [codecs]} from Jellyfin's own probe of the
    title, or None when Jellyfin hasn't looked inside it (a stream never
    played)."""
    sources = item.get("MediaSources") or []
    streams = (sources[0].get("MediaStreams") if sources else None) or []
    video = [s for s in streams if s.get("Type") == "Video"]
    if not video:
        return None
    return {"video": _norm_codec(video[0].get("Codec")),
            "audio": [_norm_codec(s.get("Codec")) for s in streams if s.get("Type") == "Audio"]}


def fits(container: str, streams: Optional[dict], caps: frozenset) -> Optional[bool]:
    """Plays in the browser as it is: True / False, None = can't tell
    without looking inside (an MP4 Jellyfin hasn't probed)."""
    if (container or "").lower() not in AS_IS_CONTAINERS:
        return False
    if streams is None:
        return None
    first_audio = streams["audio"][0] if streams["audio"] else None
    return streams["video"] in ALWAYS_VIDEO | caps and (first_audio is None or first_audio in ALWAYS_AUDIO)


def plan(probe: dict, caps: frozenset) -> dict:
    """What ffmpeg does with each stream (pure). `probe` is ffprobe's JSON."""
    streams = probe.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"
             and not (s.get("disposition") or {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        raise ValueError("no picture")
    v = video[0]
    vcodec = _norm_codec(v.get("codec_name"))
    a = next((s for s in audio if (s.get("disposition") or {}).get("default")), audio[0] if audio else None)
    acodec = _norm_codec(a.get("codec_name")) if a else None
    return {
        "video_index": v.get("index"),
        "video": "copy" if vcodec in ALWAYS_VIDEO | caps else "encode",
        "hevc": vcodec == "hevc",
        "audio_index": a.get("index") if a else None,
        "audio": None if a is None else ("copy" if acodec in ALWAYS_AUDIO else "aac"),
        "matroska": "matroska" in ((probe.get("format") or {}).get("format_name") or ""),
        "duration": float((probe.get("format") or {}).get("duration") or 0) or None,
    }


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def output_estimate(probe: dict, p: dict) -> int:
    """About how big the MP4 will be, from the bit rates of what it keeps
    (an MKV's other sound tracks, often lossless and large, are left out);
    0 when the source doesn't say (pure)."""
    if not p.get("duration"):
        return 0
    streams = {s.get("index"): s for s in probe.get("streams") or []}

    def bps(i):
        s = streams.get(i) or {}
        tags = s.get("tags") or {}
        return _int(s.get("bit_rate") or tags.get("BPS") or tags.get("BPS-eng"))

    if p["video"] != "copy":
        return 0
    video = bps(p["video_index"])
    if not video:
        return 0
    audio = 0
    if p["audio"] == "aac":
        audio = 192000
    elif p["audio"] == "copy":
        audio = bps(p["audio_index"])
    return int((video + audio) * p["duration"] / 8 * 1.02)


def input_args(url: str, token: str, matroska: bool) -> list:
    """Reading Jellyfin's Download: resumes after a break, waits through a
    pause (no read timeout: a provider's title sends nothing while someone
    watches). An MKV is read straight through (no jump to its end)."""
    return (["-headers", f"X-Emby-Token: {token}\r\n",
             "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
             "-reconnect_delay_max", "30"]
            + (["-seekable", "0"] if matroska else []) + ["-i", url])


def ffmpeg_args(p: dict, url: str, token: str, out: str, encoder: str) -> list:
    """The ffmpeg that makes the MP4 (pure apart from its inputs)."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-nostats",
            "-progress", "pipe:1", "-y"]
    if p["video"] == "encode" and encoder == "vaapi":
        args += ["-vaapi_device", "/dev/dri/renderD128"]
    args += input_args(url, token, p["matroska"])
    args += ["-map", f"0:{p['video_index']}"]
    if p["audio_index"] is not None:
        args += ["-map", f"0:{p['audio_index']}"]
    if p["video"] == "copy":
        args += ["-c:v", "copy"] + (["-tag:v", "hvc1"] if p["hevc"] else [])
    elif encoder == "vaapi":
        args += ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "22"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
    if p["audio"] == "copy":
        args += ["-c:a", "copy"]
    elif p["audio"] == "aac":
        args += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    args += ["-sn", "-dn", "-map_chapters", "-1", "-max_muxing_queue_size", "4096",
             "-fflags", "+bitexact", "-f", "mp4", out]
    return args


class Job:
    """One title made into an MP4 for one kind of device."""

    def __init__(self, key: str, item_id: str, estimate: int):
        self.key = key
        self.item_id = item_id
        self.path = spool_dir() / f"{key}.mp4"
        self.marker = spool_dir() / f"{key}.done"
        self.gen = uuid.uuid4().hex[:12]
        self.estimate = estimate or 0
        self.done = False
        self.size = 0              # final size once done
        self.error = ""
        self.fraction = 0.0        # how far ffmpeg got (by time)
        self.started = time.time()
        self.finished = 0.0
        self.last_read = time.monotonic()
        self.readers = 0
        self.proc = None
        self.task = None
        # Set once it has looked inside the title (a better estimate) or failed
        self.probed = asyncio.Event()

    def written(self) -> int:
        if self.done:
            return self.size
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def guess_total(self) -> int:
        """About how big the MP4 will be: from how far ffmpeg got, else the source's size."""
        got = self.written()
        if self.done:
            return self.size
        if self.fraction > 0.02:
            return max(got, int(got / self.fraction))
        return max(got, self.estimate)


_jobs: dict = {}
_slots: Optional[asyncio.Semaphore] = None
_sweeper: Optional[asyncio.Task] = None
_probes: dict = {}      # item id -> (time, ffprobe JSON)
PROBE_KEEP_S = 6 * 3600


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(MAX_JOBS)
    return _slots


async def probe(url: str, token: str, item_id: str) -> dict:
    """ffprobe of the title (kept a while: a resume needn't look again)."""
    hit = _probes.get(item_id)
    if hit and time.time() - hit[0] < PROBE_KEEP_S:
        return hit[1]
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise FamilyError(CANT, 409)
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
        "-probesize", "10M", "-analyzeduration", "10M", "-headers", f"X-Emby-Token: {token}\r\n", url,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        raise FamilyError(BROKE, 504)
    except asyncio.CancelledError:
        proc.kill()
        raise
    if proc.returncode != 0:
        logger.info(f"[Offline] ffprobe of {item_id} failed: {(err or b'').decode(errors='replace').strip()[-200:]}")
        raise FamilyError(BROKE, 502)
    data = json.loads(out or b"{}")
    if len(_probes) > 500:
        _probes.clear()
    _probes[item_id] = (time.time(), data)
    return data


def _done_on_disk(job: Job) -> bool:
    """Finished before Tentacle restarted: its marker says so and the size matches."""
    try:
        info = json.loads(job.marker.read_text())
        if job.path.stat().st_size == int(info["size"]):
            job.gen, job.size, job.done = info["gen"], int(info["size"]), True
            job.finished = time.time()
            return True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return False


def get_or_start(item_id: str, caps: frozenset, url: str, token: str, estimate: int, label: str) -> Job:
    """The job for this title and kind of device, started if needed."""
    if not shutil.which("ffmpeg"):
        raise FamilyError(CANT, 409)
    key = f"{item_id}-{caps_key(caps)}"
    job = _jobs.get(key)
    if job and not job.error and (job.done or (job.task and not job.task.done())):
        return job
    if job and job.error and time.time() - job.finished < RETRY_AFTER_S:
        return job      # it just failed: not again at once (the answer says so)
    spool_dir().mkdir(parents=True, exist_ok=True)
    job = Job(key, item_id, estimate)
    if _done_on_disk(job):
        _jobs[key] = job
        return job
    free = shutil.disk_usage(spool_dir()).free
    if free < (estimate or 0) * 1.1 + ROOM_SPARE:
        logger.warning(f"[Offline] No room to get {item_id} ready: {free >> 20} MB free, "
                       f"about {(estimate or 0) >> 20} MB needed")
        raise FamilyError(NO_ROOM, 507)
    for stale in (job.path, job.marker):
        try:
            stale.unlink()
        except OSError:
            pass
    job.task = asyncio.get_running_loop().create_task(_run(job, caps, url, token, label))
    _jobs[key] = job
    _ensure_sweeper()
    return job


async def _run(job: Job, caps: frozenset, url: str, token: str, label: str):
    async with _semaphore():
        try:
            info = await probe(url, token, job.item_id)
            p = plan(info, caps)
        except FamilyError as e:
            job.error, job.finished = e.message, time.time()
            return
        except (ValueError, KeyError, TypeError) as e:
            logger.info(f"[Offline] {job.item_id} can't be made into an MP4: {e}")
            job.error, job.finished = BROKE, time.time()
            return
        finally:
            job.probed.set()
        job.estimate = output_estimate(info, p) or max(job.estimate, _int((info.get("format") or {}).get("size")))
        enc = "x264"
        if p["video"] == "encode":
            from services import multiview
            enc = await multiview.encoder()
        args = ffmpeg_args(p, url, token, str(job.path), enc)
        args[0] = shutil.which("ffmpeg") or "ffmpeg"
        what = (f"picture {'copied' if p['video'] == 'copy' else 'converted (' + enc + ')'}, "
                f"sound {p['audio'] or 'none'}")
        logger.info(f"[Offline] {label}: making {job.item_id} into an MP4 ({what})")
        t0 = time.monotonic()
        cpu0 = _children_cpu()
        job.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        err_tail = bytearray()

        async def read_err():
            async for line in job.proc.stderr:
                err_tail.extend(line)
                del err_tail[:-2000]

        async def read_progress():
            async for line in job.proc.stdout:
                k, _, v = line.decode(errors="replace").strip().partition("=")
                if k == "out_time_us" and p["duration"]:
                    try:
                        job.fraction = min(1.0, int(v) / 1e6 / p["duration"])
                    except ValueError:
                        pass

        try:
            await asyncio.gather(read_err(), read_progress(), job.proc.wait())
        except asyncio.CancelledError:
            _kill(job)
            raise
        rc = job.proc.returncode
        secs = time.monotonic() - t0
        if rc == 0 and job.path.exists():
            job.size = job.path.stat().st_size
            job.done = True
            job.finished = time.time()
            job.marker.write_text(json.dumps({"gen": job.gen, "size": job.size}))
            logger.info(f"[Offline] {job.item_id} ready: {job.size >> 20} MB in {secs:.0f} s "
                        f"(ffmpeg CPU {_children_cpu() - cpu0:.0f} s)")
        else:
            logger.warning(f"[Offline] Making {job.item_id} into an MP4 failed (ffmpeg {rc}) after {secs:.0f} s: "
                           f"{err_tail.decode(errors='replace').strip()[-300:]}")
            job.error, job.finished = BROKE, time.time()
            _delete(job)


def _children_cpu() -> float:
    t = os.times()
    return t.children_user + t.children_system


def _kill(job: Job):
    if job.proc and job.proc.returncode is None:
        try:
            job.proc.kill()
        except ProcessLookupError:
            pass


def _delete(job: Job):
    for path in (job.path, job.marker):
        try:
            path.unlink()
        except OSError:
            pass


def _ensure_sweeper():
    global _sweeper
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.get_running_loop().create_task(_sweep_forever())


async def _sweep_forever():
    while _jobs:
        await asyncio.sleep(300)
        sweep()


def sweep(now: Optional[float] = None):
    """Stops what nobody reads any more and deletes what was read long ago."""
    now = time.monotonic() if now is None else now
    for key, job in list(_jobs.items()):
        idle = now - job.last_read
        if job.readers:
            continue
        failed_long_ago = job.error and time.time() - job.finished > RETRY_AFTER_S
        if failed_long_ago or (job.done and idle > KEEP_DONE_S) or (not job.done and not job.error and idle > IDLE_STOP_S):
            if not job.done and not job.error:
                logger.info(f"[Offline] Nobody is saving {job.item_id} any more: stopped")
            if job.task and not job.task.done():
                _kill(job)
                job.task.cancel()
            _delete(job)
            del _jobs[key]
    # Files left from before a restart that nobody asked for since.
    try:
        known = {j.path.name for j in _jobs.values()} | {j.marker.name for j in _jobs.values()}
        for f in spool_dir().iterdir():
            if f.name not in known and time.time() - f.stat().st_mtime > KEEP_DONE_S:
                f.unlink()
    except OSError:
        pass


async def read(job: Job, start: int, end: Optional[int] = None, chunk: int = 1 << 20):
    """The MP4's bytes from `start` (to `end`, inclusive), waiting for those
    not written yet; ends short when the making failed."""
    job.readers += 1
    pos = start
    try:
        # Not begun yet (looking inside the title, or waiting its turn)
        while not job.path.exists() and not (job.done or job.error):
            job.last_read = time.monotonic()
            await asyncio.sleep(0.5)
        with open(job.path, "rb") as f:
            while end is None or pos <= end:
                have = job.written()
                if pos < have:
                    want = min(chunk, have - pos) if end is None else min(chunk, have - pos, end + 1 - pos)
                    data = await asyncio.to_thread(_pread, f, pos, want)
                    if not data:
                        await asyncio.sleep(0.5)
                        continue
                    pos += len(data)
                    job.last_read = time.monotonic()
                    yield data
                    continue
                if job.done or job.error:
                    break
                job.last_read = time.monotonic()
                await asyncio.sleep(0.5)
    except FileNotFoundError:
        return
    finally:
        job.readers -= 1
        job.last_read = time.monotonic()


def _pread(f, pos: int, n: int) -> bytes:
    f.seek(pos)
    return f.read(n)


def forget_all():
    """For tests: stop and forget every job."""
    global _sweeper, _slots
    for job in _jobs.values():
        _kill(job)
        if job.task and not job.task.done():
            job.task.cancel()
    _jobs.clear()
    _probes.clear()
    _sweeper = None
    _slots = None

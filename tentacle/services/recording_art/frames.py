"""The best of a few frames from a recording, finished or still recording.

A finished recording is read through Jellyfin's stream of the item (GET
/Videos/{id}/stream, static, with a byte Range: no recordings folder
mounted, no provider connection), a few MB at each of 30/50/70% of the
programme's own time (after the pre-padding). One still recording is read
through GET /Items/{id}/File (Jellyfin's stream of a growing file ignores
Range), a few MB at each of LIVE_FRAMES points spread over the last
LIVE_SPAN of what is written so far (live_offsets: longer than an ad
break, so the programme's own frames are most of them). ffmpeg decodes the
first key frame of each piece at the lowest priority, with a timeout, and
signalstats says how bright and how spread its light is: black and flat
frames (a fade, a slate, a blank channel) are left out. Of the rest, the
most detailed wins, but a frame that looks like an ad break comes after
any that doesn't (the cheap signals of the ad-breaks research): its sound
is silent (a break's edge is black + silence), or the station's logo most
frames share is missing from it (logo_missing). That is a guard, not a
detector: an ad can still win when most frames are ads, and a graphic of
the programme's own can win over its pictures, which is why the cards and
TMDB come first.

Only MPEG-TS recordings (Jellyfin's own format) are read this way: a byte
in the middle of a .ts is a place to start decoding; in an .mkv or .mp4 it
isn't.
"""
import io
import logging
import math
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

FRACTIONS = (0.3, 0.5, 0.7)
LIVE_FRAMES = 8                     # candidates from a growing recording
LIVE_SPAN = 14 * 60                 # spread over at most this much before what it has written
PTS_BYTES = 1024 * 1024             # read at each end of a growing recording for its timestamps
PTS_WRAP = 2 ** 33 / 90000          # MPEG-TS timestamps wrap after 26.5 h
MAX_WRITTEN = 24 * 3600
READ_BYTES = 6 * 1024 * 1024        # a few seconds of HD video: a key frame is in there
FFMPEG_TIMEOUT = 30
READ_TIMEOUT = 30
TS_CONTAINERS = {"ts", "mpegts", "m2ts", "mts"}
BLACK_LUMA = 26                     # mean luma (16 = black in TV range)
FLAT_SPREAD = 22                    # 90th minus 10th percentile of luma
MIN_DETAIL = 2.0
SILENT_DB = -50.0                   # mean volume of a piece: silent (volumedetect)
LOGO_SIZE = (320, 180)              # the logo check's scale; its corners: LOGO_WIDTH x LOGO_BAND
LOGO_BAND = 0.25
LOGO_WIDTH = 0.3
LOGO_EDGE = 40                      # an edge pixel (FIND_EDGES)
LOGO_COMMON = 0.35                  # of the frames (2 at least) an edge pixel is in to be the logo's
LOGO_MIN = 20                       # logo pixels, else there is no logo to go by
LOGO_SHARE = 0.4                    # of the share of them the frame with most has


class FramesFailed(Exception):
    """No frame could be taken (ffmpeg missing, failing or too slow; the stream unreadable)."""


@dataclass
class Frame:
    data: bytes          # a JPEG
    detail: float
    seconds: float       # where in the recording
    silent: bool = False
    note: str = ""


def offsets(duration: float, pre_padding: float = 0.0, programme: Optional[float] = None) -> list:
    """Seconds into the recording for each candidate: FRACTIONS of the
    programme's own time, after the pre-padding (the whole recording when
    the programme's length isn't known)."""
    if duration <= 0:
        return []
    pre = max(0.0, min(pre_padding or 0.0, duration * 0.5))
    length = programme if programme and programme > 0 else duration - pre
    length = min(length, duration - pre)
    return [round(min(duration - 5, pre + f * length), 1) for f in FRACTIONS if pre + f * length < duration - 5]


def live_offsets(written: float, pre_padding: float = 0.0) -> list:
    """Seconds into a recording still recording, `written` seconds so far:
    LIVE_FRAMES spread evenly over the last LIVE_SPAN of it (20 s from its
    end), inside the programme (30 s past the pre-padding at least)."""
    hi = written - 20
    span = min(hi - (max(0.0, pre_padding or 0.0) + 30), LIVE_SPAN)
    if span < 0:
        return []
    return [round(hi - span * k / (LIVE_FRAMES - 1), 1) for k in range(LIVE_FRAMES)]


def pts_of(chunk: bytes, last: bool, timeout: int = FFMPEG_TIMEOUT) -> Optional[float]:
    """The first (or last) video timestamp, in seconds, in a piece of
    MPEG-TS, or None (no ffprobe, nothing readable)."""
    cmd = ["ffprobe", "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time", "-of", "csv=p=0"]
    try:
        r = subprocess.run(cmd, input=chunk, capture_output=True, timeout=timeout, preexec_fn=_lowest_priority)
    except (OSError, subprocess.TimeoutExpired):
        return None
    times = []
    for line in (r.stdout or b"").decode(errors="replace").splitlines():
        try:
            times.append(float(line.strip().strip(",")))
        except ValueError:
            continue
    return (max(times) if last else min(times)) if times else None


def written_seconds(read: Callable[[int, int], bytes], size: int, probe: Callable = pts_of) -> Optional[float]:
    """How many seconds a recording still recording holds so far: its last
    video timestamp minus its first (not the clock: Jellyfin's DateCreated
    of one in progress is its NFO's local time). None when they can't be
    read or make no sense (a re-dial that brought another clock)."""
    if size < 2 * 188:
        return None
    first = probe(read(0, PTS_BYTES), False)
    start = max(0, size - PTS_BYTES)
    last = probe(read(start - start % 188, PTS_BYTES), True)
    if first is None or last is None:
        return None
    span = last - first
    if span < 0:
        span += PTS_WRAP
    return span if 0 < span <= MAX_WRITTEN else None


def _lowest_priority():
    try:
        os.nice(19)
    except OSError:
        pass


def grab(chunk: bytes, timeout: int = FFMPEG_TIMEOUT) -> Optional[tuple]:
    """(JPEG, signalstats) of the first key frame in a piece of MPEG-TS, or
    None when there is none. Raises FramesFailed when ffmpeg can't run."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-skip_frame", "nokey",
           "-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0", "-frames:v", "1",
           "-vf", "signalstats,metadata=mode=print", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"]
    try:
        r = subprocess.run(cmd, input=chunk, capture_output=True, timeout=timeout, preexec_fn=_lowest_priority)
    except FileNotFoundError:
        raise FramesFailed("ffmpeg isn't installed")
    except subprocess.TimeoutExpired:
        raise FramesFailed(f"ffmpeg took longer than {timeout} s")
    if r.returncode != 0 or not r.stdout:
        return None
    stats = {k.decode(): float(v) for k, v in re.findall(rb"lavfi\.signalstats\.(\w+)=([\d.]+)", r.stderr or b"")}
    volume = _volume(chunk, timeout)
    if volume is not None:
        stats["VOLUME"] = volume
    return r.stdout, stats


def _volume(chunk: bytes, timeout: int) -> Optional[float]:
    """The mean volume (dB) of a piece's first sound track, or None (none, or unreadable)."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-f", "mpegts", "-i", "pipe:0",
           "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, input=chunk, capture_output=True, timeout=timeout, preexec_fn=_lowest_priority)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(rb"mean_volume: (-?[\d.]+|-inf) dB", r.stderr or b"")
    if r.returncode != 0 or not m:
        return None
    return -200.0 if m.group(1) == b"-inf" else float(m.group(1))


def detail_of(jpeg: bytes) -> float:
    """How much is in a picture: the mean strength of its edges."""
    from PIL import Image, ImageFilter, ImageStat
    img = Image.open(io.BytesIO(jpeg)).convert("L").resize((320, 180))
    return float(ImageStat.Stat(img.filter(ImageFilter.FIND_EDGES)).mean[0])


def judge(jpeg: bytes, stats: dict) -> tuple:
    """(detail, None) for a usable frame, or (0, why not)."""
    yavg = stats.get("YAVG")
    if yavg is not None and yavg < BLACK_LUMA:
        return 0.0, f"black (mean light {yavg:.0f})"
    lo, hi = stats.get("YLOW"), stats.get("YHIGH")
    if lo is not None and hi is not None and hi - lo < FLAT_SPREAD:
        return 0.0, f"flat (light {lo:.0f}-{hi:.0f})"
    try:
        detail = detail_of(jpeg)
    except Exception:
        return 0.0, "not a picture"
    if detail < MIN_DETAIL:
        return 0.0, f"flat (detail {detail:.1f})"
    return detail, None


def _logo_pixels(jpeg: bytes) -> set:
    """The edge pixels in the corners of a frame, where a station's logo
    and a score bug sit."""
    from PIL import Image, ImageFilter
    w, h = LOGO_SIZE
    img = Image.open(io.BytesIO(jpeg)).convert("L").resize(LOGO_SIZE).filter(ImageFilter.FIND_EDGES)
    px = img.load()
    band, side = int(h * LOGO_BAND), int(w * LOGO_WIDTH)
    return {(x, y) for y in list(range(1, band)) + list(range(h - band, h - 1))
            for x in list(range(1, side)) + list(range(w - side, w - 1)) if px[x, y] >= LOGO_EDGE}


def logo_missing(frames: list) -> list:
    """For each of three or more frames: True when it has few of the edges
    in the corners that LOGO_COMMON of the frames share (a station's logo,
    a score bug: ads have neither), less than LOGO_SHARE of the share the
    frame with most of them has. Fewer than three frames, or nothing
    shared: all False. Measured on 16 frames of a golf recording (30 Oct
    2026): the frames with the channel's bug had 0.31-0.77 of them, the
    full-screen ads 0.01-0.15."""
    if len(frames) < 3:
        return [False] * len(frames)
    try:
        pixels = [_logo_pixels(f.data) for f in frames]
    except Exception:
        return [False] * len(frames)
    counts: dict = {}
    for p in pixels:
        for xy in p:
            counts[xy] = counts.get(xy, 0) + 1
    need = max(2, math.ceil(len(frames) * LOGO_COMMON))
    shared = {xy for xy, n in counts.items() if n >= need}
    if len(shared) < LOGO_MIN:
        return [False] * len(frames)
    share = [len(shared & mine) / len(shared) for mine in pixels]
    return [s < LOGO_SHARE * max(share) for s in share]


def best_frame(read: Callable[[int, int], bytes], size: int, duration: float, at: list,
               grabber: Callable = grab) -> tuple:
    """(the best usable Frame or None, what happened to each candidate).
    `read(start, length)` returns that many bytes of the recording from
    `start`. The most detailed wins, after any that looks like an ad
    break (silent, or without the logo the others share). Raises
    FramesFailed when ffmpeg can't run at all."""
    usable, notes = [], []
    for seconds in at:
        start = int(size * seconds / duration) if duration else 0
        start = max(0, min(start - start % 188, size - 188))   # on a TS packet
        try:
            chunk = read(start, READ_BYTES)
        except Exception as e:
            notes.append(f"{seconds:.0f} s: not read ({type(e).__name__})")
            continue
        got = grabber(chunk)
        if not got:
            notes.append(f"{seconds:.0f} s: no frame")
            continue
        jpeg, stats = got
        detail, why = judge(jpeg, stats)
        if why:
            notes.append(f"{seconds:.0f} s: {why}")
            continue
        silent = stats.get("VOLUME") is not None and stats["VOLUME"] < SILENT_DB
        frame = Frame(jpeg, detail, seconds, silent, f"{seconds:.0f} s: detail {detail:.1f}" + (", silent" if silent else ""))
        notes.append(frame.note)
        usable.append(frame)
    no_logo = logo_missing(usable)
    for f, missing in zip(usable, no_logo):
        if missing:
            notes[notes.index(f.note)] = f.note = f.note + ", no station logo"
    ranked = sorted(zip(usable, no_logo), key=lambda fm: (fm[0].silent or fm[1], -fm[0].detail))
    return (ranked[0][0] if ranked else None), notes

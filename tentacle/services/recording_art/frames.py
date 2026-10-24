"""The best of a few frames from a finished recording.

Read through Jellyfin's stream of the item (GET /Videos/{id}/stream,
static, with a byte Range: no recordings folder mounted, no provider
connection), a few MB at each of 30/50/70% of the programme's own time
(after the pre-padding). ffmpeg decodes the first key frame of each piece at
the lowest priority, with a timeout, and signalstats says how bright and how
spread its light is: black and flat frames (a fade, a slate, a blank
channel) are left out, and the most detailed of the rest wins. A commercial
can't be told from the game, which is why the cards and TMDB come first.

Only MPEG-TS recordings (Jellyfin's own format) are read this way: a byte
in the middle of a .ts is a place to start decoding; in an .mkv or .mp4 it
isn't.
"""
import io
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

FRACTIONS = (0.3, 0.5, 0.7)
READ_BYTES = 6 * 1024 * 1024        # a few seconds of HD video: a key frame is in there
FFMPEG_TIMEOUT = 30
READ_TIMEOUT = 30
TS_CONTAINERS = {"ts", "mpegts", "m2ts", "mts"}
BLACK_LUMA = 26                     # mean luma (16 = black in TV range)
FLAT_SPREAD = 22                    # 90th minus 10th percentile of luma
MIN_DETAIL = 2.0


class FramesFailed(Exception):
    """No frame could be taken (ffmpeg missing, failing or too slow; the stream unreadable)."""


@dataclass
class Frame:
    data: bytes          # a JPEG
    detail: float
    seconds: float       # where in the recording


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
    return r.stdout, stats


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


def best_frame(read: Callable[[int, int], bytes], size: int, duration: float, at: list,
               grabber: Callable = grab) -> tuple:
    """(the most detailed usable Frame or None, what happened to each candidate).
    `read(start, length)` returns that many bytes of the recording from
    `start`. Raises FramesFailed when ffmpeg can't run at all."""
    best, notes = None, []
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
        notes.append(f"{seconds:.0f} s: detail {detail:.1f}")
        if best is None or detail > best.detail:
            best = Frame(jpeg, detail, seconds)
    return best, notes

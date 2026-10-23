""""Nothing on right now": what a viewer gets from an event or PPV channel
with nothing on, instead of an error.

An event channel ("ESPN+: Houston vs. Baylor (2026-10-23 20:00)") whose
event starts later, or a channel the provider answers with its placeholder
(black.ts), used to make the viewer wait 20-60 s while the provider was asked
and re-asked, and then fail with nothing to say (Jellyfin shows "playback
error", the TV app went back to where it was). Now the viewer gets, at once,
a short MPEG-TS stream that says so: "Nothing on right now", the event and
when it starts. It plays on every client (Jellyfin Web, the TV app, phones)
because it is just a channel picture; Jellyfin needs nothing new.

The same picture, with its own title, tells a viewer when the provider
doesn't send a channel ("Not coming through") or when someone else holds the
provider's connections ("Connection in use"), instead of a spinner and a
playback error.

Only viewers get it. A recording of such a channel is still refused (503,
Jellyfin retries a minute later) so a recording never saves the slate.

The picture is made by ffmpeg (in the image, with the DejaVu fonts), 1280x720
H.264 + AAC stereo like a typical channel (Jellyfin may cache a channel's
probe), and kept per text. Without ffmpeg there is no slate and the open
fails as before.
"""
import asyncio
import logging
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

TITLE = "Nothing on right now"
# An event that starts later than this is not asked for: providers switch an
# event's stream on shortly before it starts, and until then answer slowly
# (20 s seen) with their placeholder.
EARLY_LEAD = timedelta(minutes=30)
SECONDS = 30            # how long the slate plays before the stream ends
_BURST_SECONDS = 4      # sent at once, so the player starts at once; the rest in real time
_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF")
_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_CACHE_SIZE = 16
_lock: "asyncio.Lock | None" = None


def _when(start: datetime, now: datetime, tz) -> str:
    """'today at 8:00 PM', 'tomorrow at ...', 'Friday at ...', 'Oct 30 at ...'."""
    s, n = start.astimezone(tz), now.astimezone(tz)
    clock = s.strftime("%I:%M %p").lstrip("0")
    days = (s.date() - n.date()).days
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if 1 < days < 7:
        return f"{s.strftime('%A')} at {clock}"
    return f"{s.strftime('%b')} {s.day} at {clock}"


def early(event, now: Optional[datetime] = None, tz=None) -> Optional[tuple]:
    """The slate's two lines for an event channel opened well before its event
    (or one whose name says there is no event), or None: then ask the provider.
    `event` is livetv_events.parse's answer for the channel."""
    if event == "no_event":
        return ("No event on this channel at the moment", "")
    if not isinstance(event, dict) or not event.get("timed") or not event.get("start"):
        return None
    now = now or datetime.now(timezone.utc)
    if event["start"] - now <= EARLY_LEAD:
        return None
    tz = tz or datetime.now().astimezone().tzinfo
    return (event.get("title") or "The event", "starts " + _when(event["start"], now, tz))


def not_coming_through() -> tuple:
    """The slate when the provider doesn't deliver a channel (refused, no
    data): the viewer is told it's the provider, not their screen."""
    return ("Not coming through", "Your provider isn't sending this channel",
            "Try again in a minute or pick another channel")


def connection_in_use(limit: int = 1) -> tuple:
    """The slate when the provider's connections are all taken by others."""
    first = ("Someone else is using your provider connection" if limit <= 1
             else f"All {limit} provider connections are in use")
    return ("Connection in use", first, "Try again when someone stops watching")


def unavailable(event, channel_name: str) -> tuple:
    """The slate's two lines when the provider served its placeholder."""
    if isinstance(event, dict) and event.get("title"):
        return (event["title"], "isn't on yet, or has ended")
    return (channel_name or "This channel", "has no live event at the moment")


def _font() -> Optional[str]:
    for d in _FONT_DIRS:
        for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return None


async def _render(lines: tuple) -> Optional[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    font = _font()
    if not ffmpeg or not font:
        logger.warning("[LiveTV] No ffmpeg or font for the 'Nothing on right now' picture")
        return None
    with tempfile.TemporaryDirectory(prefix="tentacle-slate-") as d:
        # (line, line) under "Nothing on right now", or (title, line, line)
        title, lines = (lines[0], lines[1:]) if len(lines) == 3 else (TITLE, lines)
        texts = [(title, 60, -90, "white"), (lines[0][:70], 40, 10, "0xDDE3EA"), (lines[1][:70], 34, 70, "0x9AA4AF")]
        draws = []
        for i, (text, size, dy, color) in enumerate(texts):
            if not text:
                continue
            path = os.path.join(d, f"{i}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            draws.append(f"drawtext=fontfile='{font}':textfile='{path}':fontsize={size}:fontcolor={color}"
                         f":x=(w-text_w)/2:y=(h-text_h)/2+{dy}")
        args = [ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c=0x101418:s=1280x720:r=30:d={SECONDS}",
                "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={SECONDS}",
                "-vf", ",".join(draws), "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-b:a", "64k",
                "-shortest", "-f", "mpegts", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("[LiveTV] The 'Nothing on right now' picture took too long to make")
            return None
    if proc.returncode != 0 or not out:
        logger.warning(f"[LiveTV] The 'Nothing on right now' picture failed: "
                       f"{err.decode('utf-8', 'replace')[-300:]}")
        return None
    return out


async def picture(lines: tuple) -> Optional[bytes]:
    """The slate for these two lines, or a title and two lines (MPEG-TS,
    SECONDS long), made once per text."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if lines in _cache:
            _cache.move_to_end(lines)
            return _cache[lines]
        began = time.monotonic()
        data = await _render(lines)
        if data:
            _cache[lines] = data
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
            logger.info(f"[LiveTV] Made the 'Nothing on right now' picture ({lines[0]!r}) "
                        f"in {time.monotonic() - began:.1f}s")
        return data


async def paced(data: bytes, chunk: int = 188 * 64):
    """The slate's bytes: the first seconds at once (the player starts at
    once), the rest about as fast as it plays, so it shows for SECONDS."""
    burst = len(data) * _BURST_SECONDS // SECONDS
    per_byte = SECONDS / max(1, len(data))
    for i in range(0, len(data), chunk):
        piece = data[i:i + chunk]
        if i >= burst:
            await asyncio.sleep(len(piece) * per_byte)
        yield piece

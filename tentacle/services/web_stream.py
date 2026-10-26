"""Live pictures a web browser can play: fragmented MP4.

Browsers play neither MPEG-TS nor a stream with several sound tracks they
can switch between, so Jellyfin Web's Live TV gets its multi-view picture
and its guide's muted preview as fragmented MP4 (played through Media
Source Extensions):

- multi-view: the session's picture as it is (no encode), with the one
  sound track of the tile being heard; another tile heard = another read
  (the page swaps players once the new one plays).
- preview: one channel, small (360p), no sound, made by a cheap encode, so
  every channel plays whatever its codec or interlacing.

Both read until the browser goes away; ffmpeg is stopped with the read.
"""
import asyncio
import logging
from typing import AsyncIterator, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

PIECE = 64 * 1024
# Fragments start on keyframes (multi-view's encoder makes one a second;
# the preview's encode one a second too), each sent as soon as it is made.
# delay_moov: the header waits for the first fragment, so a copied AAC track
# carries its config (aac_adtstoasc makes it from the first packet); without
# it browsers refuse the stream.
FMP4 = ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof+delay_moov",
        "-flush_packets", "1", "pipe:1"]
PREVIEW_HEIGHT = 360


def multiview_args(ffmpeg: str, audio: int) -> list:
    """The multi-view picture (MPEG-TS on stdin: one video, a sound track per
    tile) as fMP4 with only sound track `audio`. Copied, not encoded."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-fflags", "+genpts+discardcorrupt", "-analyzeduration", "1000000", "-probesize", "2000000",
            "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", f"0:a:{int(audio)}?", "-c", "copy", "-bsf:a", "aac_adtstoasc"] + FMP4


def preview_args(ffmpeg: str) -> list:
    """One channel (MPEG-TS on stdin) as a small silent fMP4: deinterlaced
    when it is interlaced, 360p, H.264 (ultrafast: Constrained Baseline, which
    every browser plays), a keyframe a second."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-fflags", "+genpts+discardcorrupt", "-analyzeduration", "2000000", "-probesize", "3000000",
            "-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", f"yadif=mode=0:deint=interlaced,scale=-2:{PREVIEW_HEIGHT},fps=30000/1001",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-b:v", "900k", "-maxrate", "1200k", "-bufsize", "1200k",
            "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-threads", "2"] + FMP4


def _nice():
    import os
    try:
        os.nice(5)
    except OSError:
        pass


async def fmp4(source: AsyncIterator[bytes], args: list,
               done: Optional[Callable[[], Awaitable[None]]] = None) -> AsyncIterator[bytes]:
    """ffmpeg `args` fed with `source` and read out, for as long as the
    reader reads; then ffmpeg is stopped, `source` closed and `done` awaited."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, preexec_fn=_nice)

    async def feed():
        try:
            async for piece in source:
                proc.stdin.write(piece)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.info(f"[WebStream] source ended: {e}")
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    async def errors():
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    logger.debug(f"[WebStream] ffmpeg: {text[:200]}")
        except Exception:
            pass

    feeder = asyncio.create_task(feed())
    err = asyncio.create_task(errors())
    try:
        while True:
            piece = await proc.stdout.read(PIECE)
            if not piece:
                break
            yield piece
    finally:
        feeder.cancel()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        err.cancel()
        for t in (feeder, err):
            try:
                await t
            except BaseException:
                pass
        close = getattr(source, "aclose", None)
        if close is not None:
            try:
                await close()
            except BaseException:
                pass
        if done is not None:
            try:
                await done()
            except Exception as e:
                logger.info(f"[WebStream] closing: {e}")

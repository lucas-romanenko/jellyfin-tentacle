"""Thin wrapper over yt-dlp.

Kept separate so the indexer and the resolver share one place that knows how to
talk to YouTube — the client chain, the JS runtime, and the error mapping — and
so it can be stubbed in tests without touching the network.
"""
import logging
from typing import Optional

from services.youtube.errors import classify

logger = logging.getLogger(__name__)

# The extraction client chain, in order of preference.
#
# visionos returns a full HLS VOD master with MPEG-TS segments and needs neither
# a PO token nor a JS runtime — and MPEG-TS matters, because jellyfin-ffmpeg
# 7.1.3 cannot seek HLS whose segments are fMP4 (it produces NAL-unit errors and
# corrupt output). web_embedded covers "made for kids" videos, which visionos
# reports as UNPLAYABLE, but only offers DASH.
PLAYER_CLIENTS = ["visionos", "web_embedded"]

_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noprogress": True,
    "ignore_no_formats_error": True,
}


def _ydl(extra: dict = None):
    import yt_dlp
    opts = dict(_BASE_OPTS)
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def available() -> bool:
    """Whether yt-dlp can be imported. The feature stays off without it."""
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def version() -> Optional[str]:
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return None


def extract(url: str, extra_opts: dict = None) -> dict:
    """Run an extraction, translating yt-dlp errors into ours.

    Raises YouTubeBlocked / YouTubeUnavailable / VideoUnavailable — never
    returns an empty result to mean failure, because a caller that prunes on an
    empty list would then wipe the channel.
    """
    try:
        with _ydl(extra_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # yt_dlp raises its own hierarchy
        raise classify(e) from e
    if info is None:
        raise classify(Exception(f"yt-dlp returned nothing for {url}"))
    return info


def flat_listing(url: str, limit: int) -> dict:
    """Cheap listing of a channel tab or playlist — ids and titles only.

    ~1s and, in testing, never bot-checked. `live_status` is present but
    `release_timestamp` is not, so scheduled streams need a per-video call.
    """
    return extract(url, {"extract_flat": True, "playlistend": max(1, limit)})


def video_details(video_id: str, js_runtime: str = None) -> dict:
    """Full metadata for one video (duration, upload date, live status)."""
    extra = {}
    if js_runtime:
        extra["js_runtimes"] = {js_runtime: {}}
    return extract(f"https://www.youtube.com/watch?v={video_id}", extra)

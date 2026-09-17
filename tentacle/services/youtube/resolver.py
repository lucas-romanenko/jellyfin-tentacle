"""Resolve a YouTube video to a playable, proxied HLS playlist.

Google's media URLs expire in roughly six hours and are signed to the IP that
extracted them, so they can never be handed to a client. Instead the resolver
fetches the HLS master, caches it briefly, and rewrites every URL to point back
at Tentacle — which then streams the bytes.

Client choice is deliberate: `visionos` returns MPEG-TS segments, and
jellyfin-ffmpeg 7.1.3 cannot seek HLS whose segments are fMP4 (it emits
"Invalid NAL unit size" and produces corrupt output). Verified against real
YouTube: the visionos master carries no #EXT-X-MAP and ends with #EXT-X-ENDLIST.
"""
import logging
import threading
import time
from typing import Optional

from services.youtube import client
from services.youtube.errors import YouTubeError

logger = logging.getLogger(__name__)

# Google's URLs last ~6h; re-resolve comfortably inside that.
CACHE_TTL_SECONDS = 4 * 3600

_cache: dict = {}
_cache_lock = threading.Lock()
# One extraction per video at a time — a Jellyfin PlaybackInfo probe can arrive
# several times in parallel for the same item.
_inflight: dict = {}
_inflight_lock = threading.Lock()


class ResolvedVideo:
    __slots__ = ("video_id", "master_url", "headers", "expires_at", "duration")

    def __init__(self, video_id, master_url, headers, duration=None):
        self.video_id = video_id
        self.master_url = master_url
        self.headers = headers or {}
        self.duration = duration
        self.expires_at = time.time() + CACHE_TTL_SECONDS

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


def _extract(video_id: str, max_height: int) -> ResolvedVideo:
    last_error = None
    for player_client in client.PLAYER_CLIENTS:
        try:
            info = client.extract(
                f"https://www.youtube.com/watch?v={video_id}",
                {"extractor_args": {"youtube": {"player_client": [player_client]}}},
            )
        except YouTubeError as e:
            last_error = e
            logger.debug(f"[YouTube] {player_client} failed for {video_id}: {e}")
            continue

        master, headers = _pick_hls_master(info, max_height)
        if master:
            logger.info(f"[YouTube] Resolved {video_id} via {player_client}")
            return ResolvedVideo(video_id, master, headers, info.get("duration"))
        last_error = YouTubeError(f"{player_client} returned no usable HLS")

    raise last_error or YouTubeError(f"Could not resolve {video_id}")


def _pick_hls_master(info: dict, max_height: int) -> tuple:
    """The HLS master URL and the headers needed to fetch it."""
    formats = info.get("formats") or []
    hls = [f for f in formats
           if (f.get("protocol") or "").startswith("m3u8") and f.get("manifest_url")]
    if not hls:
        return None, None
    # Every HLS variant shares one master manifest; the ladder is filtered
    # later, when the playlist is rewritten.
    preferred = [f for f in hls if (f.get("height") or 0) <= max_height] or hls
    chosen = preferred[0]
    return chosen.get("manifest_url"), chosen.get("http_headers")


def resolve(video_id: str, max_height: int = 1080, force: bool = False) -> ResolvedVideo:
    """Cached, single-flight resolution."""
    if not force:
        with _cache_lock:
            hit = _cache.get(video_id)
            if hit and not hit.expired:
                return hit

    with _inflight_lock:
        lock = _inflight.setdefault(video_id, threading.Lock())

    with lock:
        if not force:
            with _cache_lock:
                hit = _cache.get(video_id)
                if hit and not hit.expired:
                    return hit
        resolved = _extract(video_id, max_height)
        with _cache_lock:
            _cache[video_id] = resolved
        return resolved


def invalidate(video_id: str) -> None:
    with _cache_lock:
        _cache.pop(video_id, None)


def cache_size() -> int:
    with _cache_lock:
        return len(_cache)

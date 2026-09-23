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
from services.youtube.errors import YouTubeBlocked, YouTubeError, YouTubeUnavailable

logger = logging.getLogger(__name__)

# Google's URLs last ~6h; re-resolve comfortably inside that.
CACHE_TTL_SECONDS = 4 * 3600

_cache: dict = {}
_cache_lock = threading.Lock()
# One extraction per video at a time — a Jellyfin PlaybackInfo probe can arrive
# several times in parallel for the same item.
_inflight: dict = {}
_inflight_lock = threading.Lock()

# Videos that failed to resolve, and when to try again. Jellyfin re-probes a
# .strm on every PlaybackInfo and on scans, so an unplayable video (members-
# only, no HLS for any client) was re-extracted over and over — seven 502s a
# night for one item. The back-off doubles per consecutive failure. A network
# hiccup gets a short fixed pause instead, so a working video is never held
# back for long; a bot check is never recorded here, because it says nothing
# about this video and warming already stops on it.
FAILURE_BACKOFF_START = 10 * 60
FAILURE_BACKOFF_MAX = 6 * 3600
TRANSIENT_BACKOFF = 60
_failures: dict = {}  # video_id -> (retry_at, consecutive_failures, message)


class ResolveBackoff(YouTubeError):
    """A recent attempt failed; not trying YouTube again until the back-off ends."""

    def __init__(self, message: str, retry_in: int):
        super().__init__(message)
        self.retry_in = retry_in


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


def _check_backoff(video_id: str) -> None:
    with _cache_lock:
        failed = _failures.get(video_id)
    if not failed:
        return
    retry_at, count, message = failed
    remaining = int(retry_at - time.time())
    if remaining > 0:
        raise ResolveBackoff(
            f"{message} (failed {count}x; next attempt in {remaining}s)", remaining)


def _record_failure(video_id: str, error: Exception) -> None:
    if isinstance(error, YouTubeBlocked):
        return
    with _cache_lock:
        _, count, _ = _failures.get(video_id, (0, 0, ""))
        count += 1
        if isinstance(error, YouTubeUnavailable):
            delay = TRANSIENT_BACKOFF
        else:
            delay = min(FAILURE_BACKOFF_START * 2 ** (count - 1), FAILURE_BACKOFF_MAX)
        _failures[video_id] = (time.time() + delay, count, str(error))
    logger.info(f"[YouTube] {video_id} could not be resolved ({count}x); "
                f"not retrying for {delay}s: {error}")


def resolve(video_id: str, max_height: int = 1080, force: bool = False,
            backoff: bool = True) -> ResolvedVideo:
    """Cached, single-flight resolution.

    Raises ResolveBackoff without calling YouTube while a recent failure is
    still backing off; `force` skips the back-off along with the cache.
    `backoff=False` neither checks nor records failures — for a live stream,
    whose broadcast can start working at any moment.
    """
    backoff = backoff and not force
    if not force:
        with _cache_lock:
            hit = _cache.get(video_id)
            if hit and not hit.expired:
                return hit
    if backoff:
        _check_backoff(video_id)

    with _inflight_lock:
        lock = _inflight.setdefault(video_id, threading.Lock())

    with lock:
        if not force:
            with _cache_lock:
                hit = _cache.get(video_id)
                if hit and not hit.expired:
                    return hit
        if backoff:
            # A parallel probe may have just failed while this one waited.
            _check_backoff(video_id)
        try:
            resolved = _extract(video_id, max_height)
        except YouTubeError as e:
            if backoff:
                _record_failure(video_id, e)
            raise
        with _cache_lock:
            _cache[video_id] = resolved
            _failures.pop(video_id, None)
        return resolved


# Video codecs ffmpeg's mpegts muxer can carry in a stream copy. VP9 and AV1
# are absent from MPEG-TS, so a rendition in either cannot be remuxed at all.
_TS_VCODECS = ("avc1", "avc3", "h264", "hev1", "hvc1", "h265")


def _ts_muxable(fmt: dict) -> bool:
    return (fmt.get("vcodec") or "").lower().startswith(_TS_VCODECS)


def _codec_rank(fmt: dict) -> int:
    """H.264 ahead of HEVC at the same height."""
    return 0 if (fmt.get("vcodec") or "").lower().startswith(("avc", "h264")) else 1


def pick_tracks(video_id: str, max_height: int = 1080) -> tuple:
    """(video_playlist_url, audio_playlist_url, headers) for a live remux.

    YouTube's HLS variants are video-only with audio in a separate rendition.
    Handing ffmpeg the master leaves it to choose, and it picks the first
    variant — 240p. Selecting the tracks ourselves is the only way to honour
    the channel's quality setting.
    """
    for player_client in client.PLAYER_CLIENTS:
        try:
            info = client.extract(
                f"https://www.youtube.com/watch?v={video_id}",
                {"extractor_args": {"youtube": {"player_client": [player_client]}}},
            )
        except YouTubeError as e:
            logger.debug(f"[YouTube] {player_client} failed for {video_id}: {e}")
            continue

        hls = [f for f in (info.get("formats") or [])
               if (f.get("protocol") or "").startswith("m3u8") and f.get("url")]
        if not hls:
            continue

        # Split on vcodec alone. yt-dlp reports an audio-only HLS rendition with
        # acodec=None (unknown) rather than a codec name, so testing acodec
        # excluded every audio track and the stream came out silent.
        videos = [f for f in hls if (f.get("vcodec") or "none") != "none"]
        audios = [f for f in hls if (f.get("vcodec") or "none") == "none"]

        # The live endpoint remuxes with `-c copy -f mpegts`, which cannot carry
        # VP9 or AV1, so those are not candidates at all.
        videos = [f for f in videos if _ts_muxable(f)]
        within = [f for f in videos if (f.get("height") or 0) <= max_height]
        if within:
            within.sort(key=lambda f: (-(f.get("height") or 0), _codec_rank(f)))
            best_video = within[0]
        elif videos:
            # Nothing fits the cap: the smallest overshoots it least. Sorting the
            # whole ladder by -height made a 480p channel stream 2160p.
            videos.sort(key=lambda f: ((f.get("height") or 0), _codec_rank(f)))
            best_video = videos[0]
        else:
            continue

        best_audio = None
        if audios:
            audios.sort(key=lambda f: -(f.get("tbr") or f.get("abr") or 0))
            best_audio = audios[0]
        # No separate rendition means the video track already carries audio.

        logger.info(
            f"[YouTube] {video_id}: {player_client} "
            f"{best_video.get('height')}p {best_video.get('vcodec')}"
            f"{' + audio ' + str(best_audio.get('format_id')) if best_audio else ' (muxed audio)'}"
        )
        return (best_video["url"],
                best_audio["url"] if best_audio else None,
                best_video.get("http_headers") or {})

    raise YouTubeError(f"No usable HLS tracks for {video_id}")


def is_cached(video_id: str) -> bool:
    """Whether a resolve for this video would return without calling YouTube."""
    with _cache_lock:
        hit = _cache.get(video_id)
        return bool(hit and not hit.expired)


def invalidate(video_id: str) -> None:
    with _cache_lock:
        _cache.pop(video_id, None)
        _failures.pop(video_id, None)


def clear_failures() -> int:
    """Forget every recorded failure, so the next probe tries YouTube again."""
    with _cache_lock:
        n = len(_failures)
        _failures.clear()
    return n


def failure_count() -> int:
    """Videos currently backing off after a failed resolve."""
    now = time.time()
    with _cache_lock:
        return sum(1 for retry_at, _, _ in _failures.values() if retry_at > now)


def cache_size() -> int:
    with _cache_lock:
        return len(_cache)

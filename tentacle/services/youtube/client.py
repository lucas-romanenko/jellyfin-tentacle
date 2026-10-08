"""Thin wrapper over yt-dlp.

Kept separate so the indexer and the resolver share one place that knows how to
talk to YouTube — the client chain, the JS runtime, and the error mapping — and
so it can be stubbed in tests without touching the network.
"""
import logging
from typing import Optional

from services.youtube import traffic
from services.youtube.errors import (PausedByBotCheck, VideoUnavailable, YouTubeBlocked,
                                     YouTubeUnavailable, classify)

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


_counting_class = None


def _ydl_class():
    """yt-dlp's YoutubeDL, with every HTTP request it makes counted (they all go
    through YoutubeDL.urlopen)."""
    global _counting_class
    if _counting_class is None:
        import yt_dlp

        class CountingYoutubeDL(yt_dlp.YoutubeDL):
            def urlopen(self, req):
                traffic.count(getattr(req, "url", req))
                return super().urlopen(req)

        _counting_class = CountingYoutubeDL
    return _counting_class


class _Warnings:
    """yt-dlp's logger for one extraction: keeps its warnings, prints nothing.

    With ignore_no_formats_error (which an upcoming stream's details need),
    yt-dlp does not raise when YouTube refuses a video page. It reports the
    reason as a warning and returns a "video" with no formats, no date, no length
    and the title "youtube video #<id>". The reason is only to be had here.
    """

    def __init__(self):
        self.messages = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.messages.append(str(msg))

    def error(self, msg):
        self.messages.append(str(msg))


def _ydl(extra: dict = None, log: _Warnings = None):
    opts = dict(_BASE_OPTS)
    # The proxy (if set) and a player cache that survives restarts, so the
    # player code is not fetched again for every extraction.
    opts.update(traffic.ydl_options())
    if extra:
        opts.update(extra)
    if log is not None:
        opts["logger"] = log
    return _ydl_class()(opts)


# What in a video page's refusal is about this server rather than the video.
_SERVER_MARKERS = ("not a bot", "rate-limited", "rate limited", "captcha", "http error 429",
                   "too many requests", "unusual traffic", "google.com/sorry")


def _refusal(info: dict, warnings: list):
    """The error a video page with no formats really was, or None.

    A rate limit, a captcha or a bot check is about this server (the pause);
    private, removed, members-only or age-gated is about the video. Both used
    to come back as an ordinary video: a new upload read during a rate limit
    became a dateless "youtube video #<id>" that retention then deleted, no
    bot check on a video page ever started the pause, and a dead video was
    never known to be gone. An upcoming stream has no formats yet and is fine.
    """
    if info.get("formats"):
        return None
    upcoming = info.get("live_status") == "is_upcoming"
    # yt-dlp's own remarks ("No video formats found!") are not YouTube's answer.
    reasons = [m for m in warnings if m.startswith("[youtube")]
    errors = [classify(Exception(m)) for m in reasons]
    # A pause stops every YouTube request, so only wording that is about this
    # server starts one. "Try again later" alone is not: YouTube also says it
    # about one video ("still being processed", "something went wrong"), and
    # read as a block such a video paused everything again after every pause.
    for message, error in zip(reasons, errors):
        if isinstance(error, YouTubeBlocked) and any(m in message.lower() for m in _SERVER_MARKERS):
            return error
    for error in errors:
        if isinstance(error, VideoUnavailable) and not upcoming:
            return error
    for message, error in zip(reasons, errors):
        if isinstance(error, YouTubeBlocked) and not upcoming:
            return YouTubeUnavailable(message)      # a hiccup: the resolver backs off, the indexer retries
    if not upcoming and warnings:
        # Nothing recognised: returned as before, but said, so a new wording
        # from YouTube shows up in the log rather than as placeholder videos.
        logger.info(f"[YouTube] {info.get('id')}: no formats, and yt-dlp said: {warnings[0][:200]}")
    return None


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
    empty list would then wipe the channel. A bot check starts the app-wide
    pause (services.youtube.traffic); during it, PausedByBotCheck is raised
    without contacting YouTube.
    """
    # Nothing is sent while YouTube requests are paused after a bot check.
    traffic.ensure_allowed()
    log = _Warnings()
    try:
        with _ydl(extra_opts, log) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # yt_dlp raises its own hierarchy
        error = classify(e)
        if isinstance(error, YouTubeBlocked) and not isinstance(error, PausedByBotCheck):
            traffic.record_block(str(e))
        raise error from e
    if info is None:
        raise classify(Exception(f"yt-dlp returned nothing for {url}"))
    if not (extra_opts or {}).get("extract_flat"):
        refused = _refusal(info, log.messages)
        if refused is not None:
            if isinstance(refused, YouTubeBlocked):
                traffic.record_block(str(refused))
            raise refused
    traffic.note_success()
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

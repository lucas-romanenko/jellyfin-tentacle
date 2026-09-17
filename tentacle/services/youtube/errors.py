"""Classification of yt-dlp failures.

The distinction that matters is "we could not see the channel" vs "the channel
has nothing" — issue #16 was exactly that confusion on the IPTV side, where an
empty response was read as "the provider removed everything" and deleted 15,988
titles. A bot-check must raise, never return an empty list.
"""


class YouTubeError(Exception):
    """Base for indexing failures."""


class YouTubeBlocked(YouTubeError):
    """YouTube is refusing to answer (bot check / 429). Back off, delete nothing."""


class YouTubeUnavailable(YouTubeError):
    """A transient failure — network, 5xx, extractor hiccup."""


class VideoUnavailable(YouTubeError):
    """This one video can't be read (private, members-only, removed). Skip it."""


_BLOCKED_MARKERS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "http error 429",
    "too many requests",
    "blocked it in your country",
)
_VIDEO_MARKERS = (
    "private video",
    "members-only",
    "video unavailable",
    "this video is unavailable",
    "removed by the uploader",
    "account associated with this video has been terminated",
    "video has been removed",
)


def classify(error: Exception) -> YouTubeError:
    """Map a yt-dlp exception onto one of ours."""
    text = str(error).lower()
    if any(m in text for m in _BLOCKED_MARKERS):
        return YouTubeBlocked(str(error))
    if any(m in text for m in _VIDEO_MARKERS):
        return VideoUnavailable(str(error))
    return YouTubeUnavailable(str(error))

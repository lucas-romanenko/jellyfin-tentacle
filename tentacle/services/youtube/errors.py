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


class PausedByBotCheck(YouTubeBlocked):
    """Not sent at all: every YouTube request is paused after a bot check
    (services.youtube.traffic). Handled like a block, but never starts a new pause."""


class YouTubeUnavailable(YouTubeError):
    """A transient failure — network, 5xx, extractor hiccup."""


class VideoUnavailable(YouTubeError):
    """This one video can't be read (private, members-only, removed). Skip it."""


_BLOCKED_MARKERS = (
    "confirm you're not a bot",
    "confirm you are not a bot",
    "sign in to confirm you",       # "…you're not a bot"; the age gate is below
    "http error 429",
    "too many requests",
    "unusual traffic",
    "google.com/sorry",
    # The session rate limit, which yt-dlp words as "Video unavailable. This
    # content isn't available, try again later. The current session has been
    # rate-limited by YouTube…", and the captcha before playback. Both are about
    # this server, not the video: read as "video unavailable" they started no
    # pause and got playable videos retired (#241).
    "try again later",
    "rate-limited",
    "rate limited",
    "captcha",
)
# Checked first: these mention signing in or a block too, but they are about one
# video, not about this server. Read as a bot check, an age-restricted upload
# paused the channel for hours and was retried after every pause.
_VIDEO_FIRST_MARKERS = (
    "confirm your age",
    "age-restricted",
    "inappropriate for some users",
    "blocked it in your country",
    "not available in your country",
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


# What says for certain that this video cannot be played here. A bare "Video
# unavailable" does not: yt-dlp puts it in front of other failures too (#241),
# so only these count towards taking a library video out (resolver/_unplayable).
_GONE_MARKERS = _VIDEO_FIRST_MARKERS + (
    "private video",
    "members-only",
    "join this channel",
    "removed by the uploader",
    "account associated with this video has been terminated",
    "video has been removed",
    "no longer available",
)


def is_gone(error: Exception) -> bool:
    """Whether a failure says for certain that this one video can't be played."""
    text = str(error).lower()
    return isinstance(error, VideoUnavailable) and any(m in text for m in _GONE_MARKERS)


def classify(error: Exception) -> YouTubeError:
    """Map a yt-dlp exception onto one of ours."""
    text = str(error).lower()
    if any(m in text for m in _VIDEO_FIRST_MARKERS):
        return VideoUnavailable(str(error))
    if any(m in text for m in _BLOCKED_MARKERS):
        return YouTubeBlocked(str(error))
    if any(m in text for m in _VIDEO_MARKERS):
        return VideoUnavailable(str(error))
    return YouTubeUnavailable(str(error))

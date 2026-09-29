"""Discover a channel's videos and keep the DB in step with it.

Nothing here writes media files — that is library.py's job — and nothing here
deletes a video because a listing came back short. Issue #16 is the cautionary
tale: an empty response read as "everything was removed" destroyed 15,988
titles. A listing that fails raises; retention only ever acts on a listing that
succeeded.
"""
import logging
import re
import time
from urllib.parse import urlparse
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from models.database import YouTubeChannel, YouTubeVideo
from services.youtube import client, feeds, traffic
from services.youtube.errors import VideoUnavailable, YouTubeBlocked, YouTubeError

logger = logging.getLogger(__name__)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# A broadcast that is on air, or about to be. Guide material, never a library
# item — it has no final duration, so Jellyfin would file it as a zero-length
# movie.
PENDING_LIVE = ("is_live", "is_upcoming")
# A broadcast that has ended. yt-dlp reports BOTH of these: post_live for one
# that just finished (YouTube is still processing it) and was_live once it
# settles. Handling only was_live let just-ended streams into the library.
FINISHED_LIVE = ("was_live", "post_live")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

# Guest extraction is rate-limited at roughly 300 videos/hour, so details are
# fetched slowly, and never at a fixed beat (see _detail_pause).
DETAIL_SPACING_SECONDS = 5.0

# How long to stand down after a bot check. Retrying into one makes it worse.
# The pause itself is app-wide now (services.youtube.traffic); this is kept for
# a channel's own blocked_until, which the page shows.
BLOCK_BACKOFF_HOURS = 6

# A scheduled check reads the channel's feed first and lists its tabs only when
# something is new — and at least this often anyway, as a safety net for a feed
# that lags or misses an upload.
FULL_CHECK_HOURS = 24

# A live or upcoming stream the listing no longer shows is re-read at most this often.
PENDING_DETAIL_HOURS = 6

# A video whose details could not be read (members-only, private, a hiccup) is
# retried after this long, doubling up to the cap — not on every run.
DETAIL_RETRY_START_HOURS = 6
DETAIL_RETRY_MAX_HOURS = 48

# Skip reasons of videos whose details could not be read, retried at next_check_at.
UNAVAILABLE_REASON = "unavailable (private, members-only or removed)"
UNREADABLE_REASON = "could not read details"
_RETRYABLE = (UNAVAILABLE_REASON, UNREADABLE_REASON)

# Listing live statuses that mean the broadcast is over.
_ENDED = ("was_live", "post_live", "not_live")
# Recorded verbatim on skipped rows so the restore path can find exactly the
# videos this preference excluded, and not ones excluded for another reason.
STREAM_PREFERENCE_REASON = "finished live stream and past live streams are not included"



# A title that is really a video id: the bare id today's fallback stores when
# neither the details nor the listing had one, and "youtube video #<id>" from
# an earlier build. Such a row is repaired from the listing on a later refresh
# rather than kept for ever (#131).
_PLACEHOLDER_TITLE_RE = re.compile(r"^youtube video #[A-Za-z0-9_-]{11}$", re.IGNORECASE)
# What yt-dlp lists in place of a title for an entry it cannot show. Never a
# replacement for anything.
_UNAVAILABLE_TITLE_RE = re.compile(r"^\[(private|deleted|unavailable)[^\]]*\]$", re.IGNORECASE)
# Details fetches spent per run on placeholders the listing could not repair.
# Each is rate-limited, and new videos come first.
TITLE_REPAIRS_PER_RUN = 3


def is_placeholder_title(title, video_id: str) -> bool:
    """Whether a stored title is a stand-in rather than the video's name."""
    title = (title or "").strip()
    return not title or title == video_id or bool(_PLACEHOLDER_TITLE_RE.match(title))


def _usable_title(title, video_id: str):
    """`title` if it names the video, else None."""
    title = (title or "").strip()
    if is_placeholder_title(title, video_id) or _UNAVAILABLE_TITLE_RE.match(title):
        return None
    return title


def is_library_status(column):
    """SQL filter for "this is a library item, not a pending broadcast".

    Written out rather than a bare NOT IN because SQL evaluates
    `NULL NOT IN (...)` as NULL, not true — so any video whose live_status was
    never set would be silently dropped from every count.
    """
    from sqlalchemy import or_
    return or_(column.is_(None), column.notin_(PENDING_LIVE))

def slugify(text: str) -> str:
    """Filesystem- and tag-safe identifier for a channel."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return slug[:48] or "channel"


# Everything this feature fetches has to be on YouTube. yt-dlp's generic
# extractor will happily fetch any other host, which would make "add a channel"
# a request-forgery primitive against the private network Tentacle sits in
# (see services/ssrf.py, which guards the IPTV side for the same reason).
YOUTUBE_HOSTS = frozenset((
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be",
))


def parse_input_url(url: str) -> dict:
    """Work out what the user pasted: a channel, a handle, or a playlist."""
    url = (url or "").strip()
    if not url:
        raise ValueError("No URL given")
    if not url.startswith("http"):
        # Bare "@handle" or a channel id
        url = f"https://www.youtube.com/{url.lstrip('/')}"

    host = (urlparse(url).hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise ValueError("Not a YouTube channel or playlist URL")

    playlist = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    if playlist:
        return {"kind": "playlist", "playlist_id": playlist.group(1),
                "canonical": f"https://www.youtube.com/playlist?list={playlist.group(1)}"}

    channel = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", url)
    if channel:
        return {"kind": "channel", "channel_id": channel.group(1),
                "canonical": f"https://www.youtube.com/channel/{channel.group(1)}"}

    handle = re.search(r"/@([A-Za-z0-9_.-]+)", url)
    if handle:
        return {"kind": "channel", "handle": handle.group(1),
                "canonical": f"https://www.youtube.com/@{handle.group(1)}"}

    user = re.search(r"/(user|c)/([A-Za-z0-9_.-]+)", url)
    if user:
        # Rebuilt rather than passed through: the pasted URL is only known to
        # be on YouTube, and anything after the name (a path, a query) would be
        # handed to yt-dlp as-is.
        return {"kind": "channel", "handle": user.group(2),
                "canonical": f"https://www.youtube.com/{user.group(1)}/{user.group(2)}"}

    raise ValueError("Not a YouTube channel or playlist URL")


def resolve_channel(url: str) -> dict:
    """One listing call to learn a channel's identity and artwork."""
    parsed = parse_input_url(url)
    listing_url = parsed["canonical"]
    if parsed["kind"] == "channel":
        listing_url = listing_url.rstrip("/") + "/videos"

    info = client.flat_listing(listing_url, 1)
    if parsed["kind"] == "playlist":
        # A playlist listing names its owner in "channel"; the playlist's own
        # name is "title". Titling it by the owner made every playlist of a
        # channel look like the channel itself.
        title = info.get("title") or info.get("channel") or info.get("uploader") or "YouTube"
    else:
        title = info.get("channel") or info.get("uploader") or info.get("title") or "YouTube"
    thumbs = info.get("thumbnails") or []

    def _pick(*keys):
        for t in thumbs:
            if any(k in (t.get("id") or "") for k in keys):
                return t.get("url")
        return thumbs[-1].get("url") if thumbs else None

    return {
        "kind": parsed["kind"],
        # Whether the uploads tab has anything in it. A channel that only ever
        # broadcasts live has an empty one, and needs its finished streams kept
        # instead or its library is empty — decided here so nobody has to know.
        "has_uploads": bool(info.get("entries")),
        "channel_id": info.get("channel_id") or parsed.get("channel_id"),
        "handle": parsed.get("handle"),
        "playlist_id": parsed.get("playlist_id"),
        "title": title,
        # Who owns it: the channel's own name for a channel, the owner's for a
        # playlist. Used to tell two same-named sources apart.
        "owner": info.get("channel") or info.get("uploader"),
        "avatar_url": _pick("avatar"),
        "banner_url": _pick("banner"),
        "canonical": parsed["canonical"],
    }


# The most that will ever be read from one tab. Keeping more than this in a
# "latest videos" row is not what the feature is for, and each new video costs
# a rate-limited detail fetch, so the cap bounds the first index to minutes.
MAX_KEEP = 100


# How far into the streams tab to look when the channel only needs it for
# what is on air. Live and upcoming broadcasts sit at the top; everything
# below is a finished stream that will be recorded as skipped after a
# rate-limited detail fetch each. Reading fifteen of those to find two live
# ones is what made "keep newest 10" report thirty entries being indexed.
LIVE_PEEK = 5


def tab_limit(channel: YouTubeChannel, url: str) -> int:
    """How far to read one tab: the streams tab only as far as needed."""
    if url.endswith("/streams") and not channel.include_streams:
        return LIVE_PEEK
    return listing_limit(channel)


def listing_limit(channel: YouTubeChannel) -> int:
    """How far down a tab to read, derived from the one setting the user has.

    "Keep the newest N" means reading a little past N: a few of the newest may
    be private, members-only or otherwise unavailable, and reading exactly N
    would leave the library short. Retention trims back to N afterwards.
    """
    keep = channel.keep_count or 10
    return min(max(keep + 5, 5), MAX_KEEP + 5)


def _tab_urls(channel: YouTubeChannel) -> list:
    """The listing URLs to poll for this channel, honouring its include flags."""
    if channel.kind == "playlist" and channel.playlist_id:
        return [f"https://www.youtube.com/playlist?list={channel.playlist_id}"]

    base = (f"https://www.youtube.com/channel/{channel.channel_id}" if channel.channel_id
            else f"https://www.youtube.com/@{channel.handle}")
    urls = []
    if channel.include_videos:
        urls.append(f"{base}/videos")
    # /streams carries live and scheduled broadcasts. A Live TV channel has to
    # poll it whatever the "past live streams" preference says — that preference
    # is about keeping finished streams in the library, which is a separate
    # question from knowing what is on air.
    if channel.include_streams or channel.live_enabled:
        urls.append(f"{base}/streams")
    if channel.include_shorts:
        urls.append(f"{base}/shorts")
    return urls


def _should_index(details: dict, channel: YouTubeChannel) -> tuple:
    """(keep, reason). Applied to full details, not the flat listing."""
    if details.get("availability") not in (None, "public"):
        return False, f"availability={details.get('availability')}"

    live_status = details.get("live_status")
    if live_status in PENDING_LIVE:
        # Kept only when the channel is exposed as a Live TV channel, where it
        # becomes a guide entry. It never becomes a library item: a stream has
        # no duration yet and Jellyfin would file it as a zero-length movie.
        if channel.live_enabled:
            return True, ""
        return False, f"live_status={live_status}"

    if live_status in FINISHED_LIVE and not channel.include_streams:
        # A finished broadcast. The /streams tab is polled for any Live TV
        # channel so we can tell what is on air, but that must not drag the
        # channel's back catalogue of finished streams into the library —
        # "Past live streams" is a separate, explicit choice. Without this a
        # channel that mostly streams filled the library with old broadcasts
        # instead of its actual uploads.
        return False, STREAM_PREFERENCE_REASON

    duration = details.get("duration") or 0
    if channel.min_duration and duration and duration < channel.min_duration:
        return False, f"duration {duration}s under minimum {channel.min_duration}s"
    return True, ""


def made_for_kids(details: dict):
    """YouTube's Made for Kids designation: True, False, or None when unknown.

    Read only from a field that actually carries it. age_limit is not that
    field: yt-dlp sets it to 0 for every video without an age restriction and
    to 18 for the rest, so "age_limit == 0" flagged ordinary videos as made
    for kids, and the old `a and b or None` expression could never produce
    False either (#130). yt-dlp 2026.8.19 does not report the designation at
    all, so this is None — "not known" — unless a future extractor supplies it.
    """
    value = details.get("is_made_for_kids")
    return None if value is None else bool(value)


def is_library_item(video) -> bool:
    """Whether this video should get .strm/NFO files.

    Live and upcoming streams are Live TV guide entries, not library items.
    Once a stream ends its live_status clears and it becomes an ordinary video,
    at which point the next sync writes its files.
    """
    return video.live_status not in PENDING_LIVE


def _apply_stream_preference(db: Session, channel: YouTubeChannel) -> int:
    """Bring finished broadcasts into line with the "Past live streams" setting.

    Settings change after the fact, and this has to work in both directions.
    Turning the preference off clears finished broadcasts out rather than
    leaving them stranded; turning it back on has to bring them back, which it
    previously did not. A video is only ever detailed once, on the run that
    first sees it, so anything already in the table is skipped as "known" — a
    video removed here would therefore never be reconsidered, and the setting
    was effectively one-way. Its live_status is already recorded, so restoring
    it needs no further calls to YouTube.

    Only a video's own folder is ever touched; nothing recurses over a shared
    parent.
    """
    from services.youtube import library

    if channel.include_streams:
        candidates = db.query(YouTubeVideo).filter(
            YouTubeVideo.channel_fk == channel.id,
            YouTubeVideo.live_status.in_(FINISHED_LIVE),
            YouTubeVideo.removed_at.isnot(None),
            YouTubeVideo.skip_reason == STREAM_PREFERENCE_REASON,
        ).all()
        # The minimum length still applies — this preference only undoes its own
        # exclusions, never someone else's.
        restored = [v for v in candidates
                    if not (channel.min_duration and v.duration
                            and v.duration < channel.min_duration)]
        for video in restored:
            video.removed_at = None
            video.skip_reason = None
        if restored:
            db.commit()
        return -len(restored)

    from sqlalchemy import and_, or_
    stale = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
        or_(
            YouTubeVideo.live_status.in_(FINISHED_LIVE),
            # A broadcast whose live_status later cleared to NULL. It was
            # indexed as media_type "livestream", so it still counts as a
            # finished stream rather than an ordinary upload.
            and_(YouTubeVideo.media_type == "livestream",
                 YouTubeVideo.live_status.is_(None)),
        ),
    ).all()
    for video in stale:
        library.remove_video(video)
        video.removed_at = datetime.utcnow()
        video.strm_path = None
        video.skip_reason = STREAM_PREFERENCE_REASON
    if stale:
        db.commit()
    return len(stale)


def _detail_pause() -> None:
    """Wait between two detail reads: never the same length twice."""
    if DETAIL_SPACING_SECONDS > 0:
        time.sleep(traffic.jitter(DETAIL_SPACING_SECONDS, 0.4))


def _retry_later(video, reason: str) -> None:
    """A read failed: skip the video now, read it again later (doubling, capped)."""
    video.check_failures = (video.check_failures or 0) + 1
    hours = min(DETAIL_RETRY_START_HOURS * 2 ** (video.check_failures - 1), DETAIL_RETRY_MAX_HOURS)
    video.next_check_at = datetime.utcnow() + timedelta(seconds=traffic.jitter(hours * 3600, 0.1))
    video.skip_reason = reason


def _details(video_id: str) -> dict:
    """One video's details: from the YouTube Data API when a key works, else yt-dlp.

    A video the API does not return (private, removed) is VideoUnavailable. When
    the API refuses or fails, yt-dlp answers instead.
    """
    if feeds.api_available():
        try:
            found = feeds.api_details([video_id])
        except (feeds.FeedUnavailable, YouTubeError) as e:
            logger.debug(f"[YouTube] API details for {video_id} failed, using yt-dlp: {e}")
        else:
            if video_id not in found:
                raise VideoUnavailable(f"{video_id}: the YouTube Data API does not return it "
                                       f"(private or removed)")
            return found[video_id]
    return client.video_details(video_id)


def _apply_details(video, details: dict) -> None:
    was = video.live_status
    video.live_status = details.get("live_status")
    if details.get("duration"):
        video.duration = details["duration"]
    published = _published(details)
    if published and not video.published_at:
        video.published_at = published
    if was != video.live_status:
        logger.info(f"[YouTube] '{video.title}' {was} → {video.live_status or 'ended'}")


def _update_pending(db: Session, channel: YouTubeChannel, listed_status: dict) -> int:
    """Bring the channel's live and upcoming streams up to date. Returns how many changed.

    The status comes from what was already fetched: the streams tab listing
    carries each entry's live_status, and the Data API (when a key works)
    answers for every pending stream in one call. Details are read only when a
    stream has just ended (for its final length) or has dropped out of the
    listing — then at most every PENDING_DETAIL_HOURS. Every pending stream used
    to get a full page load on every run.

    Only channels on Live TV: nothing else keeps pending streams, and one that
    left Live TV has no use for their status.
    """
    if not channel.live_enabled:
        return 0
    pending = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.live_status.in_(PENDING_LIVE),
        YouTubeVideo.removed_at.is_(None),
    ).all()
    if not pending:
        return 0
    api = {}
    if feeds.api_available():
        try:
            api = feeds.api_details([v.video_id for v in pending])
        except (feeds.FeedUnavailable, YouTubeError) as e:
            logger.debug(f"[YouTube] API live status for '{channel.title}' failed: {e}")
    changed = 0
    now = datetime.utcnow()
    for video in pending:
        before = (video.live_status, video.duration)
        if video.video_id in api:
            _apply_details(video, api[video.video_id])
        else:
            status = listed_status.get(video.video_id)
            if status in PENDING_LIVE:
                if status != video.live_status:
                    logger.info(f"[YouTube] '{video.title}' {video.live_status} → {status}")
                    video.live_status = status
            elif status in _ENDED or not video.next_check_at or video.next_check_at <= now:
                video.next_check_at = now + timedelta(hours=PENDING_DETAIL_HOURS)
                try:
                    details = _details(video.video_id)
                except YouTubeBlocked:
                    raise
                except YouTubeError:
                    continue
                _apply_details(video, details)
                _detail_pause()
        if (video.live_status, video.duration) != before:
            changed += 1
    db.commit()
    return changed


def _feed_entries(channel: YouTubeChannel):
    """The channel's newest uploads from its feed or the API, or None when there
    is no feed to read (then the tabs are listed, as before). Raises YouTubeBlocked."""
    try:
        return feeds.newest_uploads(channel)
    except YouTubeBlocked:
        raise
    except (feeds.FeedUnavailable, YouTubeError) as e:
        logger.info(f"[YouTube] No feed for '{channel.title}' ({e}); listing its tabs instead")
        return None


def _light_check(db: Session, channel: YouTubeChannel, known: set):
    """The scheduled check: is anything new? (result, feed_ids).

    result is None when the tabs have to be listed: something is new, the feed
    can't be read, the channel was never listed, or the last full listing is
    older than FULL_CHECK_HOURS. Otherwise only the live status of pending
    streams is brought up to date and a result is returned — one small request
    for most channels.
    """
    now = datetime.utcnow()
    entries = _feed_entries(channel)
    if entries is None:
        return None, None
    ids = [e["id"] for e in entries]
    if not channel.last_full_check or \
            now - channel.last_full_check > timedelta(seconds=traffic.jitter(FULL_CHECK_HOURS * 3600, 0.15)):
        # Due for a full listing anyway; the feed ids read here are stored with it.
        return None, ids
    # Ids the last full listing already saw in the feed and dealt with — older
    # than "the newest N", Shorts the channel skips, a video filed under another
    # source. Without this each would read as new on every check.
    handled = set(channel.feed_ids or [])
    fresh = [e["id"] for e in entries
             if e["id"] not in known and e["id"] not in handled
             and not (e.get("short") and not channel.include_shorts)]
    if fresh:
        logger.info(f"[YouTube] '{channel.title}': {len(fresh)} new in its feed; listing it")
        return None, ids

    # A Live TV channel also peeks at the top of its streams tab, as every check
    # did before: one small listing that shows what is live or scheduled, whether
    # or not the feed carries broadcasts yet. With an API key the uploads above
    # include them, and the API answers for pending streams.
    listed_status = {}
    if channel.live_enabled and not feeds.api_available():
        for url in [u for u in _tab_urls(channel) if u.endswith("/streams")]:
            info = client.flat_listing(url, LIVE_PEEK)
            for entry in (info.get("entries") or []):
                if entry.get("id"):
                    listed_status[entry["id"]] = entry.get("live_status")
        new_live = [vid for vid, status in listed_status.items()
                    if status in PENDING_LIVE and vid not in known and vid not in handled]
        if new_live:
            logger.info(f"[YouTube] '{channel.title}': a new live or scheduled stream; listing it")
            return None, ids
    live_changes = _update_pending(db, channel, listed_status) if channel.live_enabled else 0

    channel.last_checked = now
    channel.last_error = None
    channel.error_count = 0
    channel.blocked_until = None
    db.commit()
    return {"skipped": False, "light": True, "new": 0, "seen": len(ids), "filtered": 0,
            "skips": {}, "listing": channel.last_listing or {}, "beyond": 0,
            "retitled": [], "live_changes": live_changes}, ids


def _mark_blocked(db: Session, channel: YouTubeChannel, e: Exception) -> None:
    left = traffic.paused() or BLOCK_BACKOFF_HOURS * 3600
    channel.blocked_until = datetime.utcnow() + timedelta(seconds=left)
    channel.last_error = "YouTube asked us to prove we're not a bot — backing off"
    channel.error_count = (channel.error_count or 0) + 1
    db.commit()
    logger.warning(f"[YouTube] '{channel.title}' bot-checked; backing off: {e}")


def _drop_orphans(db: Session) -> int:
    """Delete video rows whose channel no longer exists.

    A run that committed a new row just after the channel was removed left one
    behind (SQLite does not enforce the foreign key). video_id is unique across
    every source, so the channel added again skipped that video for ever as
    "already indexed under another channel or playlist" (#278).
    """
    from services.youtube import library
    orphans = db.query(YouTubeVideo).filter(
        ~YouTubeVideo.channel_fk.in_(db.query(YouTubeChannel.id))).all()
    for video in orphans:
        library.remove_video(video)
        db.delete(video)
    if orphans:
        db.commit()
        logger.info(f"[YouTube] Removed {len(orphans)} video row(s) left behind by a removed channel")
    return len(orphans)


def index_channel(db: Session, channel: YouTubeChannel, limit: int = None,
                  on_progress=None, light: bool = False) -> dict:
    """Refresh one channel. Returns counts; raises on a blocked/unavailable listing.

    New videos are recorded but NOT given media files here — library.py writes
    those, so an indexing failure can never leave half-written files behind.
    """
    if traffic.paused():
        return {"skipped": True, "paused": True, "new": 0, "seen": 0}
    if channel.blocked_until and channel.blocked_until > datetime.utcnow():
        logger.info(f"[YouTube] '{channel.title}' is backed off until {channel.blocked_until} — skipping")
        return {"skipped": True, "new": 0, "seen": 0}

    _drop_orphans(db)
    limit = limit or listing_limit(channel)
    known = {v.video_id for v in db.query(YouTubeVideo.video_id).filter(
        YouTubeVideo.channel_fk == channel.id).all()}

    feed_ids = None
    if light:
        try:
            result, feed_ids = _light_check(db, channel, known)
        except YouTubeBlocked as e:
            _mark_blocked(db, channel, e)
            raise
        if result is not None:
            return result
    # Library items already in hand. Walking the listing newest-first, these
    # count towards "the newest N" exactly as a freshly fetched one does.
    kept_ids = {v.video_id for v in db.query(YouTubeVideo.video_id).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
        is_library_status(YouTubeVideo.live_status),
    ).all()}
    keep = channel.keep_count or 10
    # Rows whose title is a stand-in, repaired below if the listing names them.
    from sqlalchemy import or_
    # Library rows only: a skipped row is never shown, and repairing its title
    # would spend a detail read on it every run.
    placeholders = {v.video_id: v for v in db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
        or_(YouTubeVideo.title.is_(None), YouTubeVideo.title == "",
            YouTubeVideo.title == YouTubeVideo.video_id,
            YouTubeVideo.title.ilike("youtube video #%")),
    ).all() if is_placeholder_title(v.title, v.video_id)}
    retitled, repairs_left = [], TITLE_REPAIRS_PER_RUN
    # Videos whose details could not be read earlier and are due another try.
    retry_due = {v.video_id: v for v in db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.skip_reason.in_(_RETRYABLE),
        YouTubeVideo.next_check_at.isnot(None),
        YouTubeVideo.next_check_at <= datetime.utcnow(),
    ).all()}

    # Every listed entry in listing order, tagged with whether its tab feeds
    # the library. The streams tab does only when finished broadcasts are
    # kept; otherwise it is read for what is on air and nothing on it counts
    # towards N.
    seen_ids, new_videos, ordered = [], [], []
    # What each tab actually returned, kept on the channel afterwards. Without
    # it, "no videos" is ambiguous: YouTube may have listed nothing, or it may
    # have listed plenty that the channel's settings then excluded. Those need
    # opposite fixes, and telling them apart previously meant reading the logs.
    listing: dict = {}
    try:
        for url in _tab_urls(channel):
            info = client.flat_listing(url, min(limit, tab_limit(channel, url)))
            tab = url.rsplit("/", 1)[-1] if "/playlist?" not in url else "playlist"
            library_tab = not (tab == "streams" and not channel.include_streams)
            count = 0
            for entry in (info.get("entries") or []):
                vid = entry.get("id")
                if not vid or not VIDEO_ID_RE.match(vid):
                    continue
                count += 1
                seen_ids.append(vid)
                ordered.append((vid, entry, library_tab))
                if vid not in known:
                    new_videos.append(vid)
            listing[tab] = count
    except YouTubeBlocked as e:
        _mark_blocked(db, channel, e)
        raise
    except YouTubeError as e:
        channel.last_error = str(e)[:400]
        channel.error_count = (channel.error_count or 0) + 1
        db.commit()
        logger.warning(f"[YouTube] Listing failed for '{channel.title}': {e}")
        raise

    # Anything still listed is alive — refresh last_seen so retention leaves it be.
    now = datetime.utcnow()

    # Bring live and upcoming streams up to date from what the listing says
    # (see _update_pending). Details are only fetched for NEW videos, so a stream
    # indexed while scheduled would otherwise keep live_status="is_upcoming"
    # forever and never become playable on its Live TV channel.
    try:
        _update_pending(db, channel, {vid: entry.get("live_status") for vid, entry, _ in ordered})
    except YouTubeBlocked as e:
        _mark_blocked(db, channel, e)
        raise
    if seen_ids:
        for i in range(0, len(seen_ids), 500):
            db.query(YouTubeVideo).filter(
                YouTubeVideo.channel_fk == channel.id,
                YouTubeVideo.video_id.in_(seen_ids[i:i + 500]),
            ).update({YouTubeVideo.last_seen: now}, synchronize_session=False)
    # Committed before anything below asks YouTube: the UPDATE holds SQLite's one
    # write lock until the commit, and a detail read can take tens of seconds.
    # Every other writer in Tentacle (music, Live TV, the sync) failed with
    # "database is locked" while it waited (#253). From here on each video is
    # read first and written in its own short transaction.
    db.commit()

    added, skipped = 0, 0
    skips: dict = {}
    # Library items passed so far in listing order — the ones already kept and
    # the ones just fetched. Once it reaches N, everything further down a
    # library tab is older than "the newest N" and would only be retired by
    # retention; it is left unfetched. Each fetch is rate-limited and slow, so
    # this is also what keeps a first index short. Skipped entries do not
    # count, which is what the listing margin past N is for.
    seen_kept, beyond = 0, 0

    def _note_skip(reason: str):
        nonlocal skipped
        skipped += 1
        # Keep the reason, drop the specifics, so counts aggregate.
        key = re.sub(r"\d+", "N", reason)
        skips[key] = skips.get(key, 0) + 1

    def _report():
        if on_progress:
            on_progress(min(seen_kept, keep), keep)

    # youtube_videos.video_id is unique across ALL channels, but `known` only
    # covers this one. A video listed twice here (e.g. on /streams and /videos)
    # or already indexed under another channel or playlist source would be
    # inserted again, and the IntegrityError aborts the whole channel on every
    # run. Look those up once, and handle each id at most once.
    elsewhere = set()
    fresh = [v for v in dict.fromkeys(new_videos)]
    for i in range(0, len(fresh), 500):
        elsewhere |= {v.video_id for v in db.query(YouTubeVideo.video_id).filter(
            YouTubeVideo.channel_fk != channel.id,
            YouTubeVideo.video_id.in_(fresh[i:i + 500])).all()}
    handled = set()

    for vid, entry, library_tab in ordered:
        if vid in handled:
            continue
        handled.add(vid)
        if vid in elsewhere:
            _note_skip("already indexed under another channel or playlist")
            continue
        if vid in known:
            if vid in retry_due:
                if library_tab and seen_kept >= keep:
                    beyond += 1
                    continue
                row = retry_due[vid]
                try:
                    details = _details(vid)
                except VideoUnavailable:
                    _retry_later(row, UNAVAILABLE_REASON)
                    db.commit()
                    _note_skip(UNAVAILABLE_REASON)
                    continue
                except YouTubeBlocked as e:
                    _mark_blocked(db, channel, e)
                    raise
                except YouTubeError:
                    _retry_later(row, UNREADABLE_REASON)
                    db.commit()
                    _note_skip(UNREADABLE_REASON)
                    continue
                wanted, reason = _should_index(details, channel)
                row.title = details.get("title") or row.title
                row.description = details.get("description")
                row.published_at = _published(details) or row.published_at
                row.duration = details.get("duration")
                row.live_status = details.get("live_status")
                row.media_type = "livestream" if details.get("live_status") else "video"
                row.thumbnail_url = details.get("thumbnail")
                row.is_made_for_kids = made_for_kids(details)
                row.next_check_at, row.check_failures = None, 0
                if wanted:
                    row.removed_at, row.skip_reason = None, None
                    added += 1
                    if library_tab and details.get("live_status") not in PENDING_LIVE:
                        seen_kept += 1
                else:
                    row.skip_reason = reason
                    _note_skip(reason)
                db.commit()
                _report()
                _detail_pause()
                continue
            if vid in placeholders:
                title = _usable_title(entry.get("title"), vid)
                # Details only when the listing gave no title at all. A listing
                # that names the video by its id (or "[Private video]") has
                # nothing better behind it, and re-fetching such a row on
                # every run would spend the rate-limited budget for nothing.
                if not title and not entry.get("title") and repairs_left > 0:
                    repairs_left -= 1
                    try:
                        title = _usable_title(_details(vid).get("title"), vid)
                    except YouTubeBlocked:
                        raise
                    except YouTubeError as e:
                        logger.debug(f"[YouTube] Could not re-read the title of {vid}: {e}")
                    _detail_pause()
                if title:
                    video = placeholders[vid]
                    logger.info(f"[YouTube] Retitled {vid}: '{video.title}' → '{title}'")
                    video.title = title
                    retitled.append(vid)
                    db.commit()
            if library_tab and vid in kept_ids:
                seen_kept += 1
                _report()
            continue
        if library_tab and seen_kept >= keep:
            beyond += 1
            continue
        try:
            details = _details(vid)
        except (VideoUnavailable, YouTubeError) as e:
            if isinstance(e, YouTubeBlocked):
                _mark_blocked(db, channel, e)
                raise
            # Recorded, and read again later (see _retry_later): it used to be
            # forgotten, so it counted as new — and was read again — on every run.
            reason = UNAVAILABLE_REASON if isinstance(e, VideoUnavailable) else UNREADABLE_REASON
            logger.debug(f"[YouTube] Skipping {vid} for now: {e}")
            row = YouTubeVideo(channel_fk=channel.id, video_id=vid,
                               title=_usable_title(entry.get("title"), vid) or vid,
                               first_seen=now, last_seen=now, removed_at=now, check_failures=0)
            _retry_later(row, reason)
            db.add(row)
            db.commit()
            _note_skip(reason)
            continue

        wanted, reason = _should_index(details, channel)
        if not wanted:
            logger.debug(f"[YouTube] Skipping {vid}: {reason}")
            _note_skip(reason)
            # Recorded, not discarded. Details are fetched one every few
            # seconds, and a skipped video used to leave no trace — so a
            # channel whose back catalogue is mostly excluded paid the full
            # detail cost again on every refresh, and never settled.
            db.add(YouTubeVideo(
                channel_fk=channel.id,
                video_id=vid,
                title=details.get("title") or entry.get("title") or vid,
                published_at=_published(details),
                duration=details.get("duration"),
                live_status=details.get("live_status"),
                media_type="livestream" if details.get("live_status") else "video",
                thumbnail_url=details.get("thumbnail"),
                first_seen=now,
                last_seen=now,
                removed_at=now,
                skip_reason=reason,
            ))
            db.commit()
            _detail_pause()
            continue

        db.add(YouTubeVideo(
            channel_fk=channel.id,
            video_id=vid,
            title=details.get("title") or entry.get("title") or vid,
            description=details.get("description"),
            published_at=_published(details),
            duration=details.get("duration"),
            live_status=details.get("live_status"),
            media_type="livestream" if details.get("live_status") else "video",
            thumbnail_url=details.get("thumbnail"),
            is_made_for_kids=made_for_kids(details),
            first_seen=now,
            last_seen=now,
        ))
        added += 1
        db.commit()
        if library_tab and details.get("live_status") not in PENDING_LIVE:
            seen_kept += 1
        # Reported per video: a first index can take minutes, and waiting for
        # the whole channel to finish leaves the UI with nothing to show.
        _report()
        _detail_pause()

    unindexed = _apply_stream_preference(db, channel)
    if unindexed > 0:
        logger.info(
            f"[YouTube] Removed {unindexed} item(s) from '{channel.title}' that no longer "
            f"match its settings"
        )
    elif unindexed < 0:
        logger.info(
            f"[YouTube] Restored {-unindexed} past live stream(s) to '{channel.title}'"
        )

    channel.last_checked = now
    channel.last_full_check = now
    # What the feed showed before this listing has been dealt with by it; the
    # next light check only reacts to ids beyond these. (A manual refresh reads
    # no feed and leaves the stored ids as they were.)
    if feed_ids is not None:
        channel.feed_ids = feed_ids
    channel.last_error = None
    channel.error_count = 0
    channel.blocked_until = None
    channel.last_skips = skips
    channel.last_listing = listing
    channel.last_indexed_count = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
    ).count()
    db.commit()
    logger.info(f"[YouTube] '{channel.title}': {added} new, {len(seen_ids)} listed, {skipped} skipped"
                + (f", {beyond} past the newest {keep} not fetched" if beyond else ""))
    if skips:
        logger.info(f"[YouTube] '{channel.title}' skips: {skips}")
    return {"skipped": False, "new": added, "seen": len(seen_ids),
            "filtered": skipped, "skips": skips, "listing": listing, "beyond": beyond,
            "retitled": retitled}


def _published(details: dict):
    ts = details.get("release_timestamp") or details.get("timestamp")
    if ts:
        try:
            return datetime.utcfromtimestamp(int(ts))
        except (TypeError, ValueError, OSError):
            pass
    upload = details.get("upload_date")
    if upload:
        try:
            return datetime.strptime(upload, "%Y%m%d")
        except ValueError:
            pass
    return None

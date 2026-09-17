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
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from models.database import YouTubeChannel, YouTubeVideo
from services.youtube import client
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
# fetched slowly. The listing call is cheap and unmetered by comparison.
DETAIL_SPACING_SECONDS = 5.0
# How long to stand down after a bot check. Retrying into one makes it worse.
BLOCK_BACKOFF_HOURS = 6



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


def parse_input_url(url: str) -> dict:
    """Work out what the user pasted: a channel, a handle, or a playlist."""
    url = (url or "").strip()
    if not url:
        raise ValueError("No URL given")
    if not url.startswith("http"):
        # Bare "@handle" or a channel id
        url = f"https://www.youtube.com/{url.lstrip('/')}"

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

    user = re.search(r"/(?:user|c)/([A-Za-z0-9_.-]+)", url)
    if user:
        return {"kind": "channel", "handle": user.group(1), "canonical": url}

    raise ValueError("Not a YouTube channel or playlist URL")


def resolve_channel(url: str) -> dict:
    """One listing call to learn a channel's identity and artwork."""
    parsed = parse_input_url(url)
    listing_url = parsed["canonical"]
    if parsed["kind"] == "channel":
        listing_url = listing_url.rstrip("/") + "/videos"

    info = client.flat_listing(listing_url, 1)
    title = info.get("channel") or info.get("uploader") or info.get("title") or "YouTube"
    thumbs = info.get("thumbnails") or []

    def _pick(*keys):
        for t in thumbs:
            if any(k in (t.get("id") or "") for k in keys):
                return t.get("url")
        return thumbs[-1].get("url") if thumbs else None

    return {
        "kind": parsed["kind"],
        "channel_id": info.get("channel_id") or parsed.get("channel_id"),
        "handle": parsed.get("handle"),
        "playlist_id": parsed.get("playlist_id"),
        "title": title,
        "avatar_url": _pick("avatar"),
        "banner_url": _pick("banner"),
        "canonical": parsed["canonical"],
    }


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
        return False, "finished live stream and past live streams are not included"

    duration = details.get("duration") or 0
    if channel.min_duration and duration and duration < channel.min_duration:
        return False, f"duration {duration}s under minimum {channel.min_duration}s"
    return True, ""


def is_library_item(video) -> bool:
    """Whether this video should get .strm/NFO files.

    Live and upcoming streams are Live TV guide entries, not library items.
    Once a stream ends its live_status clears and it becomes an ordinary video,
    at which point the next sync writes its files.
    """
    return video.live_status not in PENDING_LIVE


def _drop_disqualified(db: Session, channel: YouTubeChannel) -> int:
    """Remove library items that no longer match the channel's settings.

    Settings change after the fact — most importantly, enabling Live TV used to
    pull a channel's finished broadcasts into the library. Turning "Past live
    streams" off should clear them out rather than leaving them stranded, and
    only the video's own folder is touched.
    """
    from services.youtube import library

    if channel.include_streams:
        return 0

    stale = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.live_status.in_(FINISHED_LIVE),
        YouTubeVideo.removed_at.is_(None),
    ).all()
    for video in stale:
        library.remove_video(video)
        video.removed_at = datetime.utcnow()
        video.strm_path = None
    if stale:
        db.commit()
    return len(stale)


def index_channel(db: Session, channel: YouTubeChannel, limit: int = None,
                  on_progress=None) -> dict:
    """Refresh one channel. Returns counts; raises on a blocked/unavailable listing.

    New videos are recorded but NOT given media files here — library.py writes
    those, so an indexing failure can never leave half-written files behind.
    """
    if channel.blocked_until and channel.blocked_until > datetime.utcnow():
        logger.info(f"[YouTube] '{channel.title}' is backed off until {channel.blocked_until} — skipping")
        return {"skipped": True, "new": 0, "seen": 0}

    limit = limit or channel.backfill or 30
    known = {v.video_id for v in db.query(YouTubeVideo.video_id).filter(
        YouTubeVideo.channel_fk == channel.id).all()}

    seen_ids, new_videos = [], []
    try:
        for url in _tab_urls(channel):
            info = client.flat_listing(url, limit)
            for entry in (info.get("entries") or []):
                vid = entry.get("id")
                if not vid or not VIDEO_ID_RE.match(vid):
                    continue
                seen_ids.append(vid)
                if vid not in known:
                    new_videos.append((vid, entry))
    except YouTubeBlocked as e:
        channel.blocked_until = datetime.utcnow() + timedelta(hours=BLOCK_BACKOFF_HOURS)
        channel.last_error = "YouTube asked us to prove we're not a bot — backing off"
        channel.error_count = (channel.error_count or 0) + 1
        db.commit()
        logger.warning(f"[YouTube] '{channel.title}' bot-checked; backing off {BLOCK_BACKOFF_HOURS}h: {e}")
        raise
    except YouTubeError as e:
        channel.last_error = str(e)[:400]
        channel.error_count = (channel.error_count or 0) + 1
        db.commit()
        logger.warning(f"[YouTube] Listing failed for '{channel.title}': {e}")
        raise

    # Anything still listed is alive — refresh last_seen so retention leaves it be.
    now = datetime.utcnow()

    # Re-check anything currently marked live or upcoming. Details are only
    # fetched for NEW videos, so a stream indexed while scheduled would keep
    # live_status="is_upcoming" forever and never become playable on its Live TV
    # channel. The set is small — only pending broadcasts.
    pending = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.live_status.in_(PENDING_LIVE),
        YouTubeVideo.removed_at.is_(None),
    ).all()
    for video in pending:
        try:
            details = client.video_details(video.video_id)
        except YouTubeBlocked:
            raise
        except YouTubeError:
            continue
        was = video.live_status
        video.live_status = details.get("live_status")
        if details.get("duration"):
            video.duration = details["duration"]
        published = _published(details)
        if published and not video.published_at:
            video.published_at = published
        if was != video.live_status:
            logger.info(
                f"[YouTube] '{video.title}' {was} → {video.live_status or 'ended'}"
            )
    if pending:
        db.commit()
    if seen_ids:
        for i in range(0, len(seen_ids), 500):
            db.query(YouTubeVideo).filter(
                YouTubeVideo.channel_fk == channel.id,
                YouTubeVideo.video_id.in_(seen_ids[i:i + 500]),
            ).update({YouTubeVideo.last_seen: now}, synchronize_session=False)

    added, skipped = 0, 0
    skips: dict = {}

    def _note_skip(reason: str):
        nonlocal skipped
        skipped += 1
        # Keep the reason, drop the specifics, so counts aggregate.
        key = re.sub(r"\d+", "N", reason)
        skips[key] = skips.get(key, 0) + 1

    for vid, entry in new_videos:
        try:
            details = client.video_details(vid)
        except VideoUnavailable as e:
            logger.debug(f"[YouTube] Skipping {vid}: {e}")
            _note_skip("unavailable (private, members-only or removed)")
            continue
        except YouTubeBlocked as e:
            channel.blocked_until = datetime.utcnow() + timedelta(hours=BLOCK_BACKOFF_HOURS)
            db.commit()
            logger.warning(f"[YouTube] Bot-checked mid-index of '{channel.title}': {e}")
            raise
        except YouTubeError as e:
            logger.debug(f"[YouTube] Details failed for {vid}: {e}")
            _note_skip("could not read details")
            continue

        keep, reason = _should_index(details, channel)
        if not keep:
            logger.debug(f"[YouTube] Skipping {vid}: {reason}")
            _note_skip(reason)
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
            is_made_for_kids=details.get("age_limit") == 0 and details.get("is_live") is None or None,
            first_seen=now,
            last_seen=now,
        ))
        added += 1
        db.commit()
        # Reported per video: a first index can take minutes, and waiting for
        # the whole channel to finish leaves the UI with nothing to show.
        if on_progress:
            on_progress(added, len(new_videos))
        time.sleep(DETAIL_SPACING_SECONDS)

    unindexed = _drop_disqualified(db, channel)
    if unindexed:
        logger.info(
            f"[YouTube] Removed {unindexed} item(s) from '{channel.title}' that no longer "
            f"match its settings"
        )

    channel.last_checked = now
    channel.last_error = None
    channel.error_count = 0
    channel.blocked_until = None
    channel.last_skips = skips
    channel.last_indexed_count = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
    ).count()
    db.commit()
    logger.info(f"[YouTube] '{channel.title}': {added} new, {len(seen_ids)} listed, {skipped} skipped")
    if skips:
        logger.info(f"[YouTube] '{channel.title}' skips: {skips}")
    return {"skipped": False, "new": added, "seen": len(seen_ids),
            "filtered": skipped, "skips": skips}


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

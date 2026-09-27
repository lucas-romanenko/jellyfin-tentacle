"""Expose a YouTube channel's live and upcoming streams as a Live TV channel.

These are deliberately NOT LiveChannel rows: that table requires a provider_id,
and a YouTube channel is not an IPTV provider. Instead the HDHomeRun lineup and
the XMLTV guide union in the channels computed here, so YouTube channels appear
alongside IPTV ones without pretending to be them.

Guide entries come straight from the indexed live/upcoming videos. Two rules
from Jellyfin's DVR behaviour shape this:
  * A programme's identity is channel + start time, so moving a start deletes
    any timer set against it. Start times are therefore frozen once written.
  * No filler programmes. A channel with nothing scheduled simply has an empty
    guide — inventing "No programme" entries floods Jellyfin's "On Now".
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from models.database import EPGProgram, YouTubeChannel, YouTubeVideo
from services.epg_categories import infer_category
from services.youtube.indexer import PENDING_LIVE

logger = logging.getLogger(__name__)

# Guide numbers for YouTube channels start here. IPTV channels use their
# provider stream id as the guide number, and a panel of any size has ids in
# this range too, so the number is checked against them (#179).
GUIDE_NUMBER_BASE = 9000
# Where a channel goes when GUIDE_NUMBER_BASE + id is an IPTV channel's number.
GUIDE_NUMBER_FALLBACK_BASE = 90000
# How long a live stream is assumed to run when its duration is unknown.
DEFAULT_LIVE_HOURS = 3

# Collisions already logged, so a lineup Jellyfin polls does not repeat them.
_warned_collisions: set = set()


def epg_channel_id(channel: YouTubeChannel) -> str:
    return f"yt.{channel.slug}"


def iptv_guide_numbers(db: Session) -> set:
    """Guide numbers the enabled IPTV channels use (what hdhr_lineup lists)."""
    from models.database import LiveChannel
    return {
        str(stream_id or ch_id)
        for stream_id, ch_id in db.query(LiveChannel.stream_id, LiveChannel.id).filter(
            LiveChannel.enabled == True)  # noqa: E712
    }


def guide_number(channel: YouTubeChannel, taken: set = None) -> str:
    """The channel's guide number, clear of `taken` (numbers already in the lineup).

    Jellyfin's HDHomeRun tuner names a channel hdhr_<GuideNumber>, so two
    lineup entries with one number become ONE channel: one of them vanishes
    from Live TV and the guide maps both schedules onto the other. A number
    the user pinned is used while it is free; one that another channel has
    is set aside (and logged) rather than handed to Jellyfin twice (#179).
    """
    if channel.channel_number:
        number = str(channel.channel_number)
        if not taken or number not in taken:
            return number
        if (channel.id, number) not in _warned_collisions:
            _warned_collisions.add((channel.id, number))
            logger.warning(f"[YouTube] Live TV channel '{channel.title}' is pinned to guide number "
                           f"{number}, which another Live TV channel uses: using a free number instead "
                           f"(Jellyfin would merge the two)")
    number = str(GUIDE_NUMBER_BASE + channel.id)
    if not taken or number not in taken:
        return number
    fallback = GUIDE_NUMBER_FALLBACK_BASE + channel.id
    while str(fallback) in taken:
        fallback += GUIDE_NUMBER_FALLBACK_BASE
    if (channel.id, number) not in _warned_collisions:
        _warned_collisions.add((channel.id, number))
        logger.warning(f"[YouTube] Guide number {number} for '{channel.title}' is an IPTV channel's; "
                       f"using {fallback}. Pin a channel number to choose one yourself.")
    return str(fallback)


def _lineup_channels(db: Session) -> list:
    return db.query(YouTubeChannel).filter(
        YouTubeChannel.live_enabled == True,  # noqa: E712
        YouTubeChannel.enabled == True,  # noqa: E712
    ).order_by(YouTubeChannel.title).all()


def lineup_numbers(db: Session, channels: list = None) -> dict:
    """YouTube channel id -> guide number, for every channel in the lineup:
    clear of the enabled IPTV channels AND of each other (#179). Pinned
    numbers are claimed first, in id order, so a pin keeps its number over an
    automatic 9000 + id; everything is decided from the database alone, so the
    lineup, the XMLTV, the M3U and the YouTube page agree."""
    channels = _lineup_channels(db) if channels is None else channels
    taken = iptv_guide_numbers(db)
    out = {}
    by_id = sorted(channels, key=lambda c: c.id)
    for ch in by_id:
        if ch.channel_number and str(ch.channel_number) not in taken:
            out[ch.id] = str(ch.channel_number)
            taken.add(out[ch.id])
    for ch in by_id:
        if ch.id not in out:
            out[ch.id] = guide_number(ch, taken)
            taken.add(out[ch.id])
    return out


def number_for(db: Session, channel: YouTubeChannel) -> str:
    """The guide number this channel has (in the lineup) or would get."""
    numbers = lineup_numbers(db)
    if channel.id in numbers:
        return numbers[channel.id]
    return guide_number(channel, iptv_guide_numbers(db) | set(numbers.values()))


def live_channels(db: Session) -> list:
    """Channels the user has opted into Live TV, as lineup/XMLTV dicts."""
    out = []
    channels = _lineup_channels(db)
    numbers = lineup_numbers(db, channels)
    for ch in channels:
        out.append({
            "youtube_channel_id": ch.id,
            "guide_number": numbers[ch.id],
            "name": ch.title,
            "logo_url": ch.avatar_url,
            "group_title": "YouTube",
            "epg_channel_id": epg_channel_id(ch),
        })
    return out


def current_live_video(db: Session, channel_id: int):
    """The video to play for this channel right now, if one is live."""
    return (
        db.query(YouTubeVideo)
        .filter(
            YouTubeVideo.channel_fk == channel_id,
            YouTubeVideo.live_status == "is_live",
            YouTubeVideo.removed_at.is_(None),
        )
        .order_by(YouTubeVideo.published_at.desc().nullslast())
        .first()
    )


def refresh_jellyfin_guide(db: Session) -> bool:
    """Tell Jellyfin the lineup changed. Never raises: a guide refresh that
    fails is logged and the next scheduled one catches up, but a channel that
    was added should not fail over it."""
    from models.database import get_setting
    from services.jellyfin_guide import refresh_jellyfin_guide as _refresh

    url = get_setting(db, "jellyfin_url", "")
    key = get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        logger.info("[YouTube] Jellyfin is not configured — guide refresh skipped")
        return False
    try:
        _refresh(url, key)
        logger.info("[YouTube] Asked Jellyfin to refresh its Live TV guide")
        return True
    except Exception as e:
        logger.warning(f"[YouTube] Jellyfin guide refresh failed: {e}")
        return False


def refresh_guide(db: Session, channel: YouTubeChannel) -> int:
    """Write guide entries for this channel's live and upcoming streams.

    Returns the number of programmes written. Existing entries keep their start
    time — rewriting one would orphan any DVR timer set against it.
    """
    cid = epg_channel_id(channel)
    videos = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.live_status.in_(PENDING_LIVE),
        YouTubeVideo.removed_at.is_(None),
    ).all()

    written = 0
    for video in videos:
        start = video.published_at
        if not start:
            continue
        existing = db.query(EPGProgram).filter(
            EPGProgram.channel_id == cid,
            EPGProgram.start == start,
        ).first()

        stop = start + timedelta(
            seconds=video.duration or DEFAULT_LIVE_HOURS * 3600
        )
        # A stream that's live right now runs at least until we next look.
        if video.live_status == "is_live":
            stop = max(stop, datetime.utcnow() + timedelta(minutes=30))

        if existing:
            # Only ever extend — never move the start.
            if stop > existing.stop:
                existing.stop = stop
                written += 1
            continue

        db.add(EPGProgram(
            channel_id=cid,
            title=video.title,
            description=video.description,
            start=start,
            stop=stop,
            # Inferred from the title, same as provider EPG with no category.
            # Being live says nothing about genre — a cooking stream is not sport.
            category=infer_category(video.title, channel.title,
                                    live_prefix_is_sport=False),
            icon_url=video.thumbnail_url,
        ))
        written += 1

    # Drop entries for streams that never happened and are long past.
    cutoff = datetime.utcnow() - timedelta(days=1)
    db.query(EPGProgram).filter(
        EPGProgram.channel_id == cid,
        EPGProgram.stop < cutoff,
    ).delete(synchronize_session=False)

    db.commit()
    return written

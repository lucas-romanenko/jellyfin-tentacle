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

logger = logging.getLogger(__name__)

# Guide numbers for YouTube channels start here, well clear of IPTV stream ids.
GUIDE_NUMBER_BASE = 9000
# How long a live stream is assumed to run when its duration is unknown.
DEFAULT_LIVE_HOURS = 3


def epg_channel_id(channel: YouTubeChannel) -> str:
    return f"yt.{channel.slug}"


def guide_number(channel: YouTubeChannel) -> str:
    return channel.channel_number or str(GUIDE_NUMBER_BASE + channel.id)


def live_channels(db: Session) -> list:
    """Channels the user has opted into Live TV, as lineup/XMLTV dicts."""
    out = []
    for ch in db.query(YouTubeChannel).filter(
        YouTubeChannel.live_enabled == True,  # noqa: E712
        YouTubeChannel.enabled == True,  # noqa: E712
    ).order_by(YouTubeChannel.title).all():
        out.append({
            "youtube_channel_id": ch.id,
            "guide_number": guide_number(ch),
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


def refresh_guide(db: Session, channel: YouTubeChannel) -> int:
    """Write guide entries for this channel's live and upcoming streams.

    Returns the number of programmes written. Existing entries keep their start
    time — rewriting one would orphan any DVR timer set against it.
    """
    cid = epg_channel_id(channel)
    videos = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.live_status.in_(("is_live", "is_upcoming")),
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

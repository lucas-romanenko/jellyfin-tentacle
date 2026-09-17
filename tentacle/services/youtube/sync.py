"""Periodic refresh: index every enabled channel, write files, apply retention."""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from models.database import YouTubeChannel, YouTubeVideo, get_setting
from services.youtube import indexer, library
from services.youtube.errors import YouTubeError

logger = logging.getLogger(__name__)


def base_url(db: Session) -> str:
    """The address written into .strm files.

    Must be reachable by the Jellyfin server, not just by a browser — Jellyfin's
    ffmpeg is what fetches it.
    """
    configured = (get_setting(db, "youtube_base_url", "") or "").strip()
    return configured.rstrip("/")


def sync_channel(db: Session, channel: YouTubeChannel, base: str) -> dict:
    """Index one channel, write any new media files, then apply retention."""
    result = indexer.index_channel(db, channel)
    if result.get("skipped"):
        return result

    written = 0
    for video in db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
    ).all():
        if video.strm_path or not indexer.is_library_item(video):
            continue
        try:
            library.write_video(video, channel, base)
            written += 1
        except OSError as e:
            logger.warning(f"[YouTube] Could not write files for {video.video_id}: {e}")
    db.commit()

    # Guide entries for live/upcoming streams when the channel is on Live TV.
    guide = 0
    if channel.live_enabled:
        from services.youtube import livetv
        guide = livetv.refresh_guide(db, channel)

    removed = apply_retention(db, channel)
    result.update({"written": written, "retired": removed, "guide": guide})
    return result


def apply_retention(db: Session, channel: YouTubeChannel) -> int:
    """Retire videos past the channel's keep window.

    Only ever acts on a listing that succeeded — index_channel raises otherwise,
    so this is never reached with a partial picture. Each removal touches a
    single video's own folder.
    """
    videos = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
    ).order_by(YouTubeVideo.published_at.desc().nullslast()).all()

    doomed = []
    if channel.keep_count and len(videos) > channel.keep_count:
        doomed.extend(videos[channel.keep_count:])
    if channel.keep_days:
        cutoff = datetime.utcnow() - timedelta(days=channel.keep_days)
        doomed.extend(v for v in videos
                      if v.published_at and v.published_at < cutoff and v not in doomed)

    for video in doomed:
        library.remove_video(video)
        video.removed_at = datetime.utcnow()
        video.strm_path = None
    if doomed:
        db.commit()
        logger.info(f"[YouTube] Retired {len(doomed)} video(s) from '{channel.title}'")
    return len(doomed)


def run_youtube_sync() -> dict:
    """Scheduler entry point."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        if get_setting(db, "youtube_enabled", "false") != "true":
            return {"enabled": False}
        base = base_url(db)
        if not base:
            logger.warning("[YouTube] youtube_base_url is not set — skipping (a .strm needs an address Jellyfin can reach)")
            return {"enabled": True, "error": "youtube_base_url not set"}

        totals = {"channels": 0, "new": 0, "written": 0, "retired": 0, "errors": 0}
        for channel in db.query(YouTubeChannel).filter(YouTubeChannel.enabled == True).all():  # noqa: E712
            totals["channels"] += 1
            try:
                r = sync_channel(db, channel, base)
                totals["new"] += r.get("new", 0)
                totals["written"] += r.get("written", 0)
                totals["retired"] += r.get("retired", 0)
            except YouTubeError as e:
                totals["errors"] += 1
                logger.warning(f"[YouTube] '{channel.title}' failed: {e}")
        if totals["new"] or totals["written"]:
            logger.info(f"[YouTube] Sync complete: {totals}")
        return totals
    finally:
        db.close()

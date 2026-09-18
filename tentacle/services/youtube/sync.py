"""Periodic refresh: index every enabled channel, write files, apply retention."""
import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from models.database import YouTubeChannel, YouTubeVideo, get_setting
from services.youtube import indexer, library
from services.youtube.errors import YouTubeError

logger = logging.getLogger(__name__)

# How long to let Jellyfin index newly written files before querying for their
# tags. Jellyfin reads NFO tags at scan time, so querying too early returns
# nothing and the playlist is built empty.
SCAN_SETTLE_SECONDS = 20


def base_url(db: Session) -> str:
    """The address written into .strm files.

    Must be reachable by the Jellyfin server, not just by a browser — Jellyfin's
    ffmpeg is what fetches it.
    """
    configured = (get_setting(db, "youtube_base_url", "") or "").strip()
    return configured.rstrip("/")


def _probe(url):
    """One GET, redirects not followed. A seam so this is testable offline."""
    import httpx
    with httpx.Client(timeout=10, follow_redirects=False) as c:
        return c.get(url)


def check_base_url(base: str) -> dict:
    """Fetch the address a .strm will carry and say whether it serves Tentacle.

    The setting only ever said the Jellyfin server must be able to reach it,
    which is true but unverified — and the failure is silent and badly
    misleading. An address behind Cloudflare Access, a reverse proxy asking for
    a login, or simply the wrong host answers a media request with an HTML login
    page, and ffmpeg reports that as "Invalid data found when processing input".
    Nothing in Tentacle or Jellyfin points at the address.
    """
    try:
        r = _probe(f"{base.rstrip('/')}/api/youtube/status")
    except Exception as e:
        return {"ok": False, "detail": f"Could not reach {base}: {e}"}

    if r.status_code in (301, 302, 303, 307, 308):
        where = r.headers.get("location", "")
        if "cloudflareaccess.com" in where or "/cdn-cgi/access/" in where:
            return {"ok": False, "detail":
                    "Cloudflare Access is protecting this address — it answers with a "
                    "login page, not media. Use the address on your own network "
                    "instead (for example http://192.168.1.10:8888)."}
        return {"ok": False, "detail":
                f"This address redirects to {where or 'somewhere else'}. A .strm has to "
                f"be served directly, with no login in front of it."}
    if r.status_code in (401, 403):
        return {"ok": False, "detail":
                f"This address asks for authentication (HTTP {r.status_code}). Jellyfin's "
                f"ffmpeg cannot log in — use an address with no login in front of it."}
    if r.status_code != 200:
        return {"ok": False, "detail": f"This address answered HTTP {r.status_code}."}
    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "detail":
                "This address answered with something other than Tentacle. Check it "
                "points at Tentacle itself and not a proxy or another service."}
    if "yt_dlp_available" not in body:
        return {"ok": False, "detail": "This address is answering, but it is not Tentacle."}
    return {"ok": True, "detail": f"{base} serves Tentacle directly."}


def sync_channel(db: Session, channel: YouTubeChannel, base: str, on_progress=None) -> dict:
    """Index one channel, write any new media files, then apply retention."""
    result = indexer.index_channel(db, channel, on_progress=on_progress)
    if result.get("skipped"):
        return result

    written, art = 0, 0
    for video in db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
    ).all():
        if not indexer.is_library_item(video):
            continue
        if video.strm_path:
            # Already written. Artwork arrived later than the writer did, so
            # videos indexed before it exist with no images — pick those up
            # instead of leaving them blank until something rewrites them.
            if video.folder_path:
                art += library.fetch_artwork(video, Path(video.folder_path))
            continue
        try:
            art += library.write_video(video, channel, base).get("artwork", 0)
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
    result.update({"written": written, "retired": removed, "guide": guide,
                   "artwork": art})
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


def publish_to_jellyfin(db: Session, changed_playlists: list) -> None:
    """Get Jellyfin to ingest new files, then rebuild the affected playlists.

    Without this nothing appears until Jellyfin's own scheduled scan: Tentacle
    writes the .strm and NFO, but Jellyfin reads the tags at scan time, so the
    playlists stay empty and their home rows are filtered out as empty — which
    looks exactly like the row toggle having done nothing.
    """
    import time

    from models.database import TentacleUser
    from services.jellyfin import JellyfinService

    url = get_setting(db, "jellyfin_url", "")
    key = get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return

    try:
        jf = JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))
        jf.trigger_library_scan()
        logger.info("[YouTube] Triggered a Jellyfin library scan")
    except Exception as e:
        logger.warning(f"[YouTube] Could not trigger a Jellyfin scan: {e}")
        return

    if not changed_playlists:
        return

    # Give Jellyfin a moment to index the new files before querying for them.
    time.sleep(SCAN_SETTLE_SECONDS)
    try:
        from services.smartlists import (
            _notify_jellyfin_plugin, bump_playlist_version,
            refresh_smartlist_playlists, write_home_config,
        )
        for user in db.query(TentacleUser).all():
            refresh_smartlist_playlists(db, user_id=user.id, only_names=changed_playlists)
            write_home_config(db, user_id=user.id)
        bump_playlist_version()
        _notify_jellyfin_plugin(db)
    except Exception as e:
        logger.warning(f"[YouTube] Playlist rebuild after scan failed: {e}")


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
        changed = []
        for channel in db.query(YouTubeChannel).filter(YouTubeChannel.enabled == True).all():  # noqa: E712
            totals["channels"] += 1
            try:
                r = sync_channel(db, channel, base)
                totals["new"] += r.get("new", 0)
                totals["written"] += r.get("written", 0)
                totals["retired"] += r.get("retired", 0)
                if r.get("written") or r.get("retired"):
                    changed.append(channel.title)
            except YouTubeError as e:
                totals["errors"] += 1
                logger.warning(f"[YouTube] '{channel.title}' failed: {e}")
        if changed:
            publish_to_jellyfin(db, changed)
        if totals["new"] or totals["written"]:
            logger.info(f"[YouTube] Sync complete: {totals}")
        return totals
    finally:
        db.close()

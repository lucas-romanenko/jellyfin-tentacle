"""YouTube source: admin CRUD plus the public playback endpoints.

The playback routes are deliberately unauthenticated, like /api/live/stream:
Jellyfin's ffmpeg fetches a .strm's contents with no Tentacle session. They are
not an open proxy — a video id must exist in youtube_videos, and segment URLs
are opaque tokens minted by our own playlist rewriter, so an arbitrary host can
never be requested through them.
"""
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from models.database import YouTubeChannel, YouTubeVideo, get_db, get_setting
from routers.auth import require_admin
from services.youtube import client, indexer, library, playlist, resolver
from services.youtube.errors import YouTubeBlocked, YouTubeError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/youtube", tags=["youtube"])

UPSTREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=10.0, pool=15.0)


# ── Playback (public) ───────────────────────────────────────────────────────

def _known_video(db: Session, video_id: str) -> YouTubeVideo:
    if not indexer.VIDEO_ID_RE.match(video_id or ""):
        raise HTTPException(400, "Invalid video id")
    video = db.query(YouTubeVideo).filter(YouTubeVideo.video_id == video_id).first()
    if not video:
        raise HTTPException(404, "Unknown video")
    return video


@router.get("/v/{video_id}/master.m3u8")
@router.head("/v/{video_id}/master.m3u8")
def master_playlist(video_id: str, db: Session = Depends(get_db)):
    """What a .strm points at. Jellyfin re-probes this on every PlaybackInfo."""
    video = _known_video(db, video_id)
    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == video.channel_fk).first()
    max_height = (channel.max_height if channel else 1080) or 1080

    try:
        resolved = resolver.resolve(video_id, max_height)
        with httpx.Client(timeout=UPSTREAM_TIMEOUT, follow_redirects=True) as c:
            r = c.get(resolved.master_url, headers=resolved.headers)
            r.raise_for_status()
            text = r.text
    except YouTubeBlocked as e:
        logger.warning(f"[YouTube] Blocked resolving {video_id}: {e}")
        raise HTTPException(503, "YouTube is rate-limiting this server; try again shortly")
    except (YouTubeError, httpx.HTTPError) as e:
        logger.warning(f"[YouTube] Could not resolve {video_id}: {e}")
        raise HTTPException(502, "Could not resolve this video")

    body = playlist.rewrite(text, resolved.master_url,
                            f"/api/youtube/v/{video_id}", max_height=max_height)
    return Response(content=body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-cache"})


@router.get("/v/{video_id}/r/{token}")
def proxied(video_id: str, token: str, request: Request, db: Session = Depends(get_db)):
    """Serve one upstream playlist or segment through Tentacle.

    Playlists are rewritten in turn; media is streamed straight through, byte
    ranges included, so seeking works.
    """
    _known_video(db, video_id)
    # The extension exists for ffmpeg's benefit (see playlist.rewrite); the
    # token itself is what identifies the upstream URL.
    for suffix in (".m3u8", ".ts", ".m4s", ".mp4"):
        if token.endswith(suffix):
            token = token[: -len(suffix)]
            break
    target = playlist.lookup(token)
    if not target:
        # Tokens are lost on restart; the client re-fetches the master and gets fresh ones.
        raise HTTPException(404, "Unknown or expired stream token")

    headers = {}
    resolved = resolver.resolve(video_id)
    headers.update(resolved.headers or {})
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client_ = httpx.Client(timeout=UPSTREAM_TIMEOUT, follow_redirects=True)
    try:
        req = client_.build_request("GET", target, headers=headers)
        upstream = client_.send(req, stream=True)
        upstream.raise_for_status()
    except httpx.HTTPError as e:
        client_.close()
        logger.warning(f"[YouTube] Upstream fetch failed for {video_id}: {e}")
        raise HTTPException(502, "Upstream fetch failed")

    content_type = upstream.headers.get("content-type", "")
    if "mpegurl" in content_type.lower() or target.endswith(".m3u8") or "/api/manifest/" in target:
        try:
            text = upstream.read().decode("utf-8", errors="replace")
        finally:
            upstream.close()
            client_.close()
        body = playlist.rewrite(text, target, f"/api/youtube/v/{video_id}")
        return Response(content=body, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})

    def _stream():
        try:
            for chunk in upstream.iter_bytes(chunk_size=65536):
                yield chunk
        finally:
            upstream.close()
            client_.close()

    passthrough = {k: v for k, v in upstream.headers.items()
                   if k.lower() in ("content-length", "content-range", "accept-ranges")}
    return StreamingResponse(_stream(), status_code=upstream.status_code,
                             media_type=content_type or "video/mp2t",
                             headers=passthrough)


@router.get("/live/{channel_id}/master.m3u8")
@router.head("/live/{channel_id}/master.m3u8")
def live_master(channel_id: int, db: Session = Depends(get_db)):
    """What the HDHomeRun lineup points a YouTube Live TV channel at.

    The live video changes over the day, so it is resolved per request rather
    than baked into a stored URL.
    """
    from services.youtube import livetv as yt_livetv

    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if not channel or not channel.live_enabled:
        raise HTTPException(404, "Not a Live TV channel")

    video = yt_livetv.current_live_video(db, channel_id)
    if not video:
        # Nothing is live. 503 rather than 404: the channel exists, it just has
        # nothing on right now, and Jellyfin retries rather than dropping it.
        raise HTTPException(503, f"{channel.title} is not streaming right now")

    try:
        resolved = resolver.resolve(video.video_id, channel.max_height or 1080)
        with httpx.Client(timeout=UPSTREAM_TIMEOUT, follow_redirects=True) as c:
            r = c.get(resolved.master_url, headers=resolved.headers)
            r.raise_for_status()
            text = r.text
    except YouTubeBlocked:
        raise HTTPException(503, "YouTube is rate-limiting this server; try again shortly")
    except (YouTubeError, httpx.HTTPError) as e:
        logger.warning(f"[YouTube] Live resolve failed for '{channel.title}': {e}")
        raise HTTPException(502, "Could not resolve the live stream")

    body = playlist.rewrite(text, resolved.master_url,
                            f"/api/youtube/v/{video.video_id}",
                            max_height=channel.max_height or 1080)
    return Response(content=body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-cache"})


class LiveToggle(BaseModel):
    enabled: bool
    channel_number: Optional[str] = None


@router.post("/channels/{channel_id}/live", dependencies=[Depends(require_admin)])
def toggle_live(channel_id: int, body: LiveToggle, db: Session = Depends(get_db)):
    """Expose (or stop exposing) this channel's live streams as a Live TV channel."""
    from services.youtube import livetv as yt_livetv

    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "Channel not found")
    channel.live_enabled = body.enabled
    if body.channel_number:
        channel.channel_number = body.channel_number
    db.commit()

    guide = 0
    if body.enabled:
        guide = yt_livetv.refresh_guide(db, channel)
    else:
        from models.database import EPGProgram
        db.query(EPGProgram).filter(
            EPGProgram.channel_id == yt_livetv.epg_channel_id(channel)
        ).delete(synchronize_session=False)
        db.commit()

    logger.info(f"[YouTube] Live TV {'enabled' if body.enabled else 'disabled'} for '{channel.title}'")
    return {"success": True, "live_enabled": body.enabled,
            "guide_number": yt_livetv.guide_number(channel), "programmes": guide}


# ── Admin ───────────────────────────────────────────────────────────────────

class ChannelCreate(BaseModel):
    url: str
    include_videos: bool = True
    include_streams: bool = False
    include_shorts: bool = False
    min_duration: int = 60
    backfill: int = 30
    keep_count: Optional[int] = 200
    max_height: int = 1080
    rating: Optional[str] = None
    extra_tags: list = []


@router.get("/status", dependencies=[Depends(require_admin)])
def status(request: Request, db: Session = Depends(get_db)):
    """Whether the feature can run at all, plus a summary."""
    import os

    base = (get_setting(db, "youtube_base_url", "") or "").strip()
    # Suggest the address this request came in on, which is almost always the
    # one Jellyfin can reach too. Only a suggestion — the user confirms it.
    suggested = base
    if not suggested:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        scheme = request.headers.get("x-forwarded-proto", "http")
        if host:
            suggested = f"{scheme}://{host}"

    return {
        "enabled": get_setting(db, "youtube_enabled", "false") == "true",
        "base_url": base,
        "suggested_base_url": suggested,
        "media_root_mounted": os.path.isdir(str(library.YOUTUBE_MEDIA_ROOT)),
        "media_root": str(library.YOUTUBE_MEDIA_ROOT),
        "yt_dlp_available": client.available(),
        "yt_dlp_version": client.version(),
        "channels": db.query(YouTubeChannel).count(),
        "videos": db.query(YouTubeVideo).count(),
        "resolver_cache": resolver.cache_size(),
    }


class SetupBody(BaseModel):
    enabled: bool
    base_url: str = ""


@router.post("/setup", dependencies=[Depends(require_admin)])
def save_setup(body: SetupBody, db: Session = Depends(get_db)):
    """Turn the feature on and set the address written into .strm files."""
    from models.database import set_setting

    base = (body.base_url or "").strip().rstrip("/")
    if body.enabled and not base:
        raise HTTPException(
            400,
            "A base URL is required: every .strm carries this address, and the "
            "Jellyfin server is what fetches it.",
        )
    set_setting(db, "youtube_enabled", "true" if body.enabled else "false")
    if base:
        set_setting(db, "youtube_base_url", base)
    logger.info(f"[YouTube] Source {'enabled' if body.enabled else 'disabled'} (base {base or 'unset'})")
    return {"success": True, "enabled": body.enabled, "base_url": base}


@router.get("/channels", dependencies=[Depends(require_admin)])
def list_channels(request: Request, db: Session = Depends(get_db)):
    from models.database import YouTubeRowSubscription
    from routers.auth import get_user_from_request

    try:
        user = get_user_from_request(request, db)
        my_rows = {r.channel_fk for r in db.query(YouTubeRowSubscription).filter(
            YouTubeRowSubscription.user_id == user.id).all()}
    except Exception:
        my_rows = set()   # bootstrap mode / no session

    out = []
    for ch in db.query(YouTubeChannel).order_by(YouTubeChannel.title).all():
        out.append({
            "id": ch.id, "title": ch.title, "slug": ch.slug, "kind": ch.kind,
            "input_url": ch.input_url, "avatar_url": ch.avatar_url,
            "enabled": ch.enabled,
            "video_count": db.query(YouTubeVideo).filter(YouTubeVideo.channel_fk == ch.id).count(),
            "last_checked": ch.last_checked, "last_error": ch.last_error,
            "blocked_until": ch.blocked_until,
            "include_videos": ch.include_videos, "include_streams": ch.include_streams,
            "include_shorts": ch.include_shorts, "min_duration": ch.min_duration,
            "keep_count": ch.keep_count, "max_height": ch.max_height,
            "rating": ch.rating, "extra_tags": ch.extra_tags or [],
            "home_row": ch.id in my_rows,
            "live_enabled": ch.live_enabled,
        })
    return out


@router.post("/channels", dependencies=[Depends(require_admin)])
def add_channel(body: ChannelCreate, db: Session = Depends(get_db)):
    if not client.available():
        raise HTTPException(400, "yt-dlp is not installed in this image")
    try:
        info = indexer.resolve_channel(body.url)
    except YouTubeBlocked:
        raise HTTPException(503, "YouTube is rate-limiting this server; try again shortly")
    except (YouTubeError, ValueError) as e:
        raise HTTPException(400, f"Could not read that channel: {e}")

    existing = None
    if info.get("channel_id"):
        existing = db.query(YouTubeChannel).filter(
            YouTubeChannel.channel_id == info["channel_id"]).first()
    if existing:
        raise HTTPException(409, f"'{existing.title}' is already added")

    slug = indexer.slugify(info["title"])
    if db.query(YouTubeChannel).filter(YouTubeChannel.slug == slug).first():
        slug = f"{slug}-{int(datetime.utcnow().timestamp()) % 10000}"

    channel = YouTubeChannel(
        input_url=body.url, kind=info["kind"], channel_id=info.get("channel_id"),
        handle=info.get("handle"), playlist_id=info.get("playlist_id"),
        title=info["title"], slug=slug,
        avatar_url=info.get("avatar_url"), banner_url=info.get("banner_url"),
        include_videos=body.include_videos, include_streams=body.include_streams,
        include_shorts=body.include_shorts, min_duration=body.min_duration,
        backfill=body.backfill, keep_count=body.keep_count,
        max_height=body.max_height, rating=body.rating, extra_tags=body.extra_tags,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    logger.info(f"[YouTube] Added channel '{channel.title}' ({channel.slug})")
    return {"id": channel.id, "title": channel.title, "slug": channel.slug}


@router.post("/refresh", dependencies=[Depends(require_admin)])
def refresh_now(db: Session = Depends(get_db)):
    """Index every enabled channel now, rather than waiting for the interval."""
    from services.youtube.sync import base_url, sync_channel

    if not client.available():
        raise HTTPException(400, "yt-dlp is not installed in this image")
    base = base_url(db)
    if not base:
        raise HTTPException(
            400,
            "Set youtube_base_url first — a .strm has to carry an address the "
            "Jellyfin server itself can reach.",
        )

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
            logger.warning(f"[YouTube] Refresh failed for '{channel.title}': {e}")
    return totals


class RowToggle(BaseModel):
    enabled: bool
    max_items: int = 30


@router.post("/channels/{channel_id}/row")
def toggle_home_row(channel_id: int, body: RowToggle, request: Request,
                    db: Session = Depends(get_db)):
    """Add or remove this channel's home row for the calling user.

    Rows are per-user, like every other Tentacle playlist, so one household
    member subscribing doesn't put the channel on everyone's home screen.
    """
    from models.database import YouTubeRowSubscription
    from routers.auth import get_user_from_request

    user = get_user_from_request(request, db)
    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "Channel not found")

    sub = db.query(YouTubeRowSubscription).filter(
        YouTubeRowSubscription.channel_fk == channel_id,
        YouTubeRowSubscription.user_id == user.id,
    ).first()

    if body.enabled and not sub:
        db.add(YouTubeRowSubscription(channel_fk=channel_id, user_id=user.id,
                                      max_items=body.max_items))
    elif body.enabled and sub:
        sub.max_items = body.max_items
    elif sub:
        db.delete(sub)
    db.commit()

    # Build/remove the playlist straight away rather than waiting for the sync.
    try:
        from services.smartlists import (
            _notify_jellyfin_plugin, bump_playlist_version, refresh_smartlist_playlists,
            sync_smartlists, write_home_config,
        )
        sync_smartlists(db, user_id=user.id)
        refresh_smartlist_playlists(db, user_id=user.id, only_names=[channel.title])
        write_home_config(db, user_id=user.id)
        bump_playlist_version()
        _notify_jellyfin_plugin(db)
    except Exception as e:
        logger.warning(f"[YouTube] Playlist rebuild after row toggle failed: {e}")

    return {"success": True, "enabled": body.enabled, "playlist": channel.title}


@router.delete("/channels/{channel_id}", dependencies=[Depends(require_admin)])
def delete_channel(channel_id: int, delete_files: bool = False, db: Session = Depends(get_db)):
    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "Channel not found")
    removed = 0
    if delete_files:
        for video in db.query(YouTubeVideo).filter(YouTubeVideo.channel_fk == channel.id).all():
            removed += library.remove_video(video)
    title = channel.title
    db.delete(channel)   # cascades to videos and row subscriptions
    db.commit()
    logger.info(f"[YouTube] Removed channel '{title}' ({removed} file(s) deleted)")
    return {"success": True, "files_deleted": removed}

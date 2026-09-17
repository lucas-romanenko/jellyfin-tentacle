"""YouTube source: admin CRUD plus the public playback endpoints.

The playback routes are deliberately unauthenticated, like /api/live/stream:
Jellyfin's ffmpeg fetches a .strm's contents with no Tentacle session. They are
not an open proxy — a video id must exist in youtube_videos, and segment URLs
are opaque tokens minted by our own playlist rewriter, so an arbitrary host can
never be requested through them.
"""
import logging
import threading
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

def _proxy_prefix(db: Session, video_id: str) -> str:
    """Absolute prefix for rewritten playlist URLs.

    Absolute on purpose. Root-relative paths only resolve correctly while the
    playlist is being read straight off this server — anything that relocates
    it (writes it to a temp file, hands it to a player with a different base)
    loses the host and every segment 404s. The .strm already carries an
    absolute URL for the same reason.
    """
    base = (get_setting(db, "youtube_base_url", "") or "").strip().rstrip("/")
    return f"{base}/api/youtube/v/{video_id}" if base else f"/api/youtube/v/{video_id}"


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
                            _proxy_prefix(db, video_id), max_height=max_height)
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
        body = playlist.rewrite(text, target, _proxy_prefix(db, video_id))
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


@router.get("/live/{channel_id}/stream.ts")
@router.head("/live/{channel_id}/stream.ts")
def live_stream(channel_id: int, db: Session = Depends(get_db)):
    """What the HDHomeRun lineup points a YouTube Live TV channel at.

    Serves a continuous MPEG-TS byte stream, NOT an HLS playlist. Jellyfin's
    tuner opens this with SharedHttpStream, which reads the response body as
    video — hand it a playlist and it copies the playlist text as if it were
    video data, which is why playback stopped at 0 ms. The IPTV live path does
    the same thing for the same reason.

    ffmpeg does the muxing: YouTube's HLS variants are video-only with audio in
    a separate rendition, so concatenating one variant's segments would produce
    silent video. Remuxing is stream-copy only — no re-encoding.
    """
    import shutil
    import subprocess

    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if not channel or not channel.live_enabled:
        raise HTTPException(404, "Not a Live TV channel")

    from services.youtube import livetv as yt_livetv
    video = yt_livetv.current_live_video(db, channel_id)
    if not video:
        raise HTTPException(503, f"{channel.title} is not streaming right now")

    try:
        video_url, audio_url, headers = resolver.pick_tracks(
            video.video_id, channel.max_height or 1080)
    except YouTubeBlocked:
        raise HTTPException(503, "YouTube is rate-limiting this server; try again shortly")
    except YouTubeError as e:
        logger.warning(f"[YouTube] Live resolve failed for '{channel.title}': {e}")
        raise HTTPException(502, "Could not resolve the live stream")

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    user_agent = headers.get("User-Agent", "Mozilla/5.0")
    # ffmpeg runs on this host, so Google's IP-signed URLs are valid for it —
    # no need to route the segments back through our own proxy.
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-user_agent", user_agent, "-i", video_url]
    if audio_url:
        cmd += ["-user_agent", user_agent, "-i", audio_url,
                "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += [
        "-c", "copy",
        "-f", "mpegts",
        # Resend headers so a consumer joining mid-stream can still find the
        # program tables.
        "-mpegts_flags", "+resend_headers",
        "-muxdelay", "0", "-muxpreload", "0",
        "pipe:1",
    ]

    logger.info(f"[YouTube] Live TS stream for '{channel.title}' ({video.video_id})")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _stream():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
                if err:
                    logger.warning(f"[YouTube] ffmpeg for '{channel.title}': {err[:400]}")
            except Exception:
                pass
            proc.stdout.close()
            proc.stderr.close()
            proc.wait(timeout=5)
            logger.info(f"[YouTube] Live stream ended for '{channel.title}'")

    return StreamingResponse(
        _stream(),
        media_type="video/mp2t",
        headers={"Connection": "close", "Cache-Control": "no-cache, no-store"},
    )


@router.get("/live/{channel_id}/master.m3u8")
@router.head("/live/{channel_id}/master.m3u8")
def live_master(channel_id: int, db: Session = Depends(get_db)):
    """The same live stream as HLS, for players that prefer a playlist.

    Not what the tuner uses — see live_stream above. Kept for direct testing
    and for clients that handle HLS better than a raw TS pipe.
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
                            _proxy_prefix(db, video.video_id),
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
        # Videos indexed before now were fetched with live streams skipped, so
        # the guide would be empty until the next scheduled run. Go and look.
        with _refresh_lock:
            if not _refresh_state["running"]:
                _refresh_state.update({
                    "running": True, "started_at": datetime.utcnow().isoformat(),
                    "finished_at": None, "channel": None, "channels_done": 0,
                    "channels_total": 0, "new": 0, "written": 0, "retired": 0,
                    "errors": 0, "error_detail": None, "channel_total": 0,
                })
                threading.Thread(target=_run_refresh, daemon=True,
                                 name="youtube-refresh").start()
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


@router.get("/diagnose", dependencies=[Depends(require_admin)])
def diagnose(request: Request, db: Session = Depends(get_db)):
    """Walk the whole chain and report the first thing that is actually wrong.

    Playback and home rows depend on a long chain — image, mount, settings,
    index, files, Jellyfin library, Jellyfin scan, playlist, home row — and a
    break anywhere shows up to the user as "nothing happened" or a bare
    playback error. This names the link.
    """
    import os
    from models.database import YouTubeRowSubscription
    from routers.auth import get_user_from_request

    checks = []

    def add(ok, name, detail, fix=None):
        checks.append({"ok": bool(ok), "name": name, "detail": detail, "fix": fix})

    add(client.available(), "yt-dlp installed",
        client.version() or "missing",
        "Pull a newer Tentacle image.")

    root = str(library.YOUTUBE_MEDIA_ROOT)
    mounted = os.path.isdir(root)
    writable = mounted and os.access(root, os.W_OK)
    add(writable, "Media folder mounted and writable", root,
        f"Add '- /your/host/path:{root}' to Tentacle's volumes and recreate the container.")

    base = (get_setting(db, "youtube_base_url", "") or "").strip()
    enabled = get_setting(db, "youtube_enabled", "false") == "true"
    add(enabled, "YouTube source turned on", "on" if enabled else "off",
        "Turn it on at the top of this page.")
    add(bool(base), "Tentacle address set", base or "not set",
        "Set it at the top of this page. It must be reachable BY the Jellyfin server.")

    channels = db.query(YouTubeChannel).all()
    add(channels, "Channels added", f"{len(channels)} channel(s)", "Paste a channel URL above.")

    videos = db.query(YouTubeVideo).filter(YouTubeVideo.removed_at.is_(None)).count()
    add(videos, "Videos indexed", f"{videos} video(s)",
        "Press 'Refresh now'. The first index takes a few minutes.")

    on_disk = 0
    for v in db.query(YouTubeVideo).filter(YouTubeVideo.strm_path.isnot(None)).all():
        if v.strm_path and os.path.isfile(v.strm_path):
            on_disk += 1
    add(on_disk, "Pointer files written to disk", f"{on_disk} .strm file(s) under {root}",
        "Press 'Refresh now'.")

    # ── Jellyfin's side ──
    jf_url = get_setting(db, "jellyfin_url", "")
    jf_key = get_setting(db, "jellyfin_api_key", "")
    jf_items = {}
    if jf_url and jf_key:
        try:
            from services.jellyfin import JellyfinService
            jf = JellyfinService(jf_url, jf_key, get_setting(db, "jellyfin_user_id", ""))
            for ch in channels:
                tag = f"yt:{ch.slug}"
                found = jf.query_items(include_types=["Movie"], tags=[tag]) or []
                jf_items[ch.title] = len(found)
            total = sum(jf_items.values())
            add(total, "Jellyfin can see the videos",
                ", ".join(f"{k}: {v}" for k, v in jf_items.items()) or "none",
                "Add a Jellyfin library of type Movies pointing at the same host folder "
                f"you mounted at {root} (metadata fetchers OFF), then scan it. Jellyfin "
                "reads the tags from the NFO files, so nothing appears until it has scanned.")
        except Exception as e:
            add(False, "Jellyfin reachable", str(e)[:200], "Check Jellyfin's URL and API key in Settings.")
    else:
        add(False, "Jellyfin configured", "URL or API key missing", "Set them in Settings.")

    # ── Live TV ──
    live_channels = [c for c in channels if c.live_enabled]
    if live_channels:
        live_now = db.query(YouTubeVideo).filter(
            YouTubeVideo.live_status == "is_live",
            YouTubeVideo.removed_at.is_(None)).count()
        add(live_now, "A stream is live right now", f"{live_now} live",
            "Nothing is streaming, so the Live TV channel has nothing to play — Jellyfin "
            "shows that as a playback error. Press 'Refresh now' to re-check, and note "
            "that many channels never stream at all.")

    # ── Home rows ──
    try:
        user = get_user_from_request(request, db)
        subs = db.query(YouTubeRowSubscription).filter(
            YouTubeRowSubscription.user_id == user.id).all()
        if subs:
            from routers.smartlists import _read_home_json
            config = _read_home_json(user) or {}
            names = {c.title for c in channels
                     if c.id in {sub.channel_fk for sub in subs}}
            rows = {r.get("display_name") for r in (config.get("rows") or [])}
            missing = names - rows
            add(not missing, "Home rows present in your config",
                f"{len(names & rows)}/{len(names)} in place"
                + (f" (missing: {', '.join(sorted(missing))})" if missing else ""),
                "Toggle 'Home row' off and on again.")
    except Exception:
        pass

    first_bad = next((c for c in checks if not c["ok"]), None)
    return {"checks": checks, "blocking": first_bad}


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
            "live_now": db.query(YouTubeVideo).filter(
                YouTubeVideo.channel_fk == ch.id,
                YouTubeVideo.live_status == "is_live",
                YouTubeVideo.removed_at.is_(None)).count(),
            "upcoming": db.query(YouTubeVideo).filter(
                YouTubeVideo.channel_fk == ch.id,
                YouTubeVideo.live_status == "is_upcoming",
                YouTubeVideo.removed_at.is_(None)).count(),
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


# Indexing runs in the background and is polled. It cannot be a plain request:
# YouTube rate-limits guest extraction, so details are fetched ~5s apart, and a
# first index of 30 videos takes several minutes — far longer than a browser or
# a reverse proxy will wait.
_refresh_state: dict = {
    "running": False, "started_at": None, "finished_at": None,
    "channel": None, "channels_done": 0, "channels_total": 0,
    "new": 0, "written": 0, "retired": 0, "errors": 0, "error_detail": None,
    "channel_total": 0,
}
_refresh_lock = threading.Lock()


def _run_refresh():
    """Index every enabled channel. Runs on its own thread with its own session."""
    from models.database import SessionLocal
    from services.youtube.sync import base_url, sync_channel

    db = SessionLocal()
    try:
        base = base_url(db)
        channels = db.query(YouTubeChannel).filter(YouTubeChannel.enabled == True).all()  # noqa: E712
        _refresh_state["channels_total"] = len(channels)
        for channel in channels:
            _refresh_state["channel"] = channel.title
            channel_base = _refresh_state["new"]

            def _progress(added, total, _base=channel_base):
                _refresh_state["new"] = _base + added
                _refresh_state["channel_total"] = total

            try:
                r = sync_channel(db, channel, base, on_progress=_progress)
                _refresh_state["new"] = channel_base + r.get("new", 0)
                _refresh_state["written"] += r.get("written", 0)
                _refresh_state["retired"] += r.get("retired", 0)
            except YouTubeError as e:
                _refresh_state["errors"] += 1
                _refresh_state["error_detail"] = f"{channel.title}: {e}"
                logger.warning(f"[YouTube] Refresh failed for '{channel.title}': {e}")
            except Exception as e:
                _refresh_state["errors"] += 1
                _refresh_state["error_detail"] = f"{channel.title}: {e}"
                logger.error(f"[YouTube] Refresh crashed for '{channel.title}': {e}", exc_info=True)
            _refresh_state["channels_done"] += 1
    finally:
        db.close()
        _refresh_state["channel"] = None
        _refresh_state["finished_at"] = datetime.utcnow().isoformat()
        _refresh_state["running"] = False
        logger.info(f"[YouTube] Refresh finished: {_refresh_state['new']} new video(s)")


@router.post("/refresh", dependencies=[Depends(require_admin)])
def refresh_now(db: Session = Depends(get_db)):
    """Start indexing in the background. Poll /refresh/status for progress."""
    if not client.available():
        raise HTTPException(400, "yt-dlp is not installed in this image")
    from services.youtube.sync import base_url
    if not base_url(db):
        raise HTTPException(
            400,
            "Turn the YouTube source on first — a .strm has to carry an address "
            "the Jellyfin server itself can reach.",
        )

    with _refresh_lock:
        if _refresh_state["running"]:
            raise HTTPException(409, "An index is already running")
        _refresh_state.update({
            "running": True, "started_at": datetime.utcnow().isoformat(),
            "finished_at": None, "channel": None, "channels_done": 0,
            "channels_total": 0, "new": 0, "written": 0, "retired": 0,
            "errors": 0, "error_detail": None, "channel_total": 0,
        })

    threading.Thread(target=_run_refresh, daemon=True, name="youtube-refresh").start()
    return {"started": True}


@router.get("/refresh/status", dependencies=[Depends(require_admin)])
def refresh_status():
    return dict(_refresh_state)


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

    # Build the playlist, then put it on the home screen. Home rows are never
    # auto-created by write_home_config ("users add rows manually"), so creating
    # the playlist alone left the toggle doing nothing visible — which is not
    # what a control labelled "Home row" promises.
    row_added = False
    try:
        from services.smartlists import (
            _get_smartlists_with_playlist_ids, _notify_jellyfin_plugin,
            bump_playlist_version, refresh_smartlist_playlists, sync_smartlists,
            write_home_config,
        )
        sync_smartlists(db, user_id=user.id)
        refresh_smartlist_playlists(db, user_id=user.id, only_names=[channel.title])

        playlist_id = next(
            (p["playlist_id"] for p in _get_smartlists_with_playlist_ids(db, user_id=user.id)
             if p["name"] == channel.title),
            None,
        )
        row_added = _set_home_row(db, user, channel.title, playlist_id, body.enabled)

        write_home_config(db, user_id=user.id)
        bump_playlist_version()
        _notify_jellyfin_plugin(db)
    except Exception as e:
        logger.warning(f"[YouTube] Playlist/row update after toggle failed: {e}", exc_info=True)

    return {"success": True, "enabled": body.enabled, "playlist": channel.title,
            "row_added": row_added}


def _set_home_row(db: Session, user, name: str, playlist_id, enabled: bool) -> bool:
    """Add or remove this channel's row in the user's home config."""
    from routers.smartlists import _read_home_json, _write_home_json
    from services.smartlists import home_config_lock

    with home_config_lock:
        config = _read_home_json(user) or {
            "hero": {"enabled": False, "playlist_id": "", "display_name": ""}, "rows": [],
        }
        config.setdefault("rows", [])

        if enabled:
            if not playlist_id:
                logger.warning(f"[YouTube] No Jellyfin playlist for '{name}' yet — row not added")
                return False
            if any(r.get("playlist_id") == playlist_id for r in config["rows"]):
                return True
            for r in config["rows"]:
                r["order"] = r.get("order", 0) + 1
            config["rows"].insert(0, {
                "type": "playlist",
                "playlist_id": playlist_id,
                "display_name": name,
                "order": 1,
                "max_items": 30,
            })
        else:
            config["rows"] = [r for r in config["rows"] if r.get("display_name") != name]
            for i, r in enumerate(config["rows"], start=1):
                r["order"] = i

        _write_home_json(user, config)
    return enabled


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

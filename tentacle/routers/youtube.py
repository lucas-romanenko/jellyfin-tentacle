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
from services.youtube.sync import check_base_url
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
        # go and look — and refresh Jellyfin's guide once that is done, since
        # the channel just joined the lineup.
        _start_refresh(channel_ids=[channel.id], guide=True)
    else:
        from models.database import EPGProgram
        db.query(EPGProgram).filter(
            EPGProgram.channel_id == yt_livetv.epg_channel_id(channel)
        ).delete(synchronize_session=False)
        db.commit()
        # It left the lineup; Jellyfin should stop listing it.
        yt_livetv.refresh_jellyfin_guide(db)

    logger.info(f"[YouTube] Live TV {'enabled' if body.enabled else 'disabled'} for '{channel.title}'")
    return {"success": True, "live_enabled": body.enabled,
            "guide_number": yt_livetv.guide_number(channel), "programmes": guide}


# ── Admin ───────────────────────────────────────────────────────────────────

class ChannelCreate(BaseModel):
    """What the form sends: a URL, how many of the newest videos to keep, and
    two choices. Everything else is derived. Fields from the previous form
    (backfill, min_duration, include_videos, include_streams) are dropped on
    the floor rather than rejected, so an older client still works."""
    url: str
    keep_count: int = 10
    include_shorts: bool = False
    live: bool = False
    max_height: int = 1080
    rating: Optional[str] = None
    extra_tags: list = []


@router.get("/ping")
def ping():
    """Unauthenticated liveness marker, deliberately.

    This is what check_base_url probes, and it has to sit behind exactly the
    same (absence of) auth as the .strm endpoints — otherwise the check proves
    nothing about whether ffmpeg can fetch a video. Probing an admin route
    instead reported every correctly configured instance as needing a login,
    since ffmpeg has no session either. Carries no data beyond "Tentacle is
    here".
    """
    return {"tentacle": True, "youtube": True}


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
        "videos": db.query(YouTubeVideo).filter(YouTubeVideo.removed_at.is_(None)).count(),
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
    previous = (get_setting(db, "youtube_base_url", "") or "").strip().rstrip("/")
    set_setting(db, "youtube_enabled", "true" if body.enabled else "false")
    if base:
        set_setting(db, "youtube_base_url", base)

    # Every existing .strm carries the old address. Leaving them alone means
    # changing this setting appears to do nothing — the videos keep pointing at
    # somewhere that no longer serves them, and playback keeps failing for a
    # reason the setting page claims to have fixed.
    rewritten = 0
    if base and previous and base != previous:
        for video in db.query(YouTubeVideo).filter(
            YouTubeVideo.removed_at.is_(None),
            YouTubeVideo.strm_path.isnot(None),
        ).all():
            if library.rewrite_strm(video, base):
                rewritten += 1
        if rewritten:
            logger.info(f"[YouTube] Repointed {rewritten} .strm file(s) at {base}")
            try:
                from services.jellyfin import JellyfinService
                url = get_setting(db, "jellyfin_url", "")
                key = get_setting(db, "jellyfin_api_key", "")
                if url and key:
                    JellyfinService(url, key,
                                    get_setting(db, "jellyfin_user_id", "")).trigger_library_scan()
            except Exception as e:
                logger.warning(f"[YouTube] Could not trigger a Jellyfin scan: {e}")

    logger.info(f"[YouTube] Source {'enabled' if body.enabled else 'disabled'} (base {base or 'unset'})")
    reachable = check_base_url(base) if base else None
    return {"success": True, "enabled": body.enabled, "base_url": base,
            "rewritten": rewritten, "reachable": reachable}


@router.get("/diagnose", dependencies=[Depends(require_admin)])
def diagnose(request: Request, db: Session = Depends(get_db)):
    """Walk the whole chain and report the first thing that is actually wrong.

    Playback and home rows depend on a long chain — image, mount, settings,
    index, files, Jellyfin library, Jellyfin scan, playlist, home row — and a
    break anywhere shows up to the user as "nothing happened" or a bare
    playback error. This names the link.
    """
    import os
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
    if base:
        # The single most common way playback fails, and the least visible: the
        # address answers a browser fine and answers ffmpeg with a login page.
        probe = check_base_url(base)
        add(probe["ok"], "Tentacle address serves media directly", probe["detail"],
            None if probe["ok"] else
            "A .strm is fetched by Jellyfin's ffmpeg, which cannot log in. Use the "
            "address Jellyfin reaches on your own network, with no proxy login in "
            "front of it. Changing it here repoints every existing video.")

    channels = db.query(YouTubeChannel).all()
    add(channels, "Channels added", f"{len(channels)} channel(s)", "Paste a channel URL above.")

    videos = db.query(YouTubeVideo).filter(YouTubeVideo.removed_at.is_(None)).count()
    add(videos, "Videos indexed", f"{videos} video(s)",
        "Press 'Refresh now'. The first index takes a few minutes.")

    # Library items are what a home row is built from. A channel can be
    # indexed yet contribute nothing, most often because a setting excluded
    # every upload — which used to be entirely invisible.
    for ch in channels:
        lib = db.query(YouTubeVideo).filter(
            YouTubeVideo.channel_fk == ch.id,
            YouTubeVideo.removed_at.is_(None),
            indexer.is_library_status(YouTubeVideo.live_status),
        ).count()
        skips = ch.last_skips or {}
        listing = ch.last_listing or {}
        detail = f"{lib} video(s) available for its home row"
        if listing:
            detail += " — YouTube listed " + ", ".join(
                f"{n} on /{tab}" for tab, n in listing.items())
        if skips:
            detail += "; skipped " + "; ".join(f"{v}× {k}" for k, v in skips.items())
        fix = None
        if not lib:
            # "Nothing to show" has two opposite causes. Say which one this is
            # rather than listing every setting and leaving the user to guess.
            if not ch.include_videos:
                fix = ("'Videos' is turned off for this channel, so its uploads are never "
                       "looked at — only its live streams. Turn Videos on and press "
                       "'Refresh now'.")
            elif not listing.get("videos"):
                fix = (f"YouTube's Videos tab for '{ch.title}' returned nothing — the channel "
                       f"may only ever broadcast live. Its finished streams are kept instead "
                       f"when that is the case; if none have been found yet, press "
                       f"'Check for new videos'.")
            elif skips:
                fix = (f"Every upload was skipped. Reason(s) above. The minimum length is "
                       f"{ch.min_duration}s — if uploads were skipped for being too short, "
                       f"lower it and press 'Refresh now'.")
            else:
                fix = "Press 'Refresh now'. The first index takes a few minutes."
        add(lib, f"'{ch.title}' has videos for a row", detail, fix)

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

            # The two things creating a playlist needs, checked outright. A
            # bad key or an unknown account failed silently before: the
            # playlist simply never existed, with nothing on screen to say why.
            add(jf.test_connection(), "Jellyfin answers with this API key",
                jf_url, "Check Jellyfin's URL and API key in Settings.")
            try:
                me = get_user_from_request(request, db)
            except Exception:
                me = None
            if me is not None:
                jf_me = jf._get(f"/Users/{me.jellyfin_user_id}") if me.jellyfin_user_id else None
                add(bool(jf_me), "Your Jellyfin account is known to Tentacle",
                    (jf_me or {}).get("Name") or (me.jellyfin_user_id or "no account id"),
                    "Playlists are created in Jellyfin as your account, and Jellyfin does not "
                    "recognise the account Tentacle has for you. Log out of Tentacle and log "
                    "back in with your Jellyfin account.")

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
            "shows that as a playback error. Press 'Check for new videos' to re-check, "
            "and note that many channels never stream at all.")

    # ── Playlists and home rows, for whoever is asking ──
    # A channel's playlist exists for every user; putting it on the home screen
    # is the one per-user choice, so "it isn't on my home screen" is usually
    # just that it was never added there — not a fault.
    try:
        user = get_user_from_request(request, db)
        if user and channels:
            from routers.smartlists import _read_home_json
            from services.smartlists import _get_smartlists_with_playlist_ids
            have = {p["name"] for p in _get_smartlists_with_playlist_ids(db, user_id=user.id)}
            names = {c.title for c in channels}
            missing = names - have
            add(not missing, "Channel playlists exist in Jellyfin",
                f"{len(names & have)}/{len(names)} created"
                + (f" (missing: {', '.join(sorted(missing))})" if missing else ""),
                "They are created in Jellyfin as your account. If the two Jellyfin checks "
                "above pass, press 'Check for new videos' and wait for it to finish; if "
                "one fails, fix that first — a playlist Jellyfin refuses to create is "
                "logged as 'Could not create Jellyfin playlist' with the reason.")
            config = _read_home_json(user) or {}
            rows = {r.get("display_name") for r in (config.get("rows") or [])}
            on_home = names & rows
            add(True, "On your home screen",
                (", ".join(sorted(on_home)) if on_home else "none yet")
                + " — add or remove rows on the Home Screen tab, under YouTube Channels")
    except Exception:
        pass

    first_bad = next((c for c in checks if not c["ok"]), None)
    return {"checks": checks, "blocking": first_bad}


@router.get("/channels", dependencies=[Depends(require_admin)])
def list_channels(db: Session = Depends(get_db)):
    from models.database import EPGProgram
    from services.youtube import livetv as yt_livetv

    out = []
    for ch in db.query(YouTubeChannel).order_by(YouTubeChannel.title).all():
        # Guide entries this channel has in Tentacle's own EPG — what the
        # Live TV page shows next to it, and what Jellyfin's guide is built from.
        guide_programmes = db.query(EPGProgram).filter(
            EPGProgram.channel_id == yt_livetv.epg_channel_id(ch)).count() if ch.live_enabled else 0
        out.append({
            "guide_number": yt_livetv.guide_number(ch),
            "guide_programmes": guide_programmes,
            "id": ch.id, "title": ch.title, "slug": ch.slug, "kind": ch.kind,
            "input_url": ch.input_url, "avatar_url": ch.avatar_url,
            "enabled": ch.enabled,
            "video_count": db.query(YouTubeVideo).filter(
                YouTubeVideo.channel_fk == ch.id,
                YouTubeVideo.removed_at.is_(None)).count(),
            "last_checked": ch.last_checked, "last_error": ch.last_error,
            "blocked_until": ch.blocked_until,
            "include_streams": ch.include_streams, "include_shorts": ch.include_shorts,
            "keep_count": ch.keep_count, "max_height": ch.max_height,
            "rating": ch.rating, "extra_tags": ch.extra_tags or [],
            "live_enabled": ch.live_enabled,
            "last_skips": ch.last_skips or {},
            "last_listing": ch.last_listing or {},
            "library_count": db.query(YouTubeVideo).filter(
                YouTubeVideo.channel_fk == ch.id,
                YouTubeVideo.removed_at.is_(None),
                indexer.is_library_status(YouTubeVideo.live_status),
            ).count(),
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
    """Add a channel. This is the only step; the rest is automatic.

    Indexing starts immediately in the background, the videos are written and
    Jellyfin is asked to scan, the playlist is created for every user, and if
    Live TV was chosen the guide is refreshed — so by the time the progress
    toast finishes there is a row to add on the Home Screen tab.
    """
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

    # Clamped, not rejected: a stray 0 or 5000 means "the fewest" or "the most",
    # and bouncing the whole add over it would be pedantic.
    keep = max(1, min(int(body.keep_count), indexer.MAX_KEEP))
    channel = YouTubeChannel(
        input_url=body.url, kind=info["kind"], channel_id=info.get("channel_id"),
        handle=info.get("handle"), playlist_id=info.get("playlist_id"),
        title=info["title"], slug=slug,
        avatar_url=info.get("avatar_url"), banner_url=info.get("banner_url"),
        include_videos=True,
        # A channel that only ever broadcasts live has an empty uploads tab;
        # keeping its finished streams is the only way it has a library at all.
        # Decided here so nobody has to know such channels exist.
        include_streams=not info.get("has_uploads", True),
        include_shorts=body.include_shorts,
        min_duration=0,
        keep_count=keep, max_height=body.max_height,
        rating=body.rating, extra_tags=body.extra_tags,
        live_enabled=body.live,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    logger.info(f"[YouTube] Added channel '{channel.title}' ({channel.slug}), keeping newest {keep}"
                f"{', Live TV' if body.live else ''}")

    indexing = _start_refresh(channel_ids=[channel.id], guide=body.live)
    return {"id": channel.id, "title": channel.title, "slug": channel.slug,
            "keep_count": keep, "live_enabled": body.live, "indexing": indexing}


# Indexing runs in the background and is polled. It cannot be a plain request:
# YouTube rate-limits guest extraction, so details are fetched ~5s apart, and a
# first index of 30 videos takes several minutes — far longer than a browser or
# a reverse proxy will wait.
_refresh_state: dict = {
    "running": False, "started_at": None, "finished_at": None,
    "channel": None, "channels_done": 0, "channels_total": 0,
    "new": 0, "written": 0, "retired": 0, "errors": 0, "error_detail": None,
    "channel_total": 0,
    # The current channel's "keep newest N", so progress can say what the
    # entries being looked at are for.
    "keep": None, "kept": 0,
    # True when the run ended with a playlist still shorter than its library:
    # Jellyfin is still importing, and a background loop tops the playlist up
    # as the videos land. The finish toast says so instead of "done".
    "filling": False,
    # Work asked for while a run was in progress. A run that is already
    # under way has its channel list fixed, so a channel added during it is
    # queued and picked up the moment it finishes — never dropped.
    "pending": [], "pending_all": False,
    # Ask Jellyfin to refresh its Live TV guide once the run is done. Set when
    # a channel joined or left the lineup; the streams themselves are found by
    # the index, so the refresh has to come after it.
    "guide_after": False,
}
_refresh_lock = threading.Lock()


def _start_refresh(channel_ids=None, guide: bool = False) -> bool:
    """Index in the background. Returns True if started now, False if queued.

    Either way the work happens: if a run is in progress the request is
    recorded and served as soon as it ends. Callers never have to retry.
    """
    with _refresh_lock:
        if guide:
            _refresh_state["guide_after"] = True
        if _refresh_state["running"]:
            if channel_ids:
                _refresh_state["pending"] = list(
                    set(_refresh_state["pending"]) | set(channel_ids))
            else:
                _refresh_state["pending_all"] = True
            return False
        _refresh_state.update({
            "running": True, "started_at": datetime.utcnow().isoformat(),
            "finished_at": None, "channel": None, "channels_done": 0,
            "channels_total": 0, "new": 0, "written": 0, "retired": 0,
            "errors": 0, "error_detail": None, "channel_total": 0,
            "keep": None, "kept": 0, "filling": False,
            "pending": [], "pending_all": False,
        })
    threading.Thread(target=_run_refresh, args=(channel_ids,), daemon=True,
                     name="youtube-refresh").start()
    return True


def _note_channel_error(db: Session, channel: YouTubeChannel, exc: Exception) -> None:
    """Record a failed sync on the channel as well as on the run.

    The run's copy lives in a toast that is gone a few seconds later, and only
    listing failures inside index_channel ever reached the channel itself — so a
    crash anywhere after indexing (writing files, the guide, retention) left
    "indexed with 1 error" on screen and nothing anywhere to say what it was.
    """
    detail = f"{type(exc).__name__}: {exc}" if not str(exc) else str(exc)
    _refresh_state["errors"] += 1
    _refresh_state["error_detail"] = f"{channel.title}: {detail}"
    try:
        db.rollback()
        channel.last_error = detail[:400]
        channel.error_count = (channel.error_count or 0) + 1
        db.commit()
    except Exception:                       # never let bookkeeping mask the real error
        logger.debug("[YouTube] Could not record the channel error", exc_info=True)


def _run_refresh(channel_ids=None):
    """The background runner. Keeps going while work was queued behind it."""
    while True:
        try:
            _run_refresh_once(channel_ids)
        except Exception as e:                  # the loop's state must always be reset
            logger.error(f"[YouTube] Refresh run crashed: {e}", exc_info=True)
        with _refresh_lock:
            pending, pending_all = _refresh_state["pending"], _refresh_state["pending_all"]
            _refresh_state["pending"], _refresh_state["pending_all"] = [], False
            if not pending and not pending_all:
                _refresh_state["channel"] = None
                _refresh_state["finished_at"] = datetime.utcnow().isoformat()
                _refresh_state["running"] = False
                logger.info(f"[YouTube] Refresh finished: {_refresh_state['new']} new video(s)")
                return
            _refresh_state.update({"channels_done": 0, "channels_total": 0, "channel_total": 0})
        channel_ids = None if pending_all else pending


def _run_refresh_once(channel_ids=None):
    """Index the given channels (or every enabled one) with its own session."""
    from models.database import SessionLocal
    from services.youtube import livetv as yt_livetv
    from services.youtube.sync import base_url, publish_to_jellyfin, sync_channel

    db = SessionLocal()
    try:
        base = base_url(db)
        query = db.query(YouTubeChannel).filter(YouTubeChannel.enabled == True)  # noqa: E712
        if channel_ids:
            query = query.filter(YouTubeChannel.id.in_(channel_ids))
        channels = query.all()
        _refresh_state["channels_total"] = len(channels)
        changed = []
        for channel in channels:
            _refresh_state["channel"] = channel.title
            _refresh_state["keep"] = channel.keep_count or 10
            _refresh_state["kept"] = 0
            channel_base = _refresh_state["new"]

            def _progress(kept, keep):
                # In the user's terms: how many of the newest N are in hand.
                _refresh_state["kept"] = kept
                _refresh_state["keep"] = keep

            r_written = 0
            try:
                r = sync_channel(db, channel, base, on_progress=_progress)
                _refresh_state["new"] = channel_base + r.get("new", 0)
                _refresh_state["written"] += r.get("written", 0)
                _refresh_state["retired"] += r.get("retired", 0)
                r_written = r.get("written", 0) + r.get("retired", 0)
            except YouTubeError as e:
                _note_channel_error(db, channel, e)
                logger.warning(f"[YouTube] Refresh failed for '{channel.title}': {e}")
            except Exception as e:
                _note_channel_error(db, channel, e)
                logger.error(f"[YouTube] Refresh crashed for '{channel.title}': {e}", exc_info=True)
            if r_written:
                changed.append(channel)
            _refresh_state["channels_done"] += 1

        # Publish when something changed — or whenever specific channels were
        # asked for, which is what a newly added one is: its playlist has to
        # exist even if every video it listed turned out to be excluded.
        if changed or channel_ids:
            try:
                _refresh_state["channel"] = "publishing to Jellyfin"
                _refresh_state["keep"] = None
                # The stage is shown in the toast, so a wait says what it is
                # waiting for rather than sitting on "publishing".
                result = publish_to_jellyfin(
                    db, changed or list(channels),
                    on_stage=lambda msg: _refresh_state.__setitem__("channel", msg))
                _refresh_state["filling"] = bool((result or {}).get("short"))
            except Exception as e:
                logger.warning(f"[YouTube] Publish to Jellyfin failed: {e}")

        with _refresh_lock:
            guide = _refresh_state["guide_after"]
            _refresh_state["guide_after"] = False
        if guide:
            _refresh_state["channel"] = "refreshing the Jellyfin guide"
            _refresh_state["keep"] = None
            yt_livetv.refresh_jellyfin_guide(db)

        # Whatever else happened, make sure every user has every channel's
        # playlist and that none is behind its library. This is what "Check
        # for new videos" is for when something looks missing.
        try:
            from services.youtube.sync import reconcile_playlists
            _refresh_state["channel"] = "checking playlists"
            _refresh_state["keep"] = None
            reconcile_playlists(db)
        except Exception as e:
            logger.warning(f"[YouTube] Playlist check failed: {e}")
    finally:
        db.close()


@router.post("/reprobe", dependencies=[Depends(require_admin)])
def reprobe(db: Session = Depends(get_db)):
    """Make Jellyfin re-read every YouTube video's streams.

    Jellyfin probes a .strm once and reuses the result, so an item scanned while
    Tentacle was serving something different keeps playing by the old
    description — which surfaces as a playback error with nothing wrong on
    either side now. This bumps each file's modified time and asks for a scan,
    which is what makes Jellyfin probe it again.
    """
    from services.jellyfin import JellyfinService

    touched = 0
    for video in db.query(YouTubeVideo).filter(
        YouTubeVideo.removed_at.is_(None),
        YouTubeVideo.strm_path.isnot(None),
    ).all():
        if library.touch_strm(video):
            touched += 1

    scanned = False
    url = get_setting(db, "jellyfin_url", "")
    key = get_setting(db, "jellyfin_api_key", "")
    if url and key:
        try:
            JellyfinService(url, key, get_setting(db, "jellyfin_user_id", "")).trigger_library_scan()
            scanned = True
        except Exception as e:
            logger.warning(f"[YouTube] Could not trigger a Jellyfin scan: {e}")

    logger.info(f"[YouTube] Marked {touched} video(s) for re-probe; scan={scanned}")
    return {"touched": touched, "scan_triggered": scanned}


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

    started = _start_refresh()
    return {"started": started, "queued": not started}


@router.get("/refresh/status", dependencies=[Depends(require_admin)])
def refresh_status():
    state = {k: v for k, v in _refresh_state.items() if k not in ("pending", "pending_all")}
    state["queued"] = len(_refresh_state["pending"]) + (1 if _refresh_state["pending_all"] else 0)
    return state


@router.delete("/channels/{channel_id}", dependencies=[Depends(require_admin)])
def delete_channel(channel_id: int, db: Session = Depends(get_db)):
    """Remove a channel and everything that came from it.

    The files go, so Jellyfin's scan drops the items; the playlist stops being
    desired, so the next sync removes it and its home rows. Leaving any of that
    behind — the old opt-in "also delete files?" — meant a channel that was
    gone from this page but still all over Jellyfin.
    """
    channel = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "Channel not found")
    removed = 0
    for video in db.query(YouTubeVideo).filter(YouTubeVideo.channel_fk == channel.id).all():
        removed += library.remove_video(video)
    # ...and the channel's own folder, which the per-video removals leave empty.
    library.remove_channel_folder(channel.title)
    title, was_live = channel.title, bool(channel.live_enabled)
    if was_live:
        from models.database import EPGProgram
        from services.youtube import livetv as yt_livetv
        db.query(EPGProgram).filter(
            EPGProgram.channel_id == yt_livetv.epg_channel_id(channel)
        ).delete(synchronize_session=False)
    db.delete(channel)   # cascades to videos
    db.commit()
    logger.info(f"[YouTube] Removed channel '{title}' ({removed} file(s) deleted)")

    threading.Thread(target=_cleanup_after_remove, args=(title, was_live), daemon=True,
                     name="youtube-remove-cleanup").start()
    return {"success": True, "files_deleted": removed}


def _cleanup_after_remove(title: str, was_live: bool) -> None:
    """Take a removed channel out of Jellyfin entirely: rows, playlist, items, guide.

    Every step stands on its own and is logged, because a removal that stops
    halfway is the worst outcome — gone from this page, still all over
    Jellyfin. Two of these steps are deliberate rather than left to the
    regular sync: a home row whose playlist vanishes is normally kept for
    days (right for a blip, wrong for a removal), and the playlist is deleted
    by the id Tentacle recorded for it, never by name, so a user's own
    playlist that happens to share the channel's name is never touched.
    """
    from models.database import SessionLocal, TentacleUser
    from routers.smartlists import _read_home_json, _write_home_json
    from services.jellyfin import JellyfinService
    from services.smartlists import (
        _get_smartlists_with_playlist_ids, _notify_jellyfin_plugin,
        bump_playlist_version, home_config_lock, sync_smartlists, write_home_config,
    )
    from services.youtube import livetv as yt_livetv
    from services.youtube.sync import youtube_library_id

    db = SessionLocal()
    try:
        url = get_setting(db, "jellyfin_url", "")
        key = get_setting(db, "jellyfin_api_key", "")
        jf = JellyfinService(url, key, get_setting(db, "jellyfin_user_id", "")) if url and key else None

        for user in db.query(TentacleUser).all():
            # The playlist id Tentacle recorded, captured before the sync below
            # removes the folder that holds it.
            recorded = next((p["playlist_id"] for p in _get_smartlists_with_playlist_ids(db, user_id=user.id)
                             if p["name"] == title), None)

            # 1. The home row and, if it pointed here, the hero — explicitly.
            try:
                with home_config_lock:
                    config = _read_home_json(user) or {}
                    rows = config.get("rows") or []
                    kept = [r for r in rows if r.get("display_name") != title]
                    hero = config.get("hero") or {}
                    hero_hit = hero.get("display_name") == title
                    if len(kept) != len(rows) or hero_hit:
                        for i, r in enumerate(kept, start=1):
                            r["order"] = i
                        config["rows"] = kept
                        if hero_hit:
                            config["hero"] = {"enabled": False, "playlist_id": "", "display_name": ""}
                        _write_home_json(user, config)
                        logger.info(f"[YouTube] Removed '{title}' from user {user.id}'s home screen")
            except Exception as e:
                logger.warning(f"[YouTube] Could not remove '{title}' row for user {user.id}: {e}")

            # 2. The playlist: no longer desired, so the sync's orphan cleanup
            #    deletes it and its folder.
            try:
                sync_smartlists(db, user_id=user.id)
            except Exception as e:
                logger.warning(f"[YouTube] Playlist cleanup for user {user.id} failed: {e}")

            # 3. If Jellyfin still has the playlist Tentacle recorded, delete it.
            if jf and recorded:
                try:
                    if any(p.get("Id") == recorded for p in jf.get_playlists(user.jellyfin_user_id) or []):
                        jf.delete_item(recorded)
                        logger.info(f"[YouTube] Deleted leftover Jellyfin playlist {recorded} ('{title}')")
                except Exception as e:
                    logger.warning(f"[YouTube] Could not check Jellyfin playlists for user {user.id}: {e}")

            try:
                write_home_config(db, user_id=user.id)
            except Exception as e:
                logger.warning(f"[YouTube] Home config rewrite for user {user.id} failed: {e}")

        try:
            bump_playlist_version()
            _notify_jellyfin_plugin(db)
        except Exception as e:
            logger.warning(f"[YouTube] Could not notify clients: {e}")

        # 4. The items: their files are gone, so a scan drops them from the library.
        if jf:
            try:
                jf.trigger_library_scan(youtube_library_id(jf))
            except Exception as e:
                logger.warning(f"[YouTube] Could not trigger a Jellyfin scan: {e}")

        # 5. The Live TV channel: it left the lineup, so the guide has to be re-read.
        if was_live:
            yt_livetv.refresh_jellyfin_guide(db)
        logger.info(f"[YouTube] Cleanup after removing '{title}' finished")
    finally:
        db.close()

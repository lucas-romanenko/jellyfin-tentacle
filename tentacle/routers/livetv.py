"""
Tentacle - Live TV Router

Handles live channel sync, channel management, HDHomeRun emulation,
and XMLTV serving for Jellyfin integration.

HDHomeRun endpoints (Jellyfin connects to these):
  GET /hdhr/discover.json       → Device discovery
  GET /hdhr/lineup.json         → Channel lineup
  GET /hdhr/lineup_status.json  → Scan status
  GET /hdhr/xmltv.xml           → EPG guide data

Channel management:
  GET    /api/live/channels          → List channels
  PUT    /api/live/channels/{id}     → Update channel
  POST   /api/live/channels/bulk     → Bulk enable/disable
  GET    /api/live/groups            → List groups
  PUT    /api/live/groups/{id}       → Enable/disable group
  POST   /api/live/sync/{provider_id}  → Sync channels from provider
  POST   /api/live/sync-epg/{provider_id} → Sync EPG data
  GET    /api/live/sync-status       → Sync progress
  GET    /api/live/status            → Live TV status overview
"""

import asyncio
import logging
import re
import threading
from datetime import datetime
from typing import Optional, List

from services.epg_categories import infer_category
from services.youtube import livetv as youtube_livetv

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import (
    EPGProgram,
    LiveChannel,
    LiveChannelGroup,
    Provider,
    SessionLocal,
    get_db,
    get_setting,
    log_activity,
)
from routers.auth import require_admin
from services.ssrf import is_safe_url, lan_origin_guard
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

router = APIRouter()

# Reusable dependency list for Live TV *management* routes (provider config,
# sync, channel/group mutations, guide refresh). The HDHomeRun tuner-emulation,
# stream-proxy, playlist and XMLTV routes are intentionally left public because
# Jellyfin's tuner integration cannot present credentials.
_admin = [Depends(require_admin)]

# Cap simultaneous upstream pulls so a burst of clients can't exhaust our own
# socket/CPU budget (or an account that really is connection-limited). The cap
# is the admin's call -- setting `livetv_max_concurrent_streams`, 0 = no limit
# -- because the provider's advertised max_connections cannot be trusted either
# way: panels report 1 for accounts that are not capped at all (#87).
_DEFAULT_MAX_CONCURRENT_STREAMS = 6
_MAX_CONCURRENT_STREAMS = _DEFAULT_MAX_CONCURRENT_STREAMS  # kept for importers
# A slot is usually about to free up (a recording ending as the next begins, a
# viewer changing channel), so wait this long for one before refusing. Short:
# a tuner client is blocked on the answer.
_SLOT_WAIT_SECONDS = 5.0

# Max redirect hops we will follow on a stream/chunk fetch, matching httpx's own
# default ceiling.
_MAX_STREAM_REDIRECTS = 10


async def _send_checked(client, url: str, headers: dict, guard=None):
    """GET `url` through `client`, following redirects *and re-validating each hop*.

    `client` must be built with follow_redirects=False. Letting httpx follow
    redirects itself defeats the `is_safe_url` pre-flight: the URL that was
    checked is not the URL that finally gets fetched, so an upstream (an IPTV
    provider — untrusted third-party content) can answer with
    `302 -> http://10.0.0.5:8096/...` and the stream proxy, which is a public
    unauthenticated route, would fetch it and stream the body back to the caller.

    Returns the open streaming response (caller closes it) for the first hop
    that is not a redirect.

    `guard` validates every hop; callers pass one scoped to the channel's
    provider (#76) so a deliberately-configured LAN re-streamer works while
    everything else is still held to is_safe_url().
    """
    guard = guard or is_safe_url
    current = url
    for _ in range(_MAX_STREAM_REDIRECTS):
        req = client.build_request("GET", current, headers=headers)
        resp = await client.send(req, stream=True)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        location = resp.headers.get("location")
        await resp.aclose()
        if not location:
            raise HTTPException(502, "Redirect without Location header")
        current = urljoin(current, location)
        if not guard(current):
            logger.warning(f"[LiveTV] Blocked redirect to non-public host: {current}")
            raise HTTPException(502, "Stream redirect points to a non-public host")
    raise HTTPException(502, "Too many redirects")

# Opening a stream: statuses worth waiting out, and for how long. Kept well under
# a tuner client's patience; the running worker has its own, longer budget.
_OPEN_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 509}
_OPEN_RETRY_BUDGET = 20.0   # seconds

class _StreamSlots:
    """Counts upstream pulls against a limit that can change while running.

    An asyncio.Semaphore bakes its size in at creation, which is why the old
    ceiling could not be a setting. All access is from the event loop."""

    def __init__(self):
        self.active = 0
        self.refused = 0
        self.last_refused: "dict | None" = None
        self._freed: "asyncio.Event | None" = None

    async def acquire(self, limit: int, wait: float) -> bool:
        if limit <= 0 or self.active < limit:
            self.active += 1
            return True
        if self._freed is None:
            self._freed = asyncio.Event()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._freed.clear()
            try:
                await asyncio.wait_for(self._freed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
            if self.active < limit:
                self.active += 1
                return True

    def release(self):
        self.active = max(0, self.active - 1)
        if self._freed is not None:
            self._freed.set()


_stream_slots = _StreamSlots()


def _max_concurrent_streams(db) -> int:
    """The configured ceiling; 0 means unlimited. A value that does not parse
    falls back to the default rather than silently removing the cap."""
    raw = get_setting(db, "livetv_max_concurrent_streams", "")
    try:
        return max(0, int(raw)) if raw.strip() else _DEFAULT_MAX_CONCURRENT_STREAMS
    except (ValueError, AttributeError):
        return _DEFAULT_MAX_CONCURRENT_STREAMS


# One upstream pull per channel, fanned out to every client watching it.
#
# Jellyfin opens a separate tuner stream per recording and per viewer, so
# recording a channel while watching that same channel used to pull
# byte-identical data from the provider twice: double the bandwidth, double the
# playlist and segment requests, and double the footprint in whatever
# connection accounting the provider keeps -- the accounting that answers 509,
# which is what truncates recordings. Identical requests are now served from a
# single upstream pull.
#
# Only the SAME channel shares; different channels each get their own upstream
# connection. How many of those an account will carry is not something the
# advertised max_connections settles (#87) -- the ceiling is
# livetv_max_concurrent_streams, above.
_shared_streams: "dict[int, _SharedUpstream] = {}"
_shared_streams = {}
_shared_lock: "asyncio.Lock | None" = None

# Segments of slack before a client is considered too slow. HLS segments are
# usually 6s, so this is minutes of buffer -- a consumer further behind than
# this is broken, and must not be allowed to stall the upstream or its peers.
_SUBSCRIBER_QUEUE_MAX = 32

# How soon a pump that outlived its cancel() is cancelled again (see _cancel_pump).
_PUMP_RECANCEL_SECONDS = 1.0


def _get_shared_lock() -> "asyncio.Lock":
    global _shared_lock
    if _shared_lock is None:
        _shared_lock = asyncio.Lock()
    return _shared_lock


class _SharedUpstream:
    """A single upstream stream for one channel, with N subscribers."""

    def __init__(self, channel_id: int, release_sem):
        self.channel_id = channel_id
        self.subscribers: set = set()
        self.task = None
        self._release_sem = release_sem
        self._closed = False

    def subscribe(self) -> "asyncio.Queue":
        q = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self.subscribers.add(q)
        return q

    def _publish(self, item):
        for q in list(self.subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                # Drop this client's oldest segment rather than stalling the
                # upstream (and therefore every other client on the channel).
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                    logger.warning(
                        f"[LiveTV] Client on channel {self.channel_id} is behind — "
                        f"dropped a segment for it")
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def _pump(self, body_iterator):
        try:
            async for piece in body_iterator:
                if self._closed:
                    # Retired, but the cancel() that should have stopped us was
                    # lost (see _cancel_pump): stop pulling here.
                    break
                self._publish(piece)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[LiveTV] Shared upstream for channel {self.channel_id} failed: {e}")
        finally:
            self._publish(None)   # EOF sentinel for every subscriber
            await self._retire()
            # Leaving the loop by `break` does not close an async generator,
            # and its `finally` is what closes the provider connection.
            aclose = getattr(body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()

    def _cancel_pump(self):
        """Cancel the pump, and keep at it until it is really gone.

        One task.cancel() is not enough. httpx connects through anyio, whose
        connect_tcp() cancels its own cancel scope as soon as a connection
        attempt wins, and a scope that is being cancelled takes any
        CancelledError raised in its host task for its own and swallows it. A
        cancel() of ours that lands in that window is lost, and the pump goes
        on pulling from the provider with no subscriber, no registration and
        no slot -- invisible to /api/live/capacity. The window is the connect
        for the first chunk: exactly where the pump is when a client opens a
        channel and leaves at once."""
        task = self.task
        if task is None or task.done():
            return
        task.cancel()
        asyncio.get_running_loop().call_later(_PUMP_RECANCEL_SECONDS, self._cancel_pump)

    async def _retire(self):
        if self._closed:
            return
        self._closed = True
        # Deregister and free the slot BEFORE any await: this runs from a
        # finally during cancellation, where an await can raise CancelledError
        # and would otherwise strand the registration and leak the slot.
        if _shared_streams.get(self.channel_id) is self:
            del _shared_streams[self.channel_id]
        self._release_sem()
        logger.info(f"[LiveTV] Shared upstream for channel {self.channel_id} ended")

    async def unsubscribe(self, q):
        self.subscribers.discard(q)
        if not self.subscribers and not self._closed:
            # Nobody left watching: stop paying the provider for it.
            logger.info(f"[LiveTV] Last client left channel {self.channel_id} — "
                        f"closing the upstream")
            await self._retire()
            self._cancel_pump()


class _SubscriberResponse(StreamingResponse):
    """A client's view of a shared upstream, which is ALWAYS unsubscribed.

    The client is subscribed when this object is built, but the body generator
    only unsubscribes from its own `finally` -- and an async generator that is
    never started never runs it. A client that went away between the headers
    and the first chunk therefore stayed subscribed for ever: the upstream kept
    pulling from the provider with nobody watching and its concurrency slot
    never came back. Unsubscribing when the response finishes, however it
    finishes, closes that; `unsubscribe` is idempotent."""

    def __init__(self, shared: "_SharedUpstream", q):
        super().__init__(
            _subscriber_body(shared, q),
            media_type="video/mp2t",
            headers={
                "Connection": "close",
                "Cache-Control": "no-cache, no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )
        self._shared = shared
        self._q = q

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._shared.unsubscribe(self._q)


async def _subscriber_body(shared: "_SharedUpstream", q):
    """Per-client view of a shared upstream."""
    try:
        while True:
            piece = await q.get()
            if piece is None:
                return
            yield piece
    finally:
        await shared.unsubscribe(q)


# ─── Background sync tracking ────────────────────────────────────────────────

_sync_status: dict[int, dict] = {}  # provider_id → {phase, progress, message, ...}
_sync_status_lock = threading.Lock()


def _set_sync_status(provider_id: int, status: dict):
    """Thread-safe update of sync status for a provider."""
    with _sync_status_lock:
        _sync_status[provider_id] = status


def _get_sync_status(provider_id: int, default: dict | None = None) -> dict:
    """Thread-safe read of sync status for a provider."""
    with _sync_status_lock:
        return _sync_status.get(provider_id, default or {"phase": "idle", "progress": 0, "message": "No sync running"}).copy()


# ─── Pydantic models ────────────────────────────────────────────────────────


class ChannelUpdate(BaseModel):
    enabled: Optional[bool] = None
    channel_number: Optional[int] = None
    epg_channel_id: Optional[str] = None
    sort_order: Optional[int] = None


class BulkChannelUpdate(BaseModel):
    channel_ids: list[int]
    enabled: bool


class BulkChannelFilter(BaseModel):
    provider_id: int
    enabled: bool
    group: Optional[str] = None
    search: Optional[str] = None
    has_epg: Optional[bool] = None


class GroupUpdate(BaseModel):
    enabled: bool

class BulkGroupUpdate(BaseModel):
    group_ids: List[int]
    enabled: bool


class LiveProviderConfig(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None  # xtream, m3u_url, m3u_file
    server_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    m3u_url: Optional[str] = None
    epg_url: Optional[str] = None
    user_agent: Optional[str] = None
    live_tv_enabled: Optional[bool] = None


# ─── Provider config ────────────────────────────────────────────────────────


@router.get("/api/live/provider", dependencies=_admin)
def get_live_provider(db: Session = Depends(get_db)):
    """Get the live TV provider config."""
    provider = db.query(Provider).filter(Provider.live_tv_enabled == True).first()
    if not provider:
        # Fall back to any provider with detected live capability
        provider = db.query(Provider).filter(Provider.has_live == True).first()
    if not provider:
        return {"provider": None}

    return {
        "provider": {
            "id": provider.id,
            "name": provider.name,
            "provider_type": provider.provider_type or "xtream",
            "server_url": provider.server_url,
            "username": provider.username,
            "password": "••••••••" if provider.password else "",
            "m3u_url": provider.m3u_url or "",
            "epg_url": provider.epg_url or "",
            "user_agent": provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
            "live_tv_enabled": provider.live_tv_enabled,
            "last_live_sync": provider.last_live_sync.isoformat() if provider.last_live_sync else None,
        }
    }


@router.post("/api/live/provider", dependencies=_admin)
def save_live_provider(body: LiveProviderConfig, db: Session = Depends(get_db)):
    """Create or update the live TV provider."""
    # Clean up any duplicate providers with masked passwords (from earlier bug)
    dupes = db.query(Provider).filter(
        Provider.live_tv_enabled == True,
        Provider.password == "••••••••",
    ).all()
    for d in dupes:
        db.delete(d)
    if dupes:
        db.flush()

    # Find existing live TV provider only — never reuse VOD providers
    provider = db.query(Provider).filter(Provider.live_tv_enabled == True).first()

    if not provider:
        # Create a dedicated live TV provider
        pwd = body.password if body.password and body.password != "••••••••" else ""
        provider = Provider(
            name=body.name or "Live TV",
            provider_type=body.provider_type or "xtream",
            server_url=body.server_url or "",
            username=body.username or "",
            password=pwd,
            m3u_url=body.m3u_url,
            epg_url=body.epg_url,
            user_agent=body.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
            live_tv_enabled=True,
            active=False,
        )
        db.add(provider)
    else:
        if body.name is not None:
            provider.name = body.name
        if body.provider_type is not None:
            provider.provider_type = body.provider_type
        if body.server_url is not None:
            provider.server_url = body.server_url
        if body.username is not None:
            provider.username = body.username
        if body.password is not None and body.password != "••••••••":
            provider.password = body.password
        if body.m3u_url is not None:
            provider.m3u_url = body.m3u_url
        if body.epg_url is not None:
            provider.epg_url = body.epg_url
        if body.user_agent is not None:
            provider.user_agent = body.user_agent
        if body.live_tv_enabled is not None:
            provider.live_tv_enabled = body.live_tv_enabled

    db.commit()
    db.refresh(provider)

    log_activity(db, "livetv_config", f"Live TV provider updated: {provider.name}")
    return {"success": True, "provider_id": provider.id}


@router.post("/api/live/provider/test", dependencies=_admin)
def test_live_provider(db: Session = Depends(get_db)):
    """Test connection to the live TV provider."""
    provider = db.query(Provider).filter(Provider.live_tv_enabled == True).first()
    if not provider:
        provider = db.query(Provider).first()
    if not provider:
        return {"success": False, "message": "No live TV provider configured"}

    provider_type = provider.provider_type or "xtream"

    if provider_type == "xtream":
        try:
            from services.xtream_client import XtreamClient
            client = XtreamClient(
                server=provider.server_url,
                username=provider.username,
                password=provider.password,
                user_agent=provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
            )
            info = client.authenticate()
            client.close()
            return {
                "success": True,
                "message": "Connected",
                "info": {
                    "status": info.get("user_info", {}).get("status", "unknown"),
                    "exp_date": info.get("user_info", {}).get("exp_date"),
                    "max_connections": info.get("user_info", {}).get("max_connections"),
                    "active_connections": info.get("user_info", {}).get("active_cons"),
                },
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    elif provider_type == "m3u_url":
        import requests
        try:
            resp = requests.head(
                provider.m3u_url or provider.server_url,
                headers={"User-Agent": provider.user_agent or "TiviMate/4.7.0"},
                timeout=10,
            )
            return {"success": resp.status_code == 200, "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    return {"success": False, "message": f"Unknown provider type: {provider_type}"}


# ─── Channel sync ───────────────────────────────────────────────────────────


@router.post("/api/live/sync/{provider_id}", dependencies=_admin)
def sync_live_groups(provider_id: int, db: Session = Depends(get_db)):
    """Phase 1: Fetch categories/groups only (fast). No channels downloaded."""
    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")

    status = _get_sync_status(provider_id)
    if status.get("phase") == "running":
        return {"success": False, "message": "Sync already in progress", **status}

    provider_type = provider.provider_type or "xtream"
    provider_data = _snapshot_provider(provider, provider_type)

    _set_sync_status(provider_id, {"phase": "starting", "progress": 0, "message": "Fetching groups..."})

    thread = threading.Thread(target=_run_group_sync_background, args=(provider_data,), daemon=True)
    thread.start()

    return {"success": True, "message": "Group sync started", "status_url": "/api/live/sync-status"}


@router.post("/api/live/sync-channels/{provider_id}", dependencies=_admin)
def sync_live_channels(provider_id: int, db: Session = Depends(get_db)):
    """Phase 2: Fetch channels only for enabled groups. Call after enabling groups."""
    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")

    status = _get_sync_status(provider_id)
    if status.get("phase") == "running":
        return {"success": False, "message": "Sync already in progress", **status}

    # Check that there are enabled groups
    enabled_groups = db.query(LiveChannelGroup).filter(
        LiveChannelGroup.provider_id == provider_id,
        LiveChannelGroup.enabled == True,
    ).all()
    if not enabled_groups:
        return {"success": False, "message": "No groups enabled. Enable groups first, then sync channels."}

    provider_type = provider.provider_type or "xtream"
    provider_data = _snapshot_provider(provider, provider_type)

    _set_sync_status(provider_id, {"phase": "starting", "progress": 0, "message": "Starting channel sync..."})

    thread = threading.Thread(target=_run_channel_sync_background, args=(provider_data,), daemon=True)
    thread.start()

    return {"success": True, "message": "Channel sync started", "status_url": "/api/live/sync-status"}


@router.get("/api/live/capacity", dependencies=_admin)
def live_capacity(db: Session = Depends(get_db)):
    """How many upstream pulls are running against the ceiling, and whether any
    stream has been refused since start -- the only trace a lost recording
    otherwise leaves is a log line."""
    return {
        "limit": _max_concurrent_streams(db),
        "active": _stream_slots.active,
        "refused_since_start": _stream_slots.refused,
        "last_refused": _stream_slots.last_refused,
    }


@router.get("/api/live/sync-status", dependencies=_admin)
def sync_status_endpoint(provider_id: Optional[int] = None):
    """Get sync progress for a provider or all providers."""
    if provider_id is not None:
        return _get_sync_status(provider_id)
    with _sync_status_lock:
        return {k: v.copy() for k, v in _sync_status.items()}


def _snapshot_provider(provider, provider_type: str) -> dict:
    """Snapshot provider ORM object into a plain dict for background threads."""
    return {
        "id": provider.id,
        "name": provider.name,
        "provider_type": provider_type,
        "server_url": provider.server_url,
        "username": provider.username,
        "password": provider.password,
        "user_agent": provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
        "m3u_url": provider.m3u_url,
        "epg_url": provider.epg_url,
    }


def _run_group_sync_background(provider_data: dict):
    """Phase 1: Fetch groups/categories only. Fast."""
    provider_id = provider_data["id"]
    db = SessionLocal()
    try:
        provider_type = provider_data["provider_type"]
        if provider_type == "xtream":
            result = _sync_groups_from_xtream(provider_data, db)
        elif provider_type in ("m3u_url", "m3u_file"):
            # For M3U, groups come from parsing the file — do a full sync since it's the only way
            if provider_type == "m3u_url":
                result = _sync_from_m3u_url(provider_data, db)
            else:
                result = _sync_from_m3u_file(provider_data, db)
        else:
            _set_sync_status(provider_id, {"phase": "error", "progress": 0, "message": f"Unknown provider type: {provider_type}"})
            return

        _set_sync_status(provider_id, {
            "phase": "complete",
            "progress": 100,
            "message": result.get("message", "Groups synced"),
            **result,
        })
    except Exception as e:
        logger.error(f"[LiveTV] Group sync failed for provider {provider_id}: {e}", exc_info=True)
        _set_sync_status(provider_id, {"phase": "error", "progress": 0, "message": str(e)})
    finally:
        db.close()


def _run_channel_sync_background(provider_data: dict):
    """Phase 2: Fetch channels for enabled groups only."""
    provider_id = provider_data["id"]
    db = SessionLocal()
    try:
        provider_type = provider_data["provider_type"]
        if provider_type == "xtream":
            result = _sync_channels_from_xtream(provider_data, db)
        elif provider_type in ("m3u_url", "m3u_file"):
            # M3U already synced channels in phase 1 — just report what's there
            total = db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).count()
            enabled = db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id, LiveChannel.enabled == True).count()
            result = {"new": 0, "updated": 0, "total": total, "enabled": enabled, "message": "M3U channels already synced"}
        else:
            _set_sync_status(provider_id, {"phase": "error", "progress": 0, "message": f"Unknown provider type: {provider_type}"})
            return

        # Auto-chain EPG sync after channel sync — so EPG badges are accurate immediately
        all_channels = db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).all()
        if all_channels:
            ch_msg = f"Channels: {result.get('new', 0)} new, {result.get('updated', 0)} updated."
            enabled_count = sum(1 for ch in all_channels if ch.enabled)
            epg_data = {
                **provider_data,
                "channels": [
                    {"stream_id": ch.stream_id, "epg_channel_id": ch.epg_channel_id, "name": ch.name}
                    for ch in all_channels
                ],
                "enabled_count": enabled_count,
            }
            db.close()
            db = None
            _set_sync_status(provider_id, {
                "phase": "running", "progress": 95,
                "message": f"{ch_msg} Syncing EPG guide data...",
            })
            _run_epg_sync_background(epg_data)
        else:
            _set_sync_status(provider_id, {
                "phase": "complete",
                "progress": 100,
                "message": f"Done: {result.get('new', 0)} new, {result.get('updated', 0)} updated, {result.get('total', 0)} total",
                **result,
            })

    except Exception as e:
        logger.error(f"[LiveTV] Channel sync failed for provider {provider_id}: {e}", exc_info=True)
        _set_sync_status(provider_id, {"phase": "error", "progress": 0, "message": str(e)})
    finally:
        if db is not None:
            db.close()


def _sync_groups_from_xtream(provider_data: dict, db: Session) -> dict:
    """Phase 1: Fetch categories/groups from Xtream only. No channels."""
    from services.xtream_client import XtreamClient

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]

    client = XtreamClient(
        server=provider_data["server_url"],
        username=provider_data["username"],
        password=provider_data["password"],
        user_agent=provider_data["user_agent"],
    )

    try:
        _set_sync_status(provider_id, {"phase": "running", "progress": 20, "message": "Fetching categories..."})
        categories = client.get_live_categories()
        logger.info(f"[LiveTV] {provider_name}: {len(categories)} categories fetched")

        # Count channels per category (single API call)
        _set_sync_status(provider_id, {"phase": "running", "progress": 50, "message": "Counting channels..."})
        channel_counts = {}
        try:
            all_streams = client.get_live_streams()
            if isinstance(all_streams, list):
                for s in all_streams:
                    cid = str(s.get("category_id", ""))
                    channel_counts[cid] = channel_counts.get(cid, 0) + 1
        except Exception as e:
            logger.warning(f"[LiveTV] Failed to count channels for {provider_name}: {e}")

        _set_sync_status(provider_id, {"phase": "running", "progress": 70, "message": f"Saving {len(categories)} groups..."})
        _sync_groups(provider_id, categories, db, channel_counts)
        db.commit()

        total_channels = sum(channel_counts.values())
        log_activity(db, "livetv_sync", f"Live TV group sync for {provider_name}: {len(categories)} groups, {total_channels} channels")
        return {"groups": len(categories), "message": f"{len(categories)} groups synced. Enable the groups you want, then sync channels."}

    except Exception as e:
        logger.error(f"[LiveTV] Group sync failed for {provider_name}: {e}", exc_info=True)
        raise
    finally:
        client.close()


def _sync_channels_from_xtream(provider_data: dict, db: Session) -> dict:
    """Phase 2: Fetch channels only for enabled groups from Xtream."""
    from services.xtream_client import XtreamClient

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]

    client = XtreamClient(
        server=provider_data["server_url"],
        username=provider_data["username"],
        password=provider_data["password"],
        user_agent=provider_data["user_agent"],
    )

    try:
        # Get enabled groups and their category IDs
        enabled_groups = db.query(LiveChannelGroup).filter(
            LiveChannelGroup.provider_id == provider_id,
            LiveChannelGroup.enabled == True,
        ).all()

        category_ids = [g.category_id for g in enabled_groups if g.category_id]
        cat_map = {g.category_id: g.name for g in enabled_groups if g.category_id}

        logger.info(f"[LiveTV] {provider_name}: fetching channels for {len(category_ids)} enabled groups")
        _set_sync_status(provider_id, {
            "phase": "running", "progress": 5,
            "message": f"Fetching channels for {len(category_ids)} groups...",
        })

        # Fetch per-category (only enabled ones)
        all_streams = []
        for i, cat_id in enumerate(category_ids):
            try:
                streams = client.get_live_streams(category_id=cat_id)
                all_streams.extend(streams)
            except Exception as e:
                logger.warning(f"[LiveTV] {provider_name}: failed to fetch category {cat_id}: {e}")

            pct = 5 + int(((i + 1) / len(category_ids)) * 85)
            _set_sync_status(provider_id, {
                "phase": "running", "progress": pct,
                "message": f"Fetching: {i + 1}/{len(category_ids)} groups, {len(all_streams)} channels so far",
            })
            if (i + 1) % 20 == 0 or (i + 1) == len(category_ids):
                logger.info(f"[LiveTV] {provider_name}: {i + 1}/{len(category_ids)} groups, {len(all_streams)} channels")

        logger.info(f"[LiveTV] {provider_name}: {len(all_streams)} channels fetched for enabled groups")
        _set_sync_status(provider_id, {"phase": "running", "progress": 95, "message": f"Saving {len(all_streams)} channels..."})

        # Upsert channels
        stats = _upsert_channels(provider_id, all_streams, cat_map, client, db)

        # Update provider timestamp
        provider = db.query(Provider).filter(Provider.id == provider_id).first()
        if provider:
            provider.last_live_sync = datetime.utcnow()
        db.commit()

        log_activity(db, "livetv_sync", f"Live TV channel sync for {provider_name}: {stats['new']} new, {stats['updated']} updated, {stats['total']} total")
        return stats

    except Exception as e:
        logger.error(f"[LiveTV] Channel sync failed for {provider_name}: {e}", exc_info=True)
        raise
    finally:
        client.close()


def _sync_from_m3u_url(provider_data: dict, db: Session) -> dict:
    """Sync live channels from an M3U URL."""
    from services.m3u_parser import parse_m3u_from_url

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]
    url = provider_data.get("m3u_url") or provider_data.get("server_url")
    if not url:
        raise ValueError("No M3U URL configured")

    _set_sync_status(provider_id, {"phase": "running", "progress": 10, "message": "Downloading M3U..."})

    channels = parse_m3u_from_url(url, user_agent=provider_data["user_agent"])
    logger.info(f"[LiveTV] {provider_name}: parsed {len(channels)} channels from M3U URL")

    _set_sync_status(provider_id, {"phase": "running", "progress": 80, "message": f"Saving {len(channels)} channels..."})
    stats = _upsert_channels_from_m3u(provider_id, channels, db)

    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if provider:
        provider.last_live_sync = datetime.utcnow()
    db.commit()

    log_activity(db, "livetv_sync", f"Live TV M3U sync for {provider_name}: {stats['new']} new, {stats['total']} total")
    return stats


def _sync_from_m3u_file(provider_data: dict, db: Session) -> dict:
    """Sync live channels from a local M3U file."""
    from services.m3u_parser import parse_m3u_from_file

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]
    path = provider_data.get("m3u_url")  # For file type, m3u_url stores the file path
    if not path:
        raise ValueError("No M3U file path configured")

    _set_sync_status(provider_id, {"phase": "running", "progress": 10, "message": "Reading M3U file..."})

    channels = parse_m3u_from_file(path)
    logger.info(f"[LiveTV] {provider_name}: parsed {len(channels)} channels from M3U file")

    _set_sync_status(provider_id, {"phase": "running", "progress": 80, "message": f"Saving {len(channels)} channels..."})
    stats = _upsert_channels_from_m3u(provider_id, channels, db)

    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if provider:
        provider.last_live_sync = datetime.utcnow()
    db.commit()

    log_activity(db, "livetv_sync", f"Live TV file sync for {provider_name}: {stats['new']} new, {stats['total']} total")
    return stats


def _sync_groups(provider_id: int, categories: list[dict], db: Session, channel_counts: dict = None):
    """Upsert LiveChannelGroup records from Xtream categories."""
    if channel_counts is None:
        channel_counts = {}
    existing = {
        g.name: g
        for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id).all()
    }

    for cat in categories:
        name = cat.get("category_name", "")
        cat_id = str(cat.get("category_id", ""))
        count = channel_counts.get(cat_id, 0)
        if name in existing:
            existing[name].category_id = cat_id
            existing[name].channel_count = count
        else:
            db.add(LiveChannelGroup(
                provider_id=provider_id,
                name=name,
                category_id=cat_id,
                enabled=False,
                channel_count=count,
            ))
    db.flush()


def _upsert_channels(
    provider_id: int,
    streams: list[dict],
    cat_map: dict[str, str],
    client,
    db: Session,
) -> dict:
    """Upsert LiveChannel records from Xtream streams."""
    existing = {
        ch.stream_id: ch
        for ch in db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).all()
    }

    new_count = 0
    updated_count = 0
    seen_ids = set()

    for stream in streams:
        sid = str(stream.get("stream_id", ""))
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)

        name = stream.get("name", "")
        group = cat_map.get(str(stream.get("category_id", "")), "")
        url = client.live_stream_url(int(sid))

        if sid in existing:
            ch = existing[sid]
            ch.name = name
            ch.stream_url = url
            ch.logo_url = stream.get("stream_icon") or ch.logo_url
            ch.group_title = group or ch.group_title
            ch.epg_channel_id = stream.get("epg_channel_id") or ch.epg_channel_id
            ch.updated_at = datetime.utcnow()
            updated_count += 1
        else:
            db.add(LiveChannel(
                provider_id=provider_id,
                name=name,
                stream_id=sid,
                stream_url=url,
                logo_url=stream.get("stream_icon") or None,
                group_title=group,
                epg_channel_id=stream.get("epg_channel_id") or None,
                enabled=False,
            ))
            new_count += 1

    db.flush()
    return {"new": new_count, "updated": updated_count, "total": len(seen_ids)}


def _m3u_stable_id(name: str, stream_url: str) -> str:
    """Generate a stable stream_id for M3U channels from name + URL.

    Unlike array indices, this doesn't shift when the M3U file order changes.
    """
    import hashlib
    return hashlib.sha256(f"{name}|{stream_url}".encode()).hexdigest()[:16]


def _upsert_channels_from_m3u(
    provider_id: int,
    parsed_channels: list[dict],
    db: Session,
) -> dict:
    """Upsert LiveChannel records from parsed M3U data.

    Uses a stable hash of name+URL as stream_id so channel IDs don't shift
    when the M3U file order changes. Preserves user customizations (enabled,
    sort_order, channel_number) across syncs. Removes channels no longer in
    the M3U file.
    """
    # Build lookup of existing channels by stream_id
    existing = {
        ch.stream_id: ch
        for ch in db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).all()
    }

    groups = set()
    seen_ids = set()
    new_count = 0
    updated_count = 0

    for ch in parsed_channels:
        name = ch["name"]
        stream_url = ch["stream_url"]
        sid = _m3u_stable_id(name, stream_url)
        seen_ids.add(sid)
        group = ch.get("group_title") or ""
        if group:
            groups.add(group)

        if sid in existing:
            # Update metadata, preserve user settings (enabled, sort_order, channel_number)
            row = existing[sid]
            row.name = name
            row.stream_url = stream_url
            row.logo_url = ch.get("logo_url") or row.logo_url
            row.group_title = group or row.group_title
            row.epg_channel_id = ch.get("epg_channel_id") or row.epg_channel_id
            if ch.get("tvg_chno") and not row.channel_number:
                row.channel_number = int(ch["tvg_chno"])
            row.updated_at = datetime.utcnow()
            updated_count += 1
        else:
            db.add(LiveChannel(
                provider_id=provider_id,
                name=name,
                stream_id=sid,
                stream_url=stream_url,
                logo_url=ch.get("logo_url"),
                group_title=group,
                epg_channel_id=ch.get("epg_channel_id"),
                channel_number=int(ch["tvg_chno"]) if ch.get("tvg_chno") else None,
                enabled=False,
            ))
            new_count += 1

    # Remove channels no longer in M3U
    removed_ids = set(existing.keys()) - seen_ids
    if removed_ids:
        db.query(LiveChannel).filter(
            LiveChannel.provider_id == provider_id,
            LiveChannel.stream_id.in_(removed_ids),
        ).delete(synchronize_session=False)

    # Sync groups
    existing_groups = {
        g.name: g
        for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id).all()
    }
    for name in groups:
        if name not in existing_groups:
            db.add(LiveChannelGroup(
                provider_id=provider_id,
                name=name,
                enabled=False,
            ))

    db.flush()
    _update_group_counts(provider_id, db)

    return {"new": new_count, "updated": updated_count, "removed": len(removed_ids), "total": len(parsed_channels)}


def _update_group_counts(provider_id: int, db: Session):
    """Update channel_count on each group."""
    groups = db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id).all()
    for g in groups:
        count = db.query(LiveChannel).filter(
            LiveChannel.provider_id == provider_id,
            LiveChannel.group_title == g.name,
        ).count()
        g.channel_count = count


# ─── EPG sync ───────────────────────────────────────────────────────────────


@router.post("/api/live/sync-epg/{provider_id}", dependencies=_admin)
def sync_epg(provider_id: int, db: Session = Depends(get_db)):
    """Sync EPG data for enabled channels only (runs in background)."""
    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")

    # Check if already running
    existing = _get_sync_status(provider_id)
    if existing.get("phase") == "epg" and existing.get("status") == "running":
        return {"success": True, "message": "EPG sync already in progress"}

    # Get ALL provider channels (EPG data stored for all, not just enabled)
    all_channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.provider_id == provider_id)
        .all()
    )
    enabled_count = sum(1 for ch in all_channels if ch.enabled)

    if not all_channels:
        return {"success": False, "message": "No channels synced yet — run channel sync first"}

    provider_type = provider.provider_type or "xtream"

    provider_data = {
        "id": provider.id,
        "provider_type": provider_type,
        "server_url": provider.server_url,
        "username": provider.username,
        "password": provider.password,
        "user_agent": provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
        "epg_url": provider.epg_url,
        "channels": [
            {"stream_id": ch.stream_id, "epg_channel_id": ch.epg_channel_id, "name": ch.name}
            for ch in all_channels
        ],
        "enabled_count": enabled_count,
    }

    _set_sync_status(provider_id, {
        "phase": "epg",
        "status": "running",
        "progress": 0,
        "message": f"Fetching guide data for {len(all_channels)} channels ({enabled_count} enabled)...",
    })

    thread = threading.Thread(target=_run_epg_sync_background, args=(provider_data,), daemon=True)
    thread.start()
    return {"success": True, "message": f"EPG sync started for {len(all_channels)} channels ({enabled_count} enabled)"}


def _run_epg_sync_background(provider_data: dict):
    """Background EPG sync — stream-parses full XMLTV, keeps programs for ALL provider channels."""
    pid = provider_data["id"]
    channels = provider_data["channels"]
    total = len(channels)
    enabled_count = provider_data.get("enabled_count", total)

    try:
        db = SessionLocal()
        try:
            # Build set of EPG IDs for ALL provider channels (not just enabled)
            # This ensures newly-enabled channels already have EPG data available
            epg_ids = {ch["epg_channel_id"] for ch in channels if ch.get("epg_channel_id")}
            if not epg_ids:
                _set_sync_status(pid, {
                    "phase": "epg", "status": "error", "progress": 0,
                    "message": "No enabled channels have EPG IDs. Cannot fetch guide data.",
                })
                return False

            # Determine XMLTV URL
            epg_url = provider_data.get("epg_url")
            if not epg_url and provider_data["provider_type"] == "xtream":
                from services.xtream_client import XtreamClient
                client = XtreamClient(
                    server=provider_data["server_url"],
                    username=provider_data["username"],
                    password=provider_data["password"],
                    user_agent=provider_data["user_agent"],
                )
                epg_url = client.get_xmltv_url()
                client.close()

            if not epg_url:
                _set_sync_status(pid, {
                    "phase": "epg", "status": "error", "progress": 0,
                    "message": "No EPG URL available.",
                })
                return False

            # Progress callback
            def on_progress(pct, msg):
                _set_sync_status(pid, {"phase": "epg", "status": "running", "progress": pct, "message": msg})

            # Download + parse FIRST — the existing guide data stays untouched
            # until we're sure we have a good replacement. Providers sometimes
            # serve a throttled/empty-but-parseable XMLTV (especially at 3am);
            # the old delete-first flow committed that as a full guide wipe.
            # Retries bust the 8h disk cache so a bad cached file can't stick.
            import os as _os
            import time as _time
            from services.xmltv import stream_parse_xmltv, _get_cache_path

            def _drop_xmltv_cache():
                try:
                    _os.remove(_get_cache_path(epg_url))
                except OSError:
                    pass

            programs = None
            last_err = None
            for attempt in range(1, 4):
                try:
                    suffix = f" (attempt {attempt}/3)" if attempt > 1 else ""
                    _set_sync_status(pid, {"phase": "epg", "status": "running", "progress": 5, "message": f"Downloading XMLTV guide{suffix}..."})
                    programs = stream_parse_xmltv(
                        url=epg_url,
                        channel_ids=epg_ids,
                        user_agent=provider_data["user_agent"],
                        on_progress=on_progress,
                        force_download=attempt > 1,
                    )
                    if programs:
                        break
                    last_err = "provider returned no programs for our channels"
                    logger.warning(f"[LiveTV] EPG attempt {attempt}/3: {last_err}")
                except Exception as e:
                    last_err = str(e)
                    logger.warning(f"[LiveTV] EPG download attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    _drop_xmltv_cache()  # don't let a bad cached file poison the retry
                    _time.sleep(30 * attempt)

            provider_channel_epg_ids = {
                ch.epg_channel_id
                for ch in db.query(LiveChannel).filter(
                    LiveChannel.provider_id == pid,
                    LiveChannel.epg_channel_id.isnot(None),
                ).all()
            }
            old_count = (
                db.query(EPGProgram).filter(EPGProgram.channel_id.in_(provider_channel_epg_ids)).count()
                if provider_channel_epg_ids else 0
            )

            # Sanity guards — never replace a healthy guide with a suspiciously
            # empty one. Keep the old data and surface the failure instead.
            if not programs:
                msg = f"EPG sync failed: {last_err or 'no programs'} — kept existing guide data ({old_count} programs)"
                logger.error(f"[LiveTV] {msg}")
                log_activity(db, "epg_sync_failed", msg)
                db.commit()
                _drop_xmltv_cache()
                _set_sync_status(pid, {"phase": "epg", "status": "error", "progress": 0, "message": msg})
                return False
            if old_count >= 1000 and len(programs) < old_count * 0.1:
                msg = (f"EPG sync aborted: provider returned only {len(programs)} programs "
                       f"(previously {old_count}) — looks like a bad/partial guide, kept existing data")
                logger.error(f"[LiveTV] {msg}")
                log_activity(db, "epg_sync_failed", msg)
                db.commit()
                _drop_xmltv_cache()
                _set_sync_status(pid, {"phase": "epg", "status": "error", "progress": 0, "message": msg})
                return False

            # Replace guide data — quick transaction, no network inside it
            if provider_channel_epg_ids:
                db.query(EPGProgram).filter(
                    EPGProgram.channel_id.in_(provider_channel_epg_ids)
                ).delete(synchronize_session=False)
                db.flush()

            # Insert into DB
            _set_sync_status(pid, {"phase": "epg", "status": "running", "progress": 90, "message": f"Saving {len(programs)} programs..."})
            inserted = 0
            seen_epg = set()
            batch = []
            for prog in programs:
                key = (prog["channel_id"], prog["start"])
                if key in seen_epg:
                    continue
                seen_epg.add(key)
                batch.append(EPGProgram(
                    channel_id=prog["channel_id"],
                    title=prog["title"],
                    description=prog.get("description"),
                    start=prog["start"],
                    stop=prog["stop"],
                    category=prog.get("category"),
                ))
                inserted += 1
                if len(batch) >= 5000:
                    db.add_all(batch)
                    db.flush()
                    batch = []
            if batch:
                db.add_all(batch)
                db.flush()

            db.commit()
            log_activity(db, "epg_sync", f"EPG sync: {inserted} programs for {total} channels ({enabled_count} enabled)")

            _set_sync_status(pid, {
                "phase": "epg",
                "status": "complete",
                "progress": 100,
                "message": f"{inserted} programs synced for {total} channels ({enabled_count} enabled)",
                "programs": inserted,
                "channels": total,
            })
            return True
        finally:
            db.close()

    except Exception as e:
        logger.error(f"[LiveTV] EPG sync failed: {e}")
        _set_sync_status(pid, {"phase": "epg", "status": "error", "progress": 0, "message": str(e)})
        return False


# ─── Channel management ────────────────────────────────────────────────────


@router.get("/api/live/channels", dependencies=_admin)
def list_channels(
    provider_id: Optional[int] = None,
    group: Optional[str] = None,
    enabled: Optional[bool] = None,
    search: Optional[str] = None,
    has_epg: Optional[bool] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List live channels with filtering and pagination."""
    q = db.query(LiveChannel)
    if provider_id:
        q = q.filter(LiveChannel.provider_id == provider_id)
    if group:
        q = q.filter(LiveChannel.group_title == group)
    if enabled is not None:
        q = q.filter(LiveChannel.enabled == enabled)
    if search:
        q = q.filter(LiveChannel.name.ilike(f"%{search}%"))
    # Build set of epg_channel_ids that actually have programs in the DB
    # EPG sync stores data for ALL provider channels, so this is accurate after first sync
    epg_id_q = db.query(LiveChannel.epg_channel_id).filter(
        LiveChannel.epg_channel_id.isnot(None), LiveChannel.epg_channel_id != ""
    )
    if provider_id:
        epg_id_q = epg_id_q.filter(LiveChannel.provider_id == provider_id)
    all_epg_ids = {row[0] for row in epg_id_q.distinct().all()}
    epg_ids_with_programs = set()
    if all_epg_ids:
        epg_ids_with_programs = {
            row[0] for row in db.query(EPGProgram.channel_id)
            .filter(EPGProgram.channel_id.in_(all_epg_ids))
            .distinct().all()
        }

    # Filter by whether channel actually has EPG program data in the DB
    if has_epg is not None:
        if has_epg:
            if epg_ids_with_programs:
                q = q.filter(LiveChannel.epg_channel_id.in_(epg_ids_with_programs))
            else:
                q = q.filter(LiveChannel.id < 0)  # no results — no EPG data exists yet
        else:
            if epg_ids_with_programs:
                q = q.filter((LiveChannel.epg_channel_id.is_(None)) | (LiveChannel.epg_channel_id == "") | ~LiveChannel.epg_channel_id.in_(epg_ids_with_programs))
            # else: all channels have no EPG, no filter needed

    total = q.count()
    channels = q.order_by(LiveChannel.sort_order, LiveChannel.name).offset((page - 1) * per_page).limit(per_page).all()

    return {
        "channels": [
            {
                "id": ch.id,
                "name": ch.name,
                "channel_number": ch.channel_number,
                "stream_id": ch.stream_id,
                "stream_url": ch.stream_url,
                "logo_url": ch.logo_url,
                "group_title": ch.group_title,
                "epg_channel_id": ch.epg_channel_id,
                "has_epg_data": ch.epg_channel_id in epg_ids_with_programs if ch.epg_channel_id else False,
                "enabled": ch.enabled,
                "sort_order": ch.sort_order,
            }
            for ch in channels
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.put("/api/live/channels/{channel_id}", dependencies=_admin)
def update_channel(channel_id: int, update: ChannelUpdate, db: Session = Depends(get_db)):
    """Update a single channel."""
    ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(404, "Channel not found")

    if update.enabled is not None:
        ch.enabled = update.enabled
    if update.channel_number is not None:
        ch.channel_number = update.channel_number
    if update.epg_channel_id is not None:
        ch.epg_channel_id = update.epg_channel_id
    if update.sort_order is not None:
        ch.sort_order = update.sort_order

    ch.updated_at = datetime.utcnow()
    db.commit()
    return {"success": True}


# SQLite caps host parameters per statement (SQLITE_MAX_VARIABLE_NUMBER, 999 on
# older builds). A big IPTV provider can have thousands of channels/groups, so an
# unchunked IN (...) list raises "too many SQL variables" (an unhandled 500) when
# saving. Chunk the lists so bulk saves scale to any provider size.
_SQL_IN_CHUNK = 500


def _chunked(seq, size=_SQL_IN_CHUNK):
    """Yield successive `size`-length slices of `seq`."""
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


@router.post("/api/live/channels/bulk", dependencies=_admin)
def bulk_update_channels(update: BulkChannelUpdate, db: Session = Depends(get_db)):
    """Bulk enable/disable channels."""
    count = 0
    for chunk in _chunked(update.channel_ids):
        count += db.query(LiveChannel).filter(
            LiveChannel.id.in_(chunk)
        ).update({LiveChannel.enabled: update.enabled}, synchronize_session=False)
    db.commit()
    return {"success": True, "updated": count}


@router.post("/api/live/channels/bulk-filter", dependencies=_admin)
def bulk_update_channels_by_filter(update: BulkChannelFilter, db: Session = Depends(get_db)):
    """Bulk enable/disable channels matching filters (group, search)."""
    q = db.query(LiveChannel).filter(LiveChannel.provider_id == update.provider_id)
    if update.group:
        q = q.filter(LiveChannel.group_title == update.group)
    if update.search:
        q = q.filter(LiveChannel.name.ilike(f"%{update.search}%"))
    if update.has_epg is not None:
        if update.has_epg:
            q = q.filter(LiveChannel.epg_channel_id.isnot(None), LiveChannel.epg_channel_id != "")
        else:
            q = q.filter((LiveChannel.epg_channel_id.is_(None)) | (LiveChannel.epg_channel_id == ""))
    count = q.update({LiveChannel.enabled: update.enabled}, synchronize_session=False)
    db.commit()
    return {"success": True, "updated": count}


# ─── Group management ──────────────────────────────────────────────────────


@router.get("/api/live/groups", dependencies=_admin)
def list_groups(provider_id: Optional[int] = None, db: Session = Depends(get_db)):
    """List channel groups."""
    q = db.query(LiveChannelGroup)
    if provider_id:
        q = q.filter(LiveChannelGroup.provider_id == provider_id)

    groups = q.order_by(LiveChannelGroup.name).all()
    return {
        "groups": [
            {
                "id": g.id,
                "provider_id": g.provider_id,
                "name": g.name,
                "category_id": g.category_id,
                "enabled": g.enabled,
                "channel_count": g.channel_count,
            }
            for g in groups
        ]
    }


@router.put("/api/live/groups/bulk", dependencies=_admin)
def bulk_update_groups(update: BulkGroupUpdate, db: Session = Depends(get_db)):
    """Enable/disable multiple groups and their channels in one request."""
    groups = []
    for chunk in _chunked(update.group_ids):
        groups.extend(db.query(LiveChannelGroup).filter(LiveChannelGroup.id.in_(chunk)).all())
    if not groups:
        return {"success": True, "updated": 0}

    # Only cascade channel enable/disable for groups that are actually changing state.
    # Without this, re-saving already-enabled groups resets individually-disabled channels.
    changing_names = [g.name for g in groups if g.enabled != update.enabled]

    for chunk in _chunked(update.group_ids):
        db.query(LiveChannelGroup).filter(LiveChannelGroup.id.in_(chunk)).update(
            {LiveChannelGroup.enabled: update.enabled}, synchronize_session=False
        )

    if changing_names:
        provider_id = groups[0].provider_id
        for chunk in _chunked(changing_names):
            db.query(LiveChannel).filter(
                LiveChannel.provider_id == provider_id,
                LiveChannel.group_title.in_(chunk),
            ).update({LiveChannel.enabled: update.enabled}, synchronize_session=False)

    db.commit()
    return {"success": True, "updated": len(groups)}


@router.put("/api/live/groups/{group_id}", dependencies=_admin)
def update_group(group_id: int, update: GroupUpdate, db: Session = Depends(get_db)):
    """Enable/disable a group and all its channels."""
    group = db.query(LiveChannelGroup).filter(LiveChannelGroup.id == group_id).first()
    if not group:
        raise HTTPException(404, "Group not found")

    group.enabled = update.enabled

    db.query(LiveChannel).filter(
        LiveChannel.provider_id == group.provider_id,
        LiveChannel.group_title == group.name,
    ).update({LiveChannel.enabled: update.enabled}, synchronize_session=False)

    db.commit()
    return {"success": True}


# ─── Status ─────────────────────────────────────────────────────────────────


@router.get("/api/live/status", dependencies=_admin)
def live_status(db: Session = Depends(get_db)):
    """Overview of Live TV status."""
    total_channels = db.query(LiveChannel).count()
    enabled_channels = db.query(LiveChannel).filter(LiveChannel.enabled == True).count()
    total_groups = db.query(LiveChannelGroup).count()
    enabled_groups = db.query(LiveChannelGroup).filter(LiveChannelGroup.enabled == True).count()
    epg_programs = db.query(EPGProgram).count()

    # Get providers with live TV
    providers = db.query(Provider).filter(Provider.live_tv_enabled == True).all()
    provider_info = []
    for p in providers:
        ch_count = db.query(LiveChannel).filter(LiveChannel.provider_id == p.id, LiveChannel.enabled == True).count()
        provider_info.append({
            "id": p.id,
            "name": p.name,
            "type": p.provider_type or "xtream",
            "enabled_channels": ch_count,
            "last_sync": p.last_live_sync.isoformat() if p.last_live_sync else None,
        })

    return {
        "total_channels": total_channels,
        "enabled_channels": enabled_channels,
        "total_groups": total_groups,
        "enabled_groups": enabled_groups,
        "epg_programs": epg_programs,
        "providers": provider_info,
    }


# ─── Jellyfin Guide Refresh ────────────────────────────────────────────────


@router.post("/api/live/refresh-guide", dependencies=_admin)
def refresh_jellyfin_guide(db: Session = Depends(get_db)):
    """
    One-click Jellyfin refresh: checks for missing EPG data, re-syncs listing
    provider (forces channel-to-XMLTV remap), then triggers guide refresh.
    """
    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    if not jf_url or not jf_key:
        raise HTTPException(400, "Jellyfin URL or API key not configured")

    import requests as req
    headers = {"X-Emby-Token": jf_key, "Content-Type": "application/json"}

    # Pre-check: are there enabled channels missing EPG data?
    # If so, trigger a quick EPG sync first so new channels get guide data
    epg_resynced = False
    providers_with_channels = (
        db.query(Provider.id)
        .join(LiveChannel, LiveChannel.provider_id == Provider.id)
        .filter(LiveChannel.enabled == True)
        .distinct()
        .all()
    )
    for (pid,) in providers_with_channels:
        enabled_epg_ids = {
            ch.epg_channel_id
            for ch in db.query(LiveChannel).filter(
                LiveChannel.provider_id == pid,
                LiveChannel.enabled == True,
                LiveChannel.epg_channel_id.isnot(None),
            ).all()
        }
        if not enabled_epg_ids:
            continue
        # Check if any enabled channel has zero EPG programs
        epg_with_data = {
            row.channel_id
            for row in db.query(EPGProgram.channel_id)
            .filter(EPGProgram.channel_id.in_(enabled_epg_ids))
            .distinct()
            .all()
        }
        missing = enabled_epg_ids - epg_with_data
        if missing:
            logger.info(f"[LiveTV] {len(missing)} enabled channels missing EPG data for provider {pid} — triggering EPG sync")
            # Trigger EPG sync synchronously (inline, not background thread)
            # so Jellyfin gets fresh data when we refresh
            provider = db.query(Provider).filter(Provider.id == pid).first()
            if provider:
                all_channels = db.query(LiveChannel).filter(LiveChannel.provider_id == pid).all()
                enabled_count = sum(1 for ch in all_channels if ch.enabled)
                provider_data = {
                    "id": provider.id,
                    "provider_type": provider.provider_type or "xtream",
                    "server_url": provider.server_url,
                    "username": provider.username,
                    "password": provider.password,
                    "user_agent": provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
                    "epg_url": provider.epg_url,
                    "channels": [
                        {"stream_id": ch.stream_id, "epg_channel_id": ch.epg_channel_id, "name": ch.name}
                        for ch in all_channels
                    ],
                    "enabled_count": enabled_count,
                }
                # Close current DB session before background sync uses its own
                db.close()
                _run_epg_sync_background(provider_data)
                # Re-open session for the rest of this endpoint
                db = SessionLocal()
                epg_resynced = True

    try:
        from services.jellyfin_guide import GuideRefreshError, refresh_jellyfin_guide
        refresh_jellyfin_guide(jf_url, jf_key)
        msg = "Jellyfin guide refresh triggered"
        if epg_resynced:
            msg = "EPG data synced for new channels + Jellyfin guide refresh triggered"
        logger.info(f"[LiveTV] {msg}")
        return {"success": True, "message": msg}
    except GuideRefreshError as e:
        raise HTTPException(404, str(e))
    except req.RequestException as e:
        logger.error(f"[LiveTV] Failed to trigger Jellyfin guide refresh: {e}")
        raise HTTPException(502, f"Failed to connect to Jellyfin: {e}")


# ─── HDHomeRun Emulation ───────────────────────────────────────────────────


@router.get("/discover.json")
@router.get("/hdhr/discover.json")
def hdhr_discover(request: Request, db: Session = Depends(get_db)):
    """HDHomeRun device discovery endpoint."""
    tuner_count = int(get_setting(db, "hdhr_tuner_count", "3"))
    device_id = get_setting(db, "hdhr_device_id", "TENTACLE1")

    # Use explicit setting if configured, otherwise derive from request
    # (request.base_url returns localhost inside Docker — useless for Jellyfin in another container)
    base_url = get_setting(db, "hdhr_base_url", "").strip()
    if not base_url:
        # Try X-Forwarded-Host first (reverse proxy), then Host header, then request.base_url
        forwarded_host = request.headers.get("x-forwarded-host")
        scheme = request.headers.get("x-forwarded-proto", "http")
        if forwarded_host:
            base_url = f"{scheme}://{forwarded_host}"
        else:
            host = request.headers.get("host")
            if host:
                base_url = f"http://{host}"
            else:
                base_url = str(request.base_url).rstrip("/")
    base_url = base_url.rstrip("/")

    return {
        "FriendlyName": "Tentacle",
        "Manufacturer": "Silicondust",
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomerun5_atsc",
        "FirmwareVersion": "20231001",
        "DeviceID": device_id,
        "DeviceAuth": "tentacle",
        "TunerCount": tuner_count,
        "BaseURL": base_url,
        "LineupURL": f"{base_url}/lineup.json",
    }


@router.get("/lineup.json")
@router.get("/hdhr/lineup.json")
def hdhr_lineup(request: Request, db: Session = Depends(get_db)):
    """HDHomeRun channel lineup — only enabled channels.
    URLs point to our stream proxy which handles UA spoofing, 302 redirect
    following, and HLS playlist rewriting."""
    channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.enabled == True)
        .order_by(LiveChannel.sort_order, LiveChannel.channel_number, LiveChannel.name)
        .all()
    )

    # Build base URL same way as discover.json
    base_url = get_setting(db, "hdhr_base_url", "").strip()
    if not base_url:
        forwarded_host = request.headers.get("x-forwarded-host")
        scheme = request.headers.get("x-forwarded-proto", "http")
        if forwarded_host:
            base_url = f"{scheme}://{forwarded_host}"
        else:
            host = request.headers.get("host")
            if host:
                base_url = f"http://{host}"
            else:
                base_url = str(request.base_url).rstrip("/")
    base_url = base_url.rstrip("/")

    lineup = []
    for ch in channels:
        # Use stream_id as stable channel number — never shifts when channels are added/removed
        number = ch.stream_id or str(ch.id)
        entry = {
            "GuideNumber": str(number),
            "GuideName": ch.name,
            "URL": f"{base_url}/api/live/stream/{ch.id}",
        }
        if ch.logo_url:
            entry["LogoUrl"] = ch.logo_url
        lineup.append(entry)

    # YouTube channels the user exposed to Live TV. They aren't LiveChannel rows
    # (that table requires a provider_id and a YouTube channel is not an IPTV
    # provider), so they are unioned in here and play through their own endpoint.
    for yt in youtube_livetv.live_channels(db):
        entry = {
            "GuideNumber": yt["guide_number"],
            "GuideName": yt["name"],
            # Raw MPEG-TS, not HLS: Jellyfin's tuner reads the response body as
            # video, so a playlist gets copied as if it were video data.
            "URL": f"{base_url}/api/youtube/live/{yt['youtube_channel_id']}/stream.ts",
        }
        if yt["logo_url"]:
            entry["LogoUrl"] = yt["logo_url"]
        lineup.append(entry)

    return lineup


@router.get("/device.xml")
@router.get("/hdhr/device.xml")
def hdhr_device_xml(request: Request, db: Session = Depends(get_db)):
    """UPnP device descriptor — mimics a real HDHomeRun (Silicondust HDTC-2US).
    Jellyfin uses this for device identification and capability detection."""
    device_id = get_setting(db, "hdhr_device_id", "TENTACLE1")
    base_url = get_setting(db, "hdhr_base_url", "").strip()
    if not base_url:
        forwarded_host = request.headers.get("x-forwarded-host")
        scheme = request.headers.get("x-forwarded-proto", "http")
        if forwarded_host:
            base_url = f"{scheme}://{forwarded_host}"
        else:
            host = request.headers.get("host")
            if host:
                base_url = f"http://{host}"
            else:
                base_url = str(request.base_url).rstrip("/")
    base_url = base_url.rstrip("/")

    xml_content = f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>Tentacle</friendlyName>
    <manufacturer>Silicondust</manufacturer>
    <modelName>HDTC-2US</modelName>
    <modelNumber>HDTC-2US</modelNumber>
    <serialNumber></serialNumber>
    <UDN>uuid:{device_id}</UDN>
  </device>
  <URLBase>{base_url}</URLBase>
</root>"""
    return Response(content=xml_content, media_type="application/xml")


@router.get("/lineup_status.json")
@router.get("/hdhr/lineup_status.json")
def hdhr_lineup_status():
    """HDHomeRun lineup scan status."""
    return {
        "ScanInProgress": 0,
        "ScanPossible": 1,
        "Source": "Cable",
        "SourceList": ["Cable"],
    }


@router.post("/lineup.post")
@router.post("/hdhr/lineup.post")
def hdhr_lineup_post():
    """HDHomeRun lineup scan trigger (no-op, Jellyfin calls this)."""
    return Response(status_code=200)


# An HLS "master" playlist lists variant PLAYLISTS, not media segments. Our
# tuner response body is read by Jellyfin as raw video, so a variant URI has to
# be resolved here — piping the variant playlist's text through sends ASCII
# where MPEG-TS is expected, and the master (which never carries
# #EXT-X-ENDLIST) then loops for ever yielding nothing.
_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF", re.IGNORECASE)
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)", re.IGNORECASE)
_MAX_VARIANT_HOPS = 3


def _select_hls_variant(playlist_text: str, base_url: str):
    """Highest-bandwidth variant URI of an HLS master playlist.

    Returns None when `playlist_text` is already a media playlist (no
    #EXT-X-STREAM-INF tags), which is the common case.
    """
    from urllib.parse import urljoin

    lines = playlist_text.splitlines()
    best = None
    best_bw = -1
    for idx, line in enumerate(lines):
        if not _STREAM_INF_RE.match(line.strip()):
            continue
        m = _BANDWIDTH_RE.search(line)
        bw = int(m.group(1)) if m else 0
        for nxt in lines[idx + 1:]:
            nxt = nxt.strip()
            if not nxt or nxt.startswith("#"):
                continue
            if bw >= best_bw:
                best_bw = bw
                best = urljoin(base_url, nxt)
            break
    return best


@router.head("/api/live/stream/{channel_id}")
async def stream_head(channel_id: int, db: Session = Depends(get_db)):
    """HEAD handler for stream URLs — Jellyfin sends HEAD to validate before playing."""
    channel = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "Channel not found")
    return Response(
        status_code=200,
        headers={
            "Content-Type": "video/mp2t",
            "Connection": "close",
            "Cache-Control": "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/api/live/stream/{channel_id}")
async def stream_proxy(channel_id: int, db: Session = Depends(get_db)):
    """Stream proxy for IPTV channels.

    Strategy: resolve the provider's redirect chain (requires TiviMate UA)
    to get the tokenized URL on the real streaming server, then either:
      1. 302 redirect Jellyfin there (if the server serves raw TS), or
      2. Proxy the HLS stream as continuous MPEG-TS bytes (fetch m3u8,
         download chunks, pipe raw bytes).
    """
    # Already pulling this channel? Attach to it instead of opening a second
    # upstream connection for byte-identical data (recording + watching the
    # same channel is the common case). Costs the provider nothing and needs
    # no concurrency slot of its own.
    async with _get_shared_lock():
        shared = _shared_streams.get(channel_id)
        if shared is not None and not shared._closed:
            q = shared.subscribe()
            logger.info(f"[LiveTV] Channel {channel_id} already streaming — "
                        f"attaching client ({len(shared.subscribers)} now)")
            return _SubscriberResponse(shared, q)

    # Cap concurrent upstream pulls (see _StreamSlots). A refusal here is how a
    # scheduled recording silently becomes a zero-byte file -- Jellyfin shows the
    # timer as having run -- so it is logged as an error, by channel name, and
    # counted where the dashboard can see it (GET /api/live/capacity).
    limit = _max_concurrent_streams(db)
    if not await _stream_slots.acquire(limit, _SLOT_WAIT_SECONDS):
        ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
        name = ch.name if ch else f"channel {channel_id}"
        _stream_slots.refused += 1
        _stream_slots.last_refused = {"channel_id": channel_id, "channel": name,
                                      "at": datetime.utcnow().isoformat() + "Z", "limit": limit}
        logger.error(f"[LiveTV] At capacity ({limit} streams) — REFUSED '{name}' (channel {channel_id}). "
                     f"If this was a recording it is lost. Raise livetv_max_concurrent_streams "
                     f"(0 = no limit) if this server and provider can carry more.")
        raise HTTPException(503, f"Too many concurrent live streams (limit {limit})")
    sem = _stream_slots
    # Ownership of the release is handed to the streaming generator on the
    # success paths; on every early-exit / error path below we release here.
    sem_released = False

    def _release_sem():
        nonlocal sem_released
        if not sem_released:
            sem_released = True
            sem.release()

    try:
        channel = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
        if not channel:
            raise HTTPException(404, "Channel not found")

        provider = db.query(Provider).filter(Provider.id == channel.provider_id).first()
        user_agent = (provider.user_agent if provider else None) or "TiviMate/4.7.0 (Linux; Android 12)"

        stream_url = channel.stream_url
        logger.info(f"[LiveTV] Stream request for channel {channel_id} ({channel.name}): {stream_url}")

        # SSRF guard: this endpoint is public, so refuse to fetch anything that
        # resolves to a private/loopback/link-local/metadata address. A provider
        # the admin deliberately configured on a LAN address (a local
        # re-streamer: tuliprox, xTeVe, Threadfin) is the one exception, and
        # only for its own origin -- see services.ssrf.lan_origin_guard (#76).
        guard = lan_origin_guard(provider.server_url) if provider else is_safe_url
        if not guard(stream_url):
            logger.warning(f"[LiveTV] Blocked stream URL (non-public host) for channel {channel_id}: {stream_url}")
            raise HTTPException(502, "Stream URL points to a non-public host")

        upstream = await _stream_proxy_inner(channel_id, user_agent, stream_url,
                                             _release_sem, guard)
        if not isinstance(upstream, StreamingResponse):
            # A raw-TS channel is answered with a redirect; Jellyfin then talks
            # to the provider directly and there is nothing here to share.
            return upstream

        # Become the shared upstream for this channel, so any further client
        # attaches above instead of opening its own provider connection. The
        # concurrency slot is now owned by the shared pump, not by this client.
        shared = _SharedUpstream(channel_id, _release_sem)
        q = shared.subscribe()
        async with _get_shared_lock():
            _shared_streams[channel_id] = shared
        shared.task = asyncio.create_task(shared._pump(upstream.body_iterator))
        return _SubscriberResponse(shared, q)
    except BaseException:
        _release_sem()
        raise


async def _stream_proxy_inner(channel_id: int, user_agent: str, stream_url: str, _release_sem,
                              guard=None):
    """Inner stream proxy logic. `_release_sem()` is called when the concurrency
    slot can be freed: immediately on early-exit paths, or by the streaming
    generator's `finally` once the long-lived stream ends.

    `guard` validates every URL fetched from here on -- redirect hops, HLS
    variants and chunks. stream_proxy passes one scoped to the channel's own
    provider (#76); the default holds everything to is_safe_url()."""
    guard = guard or is_safe_url
    import httpx

    # One GET opens the stream. _send_checked walks the provider's redirect chain
    # with the required UA, re-validating every hop (#73), and hands back the
    # open response from the tokenized URL on the real streaming server -- whose
    # headers say what it serves, and whose body is the stream (raw TS) or the
    # first playlist (HLS). This used to be up to three GETs of the tokenized
    # URL: one to resolve redirects (body discarded), one to probe the content
    # type, and for raw TS a third to actually stream. Each is a connection in
    # the provider's accounting at the very moment it is most likely to say 509.
    #
    # Streaming GETs only, never client.get(): that buffers the WHOLE body
    # first, which never ends for a channel serving continuous MPEG-TS.
    client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(connect=15.0, read=120.0, write=10.0, pool=15.0),
    )
    resp = None
    try:
        # Same regime as the running HLS worker (#86), on a much shorter leash: a
        # 429/509 here usually clears within seconds, but refusing the tuner costs
        # far more than those seconds -- Jellyfin re-tries a failed open only once
        # a minute, so a recording starts a minute late or not at all. A tuner
        # client is blocked on this response though, so the budget is small, and a
        # status that will never fix itself still fails at once.
        import asyncio
        import random
        loop = asyncio.get_running_loop()
        open_started = loop.time()
        open_backoff = 1.0
        open_slept = 0.0
        open_url = stream_url
        while True:
            resp = None
            try:
                resp = await _send_checked(client, open_url, {"User-Agent": user_agent}, guard)
                resp.raise_for_status()
                break
            except httpx.HTTPError as e:
                if resp is not None:
                    # Retry the server that refused, not the whole chain: that
                    # URL has already passed the guard, and re-walking the
                    # redirects would cost the provider an extra request per try.
                    open_url = str(resp.url)
                    await resp.aclose()
                    resp = None
                retryable = (
                    e.response.status_code in _OPEN_RETRYABLE_STATUS
                    if isinstance(e, httpx.HTTPStatusError)
                    else isinstance(e, httpx.TransportError)
                )
                # Whichever is larger: wall clock (slow connects count) or the waits
                # we chose (so the bound holds even if the clock is not advancing).
                waited = max(loop.time() - open_started, open_slept)
                if not retryable or waited + open_backoff > _OPEN_RETRY_BUDGET:
                    logger.error(f"[LiveTV] Tokenized URL failed for channel {channel_id}"
                                 f"{f' after {waited:.0f}s of retries' if waited >= 1 else ''}: {e}")
                    raise HTTPException(502, f"Failed to connect to stream: {e}")
                logger.warning(f"[LiveTV] Opening channel {channel_id} refused "
                               f"(retry in {open_backoff:.0f}s, {waited:.0f}s so far): {e}")
                delay = open_backoff * (0.8 + random.random() * 0.4)
                open_slept += delay
                await asyncio.sleep(delay)
                open_backoff = min(open_backoff * 2, 5.0)

        tokenized_url = str(resp.url)
        logger.info(f"[LiveTV] Resolved tokenized URL for channel {channel_id}: {tokenized_url}")

        content_type = resp.headers.get("content-type", "")
        is_hls = "mpegurl" in content_type.lower()

        if is_hls:
            # HLS playlist — read the playlist text, then we're done with this client
            playlist_text = (await resp.aread()).decode("utf-8", errors="replace")
    except BaseException:
        if resp is not None:
            await resp.aclose()
        await client.aclose()
        raise

    if is_hls:
        await resp.aclose()
        await client.aclose()
    else:
        # Raw TS or other binary stream — pipe THIS response. The generator takes
        # ownership of it and of the client and cleans both up.
        logger.info(f"[LiveTV] Raw stream (CT: {content_type}) — proxying bytes for channel {channel_id}")
        upstream_ct = content_type or "video/mp2t"
        raw_client, raw_resp = client, resp

        async def stream_generator():
            try:
                async for chunk in raw_resp.aiter_bytes(chunk_size=131072):
                    yield chunk
            except Exception as e:
                logger.warning(f"[LiveTV] Stream interrupted for channel {channel_id}: {e}")
            finally:
                await raw_resp.aclose()
                await raw_client.aclose()
                _release_sem()
                logger.info(f"[LiveTV] Stream ended for channel {channel_id}")

        return StreamingResponse(
            stream_generator(),
            media_type=upstream_ct,
            headers={
                "Connection": "close",
                "Cache-Control": "no-cache, no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )

    playlist_base = tokenized_url
    logger.info(f"[LiveTV] HLS stream for channel {channel_id} — proxying chunks as MPEG-TS")

    async def hls_to_mpegts():
        """Wrapper that releases the concurrency slot once the stream ends."""
        try:
            async for chunk in _hls_worker():
                yield chunk
        finally:
            _release_sem()
            logger.info(f"[LiveTV] Stream ended for channel {channel_id}")

    async def _hls_worker():
        """Continuously fetch the HLS playlist and pipe chunk data as raw MPEG-TS."""
        import asyncio
        import random
        seen_chunks: set[str] = set()
        current_playlist = playlist_text
        current_base = playlist_base
        ua_headers = {"User-Agent": user_agent}
        # An Xtream provider answers 429/509 while the account's connection
        # allowance is momentarily saturated -- two recordings whose short HLS
        # requests collide, say -- and is fine again a second later. Counting
        # those toward a six-strike limit ended the stream after well under a
        # minute of squeeze, and Jellyfin turned that into a truncated
        # recording plus a new file. So wait transient failures out on a
        # growing delay and give up only after an unbroken run of them; a
        # status that will never fix itself still stops the stream at once.
        RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 509}
        FAILURE_BUDGET = 120.0   # seconds of unbroken failure before giving up
        BACKOFF_START = 1.0
        BACKOFF_CAP = 5.0        # short: the tuner reader is waiting on us
        CHUNK_RETRIES_IN_PLACE = 3
        failing_since = None
        backoff = BACKOFF_START
        # When the playlist now in hand was read; reloads are timed from here.
        playlist_loaded_at = asyncio.get_running_loop().time()

        def _is_retryable(exc) -> bool:
            if isinstance(exc, httpx.HTTPStatusError):
                return exc.response.status_code in RETRYABLE_STATUS
            # Timeouts, resets and refused connections are all worth another go.
            return isinstance(exc, httpx.TransportError)

        def _note_success():
            nonlocal failing_since, backoff
            failing_since = None
            backoff = BACKOFF_START

        def _note_failure(exc, what: str) -> bool:
            """Record a failure. True means the stream should stop."""
            nonlocal failing_since
            if not _is_retryable(exc):
                logger.error(f"[LiveTV] {what} failed fatally for channel "
                             f"{channel_id}, stopping: {exc}")
                return True
            now = asyncio.get_running_loop().time()
            if failing_since is None:
                failing_since = now
            waited = now - failing_since
            if waited > FAILURE_BUDGET:
                logger.error(f"[LiveTV] {what} still failing after {waited:.0f}s "
                             f"for channel {channel_id}, stopping: {exc}")
                return True
            logger.warning(f"[LiveTV] {what} failed for channel {channel_id} "
                           f"(retry in {backoff:.0f}s, {waited:.0f}s so far): {exc}")
            return False

        async def _backoff_sleep():
            nonlocal backoff
            # Jitter so several streams that were squeezed at the same moment
            # don't all come back at the same moment and squeeze it again.
            await asyncio.sleep(backoff * (0.8 + random.random() * 0.4))
            backoff = min(backoff * 2, BACKOFF_CAP)

        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        ) as hls_client:
            while True:
                # Master playlist? Follow the best variant before treating any
                # line as a media segment. Each hop goes through the same
                # checked sender as everything else (#73): a variant behind a
                # CDN redirect is normal, and every hop is re-validated.
                variant_retry = False
                for hop in range(_MAX_VARIANT_HOPS + 1):
                    variant = _select_hls_variant(current_playlist, current_base)
                    if not variant:
                        break
                    if hop == _MAX_VARIANT_HOPS:
                        logger.error(f"[LiveTV] Too many HLS variant hops for channel {channel_id}")
                        return
                    if not guard(variant):
                        logger.warning(
                            f"[LiveTV] Blocked HLS variant on non-public host for "
                            f"channel {channel_id}: {variant}")
                        return
                    logger.info(f"[LiveTV] Master playlist for channel {channel_id} — following variant")
                    try:
                        v_resp = await _send_checked(hls_client, variant, ua_headers, guard)
                        try:
                            v_resp.raise_for_status()
                            variant_text = (await v_resp.aread()).decode("utf-8", errors="replace")
                        finally:
                            await v_resp.aclose()
                    except Exception as e:
                        # Same retry regime as chunks and refreshes (#86): a 509
                        # here is the provider being momentarily busy, not a
                        # reason to end a recording.
                        if _note_failure(e, "Variant playlist fetch"):
                            return
                        await _backoff_sleep()
                        variant_retry = True
                        break
                    _note_success()
                    current_base = variant
                    current_playlist = variant_text
                    playlist_loaded_at = asyncio.get_running_loop().time()

                if variant_retry:
                    # Still holding a master playlist: its lines are variant
                    # URIs, not segments, so re-read it rather than falling
                    # through and piping playlist text out as video (#68).
                    continue

                # Parse chunk URLs from playlist
                lines = current_playlist.splitlines()
                chunk_urls = []
                target_duration = 5  # default segment length
                is_live = "#EXT-X-ENDLIST" not in current_playlist

                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("#EXT-X-TARGETDURATION:"):
                        try:
                            target_duration = int(stripped.split(":")[1])
                        except (ValueError, IndexError):
                            pass
                    elif stripped and not stripped.startswith("#"):
                        chunk_url = urljoin(current_base, stripped)
                        if not guard(chunk_url):
                            logger.warning(f"[LiveTV] Skipping HLS chunk on non-public host for channel {channel_id}: {chunk_url}")
                            continue
                        chunk_urls.append(chunk_url)

                # Fetch new chunks
                got_new = False
                chunk_pending = False
                for chunk_url in chunk_urls:
                    if chunk_url in seen_chunks:
                        continue
                    # A live playlist is a short sliding window. Going back to it
                    # after a refused chunk costs the backoff, what is left of the
                    # reload wait AND a playlist refresh that can be refused
                    # too -- long enough for the chunk to roll out of the window
                    # and leave a hole. Its URL is still good, so try it again in
                    # place a few times first.
                    payload = None
                    for attempt in range(CHUNK_RETRIES_IN_PLACE + 1):
                        try:
                            chunk_resp = await _send_checked(hls_client, chunk_url, ua_headers, guard)
                            try:
                                chunk_resp.raise_for_status()
                                payload = await chunk_resp.aread()
                            finally:
                                await chunk_resp.aclose()
                            break
                        except Exception as e:
                            chunk_error = e
                            if attempt == CHUNK_RETRIES_IN_PLACE or not _is_retryable(e):
                                break
                            if _note_failure(e, "Chunk fetch"):
                                return
                            await _backoff_sleep()
                    if payload is None:
                        if _note_failure(chunk_error, "Chunk fetch"):
                            return
                        # Deliberately NOT marked seen: a chunk lost to a
                        # transient error is still in the next playlist, and
                        # dropping it silently puts a hole in the recording.
                        # Stop here so chunks stay in order, wait, re-read the
                        # playlist and try this one again.
                        await _backoff_sleep()
                        chunk_pending = True
                        break
                    seen_chunks.add(chunk_url)
                    got_new = True
                    yield payload
                    _note_success()

                if not is_live:
                    # VOD-style playlist — we're done after all chunks
                    return

                # Live stream: wait and re-fetch playlist for new chunks.
                #
                # One reload per segment, as RFC 8216 6.3.4 has it: a playlist
                # that brought something new is good for a whole target
                # duration; only one that brought nothing (or left a chunk
                # still owed) is asked for again after half of one. Reloading
                # every half segment regardless doubled this stream's request
                # rate against the provider's connection accounting for no
                # gain -- every other reload is unchanged by construction.
                # The wait is timed from when the playlist was READ, so time
                # spent downloading chunks or backing off is not added on top
                # and a slow pass cannot let segments roll out of the window.
                reload_after = target_duration if (got_new and not chunk_pending) else target_duration / 2
                remaining = playlist_loaded_at + reload_after - asyncio.get_running_loop().time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                try:
                    pl_resp = await _send_checked(hls_client, current_base, ua_headers, guard)
                    try:
                        pl_resp.raise_for_status()
                        refreshed = (await pl_resp.aread()).decode("utf-8", errors="replace")
                    finally:
                        await pl_resp.aclose()
                except Exception as e:
                    if _note_failure(e, "Playlist refresh"):
                        return
                    await _backoff_sleep()
                    continue
                current_playlist = refreshed
                playlist_loaded_at = asyncio.get_running_loop().time()
                _note_success()

    return StreamingResponse(
        hls_to_mpegts(),
        media_type="video/mp2t",
        headers={
            "Connection": "close",
            "Cache-Control": "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/api/live/playlist.m3u")
def live_playlist_m3u(request: Request, db: Session = Depends(get_db)):
    """Generate M3U playlist for Jellyfin M3U tuner import.
    All stream URLs point to our local proxy (like Threadfin's direct mode)."""
    channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.enabled == True)
        .order_by(LiveChannel.sort_order, LiveChannel.channel_number, LiveChannel.name)
        .all()
    )

    base_url = str(request.base_url).rstrip("/")
    lines = ["#EXTM3U"]
    for ch in channels:
        number = ch.stream_id or str(ch.id)
        epg_id = ch.epg_channel_id or f"tentacle-{ch.id}"
        logo = f' tvg-logo="{ch.logo_url}"' if ch.logo_url else ""
        group = f' group-title="{ch.group_title}"' if ch.group_title else ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{epg_id}" tvg-chno="{number}"{logo}{group},{ch.name}'
        )
        lines.append(f"{base_url}/api/live/stream/{ch.id}")

    # The same YouTube channels the HDHomeRun lineup carries — a user who set
    # Tentacle up as an M3U tuner gets the same channel list either way.
    for yt in youtube_livetv.live_channels(db):
        logo = f' tvg-logo="{yt["logo_url"]}"' if yt["logo_url"] else ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{yt["guide_number"]}" tvg-chno="{yt["guide_number"]}"'
            f'{logo} group-title="{yt["group_title"]}",{yt["name"]}'
        )
        lines.append(f"{base_url}/api/youtube/live/{yt['youtube_channel_id']}/stream.ts")

    content = "\n".join(lines) + "\n"
    return Response(
        content=content,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": "inline; filename=tentacle.m3u"},
    )


@router.get("/hdhr/xmltv.xml")
@router.get("/api/live/xmltv.xml")
def hdhr_xmltv(db: Session = Depends(get_db)):
    """Serve XMLTV guide data for enabled channels."""
    from services.xmltv import generate_xmltv

    # Get enabled channels with EPG IDs
    channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.enabled == True)
        .order_by(LiveChannel.sort_order, LiveChannel.channel_number, LiveChannel.name)
        .all()
    )

    # Use stream_id as stable channel ID (same as lineup.json GuideNumber)
    # This ensures IDs never shift when channels are added/removed.
    xmltv_channels = []
    epg_ids = set()
    # One EPG ID can map to multiple channels (e.g. CP24 HD + CP24 HD BACKUP)
    epg_id_to_guide_numbers: dict[str, list[str]] = {}
    guide_number_group: dict[str, str] = {}
    for ch in channels:
        guide_number = str(ch.stream_id or ch.id)
        xmltv_channels.append({
            "id": guide_number,
            "name": ch.name,
            "logo_url": ch.logo_url,
        })
        # Channel group feeds the category inference below when a programme
        # title says nothing about its genre.
        guide_number_group[guide_number] = ch.group_title
        if ch.epg_channel_id:
            epg_ids.add(ch.epg_channel_id)
            epg_id_to_guide_numbers.setdefault(ch.epg_channel_id, []).append(guide_number)

    # YouTube Live TV channels, with their own guide ids.
    for yt in youtube_livetv.live_channels(db):
        xmltv_channels.append({
            "id": yt["guide_number"],
            "name": yt["name"],
            "logo_url": yt["logo_url"],
        })
        guide_number_group[yt["guide_number"]] = yt["group_title"]
        epg_ids.add(yt["epg_channel_id"])
        epg_id_to_guide_numbers.setdefault(yt["epg_channel_id"], []).append(yt["guide_number"])

    # Get programs for enabled channels, remapping channel_id to GuideNumber(s)
    # When multiple channels share an EPG ID, duplicate programs for each
    programs = []
    inferred_categories = 0
    if epg_ids:
        db_programs = (
            db.query(EPGProgram)
            .filter(EPGProgram.channel_id.in_(epg_ids))
            .filter(EPGProgram.stop >= datetime.utcnow())
            .all()
        )
        for p in db_programs:
            guide_numbers = epg_id_to_guide_numbers.get(p.channel_id, [])
            for gn in guide_numbers:
                # Xtream providers commonly send no category, and without one
                # Jellyfin never sets IsSports/IsNews/IsKids/IsMovie — so no
                # sports badge, empty genre filters, and the sports DVR padding
                # defaults never apply.
                category = p.category
                if not category:
                    category = infer_category(p.title, guide_number_group.get(gn))
                    if category:
                        inferred_categories += 1
                programs.append({
                    "channel_id": gn,
                    "title": p.title,
                    "description": p.description,
                    "start": p.start,
                    "stop": p.stop,
                    "category": category,
                })

    if inferred_categories:
        logger.info(
            f"[LiveTV] XMLTV: inferred a category for {inferred_categories} programme(s) "
            f"the provider sent none for"
        )

    xml_content = generate_xmltv(xmltv_channels, programs)
    return Response(content=xml_content, media_type="application/xml")

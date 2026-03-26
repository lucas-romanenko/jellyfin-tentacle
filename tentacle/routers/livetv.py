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

import logging
import threading
from datetime import datetime
from typing import Optional, List

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

logger = logging.getLogger(__name__)

router = APIRouter()

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


@router.get("/api/live/provider")
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


@router.post("/api/live/provider")
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


@router.post("/api/live/provider/test")
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


@router.post("/api/live/sync/{provider_id}")
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


@router.post("/api/live/sync-channels/{provider_id}")
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


@router.get("/api/live/sync-status")
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


@router.post("/api/live/sync-epg/{provider_id}")
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
                return

            # Clear old EPG data for this provider's channels (by provider_id, not just epg_ids)
            provider_channel_epg_ids = {
                ch.epg_channel_id
                for ch in db.query(LiveChannel).filter(
                    LiveChannel.provider_id == pid,
                    LiveChannel.epg_channel_id.isnot(None),
                ).all()
            }
            if provider_channel_epg_ids:
                db.query(EPGProgram).filter(
                    EPGProgram.channel_id.in_(provider_channel_epg_ids)
                ).delete(synchronize_session=False)
                db.flush()

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
                return

            # Progress callback
            def on_progress(pct, msg):
                _set_sync_status(pid, {"phase": "epg", "status": "running", "progress": pct, "message": msg})

            _set_sync_status(pid, {"phase": "epg", "status": "running", "progress": 5, "message": "Downloading XMLTV guide..."})

            # Stream-parse: downloads full XMLTV but only keeps programs for our channels
            from services.xmltv import stream_parse_xmltv
            programs = stream_parse_xmltv(
                url=epg_url,
                channel_ids=epg_ids,
                user_agent=provider_data["user_agent"],
                on_progress=on_progress,
            )

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
        finally:
            db.close()

    except Exception as e:
        logger.error(f"[LiveTV] EPG sync failed: {e}")
        _set_sync_status(pid, {"phase": "epg", "status": "error", "progress": 0, "message": str(e)})


# ─── Channel management ────────────────────────────────────────────────────


@router.get("/api/live/channels")
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


@router.put("/api/live/channels/{channel_id}")
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


@router.post("/api/live/channels/bulk")
def bulk_update_channels(update: BulkChannelUpdate, db: Session = Depends(get_db)):
    """Bulk enable/disable channels."""
    count = db.query(LiveChannel).filter(
        LiveChannel.id.in_(update.channel_ids)
    ).update({LiveChannel.enabled: update.enabled}, synchronize_session=False)
    db.commit()
    return {"success": True, "updated": count}


@router.post("/api/live/channels/bulk-filter")
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


@router.get("/api/live/groups")
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


@router.put("/api/live/groups/bulk")
def bulk_update_groups(update: BulkGroupUpdate, db: Session = Depends(get_db)):
    """Enable/disable multiple groups and their channels in one request."""
    groups = db.query(LiveChannelGroup).filter(LiveChannelGroup.id.in_(update.group_ids)).all()
    if not groups:
        return {"success": True, "updated": 0}

    db.query(LiveChannelGroup).filter(LiveChannelGroup.id.in_(update.group_ids)).update(
        {LiveChannelGroup.enabled: update.enabled}, synchronize_session=False
    )

    group_names = [g.name for g in groups]
    provider_id = groups[0].provider_id
    db.query(LiveChannel).filter(
        LiveChannel.provider_id == provider_id,
        LiveChannel.group_title.in_(group_names),
    ).update({LiveChannel.enabled: update.enabled}, synchronize_session=False)

    db.commit()
    return {"success": True, "updated": len(groups)}


@router.put("/api/live/groups/{group_id}")
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


@router.get("/api/live/status")
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


@router.post("/api/live/refresh-guide")
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
        # Step 1: Delete + re-create XMLTV listing provider to force full channel remap
        # Re-POSTing with the same ID doesn't remap new channels; must delete and recreate
        livetv_cfg = req.get(f"{jf_url}/System/Configuration/livetv", headers=headers, timeout=10).json()
        for lp in livetv_cfg.get("ListingProviders", []):
            if lp.get("Type") == "xmltv":
                lp_id = lp.get("Id")
                # Delete existing
                req.delete(f"{jf_url}/LiveTv/ListingProviders?Id={lp_id}", headers=headers, timeout=10)
                logger.info(f"[LiveTV] Deleted XMLTV listing provider {lp_id}")
                # Re-create without ID so Jellyfin treats it as new
                lp_new = {k: v for k, v in lp.items() if k != "Id"}
                resp = req.post(f"{jf_url}/LiveTv/ListingProviders", headers=headers, json=lp_new, timeout=15)
                resp.raise_for_status()
                new_id = resp.json().get("Id", "?")
                logger.info(f"[LiveTV] Re-created XMLTV listing provider as {new_id}")

        # Step 2: Trigger RefreshGuide task — fetches new lineup from tuner + refreshes EPG data
        tasks = req.get(f"{jf_url}/ScheduledTasks", headers=headers, timeout=10).json()
        guide_task = next((t for t in tasks if t.get("Key") == "RefreshGuide"), None)
        if not guide_task:
            raise HTTPException(404, "RefreshGuide task not found in Jellyfin")
        task_id = guide_task["Id"]
        resp = req.post(f"{jf_url}/ScheduledTasks/Running/{task_id}", headers=headers, timeout=10)
        resp.raise_for_status()

        msg = "Jellyfin guide refresh triggered"
        if epg_resynced:
            msg = "EPG data synced for new channels + Jellyfin guide refresh triggered"
        logger.info(f"[LiveTV] {msg}")
        return {"success": True, "message": msg}
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
    import httpx
    from urllib.parse import urljoin
    from fastapi.responses import RedirectResponse

    channel = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "Channel not found")

    provider = db.query(Provider).filter(Provider.id == channel.provider_id).first()
    user_agent = (provider.user_agent if provider else None) or "TiviMate/4.7.0 (Linux; Android 12)"

    stream_url = channel.stream_url
    logger.info(f"[LiveTV] Stream request for channel {channel_id} ({channel.name}): {stream_url}")

    # Step 1: Follow the provider's redirect chain with the required UA
    # to get the tokenized URL on the real streaming server
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(connect=15.0, read=30.0, write=10.0, pool=15.0),
    ) as client:
        url = stream_url
        for _ in range(10):  # max redirects
            try:
                resp = await client.get(url, headers={"User-Agent": user_agent})
            except httpx.HTTPError as e:
                logger.error(f"[LiveTV] Stream failed for channel {channel_id}: {e}")
                raise HTTPException(502, f"Failed to connect to stream: {e}")

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise HTTPException(502, "Redirect without Location header")
                url = urljoin(url, location)
                logger.info(f"[LiveTV] Following redirect → {url}")
                continue
            break

    tokenized_url = url
    logger.info(f"[LiveTV] Resolved tokenized URL for channel {channel_id}: {tokenized_url}")

    # Step 2: Try the tokenized URL to see what it serves
    # Probe with a streaming GET — check content type
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=15.0, read=120.0, write=10.0, pool=15.0),
    ) as client:
        try:
            req = client.build_request("GET", tokenized_url, headers={"User-Agent": user_agent})
            resp = await client.send(req, stream=True)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(f"[LiveTV] Tokenized URL failed for channel {channel_id}: {e}")
            raise HTTPException(502, f"Failed to connect to stream: {e}")

        content_type = resp.headers.get("content-type", "")
        is_hls = "mpegurl" in content_type.lower()

        if not is_hls:
            # Raw TS or other binary stream — pipe it directly
            # We need to keep the client alive for the duration of the stream,
            # so we transfer ownership to the generator and exit the context manager.
            pass
        else:
            # HLS playlist — read the playlist text, then we're done with this client
            playlist_text = (await resp.aread()).decode("utf-8", errors="replace")
            await resp.aclose()

    if not is_hls:
        # Raw TS — open a dedicated client that the generator owns and cleans up
        logger.info(f"[LiveTV] Raw stream (CT: {content_type}) — proxying bytes for channel {channel_id}")
        upstream_ct = content_type or "video/mp2t"
        raw_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=10.0, pool=15.0),
        )

        async def stream_generator():
            try:
                req = raw_client.build_request("GET", tokenized_url, headers={"User-Agent": user_agent})
                raw_resp = await raw_client.send(req, stream=True)
                raw_resp.raise_for_status()
                async for chunk in raw_resp.aiter_bytes(chunk_size=131072):
                    yield chunk
            except Exception as e:
                logger.warning(f"[LiveTV] Stream interrupted for channel {channel_id}: {e}")
            finally:
                await raw_client.aclose()
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
        """Continuously fetch the HLS playlist and pipe chunk data as raw MPEG-TS."""
        import asyncio
        seen_chunks: set[str] = set()
        current_playlist = playlist_text
        ua_headers = {"User-Agent": user_agent}
        consecutive_errors = 0

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        ) as hls_client:
            while True:
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
                        chunk_url = urljoin(playlist_base, stripped)
                        chunk_urls.append(chunk_url)

                # Fetch new chunks
                for chunk_url in chunk_urls:
                    if chunk_url in seen_chunks:
                        continue
                    seen_chunks.add(chunk_url)
                    try:
                        chunk_resp = await hls_client.get(chunk_url, headers=ua_headers)
                        chunk_resp.raise_for_status()
                        yield chunk_resp.content
                        consecutive_errors = 0
                    except Exception as e:
                        consecutive_errors += 1
                        logger.warning(f"[LiveTV] Chunk fetch failed ({consecutive_errors}): {e}")
                        if consecutive_errors > 5:
                            logger.error(f"[LiveTV] Too many chunk errors for channel {channel_id}, stopping")
                            return

                if not is_live:
                    # VOD-style playlist — we're done after all chunks
                    return

                # Live stream: wait and re-fetch playlist for new chunks
                await asyncio.sleep(target_duration / 2)
                try:
                    pl_resp = await hls_client.get(playlist_base, headers=ua_headers)
                    pl_resp.raise_for_status()
                    current_playlist = pl_resp.text
                except Exception as e:
                    logger.warning(f"[LiveTV] Playlist refresh failed for channel {channel_id}: {e}")
                    consecutive_errors += 1
                    if consecutive_errors > 5:
                        return

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
    for ch in channels:
        guide_number = str(ch.stream_id or ch.id)
        xmltv_channels.append({
            "id": guide_number,
            "name": ch.name,
            "logo_url": ch.logo_url,
        })
        if ch.epg_channel_id:
            epg_ids.add(ch.epg_channel_id)
            epg_id_to_guide_numbers.setdefault(ch.epg_channel_id, []).append(guide_number)

    # Get programs for enabled channels, remapping channel_id to GuideNumber(s)
    # When multiple channels share an EPG ID, duplicate programs for each
    programs = []
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
                programs.append({
                    "channel_id": gn,
                    "title": p.title,
                    "description": p.description,
                    "start": p.start,
                    "stop": p.stop,
                    "category": p.category,
                })

    xml_content = generate_xmltv(xmltv_channels, programs)
    return Response(content=xml_content, media_type="application/xml")

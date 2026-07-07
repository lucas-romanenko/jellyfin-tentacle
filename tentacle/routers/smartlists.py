"""
Tentacle - SmartLists Router
Manage per-user Jellyfin SmartList config files.
"""

import os
import json
import logging
import time
import threading
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sqlalchemy import func
from models.database import (
    get_db, get_setting, Movie, Series, Provider,
    ListSubscription, ListItem, AutoPlaylistToggle, TentacleUser, DownloadRequest,
)
from routers.auth import get_user_from_request
from services.smartlists import (
    get_desired_smartlists, sync_smartlists, _scan_existing,
    write_home_config, _notify_jellyfin_plugin, refresh_smartlist_playlists,
    _get_smartlists_with_playlist_ids, update_playlist_sort, SORT_BY_DISPLAY,
    _user_smartlists_path, get_playlist_version, bump_playlist_version,
    sync_single_custom_playlist, toggle_auto_playlist_fast,
    home_config_lock, _atomic_write_json,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/smartlists", tags=["smartlists"])

HOME_CONFIG_DIR = "/data/home-configs"

# Built-in Jellyfin home sections that can be added to the home screen
BUILTIN_SECTIONS = [
    {"section_id": "smalllibrarytiles", "display_name": "My Media"},
    {"section_id": "smalllibrarytiles_small", "display_name": "My Media (small)"},
    {"section_id": "activerecordings", "display_name": "Active Recordings"},
    {"section_id": "resumevideo", "display_name": "Continue Watching"},
    {"section_id": "resumeaudio", "display_name": "Continue Listening"},
    {"section_id": "resumebook", "display_name": "Continue Reading"},
    {"section_id": "latestmedia", "display_name": "Recently Added Media"},
    {"section_id": "nextup", "display_name": "Next Up"},
    {"section_id": "livetv", "display_name": "Live TV"},
]
BUILTIN_MAP = {s["section_id"]: s for s in BUILTIN_SECTIONS}


@router.get("/version")
def playlist_version():
    """Return the current playlist version counter. Polled by the Jellyfin plugin
    JS to detect playlist changes and live-update home screen rows."""
    return {"version": get_playlist_version()}


def _home_config_path(user: TentacleUser) -> Path:
    """Return per-user home config file path."""
    d = Path(HOME_CONFIG_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{user.jellyfin_user_id}.json"


def _read_home_json(user: TentacleUser) -> dict:
    """Read per-user home config from disk.

    Returns an empty dict ONLY when the file is genuinely missing. If the file
    exists but is corrupt/unparseable, the bad file is backed up and an error
    is raised — we must never treat a parse error as "no config", because the
    caller would then overwrite (and wipe) the user's real rows.
    """
    p = _home_config_path(user)
    if not p.exists():
        # Fall back to legacy global file only for admin (migration from pre-multi-user)
        if user.is_admin:
            legacy = Path("/data/tentacle-home.json")
            if legacy.exists():
                try:
                    return json.loads(legacy.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # Corrupt file — back it up and refuse to proceed rather than silently
        # treating it as empty (which would wipe the user's rows on next write).
        backup = p.with_name(f"{p.name}.corrupt.{int(time.time())}")
        try:
            os.replace(p, backup)
            logger.error(f"Corrupt home config for {user.display_name}, backed up to {backup}: {e}")
        except Exception as move_err:
            logger.error(f"Corrupt home config for {user.display_name} and backup failed: {move_err}")
        raise HTTPException(
            status_code=500,
            detail="Home config file was corrupt and has been backed up. Please retry.",
        )


def _write_home_json(user: TentacleUser, config: dict):
    """Write per-user home config to disk atomically (temp file + os.replace)."""
    p = _home_config_path(user)
    _atomic_write_json(p, config)


@router.get("")
def list_smartlists(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """List all desired SmartLists for the current user with on-disk status."""
    desired = get_desired_smartlists(db, user_id=user.id)
    try:
        smartlists_path = _user_smartlists_path(db, user.id)
    except ValueError:
        smartlists_path = Path(get_setting(db, "smartlists_path", "/data/smartlists"))
    existing = _scan_existing(smartlists_path)
    path_accessible = smartlists_path.exists() and smartlists_path.is_dir()

    result = []
    for sl in desired:
        entry = {
            "name": sl["name"],
            "tag": sl["tag"],
            "media_type": sl["media_type"],
            "enabled": sl["enabled"],
            "source": sl.get("source", ""),
            "exists_on_disk": sl["name"] in existing,
            "sort_by": "releasedate",
            "sort_order": "Descending",
        }
        # Read actual sort from disk config if it exists
        if sl["name"] in existing:
            _, config_data = existing[sl["name"]]
            sort_opts = (config_data.get("Order", {}).get("SortOptions") or [{}])
            if sort_opts:
                entry["sort_by"] = SORT_BY_DISPLAY.get(sort_opts[0].get("SortBy", "ReleaseDate"), "releasedate")
                entry["sort_order"] = sort_opts[0].get("SortOrder", "Descending")
        result.append(entry)

    result.sort(key=lambda x: x["name"].lower())

    return {
        "smartlists": result,
        "path": str(smartlists_path),
        "path_accessible": path_accessible,
    }


class PlaylistSortRequest(BaseModel):
    name: str
    sort_by: str
    sort_order: str


@router.post("/sort")
def set_playlist_sort(req: PlaylistSortRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Update sort order for a per-user playlist and re-populate in Jellyfin."""
    return update_playlist_sort(req.name, req.sort_by, req.sort_order, db, user_id=user.id)


@router.post("/sync-one")
def sync_one(body: dict, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Fast sync a single custom playlist — used by create/edit to avoid rebuilding all configs."""
    name = body.get("name", "")
    conditions = body.get("conditions", [])
    apply_to = body.get("apply_to", "both")
    output_tag = body.get("output_tag", name)
    if not name or not conditions:
        return {"error": "name and conditions required"}
    return sync_single_custom_playlist(db, user.id, name, conditions, apply_to, output_tag)


# ── Full resync: run in the background so the request returns immediately ──
# A full resync rebuilds every playlist in Jellyfin, which can take minutes on
# large libraries. Running it inline blew past the reverse-proxy gateway timeout
# (Cloudflare tunnel / Nginx) even though the work succeeded. We run it in a
# daemon thread with its own DB session and expose progress via /sync-status.
_resync_lock = threading.Lock()
_resync_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "summary": None,
    "error": None,
}


def _run_full_resync(user_id: int):
    """Full playlist resync pipeline, run in a background thread. Logs a clear
    start line and a completion summary (counts, per-playlist item totals,
    duration) so both manual and nightly runs are traceable in `docker logs`."""
    from models.database import SessionLocal
    start = time.time()
    logger.info(f"[Resync] Full playlist resync started (user_id={user_id})")
    db = SessionLocal()
    try:
        result = sync_smartlists(db, user_id=user_id)
        refresh = {}
        try:
            refresh = refresh_smartlist_playlists(db, user_id=user_id, only_names=None)
        except Exception as e:
            logger.error(f"[Resync] Playlist refresh failed: {e}", exc_info=True)
            result["refresh_error"] = str(e)

        # Remove duplicate/orphaned Jellyfin playlists (same name as a managed
        # playlist but not its canonical ID). Runs after the refresh so configs
        # hold current canonical IDs.
        try:
            from services.smartlists import cleanup_orphaned_playlists
            removed_dupes = cleanup_orphaned_playlists(db, user_id)
            if removed_dupes:
                result["orphans_removed"] = removed_dupes
        except Exception as e:
            logger.warning(f"[Resync] Orphan playlist cleanup failed: {e}")

        try:
            from routers.collections import sync_playlist_artwork
            sync_playlist_artwork(db)
        except Exception as e:
            logger.warning(f"[Resync] Artwork sync failed: {e}")

        try:
            write_home_config(db, user_id=user_id)
            _notify_jellyfin_plugin(db)
        except Exception as e:
            logger.warning(f"[Resync] Home config / plugin notify failed: {e}")

        bump_playlist_version()

        elapsed = round(time.time() - start, 1)
        counts = refresh.get("item_counts") or {}
        summary = {
            "created": result.get("created", 0),
            "updated": result.get("updated", 0),
            "removed": result.get("removed", 0),
            "errors": refresh.get("errors", 0),
            "playlists": len(counts),
            "orphans_removed": result.get("orphans_removed", 0),
            "elapsed_seconds": elapsed,
        }
        logger.info(
            f"[Resync] Full playlist resync complete in {elapsed}s — "
            f"created={summary['created']} updated={summary['updated']} "
            f"removed={summary['removed']} errors={summary['errors']} "
            f"playlists={summary['playlists']} orphans_removed={summary['orphans_removed']}"
        )
        if summary["errors"]:
            logger.warning(f"[Resync] {summary['errors']} playlist(s) had errors — see [SmartLists] lines above")
        if counts:
            detail = ", ".join(f"{n}={c}" for n, c in sorted(counts.items()))
            logger.info(f"[Resync] Playlist item counts: {detail}")
        with _resync_lock:
            _resync_state.update(running=False, finished_at=time.time(), summary=summary, error=None)
    except Exception as e:
        logger.error(f"[Resync] Full playlist resync failed: {e}", exc_info=True)
        with _resync_lock:
            _resync_state.update(running=False, finished_at=time.time(), error=str(e))
    finally:
        db.close()


@router.get("/sync-status")
def sync_status(user: TentacleUser = Depends(get_user_from_request)):
    """Report whether a full resync is running plus the last completion summary.
    Polled by the dashboard after triggering Resync All."""
    with _resync_lock:
        return dict(_resync_state)


@router.post("/sync")
def sync(body: dict = None, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Run per-user playlist pipeline: sync configs → populate items → artwork → home config → notify plugin.
    Pass {"full": true} to force refresh ALL playlists — this runs in the background
    and returns {"status": "started"} immediately (poll /sync-status for completion)."""
    full = (body or {}).get("full", False)

    if full:
        # Heavy full rebuild — hand off to a background thread and return now.
        with _resync_lock:
            if _resync_state["running"]:
                return {"status": "already_running", "started_at": _resync_state["started_at"]}
            _resync_state.update(
                running=True, started_at=time.time(),
                finished_at=None, summary=None, error=None,
            )
        threading.Thread(
            target=_run_full_resync, args=(user.id,), daemon=True, name=f"resync-{user.id}",
        ).start()
        return {"status": "started"}

    # Targeted sync (fast path) — synchronous, only new/changed playlists.
    result = sync_smartlists(db, user_id=user.id)
    changed = result.get("changed_names") or []
    if changed:
        try:
            refresh_result = refresh_smartlist_playlists(
                db, user_id=user.id, only_names=changed
            )
            result["refresh"] = refresh_result
        except Exception as e:
            logger.warning(f"Playlist refresh after sync failed: {e}")
            result["refresh_error"] = str(e)

    # Sync artwork so new playlists get images
    try:
        from routers.collections import sync_playlist_artwork, _uploaded_artwork
        # Clear cache for changed playlists so artwork upload is always attempted
        if changed:
            keys_to_clear = [k for k in list(_uploaded_artwork.keys())
                             if any(name in k for name in changed)]
            for k in keys_to_clear:
                _uploaded_artwork.pop(k, None)
        artwork_result = sync_playlist_artwork(db)
        logger.info(f"Artwork sync: {artwork_result}")
    except Exception as e:
        logger.warning(f"Artwork sync after sync failed: {e}")

    # Rebuild home config and notify plugin
    try:
        write_home_config(db, user_id=user.id)
        _notify_jellyfin_plugin(db)
    except Exception as e:
        logger.warning(f"Home config/notify after sync failed: {e}")

    bump_playlist_version()
    return result


@router.post("/write-home-config")
def write_home(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Generate per-user home config. Preserves existing row order and hero pick."""
    config = write_home_config(db, user_id=user.id)
    if not config:
        return {"status": "error", "message": "No SmartLists with playlist IDs found"}
    return {"status": "ok", "rows": len(config.get("rows", [])), "config": config}


@router.get("/home-config")
def read_home(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Return the current per-user home config contents."""
    config = _read_home_json(user)
    if not config:
        return {"exists": False, "config": {}}
    return {"exists": True, "config": config}


@router.post("/refresh-playlists")
def refresh_playlists(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Refresh per-user SmartList playlists in Jellyfin."""
    stats = refresh_smartlist_playlists(db, user_id=user.id)
    if "error" in stats:
        return {"success": False, **stats}
    return {"success": True, **stats}


_SORT_LABELS = {
    "datecreated": "Recently Added", "releasedate": "Release Date",
    "premieredate": "Release Date", "sortname": "Name", "name": "Name",
    "communityrating": "Rating", "random": "Random",
}


@router.get("/health")
def playlist_health(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Per-playlist health: live Jellyfin item count, sort, last refresh, status,
    plus any orphan/duplicate Jellyfin playlists. One Jellyfin call (ChildCount
    for all playlists) + on-disk configs — cheap enough to poll from the UI."""
    from services.jellyfin import JellyfinService
    from services.smartlists import _get_jellyfin_user_id

    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")
    if not jellyfin_url or not jellyfin_key:
        return {"playlists": [], "orphans": [], "error": "Jellyfin not configured"}
    try:
        smartlists_path = _user_smartlists_path(db, user.id)
    except ValueError:
        return {"playlists": [], "orphans": []}
    jf_user_id = _get_jellyfin_user_id(db, user.id)
    if not jf_user_id:
        return {"playlists": [], "orphans": [], "error": "No Jellyfin user"}

    jf = JellyfinService(jellyfin_url, jellyfin_key, jf_user_id)
    # Live playlists: id -> {name, count}
    live = {}
    try:
        for pl in jf.get_playlists(jf_user_id):
            live[pl.get("Id")] = {"name": pl.get("Name") or "", "count": pl.get("ChildCount")}
    except Exception:
        return {"playlists": [], "orphans": [], "error": "Could not reach Jellyfin"}

    existing = _scan_existing(smartlists_path)
    canonical_ids = set()
    managed_names = {n.strip().lower() for n in existing.keys()}
    playlists = []
    for name, (folder, config) in existing.items():
        if config.get("Type") != "Playlist":
            continue
        cid = None
        for up in (config.get("UserPlaylists") or []):
            if up.get("JellyfinPlaylistId"):
                cid = up["JellyfinPlaylistId"]
                break
        if not cid:
            cid = config.get("JellyfinPlaylistId")
        if cid:
            canonical_ids.add(cid)
        media_types = config.get("MediaTypes", []) or []
        is_series = "Series" in media_types and "Movie" not in media_types
        sort_opts = (config.get("Order", {}).get("SortOptions") or [{}])
        sort_raw = (sort_opts[0].get("SortBy") or "").strip() if sort_opts else ""
        sort = _SORT_LABELS.get(sort_raw.lower(), sort_raw or "—")
        info = live.get(cid) if cid else None
        count = info["count"] if info else None
        enabled = config.get("Enabled", True)
        if not enabled:
            status = "disabled"
        elif info is None:
            status = "missing"
        elif count == 0:
            status = "empty"
        else:
            status = "ok"
        playlists.append({
            "name": name,
            "count": count,
            "sort": sort,
            "media_types": media_types,
            "is_series": is_series,
            "last_refreshed": config.get("LastRefreshed"),
            "enabled": enabled,
            "status": status,
        })
    playlists.sort(key=lambda p: p["name"].lower())

    orphans = []
    for pid, info in live.items():
        nl = (info["name"] or "").strip().lower()
        if nl in managed_names and pid not in canonical_ids:
            orphans.append({"name": info["name"], "count": info["count"], "id": pid})

    return {"playlists": playlists, "orphans": orphans}


class PreviewRequest(BaseModel):
    apply_to: str = "both"
    conditions: list = []


@router.post("/preview-count")
def preview_count(body: PreviewRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Return the number of items matching the given conditions (for live preview)."""
    from services.smartlists import _classify_conditions, _conditions_to_expressions, _build_query_params, _extract_genre_logic
    from services.jellyfin import JellyfinService

    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    if not jf_url or not jf_key:
        return {"count": 0}

    conditions = body.conditions
    if not conditions:
        return {"count": 0}

    media = ["Movie", "Series"]
    if body.apply_to == "movies":
        media = ["Movie"]
    elif body.apply_to == "series":
        media = ["Series"]

    classification = _classify_conditions(conditions)

    # Build a minimal config to reuse _build_query_params
    expressions = []
    if classification == "native":
        expressions = _conditions_to_expressions(conditions)
    else:
        # For tentacle/mixed conditions, we can't easily preview without tag data
        # Just count items with any tag-based conditions as 0
        return {"count": -1}  # -1 signals "can't preview"

    config = {
        "MediaTypes": media,
        "ExpressionSets": [{"Expressions": expressions}] if expressions else [],
    }

    jf = JellyfinService(jf_url, jf_key)
    query = _build_query_params(config)
    items = jf.query_items(**query)

    # AND filter for multiple genres (skip if OR mode)
    required_genres = query.get("genres") or []
    genre_logic = _extract_genre_logic(conditions)
    if len(required_genres) > 1 and genre_logic != "or":
        required_lower = [g.lower() for g in required_genres]
        items = [
            item for item in items
            if all(
                any(ig.lower() == rg for ig in (item.get("Genres") or []))
                for rg in required_lower
            )
        ]

    return {"count": len(items)}


@router.post("/notify")
def notify(db: Session = Depends(get_db)):
    """Notify the Jellyfin plugin to reload. Does NOT touch the JSON."""
    result = _notify_jellyfin_plugin(db)
    return {"success": True, **result}


# ── Row identity helper ─────────────────────────────────────────────────────

def _row_key(row: dict) -> str:
    """Unique key for a row: 'playlist:<id>' or 'builtin:<section_id>'."""
    if row.get("type") == "builtin":
        return f"builtin:{row.get('section_id', '')}"
    return f"playlist:{row.get('playlist_id', '')}"


# ── Reorder rows ────────────────────────────────────────────────────────────

class ReorderRequest(BaseModel):
    order: list[str]  # list of row keys like "playlist:abc123" or "builtin:resumevideo"


@router.post("/reorder")
def reorder(req: ReorderRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Read JSON, reorder rows to match, write JSON back. That's it."""
    with home_config_lock:
        config = _read_home_json(user)
        if not config or "rows" not in config:
            return {"success": False, "message": "No home config found"}

        rows_by_key = {_row_key(r): r for r in config["rows"]}

        new_rows = []
        for i, key in enumerate(req.order, start=1):
            if key in rows_by_key:
                row = rows_by_key.pop(key)
                row["order"] = i
                new_rows.append(row)

        # Append any leftover rows not in the request, continuing the order
        # sequence so order ints stay unique (no duplicate trailing values).
        for row in rows_by_key.values():
            row["order"] = len(new_rows) + 1
            new_rows.append(row)

        config["rows"] = new_rows
        _write_home_json(user, config)
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    logger.info(f"Reordered home rows for {user.display_name}: {[r['display_name'] for r in new_rows]}")
    return {"success": True, "rows": len(new_rows)}


# ── Hero pick ───────────────────────────────────────────────────────────────

class HeroPickRequest(BaseModel):
    playlist_id: str


@router.get("/all-playlists")
def all_playlists(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Return all per-user SmartLists with playlist IDs."""
    playlists = _get_smartlists_with_playlist_ids(db, user_id=user.id)
    playlists.sort(key=lambda x: x["name"].lower())
    return {"playlists": playlists}


@router.get("/available-playlists")
def available_playlists(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Return per-user SmartLists with playlist IDs that are not yet in the home config rows."""
    all_pl = _get_smartlists_with_playlist_ids(db, user_id=user.id)
    config = _read_home_json(user)
    current_ids = {r.get("playlist_id") for r in config.get("rows", []) if r.get("type", "playlist") == "playlist"}
    available = [p for p in all_pl if p["playlist_id"] not in current_ids]
    available.sort(key=lambda x: x["name"].lower())
    return {"playlists": available}


class AddRowRequest(BaseModel):
    playlist_id: Optional[str] = None
    section_id: Optional[str] = None


@router.post("/add-row")
def add_row(req: AddRowRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Add a playlist or built-in section as a new row to the home config."""
    with home_config_lock:
        config = _read_home_json(user)
        if not config:
            config = {"hero": {"enabled": False, "playlist_id": "", "display_name": ""}, "rows": []}
        config.setdefault("rows", [])

        if req.section_id:
            # Adding a built-in Jellyfin section
            builtin = BUILTIN_MAP.get(req.section_id)
            if not builtin:
                return {"success": False, "message": "Unknown built-in section"}
            if any(r.get("type") == "builtin" and r.get("section_id") == req.section_id for r in config["rows"]):
                return {"success": False, "message": "Already in home screen"}
            for r in config["rows"]:
                r["order"] = r.get("order", 0) + 1
            config["rows"].insert(0, {
                "type": "builtin",
                "section_id": req.section_id,
                "display_name": builtin["display_name"],
                "order": 1,
            })
        elif req.playlist_id:
            # Adding a Tentacle playlist (per-user)
            all_playlists = _get_smartlists_with_playlist_ids(db, user_id=user.id)
            match = next((p for p in all_playlists if p["playlist_id"] == req.playlist_id), None)
            if not match:
                return {"success": False, "message": "Playlist not found"}
            if any(r.get("playlist_id") == req.playlist_id and r.get("type", "playlist") == "playlist" for r in config["rows"]):
                return {"success": False, "message": "Already in home screen"}
            home_row_limit = int(get_setting(db, "home_row_limit", "20") or "20")
            for r in config["rows"]:
                r["order"] = r.get("order", 0) + 1
            config["rows"].insert(0, {
                "type": "playlist",
                "playlist_id": req.playlist_id,
                "display_name": match["name"],
                "order": 1,
                "max_items": home_row_limit,
            })
        else:
            return {"success": False, "message": "Must provide playlist_id or section_id"}

        _write_home_json(user, config)
        rows_count = len(config["rows"])
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    return {"success": True, "rows": rows_count}


class RemoveRowRequest(BaseModel):
    row_key: Optional[str] = None  # "playlist:<id>" or "builtin:<section_id>"
    playlist_id: Optional[str] = None  # backwards compat


@router.post("/remove-row")
def remove_row(req: RemoveRowRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Remove a row from the home config."""
    with home_config_lock:
        config = _read_home_json(user)
        if not config or "rows" not in config:
            return {"success": False, "message": "No home config"}

        if req.row_key:
            config["rows"] = [r for r in config["rows"] if _row_key(r) != req.row_key]
        elif req.playlist_id:
            config["rows"] = [r for r in config["rows"] if r.get("playlist_id") != req.playlist_id]
        else:
            return {"success": False, "message": "Must provide row_key or playlist_id"}

        for i, r in enumerate(config["rows"], start=1):
            r["order"] = i

        _write_home_json(user, config)
        rows_count = len(config["rows"])
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    return {"success": True, "rows": rows_count}


@router.get("/builtin-sections")
def list_builtin_sections(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Return available built-in Jellyfin sections not yet in the home config."""
    config = _read_home_json(user)
    current_ids = {r.get("section_id") for r in config.get("rows", []) if r.get("type") == "builtin"}
    available = [s for s in BUILTIN_SECTIONS if s["section_id"] not in current_ids]
    return {"sections": available}


class RowMaxItemsRequest(BaseModel):
    playlist_id: Optional[str] = None
    row_key: Optional[str] = None
    max_items: int


@router.post("/row-max-items")
def set_row_max_items(req: RowMaxItemsRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Update max_items for a specific row."""
    with home_config_lock:
        config = _read_home_json(user)
        if not config or "rows" not in config:
            return {"success": False, "message": "No home config found"}

        val = max(5, min(100, req.max_items))
        for row in config["rows"]:
            if req.row_key and _row_key(row) == req.row_key:
                row["max_items"] = val
                break
            elif req.playlist_id and row.get("playlist_id") == req.playlist_id:
                row["max_items"] = val
                break
        else:
            return {"success": False, "message": "Row not found"}

        _write_home_json(user, config)
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    return {"success": True, "max_items": val}


@router.post("/hero")
def set_hero(req: HeroPickRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Read JSON, update hero, write JSON back. That's it."""
    with home_config_lock:
        config = _read_home_json(user)
        if not config:
            config = {"hero": {"enabled": False, "playlist_id": "", "display_name": ""}, "rows": []}
        if "rows" not in config:
            config["rows"] = []

        existing_hero = config.get("hero", {})
        if req.playlist_id:
            # Look up display name from rows first, then all playlists
            matching = next((r for r in config["rows"] if r.get("playlist_id") == req.playlist_id), None)
            if matching:
                display_name = matching["display_name"]
            else:
                all_playlists = _get_smartlists_with_playlist_ids(db, user_id=user.id)
                pl = next((p for p in all_playlists if p["playlist_id"] == req.playlist_id), None)
                display_name = pl["name"] if pl else req.playlist_id
            config["hero"] = {
                "enabled": True,
                "playlist_id": req.playlist_id,
                "display_name": display_name,
                "sort_by": existing_hero.get("sort_by", "random"),
                "sort_order": existing_hero.get("sort_order", "Descending"),
                "require_logo": existing_hero.get("require_logo", True),
                "require_trailer": existing_hero.get("require_trailer", False),
                "item_count": existing_hero.get("item_count", 10),
            }
        else:
            config["hero"] = {"enabled": False, "playlist_id": "", "display_name": "", "sort_by": "random", "sort_order": "Descending", "require_logo": True, "require_trailer": False, "item_count": 10}

        _write_home_json(user, config)
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    logger.info(f"Updated hero: {req.playlist_id or '(disabled)'}")
    return {"success": True}


class HeroSortRequest(BaseModel):
    sort_by: str
    sort_order: str
    require_logo: Optional[bool] = None
    require_trailer: Optional[bool] = None
    trailer_audio: Optional[bool] = None
    item_count: Optional[int] = None


@router.post("/hero-sort")
def set_hero_sort(req: HeroSortRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Update hero spotlight sort order."""
    with home_config_lock:
        config = _read_home_json(user)
        hero = config.get("hero", {})
        if not hero.get("enabled"):
            return {"success": False, "message": "Hero is not enabled"}

        hero["sort_by"] = req.sort_by
        hero["sort_order"] = req.sort_order
        if req.require_logo is not None:
            hero["require_logo"] = req.require_logo
        if req.require_trailer is not None:
            hero["require_trailer"] = req.require_trailer
        if req.trailer_audio is not None:
            hero["trailer_audio"] = req.trailer_audio
        if req.item_count is not None:
            hero["item_count"] = max(1, min(req.item_count, 25))
        config["hero"] = hero
        _write_home_json(user, config)
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    logger.info(f"Updated hero sort: {req.sort_by} {req.sort_order}")
    return {"success": True}


# ── Toolbar Config ─────────────────────────────────────────────────────────

VALID_TOOLBAR_BUTTONS = {"search", "discover", "activity", "favorites", "libraries", "shuffle", "genres"}
DEFAULT_TOOLBAR = [
    {"id": "search", "enabled": True},
    {"id": "discover", "enabled": True},
    {"id": "activity", "enabled": True},
    {"id": "favorites", "enabled": True},
    {"id": "libraries", "enabled": True},
    {"id": "shuffle", "enabled": False},
    {"id": "genres", "enabled": False},
]


class ToolbarButton(BaseModel):
    id: str
    enabled: bool


class ToolbarRequest(BaseModel):
    buttons: list[ToolbarButton]


@router.post("/toolbar")
def set_toolbar(req: ToolbarRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    with home_config_lock:
        config = _read_home_json(user)
        if not config:
            config = {}

        # Validate and build toolbar config
        toolbar = []
        seen = set()
        for btn in req.buttons:
            if btn.id in VALID_TOOLBAR_BUTTONS and btn.id not in seen:
                toolbar.append({"id": btn.id, "enabled": btn.enabled})
                seen.add(btn.id)

        # Add any missing buttons at the end (disabled)
        for btn_id in ["search", "discover", "activity", "favorites", "libraries", "shuffle", "genres"]:
            if btn_id not in seen:
                toolbar.append({"id": btn_id, "enabled": False})

        config["toolbar"] = toolbar
        _write_home_json(user, config)
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    logger.info(f"Updated toolbar config: {[b['id'] for b in toolbar if b['enabled']]}")
    return {"success": True}


# ── Auto Playlists ─────────────────────────────────────────────────────────

def _compute_auto_playlists(db: Session, user_id: int = None) -> list:
    """Compute all possible auto playlists from synced content, lists, and built-ins.
    Resolves enabled state from per-user toggles."""
    results = []

    # ── Source playlists (from IPTV providers) ──
    # Get distinct source_tags and which media types have content
    movie_tags = db.query(
        Movie.source_tag, Movie.provider_id, func.count(Movie.id)
    ).filter(
        Movie.source_tag.isnot(None), Movie.source_tag != "",
        Movie.source != "radarr",
    ).group_by(Movie.source_tag, Movie.provider_id).all()

    series_tags = db.query(
        Series.source_tag, Series.provider_id, func.count(Series.id)
    ).filter(
        Series.source_tag.isnot(None), Series.source_tag != "",
        Series.source != "sonarr",
    ).group_by(Series.source_tag, Series.provider_id).all()

    # Map provider_id to name
    providers = {p.id: p.name for p in db.query(Provider).all()}

    # Build source playlists
    seen_source_keys = set()
    for source_tag, provider_id, count in movie_tags:
        key = f"source:{source_tag}:movies"
        if key not in seen_source_keys:
            seen_source_keys.add(key)
            results.append({
                "key": key,
                "name": f"{source_tag} Movies",
                "tag": f"{source_tag} Movies",
                "category": "source",
                "origin": f"Provider: {providers.get(provider_id, 'Unknown')}",
                "media_type": ["Movie"],
                "item_count": count,
            })

    for source_tag, provider_id, count in series_tags:
        key = f"source:{source_tag}:series"
        if key not in seen_source_keys:
            seen_source_keys.add(key)
            results.append({
                "key": key,
                "name": f"{source_tag} TV",
                "tag": f"{source_tag} TV",
                "category": "source",
                "origin": f"Provider: {providers.get(provider_id, 'Unknown')}",
                "media_type": ["Series"],
                "item_count": count,
            })

    # ── List playlists (per-user) ──
    list_query = db.query(ListSubscription).filter(ListSubscription.active == True)
    if user_id is not None:
        list_query = list_query.filter(ListSubscription.user_id == user_id)
    lists = list_query.all()
    for lst in lists:
        item_count = db.query(func.count(ListItem.id)).filter(
            ListItem.list_id == lst.id
        ).scalar() or 0
        results.append({
            "key": f"list:{lst.id}",
            "name": lst.tag or lst.name,
            "tag": lst.tag,
            "category": "list",
            "origin": f"{lst.type.replace('_', ' ').title()} list",
            "media_type": ["Movie", "Series"],
            "item_count": item_count,
            "list_id": lst.id,
            "playlist_enabled": lst.playlist_enabled,
        })

    # ── Built-in playlists ──
    from datetime import datetime, timedelta
    recently_added_days = int(get_setting(db, "recently_added_days", "30"))
    cutoff = datetime.utcnow() - timedelta(days=recently_added_days)
    recent_movies = db.query(func.count(Movie.id)).filter(Movie.date_added >= cutoff).scalar() or 0
    recent_series = db.query(func.count(Series.id)).filter(Series.date_added >= cutoff).scalar() or 0
    dl_movies = db.query(func.count(Movie.id)).filter(Movie.source == "radarr").scalar() or 0
    dl_series = db.query(func.count(Series.id)).filter(Series.source == "sonarr").scalar() or 0

    builtins = [
        {"key": "builtin:recently_added_movies", "name": "Recently Added Movies",
         "tag": "Recently Added Movies", "origin": f"Last {recently_added_days} days",
         "media_type": ["Movie"], "item_count": recent_movies},
        {"key": "builtin:recently_added_tv", "name": "Recently Added TV",
         "tag": "Recently Added TV", "origin": f"Last {recently_added_days} days",
         "media_type": ["Series"], "item_count": recent_series},
        {"key": "builtin:downloaded_movies", "name": "Downloaded Movies",
         "tag": "Downloaded Movies", "origin": "From Radarr",
         "media_type": ["Movie"], "item_count": dl_movies},
        {"key": "builtin:downloaded_tv", "name": "Downloaded TV",
         "tag": "Downloaded TV", "origin": "From Sonarr",
         "media_type": ["Series"], "item_count": dl_series},
    ]
    # Per-user downloads playlist — items this user requested via Tentacle UI
    if user_id is not None:
        req_user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
        if req_user:
            my_dl_count = db.query(func.count(DownloadRequest.id)).filter(
                DownloadRequest.user_id == user_id,
            ).scalar() or 0
            if my_dl_count:
                user_tag = f"{req_user.display_name}'s Downloads"
                builtins.append({
                    "key": "builtin:my_downloads",
                    "name": f"{req_user.display_name}'s Downloads",
                    "tag": user_tag,
                    "origin": f"Requested by {req_user.display_name}",
                    "media_type": ["Movie", "Series"],
                    "item_count": my_dl_count,
                })

    for b in builtins:
        b["category"] = "builtin"
    results.extend(builtins)

    # ── Resolve enabled state from per-user DB toggles ──
    toggle_query = db.query(AutoPlaylistToggle)
    if user_id is not None:
        toggle_query = toggle_query.filter(AutoPlaylistToggle.user_id == user_id)
    toggles = {t.key: t.enabled for t in toggle_query.all()}
    for r in results:
        if r["category"] == "list":
            # Lists use their own playlist_enabled field
            r["enabled"] = r.pop("playlist_enabled", False)
        else:
            r["enabled"] = toggles.get(r["key"], False)

    return results


@router.get("/auto-playlists")
def list_auto_playlists(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Return all possible auto playlists with per-user enabled state."""
    return {"auto_playlists": _compute_auto_playlists(db, user_id=user.id)}


class AutoPlaylistToggleRequest(BaseModel):
    key: str
    enabled: bool


@router.post("/auto-playlists/toggle")
def toggle_auto_playlist(req: AutoPlaylistToggleRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Toggle a per-user auto playlist on/off. Triggers sync to Jellyfin."""
    # List playlists use ListSubscription.playlist_enabled
    if req.key.startswith("list:"):
        list_id = int(req.key.replace("list:", ""))
        lst = db.query(ListSubscription).filter(
            ListSubscription.id == list_id,
            ListSubscription.user_id == user.id,
        ).first()
        if not lst:
            return {"success": False, "message": "List not found"}
        lst.playlist_enabled = req.enabled
        db.commit()
    else:
        # Source / built-in playlists use per-user AutoPlaylistToggle
        toggle = db.query(AutoPlaylistToggle).filter(
            AutoPlaylistToggle.key == req.key,
            AutoPlaylistToggle.user_id == user.id,
        ).first()
        if toggle:
            toggle.enabled = req.enabled
        else:
            db.add(AutoPlaylistToggle(key=req.key, enabled=req.enabled, user_id=user.id))
        db.commit()

    # Fast sync: only this one playlist (not all 20+)
    jellyfin_error = None
    try:
        result = toggle_auto_playlist_fast(db, user.id, req.key, req.enabled)
        if result.get("error"):
            jellyfin_error = result["error"]
    except Exception as e:
        logger.error(f"Auto playlist toggle failed: {e}")
        jellyfin_error = str(e)

    action = "enabled" if req.enabled else "disabled"
    return {
        "success": True,
        "message": f"Playlist {action}",
        "enabled": req.enabled,
        "jellyfin_error": jellyfin_error,
    }

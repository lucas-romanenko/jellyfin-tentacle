"""
Tentacle - SmartLists Router
Manage per-user Jellyfin SmartList config files.
"""

import json
import logging
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, Request
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
    _user_smartlists_path,
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


def _home_config_path(user: TentacleUser) -> Path:
    """Return per-user home config file path."""
    d = Path(HOME_CONFIG_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{user.jellyfin_user_id}.json"


def _read_home_json(user: TentacleUser) -> dict:
    """Read per-user home config from disk. Returns empty dict if missing."""
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
    except Exception:
        return {}


def _write_home_json(user: TentacleUser, config: dict):
    """Write per-user home config to disk."""
    p = _home_config_path(user)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config, indent=2), encoding="utf-8")


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


@router.post("/sync")
def sync(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Run per-user SmartLists sync to disk."""
    return sync_smartlists(db, user_id=user.id)


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


@router.post("/notify")
def notify(db: Session = Depends(get_db)):
    """Notify the Jellyfin plugin to reload. Does NOT touch the JSON."""
    _notify_jellyfin_plugin(db)
    return {"success": True}


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

    # Append any leftover rows not in the request
    for row in rows_by_key.values():
        row["order"] = len(new_rows) + 1
        new_rows.append(row)

    config["rows"] = new_rows
    _write_home_json(user, config)
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
    config = _read_home_json(user)
    if not config:
        config = {"hero": {"enabled": False, "playlist_id": "", "display_name": ""}, "rows": []}

    if req.section_id:
        # Adding a built-in Jellyfin section
        builtin = BUILTIN_MAP.get(req.section_id)
        if not builtin:
            return {"success": False, "message": "Unknown built-in section"}
        if any(r.get("type") == "builtin" and r.get("section_id") == req.section_id for r in config["rows"]):
            return {"success": False, "message": "Already in home screen"}
        config["rows"].append({
            "type": "builtin",
            "section_id": req.section_id,
            "display_name": builtin["display_name"],
            "order": len(config["rows"]) + 1,
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
        config["rows"].append({
            "type": "playlist",
            "playlist_id": req.playlist_id,
            "display_name": match["name"],
            "order": len(config["rows"]) + 1,
            "max_items": home_row_limit,
        })
    else:
        return {"success": False, "message": "Must provide playlist_id or section_id"}

    _write_home_json(user, config)
    return {"success": True, "rows": len(config["rows"])}


class RemoveRowRequest(BaseModel):
    row_key: Optional[str] = None  # "playlist:<id>" or "builtin:<section_id>"
    playlist_id: Optional[str] = None  # backwards compat


@router.post("/remove-row")
def remove_row(req: RemoveRowRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Remove a row from the home config."""
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
    return {"success": True, "rows": len(config["rows"])}


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
    return {"success": True, "max_items": val}


@router.post("/hero")
def set_hero(req: HeroPickRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Read JSON, update hero, write JSON back. That's it."""
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
        }
    else:
        config["hero"] = {"enabled": False, "playlist_id": "", "display_name": "", "sort_by": "random", "sort_order": "Descending", "require_logo": True}

    _write_home_json(user, config)
    logger.info(f"Updated hero: {req.playlist_id or '(disabled)'}")
    return {"success": True}


class HeroSortRequest(BaseModel):
    sort_by: str
    sort_order: str
    require_logo: Optional[bool] = None


@router.post("/hero-sort")
def set_hero_sort(req: HeroSortRequest, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Update hero spotlight sort order."""
    config = _read_home_json(user)
    hero = config.get("hero", {})
    if not hero.get("enabled"):
        return {"success": False, "message": "Hero is not enabled"}

    hero["sort_by"] = req.sort_by
    hero["sort_order"] = req.sort_order
    if req.require_logo is not None:
        hero["require_logo"] = req.require_logo
    config["hero"] = hero
    _write_home_json(user, config)
    logger.info(f"Updated hero sort: {req.sort_by} {req.sort_order}")
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

    builtins = [
        {"key": "builtin:recently_added_movies", "name": "Recently Added Movies",
         "tag": "Recently Added Movies", "origin": f"Last {recently_added_days} days",
         "media_type": ["Movie"], "item_count": recent_movies},
        {"key": "builtin:recently_added_tv", "name": "Recently Added TV",
         "tag": "Recently Added TV", "origin": f"Last {recently_added_days} days",
         "media_type": ["Series"], "item_count": recent_series},
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
    import threading

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

    # Background sync to Jellyfin (per-user)
    target_user_id = user.id

    def _sync_bg():
        from models.database import SessionLocal
        bg_db = SessionLocal()
        try:
            sync_smartlists(bg_db, user_id=target_user_id)
            _notify_jellyfin_plugin(bg_db)
        except Exception as e:
            logger.error(f"Auto playlist sync failed: {e}")
        finally:
            bg_db.close()

    thread = threading.Thread(target=_sync_bg, daemon=True)
    thread.start()

    action = "enabled" if req.enabled else "disabled"
    return {"success": True, "message": f"Playlist {action}", "enabled": req.enabled}

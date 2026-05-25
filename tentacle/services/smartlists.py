"""
Tentacle - SmartLists Service
Creates and syncs per-user Jellyfin SmartList config files to disk.
Also generates per-user home configs for the Tentacle Jellyfin plugin.
"""

import uuid
import json
import shutil
import logging
import threading
import requests
from pathlib import Path
from sqlalchemy.orm import Session

from models.database import get_setting, TagRule, TentacleUser, DownloadRequest, Movie, Series

logger = logging.getLogger(__name__)

# Prevent concurrent playlist refreshes (webhooks can fire simultaneously)
_playlist_refresh_lock = threading.Lock()

# In-memory playlist version counter — bumped whenever playlists are modified.
# Polled by the Jellyfin plugin JS to detect changes and live-update home rows.
_playlist_version = 0
_playlist_version_lock = threading.Lock()


def bump_playlist_version():
    """Increment the playlist version counter. Called after any playlist modification."""
    global _playlist_version
    with _playlist_version_lock:
        _playlist_version += 1
    return _playlist_version


def get_playlist_version() -> int:
    """Return the current playlist version counter."""
    return _playlist_version


PRESERVED_FIELDS = ["LastRefreshed", "DateCreated", "ItemCount", "Order"]

# Tag rule condition fields that map directly to Jellyfin API filters
NATIVE_FIELDS = {"genre", "rating", "year"}
# Fields that require Tentacle-applied tags (queried via tag filter)
TENTACLE_FIELDS = {"source", "source_tag", "list", "downloaded", "runtime"}


def _extract_source_value(conditions: list) -> str | None:
    """If the conditions contain a source or source_tag equals condition,
    return the value. The tagger appends a type suffix (Movies/TV) to these."""
    for cond in conditions:
        field = cond.get("field", "")
        if field in ("source", "source_tag") and cond.get("operator") == "equals":
            return cond.get("value", "")
    return None


def _classify_conditions(conditions: list) -> str:
    """Classify tag rule conditions as 'native' (can query Jellyfin directly),
    'tentacle' (requires Tentacle tags), or 'mixed'."""
    if not conditions:
        return "tentacle"
    fields = {c.get("field", "") for c in conditions}
    if fields <= NATIVE_FIELDS:
        return "native"
    if fields.isdisjoint(NATIVE_FIELDS):
        return "tentacle"
    return "mixed"


def _conditions_to_expressions(conditions: list) -> list:
    """Convert tag rule conditions to Jellyfin-native SmartList expressions."""
    expressions = []
    op_map = {"greater_than": "GreaterThan", "less_than": "LessThan", "equals": "Equals", "contains": "Contains"}
    for cond in conditions:
        field = cond.get("field", "")
        operator = cond.get("operator", "")
        value = cond.get("value", "")
        mapped_op = op_map.get(operator)
        if not mapped_op:
            continue

        if field == "genre":
            expressions.append({"MemberName": "Genres", "Operator": mapped_op, "TargetValue": value})
        elif field == "rating":
            expressions.append({"MemberName": "CommunityRating", "Operator": mapped_op, "TargetValue": value})
        elif field == "year":
            expressions.append({"MemberName": "ProductionYear", "Operator": mapped_op, "TargetValue": value})
    return expressions


# ── Per-user SmartLists paths ────────────────────────────────────────────────

def _user_smartlists_path(db: Session, user_id: int) -> Path:
    """Return per-user SmartLists directory: /data/smartlists/{jellyfin_user_id}/"""
    user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
    if not user:
        raise ValueError(f"TentacleUser id={user_id} not found")
    base = Path(get_setting(db, "smartlists_path", "/data/smartlists"))
    return base / user.jellyfin_user_id


def _get_jellyfin_user_id(db: Session, user_id: int) -> str:
    """Resolve TentacleUser.id to jellyfin_user_id string."""
    user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
    return user.jellyfin_user_id if user else ""


def _build_config(name: str, tag: str, media_types: list, folder_id: str,
                  enabled: bool = True, jellyfin_user_id: str = "",
                  expressions: list = None, sort_by: str = "ReleaseDate") -> dict:
    user_playlists = [{"UserId": jellyfin_user_id, "JellyfinPlaylistId": ""}] if jellyfin_user_id else []

    if expressions is None:
        expressions = [{"MemberName": "Tags", "Operator": "Contains", "TargetValue": tag}]

    return {
        "Public": False,
        "UserPlaylists": user_playlists,
        "Type": "Playlist",
        "Id": folder_id,
        "Name": name,
        "FileName": "config.json",
        "CreatedByUserId": jellyfin_user_id,
        "ExpressionSets": [
            {
                "Expressions": expressions,
                "MaxItems": None,
            }
        ],
        "Order": {
            "SortOptions": [
                {
                    "SortBy": sort_by,
                    "SortOrder": "Descending",
                }
            ]
        },
        "MediaTypes": media_types,
        "IncludeExtras": False,
        "Enabled": enabled,
        "MaxItems": 500,
        "MaxPlayTimeMinutes": 0,
        "AutoRefresh": "OnLibraryChanges",
        "Schedules": [],
        "VisibilitySchedules": [],
        "SimilarityComparisonFields": [],
    }


def _find_jellyfin_playlist(name: str, user_id: str, jellyfin_url: str, jellyfin_key: str) -> str:
    """Find an existing Jellyfin playlist by exact name for a user. Returns playlist ID or empty string."""
    try:
        r = requests.get(
            f"{jellyfin_url.rstrip('/')}/Users/{user_id}/Items",
            headers={"X-Emby-Token": jellyfin_key},
            params={
                "IncludeItemTypes": "Playlist",
                "Recursive": "true",
                "SearchTerm": name,
            },
            timeout=10,
        )
        r.raise_for_status()
        for item in r.json().get("Items", []):
            if item.get("Name") == name:
                logger.info(f"[SmartLists] Found existing Jellyfin playlist '{name}' (ID: {item['Id']})")
                return item["Id"]
    except Exception as e:
        logger.debug(f"Could not search for playlist '{name}': {e}")
    return ""


def _create_jellyfin_playlist(name: str, user_id: str, jellyfin_url: str, jellyfin_key: str) -> str:
    """Find or create a private Jellyfin playlist owned by user_id."""
    # First check if a playlist with this name already exists — avoids duplicates
    existing_id = _find_jellyfin_playlist(name, user_id, jellyfin_url, jellyfin_key)
    if existing_id:
        return existing_id

    try:
        r = requests.post(
            f"{jellyfin_url.rstrip('/')}/Playlists",
            headers={
                "X-Emby-Token": jellyfin_key,
                "Content-Type": "application/json",
            },
            json={
                "Name": name,
                "UserId": user_id,
                "MediaType": "Unknown",
                "IsPublic": False,
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("Id", "")
    except Exception as e:
        logger.warning(f"Could not create Jellyfin playlist '{name}' for user {user_id}: {e}")
        return ""


def get_desired_smartlists(db: Session, user_id: int = None) -> list:
    """Build the full list of SmartList definitions from:
    1. Enabled auto playlists (source, list, built-in) — filtered by user
    2. Custom playlists (tag rules) — filtered by user

    If user_id is None, returns the union across all users (legacy compat).
    """
    from models.database import ListSubscription, ListItem, AutoPlaylistToggle, Movie, Series
    smartlists = []
    existing_tags = set()

    # ── Auto playlists (source-based, from enabled toggles) ──
    toggle_query = db.query(AutoPlaylistToggle)
    if user_id is not None:
        toggle_query = toggle_query.filter(AutoPlaylistToggle.user_id == user_id)
    toggles = {t.key: t.enabled for t in toggle_query.all()}

    # Source playlists from VOD content
    movie_tags = db.query(Movie.source_tag).filter(
        Movie.source_tag.isnot(None), Movie.source_tag != "",
        Movie.source != "radarr",
    ).distinct().all()
    for (source_tag,) in movie_tags:
        key = f"source:{source_tag}:movies"
        tag = f"{source_tag} Movies"
        if toggles.get(key) and tag not in existing_tags:
            smartlists.append({"name": tag, "tag": tag, "media_type": ["Movie"], "enabled": True, "source": "auto"})
            existing_tags.add(tag)

    series_tags = db.query(Series.source_tag).filter(
        Series.source_tag.isnot(None), Series.source_tag != "",
        Series.source != "sonarr",
    ).distinct().all()
    for (source_tag,) in series_tags:
        key = f"source:{source_tag}:series"
        tag = f"{source_tag} TV"
        if toggles.get(key) and tag not in existing_tags:
            smartlists.append({"name": tag, "tag": tag, "media_type": ["Series"], "enabled": True, "source": "auto"})
            existing_tags.add(tag)

    # Built-in playlists — (name, media_types, default_sort, max_items or None)
    # default_sort: applied on initial creation. User can always change sort from the dashboard.
    # Once the user sets a sort via the dashboard, it's preserved across syncs via PRESERVED_FIELDS.
    builtin_map = {
        "builtin:recently_added_movies": ("Recently Added Movies", ["Movie"], "DateCreated", 50),
        "builtin:recently_added_tv": ("Recently Added TV", ["Series"], "DateCreated", 50),
        "builtin:downloaded_movies": ("Downloaded Movies", ["Movie"], "DateCreated", None),
        "builtin:downloaded_tv": ("Downloaded TV", ["Series"], "DateCreated", None),
    }
    for bkey, (bname, bmedia, bdefault_sort, bmax) in builtin_map.items():
        if toggles.get(bkey) and bname not in existing_tags:
            sl = {"name": bname, "tag": bname, "media_type": bmedia, "enabled": True, "source": "auto"}
            if bdefault_sort:
                sl["default_sort"] = bdefault_sort
            if bmax:
                sl["max_items"] = bmax
            smartlists.append(sl)
            existing_tags.add(bname)

    # Per-user downloads playlist — dynamic tag based on user display name
    if user_id is not None and toggles.get("builtin:my_downloads"):
        req_user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
        if req_user:
            has_requests = db.query(DownloadRequest.id).filter(
                DownloadRequest.user_id == user_id,
            ).first()
            if has_requests:
                user_tag = f"{req_user.display_name}'s Downloads"
                if user_tag not in existing_tags:
                    smartlists.append({
                        "name": user_tag, "tag": user_tag,
                        "media_type": ["Movie", "Series"], "enabled": True, "source": "auto",
                        "default_sort": "DateCreated",
                    })
                    existing_tags.add(user_tag)

    # ── List playlists (use ListSubscription.playlist_enabled) ──
    list_query = db.query(ListSubscription).filter(
        ListSubscription.playlist_enabled == True,
        ListSubscription.active == True,
    )
    if user_id is not None:
        list_query = list_query.filter(ListSubscription.user_id == user_id)
    enabled_lists = list_query.all()
    for lst in enabled_lists:
        if lst.tag in existing_tags:
            continue
        item_types = db.query(ListItem.media_type).filter(
            ListItem.list_id == lst.id,
            ListItem.media_type.isnot(None),
        ).distinct().all()
        types = {t[0] for t in item_types if t[0]}
        if types == {"movie"}:
            media = ["Movie"]
        elif types == {"series"}:
            media = ["Series"]
        else:
            media = ["Movie", "Series"]

        smartlists.append({
            "name": lst.tag, "tag": lst.tag, "media_type": media,
            "enabled": True, "source": "list",
        })
        existing_tags.add(lst.tag)

    # ── Custom playlists from tag rules ──
    rule_query = db.query(TagRule).filter(TagRule.active == True)
    if user_id is not None:
        rule_query = rule_query.filter(TagRule.user_id == user_id)
    active_rules = rule_query.all()
    for rule in active_rules:
        if rule.output_tag in existing_tags:
            continue
        media = ["Movie", "Series"]
        if rule.apply_to == "movies":
            media = ["Movie"]
        elif rule.apply_to == "series":
            media = ["Series"]
        # Compute the correct tag for Jellyfin queries.
        # Source/source_tag conditions need the media type suffix because the tagger
        # writes tags like "Netflix Movies" / "Netflix TV", not just "Netflix".
        tag = rule.output_tag
        source_value = _extract_source_value(rule.conditions or [])
        if source_value and len(media) == 1:
            type_suffix = "Movies" if media == ["Movie"] else "TV"
            tag = f"{source_value} {type_suffix}"

        sl_entry = {"name": rule.output_tag, "tag": tag, "media_type": media, "enabled": True, "source": "custom"}
        # If all conditions are Jellyfin-native (genre/rating/year),
        # query Jellyfin directly instead of going through Tentacle tags
        classification = _classify_conditions(rule.conditions or [])
        if classification == "native":
            sl_entry["expressions"] = _conditions_to_expressions(rule.conditions)
        smartlists.append(sl_entry)
        existing_tags.add(rule.output_tag)

    return smartlists


def _scan_existing(smartlists_path: Path) -> dict:
    """Scan existing SmartList folders and return {name: (folder_path, config_data)}."""
    existing = {}
    if not smartlists_path.exists():
        return existing
    for folder in smartlists_path.iterdir():
        if not folder.is_dir():
            continue
        config_file = folder / "config.json"
        if config_file.exists():
            try:
                data = json.loads(config_file.read_text(encoding="utf-8"))
                name = data.get("Name", "")
                if name:
                    existing[name] = (folder, data)
            except Exception:
                continue
    return existing


_BUILTIN_DEFAULT_SORT = {
    "Recently Added Movies": "DateCreated",
    "Recently Added TV": "DateCreated",
    "Downloaded Movies": "DateCreated",
    "Downloaded TV": "DateCreated",
}


def _migrate_builtin_sort_defaults(existing: dict, smartlists_path: Path):
    """One-time migration: fix built-in playlists created before default_sort was added.
    If a built-in playlist has ReleaseDate sort and no _sort_migrated flag, update to DateCreated."""
    # Also migrate per-user downloads playlists ("{Name}'s Downloads")
    migrate_targets = dict(_BUILTIN_DEFAULT_SORT)
    for name in existing:
        if name.endswith("'s Downloads"):
            migrate_targets[name] = "DateCreated"

    for name, expected_sort in migrate_targets.items():
        if name not in existing:
            continue
        folder, config = existing[name]
        if config.get("_sort_migrated"):
            continue
        order = config.get("Order", {})
        sort_opts = order.get("SortOptions", [])
        current_sort = sort_opts[0].get("SortBy") if sort_opts else None
        if current_sort == "ReleaseDate":
            config["Order"] = {"SortOptions": [{"SortBy": expected_sort, "SortOrder": "Descending"}]}
            logger.info(f"[SmartLists] Migrated sort for '{name}': ReleaseDate → {expected_sort}")
        config["_sort_migrated"] = True
        config_file = folder / "config.json"
        config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")


def migrate_global_smartlists_to_user(db: Session, user_id: int):
    """One-time migration: move existing global /data/smartlists/* configs
    into the admin user's per-user directory. Only runs if the user's
    per-user directory doesn't exist yet and the global dir has content."""
    try:
        user_path = _user_smartlists_path(db, user_id)
    except ValueError:
        return

    if user_path.exists():
        return  # Already migrated

    global_path = Path(get_setting(db, "smartlists_path", "/data/smartlists"))
    if not global_path.exists():
        return

    # Check if there are SmartList folders directly in the global path
    # (not in per-user subdirs — those would be jellyfin_user_id dirs)
    configs_to_move = []
    for item in global_path.iterdir():
        if item.is_dir() and (item / "config.json").exists():
            configs_to_move.append(item)

    if not configs_to_move:
        return

    user_path.mkdir(parents=True, exist_ok=True)
    moved = 0
    for folder in configs_to_move:
        dest = user_path / folder.name
        try:
            shutil.move(str(folder), str(dest))
            moved += 1
        except Exception as e:
            logger.warning(f"Failed to migrate SmartList folder {folder.name}: {e}")

    if moved:
        logger.info(f"Migrated {moved} global SmartList configs to user dir {user_path}")


def sync_smartlists(db: Session, user_id: int = None) -> dict:
    """Sync per-user SmartList config files to disk. Returns {created, updated, total}.

    Each user gets their own SmartList directory and Jellyfin playlists (IsPublic=false).
    Does NOT write home config — call write_home_config() separately per-user.

    If user_id is None, syncs for all users.
    """
    if user_id is None:
        # Sync for all users
        users = db.query(TentacleUser).all()
        if not users:
            return {"created": 0, "updated": 0, "removed": 0, "total": 0}
        combined = {"created": 0, "updated": 0, "removed": 0, "total": 0}
        for u in users:
            result = sync_smartlists(db, user_id=u.id)
            for key in ("created", "updated", "removed", "total"):
                combined[key] += result.get(key, 0)
        # Artwork sync is global (once after all users)
        try:
            from routers.collections import sync_playlist_artwork
            combined["artwork"] = sync_playlist_artwork(db)
        except Exception as e:
            logger.warning(f"Artwork sync failed: {e}")
        return combined

    # Single-user sync
    try:
        smartlists_path = _user_smartlists_path(db, user_id)
    except ValueError as e:
        return {"created": 0, "updated": 0, "total": 0, "error": str(e)}

    if not smartlists_path.exists():
        try:
            smartlists_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Cannot create SmartLists path {smartlists_path}: {e}")
            return {"created": 0, "updated": 0, "total": 0, "error": str(e)}

    desired = get_desired_smartlists(db, user_id=user_id)
    existing = _scan_existing(smartlists_path)
    jf_user_id = _get_jellyfin_user_id(db, user_id)
    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")

    # One-time migration: fix built-in playlists stuck with ReleaseDate sort
    # that should default to DateCreated (created before default_sort was added)
    _migrate_builtin_sort_defaults(existing, smartlists_path)

    created = 0
    updated = 0
    changed_names = []  # Track which playlists were created or need refresh

    for sl in desired:
        name = sl["name"]
        tag = sl["tag"]
        media_types = sl["media_type"]
        enabled = sl["enabled"]
        expressions = sl.get("expressions")  # None for tag-based, list for native
        default_sort = sl.get("default_sort")  # Default sort for new playlists (user can change)
        forced_max = sl.get("max_items")  # Built-in playlists can cap item count

        if name in existing:
            # Update existing
            folder, old_data = existing[name]
            folder_id = old_data.get("Id", str(uuid.uuid4()))
            config = _build_config(name, tag, media_types, folder_id, enabled, jf_user_id, expressions=expressions,
                                   sort_by=default_sort or "ReleaseDate")

            # Preserve user-managed fields from existing config
            for field in PRESERVED_FIELDS:
                if field in old_data:
                    config[field] = old_data[field]

            # Built-in playlists with forced max items always override
            if forced_max:
                config["MaxItems"] = forced_max

            # Preserve UserPlaylists entries that have a linked JellyfinPlaylistId
            old_playlists = old_data.get("UserPlaylists", [])
            for entry in old_playlists:
                if entry.get("JellyfinPlaylistId"):
                    config["UserPlaylists"] = old_playlists
                    break
            else:
                # No linked playlists — create one if we have Jellyfin credentials
                if jf_user_id and jellyfin_url and jellyfin_key:
                    playlist_id = _create_jellyfin_playlist(name, jf_user_id, jellyfin_url, jellyfin_key)
                    if playlist_id:
                        config["UserPlaylists"] = [{"UserId": jf_user_id, "JellyfinPlaylistId": playlist_id}]

            # Detect if expressions changed (custom playlist was edited)
            old_exprs = old_data.get("ExpressionSets", [])
            new_exprs = config.get("ExpressionSets", [])
            if old_exprs != new_exprs or old_data.get("MediaTypes") != config.get("MediaTypes"):
                changed_names.append(name)

            config_file = folder / "config.json"
            config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
            updated += 1
        else:
            # Create new folder
            folder_id = str(uuid.uuid4())
            folder = smartlists_path / folder_id
            folder.mkdir(parents=True, exist_ok=True)

            config = _build_config(name, tag, media_types, folder_id, enabled, jf_user_id, expressions=expressions,
                                   sort_by=default_sort or "ReleaseDate")

            if forced_max:
                config["MaxItems"] = forced_max

            # Create private Jellyfin playlist for this user
            if jf_user_id and jellyfin_url and jellyfin_key:
                playlist_id = _create_jellyfin_playlist(name, jf_user_id, jellyfin_url, jellyfin_key)
                if playlist_id:
                    config["UserPlaylists"] = [{"UserId": jf_user_id, "JellyfinPlaylistId": playlist_id}]

            config_file = folder / "config.json"
            config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
            changed_names.append(name)
            created += 1

    # Clean up orphaned SmartList folders (deleted tag rules)
    desired_names = {sl["name"] for sl in desired}
    orphaned = {name: (folder, data) for name, (folder, data) in existing.items() if name not in desired_names}
    removed = 0

    # Safety check: if more than half of existing playlists would be removed,
    # something is likely wrong (DB issue, toggle reset, etc.) — skip cleanup
    if orphaned and existing and len(orphaned) > len(existing) / 2:
        logger.warning(
            f"Skipping orphan cleanup for user {user_id}: {len(orphaned)}/{len(existing)} playlists would be removed "
            f"— likely a transient issue, not intentional deletions. "
            f"Orphans: {list(orphaned.keys())}"
        )
    else:
        for name, (folder, data) in orphaned.items():
            # Delete the Jellyfin playlist if it exists
            playlist_id = None
            for entry in (data.get("UserPlaylists") or []):
                if entry.get("JellyfinPlaylistId"):
                    playlist_id = entry["JellyfinPlaylistId"]
                    break
            if playlist_id and jellyfin_url and jellyfin_key:
                try:
                    from services.jellyfin import JellyfinService
                    jf = JellyfinService(jellyfin_url, jellyfin_key, jf_user_id)
                    jf.delete_item(playlist_id)
                except Exception as e:
                    logger.warning(f"Could not delete Jellyfin playlist for '{name}': {e}")

            # Remove the folder from disk
            try:
                shutil.rmtree(folder)
                logger.info(f"Removed orphaned SmartList folder: {name}")
                removed += 1
            except Exception as e:
                logger.warning(f"Could not remove folder for '{name}': {e}")

    logger.info(f"SmartLists sync (user {user_id}): {created} created, {updated} updated, {removed} removed, {len(desired)} total")

    return {
        "created": created, "updated": updated, "removed": removed, "total": len(desired),
        "changed_names": changed_names,
    }


def _get_smartlists_with_playlist_ids(db: Session, user_id: int = None) -> list:
    """Scan existing per-user SmartList configs and return those with a non-empty JellyfinPlaylistId."""
    if user_id is not None:
        try:
            smartlists_path = _user_smartlists_path(db, user_id)
        except ValueError:
            return []
    else:
        smartlists_path = Path(get_setting(db, "smartlists_path", "/data/smartlists"))
    existing = _scan_existing(smartlists_path)
    result = []
    for name, (_folder, data) in existing.items():
        playlist_id = ""
        user_playlists = data.get("UserPlaylists", [])
        for entry in user_playlists:
            if entry.get("JellyfinPlaylistId"):
                playlist_id = entry["JellyfinPlaylistId"]
                break
        if not playlist_id:
            continue
        result.append({
            "name": name,
            "playlist_id": playlist_id,
            "media_types": data.get("MediaTypes", []),
            "enabled": data.get("Enabled", True),
        })
    return result


def _user_home_config_path(db: Session, user_id: int = None) -> Path:
    """Return per-user home config path, or legacy global path if no user."""
    if user_id is not None:
        user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
        if user:
            d = Path("/data/home-configs")
            d.mkdir(parents=True, exist_ok=True)
            return d / f"{user.jellyfin_user_id}.json"
    return Path(get_setting(db, "home_config_path", "/data/tentacle-home.json"))


def write_home_config(db: Session, user_id: int = None) -> dict:
    """Generate and write per-user home config based on current SmartLists.

    Preserves existing row order, hero pick, and built-in Jellyfin sections.
    Only playlist rows are validated against disk (built-in sections are always kept).
    Returns the config dict that was written.
    """
    home_row_limit = int(get_setting(db, "home_row_limit", "20") or "20")
    smartlists = _get_smartlists_with_playlist_ids(db, user_id=user_id)

    if not smartlists:
        logger.warning(f"No SmartLists with JellyfinPlaylistId found for user {user_id}, skipping home config write")
        return {}

    existing_config = get_home_config(db, user_id=user_id)
    existing_rows = existing_config.get("rows", []) if existing_config else []
    existing_hero = existing_config.get("hero") if existing_config else None

    # Set of playlist_ids from the disk scan (current truth)
    current_ids = {sl["playlist_id"] for sl in smartlists}
    # Lookup for display names and reverse lookup by name
    name_by_id = {sl["playlist_id"]: sl["name"] for sl in smartlists}
    id_by_name = {sl["name"]: sl["playlist_id"] for sl in smartlists}

    # Start with existing rows (in their saved order)
    rows = []
    for r in existing_rows:
        if r.get("type") == "builtin":
            # Always keep built-in sections
            rows.append(r)
        elif r.get("playlist_id") in current_ids:
            # Keep playlist rows that still exist on disk, ensure type field
            r["type"] = "playlist"
            r["display_name"] = name_by_id.get(r["playlist_id"], r["display_name"])
            rows.append(r)
        elif r.get("display_name") and r["display_name"] in id_by_name:
            # Playlist was recreated with a new ID — remap the reference
            new_id = id_by_name[r["display_name"]]
            logger.info(f"Home config: remapping '{r['display_name']}' from {r.get('playlist_id')} to {new_id}")
            r["playlist_id"] = new_id
            r["type"] = "playlist"
            rows.append(r)

    # Safety check: if we'd drop more than half the playlist rows, something is wrong
    existing_playlist_rows = [r for r in existing_rows if r.get("type") != "builtin"]
    if existing_playlist_rows and len(rows) < len(existing_rows) / 2:
        logger.warning(
            f"Home config safety: would drop from {len(existing_rows)} to {len(rows)} rows "
            f"— keeping existing config to prevent data loss"
        )
        return existing_config

    # No auto-bootstrap: users add rows manually via the Home Screen page.

    # Renumber and set max_items for playlist rows
    for i, r in enumerate(rows, start=1):
        r["order"] = i
        if r.get("type", "playlist") == "playlist":
            r.setdefault("max_items", home_row_limit)

    # Hero: preserve existing pick, remap if playlist was recreated, disable if gone
    if existing_hero and existing_hero.get("playlist_id") in current_ids:
        hero = existing_hero
    elif existing_hero and existing_hero.get("display_name") and existing_hero["display_name"] in id_by_name:
        # Hero playlist was recreated with a new ID — remap
        new_id = id_by_name[existing_hero["display_name"]]
        logger.info(f"Home config: remapping hero '{existing_hero['display_name']}' from {existing_hero.get('playlist_id')} to {new_id}")
        existing_hero["playlist_id"] = new_id
        hero = existing_hero
    else:
        hero = {"enabled": False, "playlist_id": "", "display_name": "", "sort_by": "random", "sort_order": "Descending", "require_logo": True, "require_trailer": False, "item_count": 10}

    # Toolbar: preserve existing config or use defaults
    existing_toolbar = existing_config.get("toolbar") if existing_config else None
    if not existing_toolbar:
        existing_toolbar = [
            {"id": "search", "enabled": True},
            {"id": "discover", "enabled": True},
            {"id": "activity", "enabled": True},
            {"id": "favorites", "enabled": True},
            {"id": "libraries", "enabled": True},
        ]

    config = {
        "hero": hero,
        "rows": rows,
        "toolbar": existing_toolbar,
    }

    try:
        path = _user_home_config_path(db, user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        logger.info(f"Wrote home config with {len(rows)} rows to {path}")
    except Exception as e:
        logger.error(f"Failed to write home config: {e}")
        return {}

    # If user has Tentacle home rows, disable Jellyfin's built-in home sections
    # to prevent overlap between the two home screens
    if rows:
        try:
            from services.jellyfin import JellyfinService
            jellyfin_url = get_setting(db, "jellyfin_url", "")
            jellyfin_key = get_setting(db, "jellyfin_api_key", "")
            jf_user_id = _get_jellyfin_user_id(db, user_id)
            if jellyfin_url and jellyfin_key and jf_user_id:
                jf = JellyfinService(jellyfin_url, jellyfin_key, jf_user_id)
                jf.disable_home_sections()
        except Exception as e:
            logger.debug(f"Could not disable Jellyfin home sections: {e}")

    return config


def get_home_config(db: Session, user_id: int = None) -> dict:
    """Read and return the current per-user home config contents."""
    path = _user_home_config_path(db, user_id)
    if not path.exists():
        # Fall back to legacy global file only for admin (migration from pre-multi-user)
        if user_id is not None:
            user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
            if user and user.is_admin:
                legacy = Path(get_setting(db, "home_config_path", "/data/tentacle-home.json"))
                if legacy.exists():
                    try:
                        return json.loads(legacy.read_text(encoding="utf-8"))
                    except Exception:
                        pass
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to read home config from {path}: {e}")
        return {}



def _notify_jellyfin_plugin(db: Session) -> dict:
    """POST to the Tentacle Jellyfin plugin refresh endpoint. Returns result dict."""
    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")
    if not jellyfin_url or not jellyfin_key:
        return {"notified": False, "error": "Jellyfin not configured"}
    try:
        r = requests.post(
            f"{jellyfin_url.rstrip('/')}/Tentacle/Refresh",
            headers={"X-Emby-Token": jellyfin_key},
            timeout=5,
        )
        if r.ok:
            logger.info("Notified Tentacle Jellyfin plugin to refresh")
            return {"notified": True}
        elif r.status_code == 404:
            return {"notified": False, "error": "Tentacle plugin not installed in Jellyfin"}
        elif r.status_code == 401:
            return {"notified": False, "error": "Jellyfin API key is invalid"}
        else:
            return {"notified": False, "error": f"Jellyfin returned {r.status_code}"}
    except requests.ConnectionError:
        return {"notified": False, "error": f"Cannot reach Jellyfin at {jellyfin_url}"}
    except requests.Timeout:
        return {"notified": False, "error": "Jellyfin connection timed out"}
    except Exception:
        return {"notified": False, "error": "Jellyfin plugin not reachable"}


# ── Playlist Population (replaces C# SmartLists plugin) ─────────────────

def _build_query_params(config: dict) -> dict:
    """Translate SmartList config expressions into Jellyfin query_items() kwargs."""
    params = {
        "include_types": config.get("MediaTypes", ["Movie"]),
        "tags": [],
        "genres": [],
        "years": [],
        "min_rating": None,
        "max_rating": None,
        "sort_by": None,
        "sort_order": "Ascending",
        "limit": None,
        "min_premiere_date": None,
        "max_premiere_date": None,
    }

    # Parse expression sets (mirrors C# ApplyExpression logic)
    for expr_set in config.get("ExpressionSets", []):
        for expr in expr_set.get("Expressions", []):
            member = (expr.get("MemberName") or "").lower()
            operator = (expr.get("Operator") or "").lower()
            value = expr.get("TargetValue", "")

            if member == "tags" and operator == "contains" and value:
                params["tags"].append(value)
            elif member == "genres" and operator == "contains" and value:
                for g in [v.strip() for v in value.split(",") if v.strip()]:
                    params["genres"].append(g)
            elif member in ("productionyear", "year"):
                try:
                    year = int(value)
                    if operator == "equals":
                        params["years"].append(year)
                    elif operator == "greaterthan":
                        params["min_premiere_date"] = f"{year + 1}-01-01T00:00:00Z"
                    elif operator == "lessthan":
                        params["max_premiere_date"] = f"{year - 1}-12-31T23:59:59Z"
                except ValueError:
                    pass
            elif member in ("communityrating", "rating"):
                try:
                    rating = float(value)
                    if operator == "greaterthan":
                        params["min_rating"] = rating
                    elif operator == "lessthan":
                        params["max_rating"] = rating
                except ValueError:
                    pass

    # Sorting
    order = config.get("Order") or {}
    sort_options = order.get("SortOptions") or []
    if sort_options:
        first = sort_options[0]
        sort_by_map = {
            "releasedate": "PremiereDate",
            "name": "SortName",
            "datecreated": "DateCreated",
            "communityrating": "CommunityRating",
            "random": "Random",
        }
        raw = (first.get("SortBy") or "").lower()
        params["sort_by"] = sort_by_map.get(raw, first.get("SortBy", "SortName"))
        params["sort_order"] = first.get("SortOrder", "Ascending")

    # Limit
    max_items = config.get("MaxItems")
    if max_items and max_items > 0:
        params["limit"] = max_items

    # Clean up empty lists
    if not params["tags"]:
        params["tags"] = None
    if not params["genres"]:
        params["genres"] = None
    if not params["years"]:
        params["years"] = None

    return params


def refresh_smartlist_playlists(db: Session, user_id: int = None, only_names: list = None) -> dict:
    """Read per-user SmartList configs from disk, query Jellyfin for matching items,
    and create/update playlists. This replaces the C# SmartLists plugin entirely.

    If user_id is None, refreshes for all users.
    If only_names is provided, only processes playlists with matching names.
    Returns {processed, created, updated, errors}.
    """
    with _playlist_refresh_lock:
        result = _refresh_smartlist_playlists_inner(db, user_id, only_names=only_names)
        if result.get("updated", 0) > 0 or result.get("created", 0) > 0:
            bump_playlist_version()
        return result


def _refresh_smartlist_playlists_inner(db: Session, user_id: int = None, only_names: list = None) -> dict:
    if user_id is None:
        users = db.query(TentacleUser).all()
        if not users:
            return {"processed": 0, "created": 0, "updated": 0, "errors": 0}
        combined = {"processed": 0, "created": 0, "updated": 0, "errors": 0}
        for u in users:
            result = _refresh_smartlist_playlists_inner(db, user_id=u.id, only_names=only_names)
            for key in ("processed", "created", "updated", "errors"):
                combined[key] += result.get(key, 0)
        return combined

    from services.jellyfin import JellyfinService

    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")
    jf_user_id = _get_jellyfin_user_id(db, user_id)

    if not jellyfin_url or not jellyfin_key:
        return {"error": "Jellyfin not configured", "processed": 0}

    # Use the target user's Jellyfin ID so playlist operations are scoped to them
    jf = JellyfinService(jellyfin_url, jellyfin_key, jf_user_id)

    if not jf.test_connection():
        return {"error": "Jellyfin connection failed", "processed": 0}

    try:
        smartlists_path = _user_smartlists_path(db, user_id)
    except ValueError:
        return {"error": "User not found", "processed": 0}

    existing = _scan_existing(smartlists_path)
    if not existing:
        return {"error": "No SmartList configs found on disk", "processed": 0}

    stats = {"processed": 0, "created": 0, "updated": 0, "errors": 0, "item_counts": {}}

    for name, (folder, config) in existing.items():
        if not config.get("Enabled", True) or config.get("Type") != "Playlist":
            continue
        if only_names and name not in only_names:
            continue

        try:
            _process_single_playlist(jf, folder, config, jf_user_id, stats, db=db)
        except Exception as e:
            logger.error(f"[SmartLists] Failed to process '{name}': {e}")
            stats["errors"] += 1

    logger.info(
        f"[SmartLists] Playlist refresh (user {user_id}): {stats['processed']} processed, "
        f"{stats['created']} created, {stats['updated']} updated, "
        f"{stats['errors']} errors"
    )
    return stats


def _resort_by_db_date(items: list, config: dict, db: Session = None) -> list:
    """Re-sort Jellyfin items by Tentacle's date_added when sort is DateCreated.

    Jellyfin's DateCreated is unreliable for bulk-imported content (all items get
    the same timestamp from the library scan). Tentacle's DB tracks actual download
    time, so we use that instead.
    """
    if not db or not items:
        return items

    order = config.get("Order") or {}
    sort_options = order.get("SortOptions") or []
    if not sort_options:
        return items
    sort_by = (sort_options[0].get("SortBy") or "").lower()
    sort_order = sort_options[0].get("SortOrder", "Descending")
    if sort_by != "datecreated":
        return items

    # Build TMDB ID → date_added lookup from Tentacle DB
    media_types = config.get("MediaTypes", [])
    date_map = {}  # jellyfin_item_id -> date_added
    if "Movie" in media_types:
        for m in db.query(Movie.jellyfin_item_id, Movie.date_added).filter(
            Movie.jellyfin_item_id.isnot(None)
        ).all():
            if m.jellyfin_item_id and m.date_added:
                date_map[m.jellyfin_item_id] = m.date_added
    if "Series" in media_types:
        for s in db.query(Series.jellyfin_item_id, Series.date_added).filter(
            Series.jellyfin_item_id.isnot(None)
        ).all():
            if s.jellyfin_item_id and s.date_added:
                date_map[s.jellyfin_item_id] = s.date_added

    if not date_map:
        return items

    # Also try matching by TMDB provider ID for items without jellyfin_item_id match
    tmdb_date_map = {}  # tmdb_id_str -> date_added
    if "Movie" in media_types:
        for m in db.query(Movie.tmdb_id, Movie.date_added).filter(
            Movie.date_added.isnot(None)
        ).all():
            if m.tmdb_id and m.date_added:
                tmdb_date_map[str(m.tmdb_id)] = m.date_added
    if "Series" in media_types:
        for s in db.query(Series.tmdb_id, Series.date_added).filter(
            Series.date_added.isnot(None)
        ).all():
            if s.tmdb_id and s.date_added:
                tmdb_date_map[str(s.tmdb_id)] = s.date_added

    from datetime import datetime
    fallback = datetime.min

    def get_date(item):
        # Try direct jellyfin_item_id match first
        d = date_map.get(item.get("Id"))
        if d:
            return d
        # Fallback to TMDB ID match
        tmdb_id = (item.get("ProviderIds") or {}).get("Tmdb", "")
        return tmdb_date_map.get(tmdb_id, fallback)

    reverse = sort_order == "Descending"
    items.sort(key=get_date, reverse=reverse)
    return items


def _process_single_playlist(jf, folder: Path, config: dict, user_id: str, stats: dict, db: Session = None):
    """Process a single SmartList config: query items, create/update playlist."""
    name = config.get("Name", "Unknown")

    # Query Jellyfin for matching items
    query = _build_query_params(config)
    items = jf.query_items(**query)

    # Jellyfin Genres filter is OR — post-filter to require ALL genres (AND logic)
    required_genres = query.get("genres") or []
    if len(required_genres) > 1:
        required_lower = [g.lower() for g in required_genres]
        items = [
            item for item in items
            if all(
                any(ig.lower() == rg for ig in (item.get("Genres") or []))
                for rg in required_lower
            )
        ]

    items = _resort_by_db_date(items, config, db)
    item_ids = [item["Id"] for item in items]

    logger.info(f"[SmartLists] '{name}': {len(item_ids)} matching items from Jellyfin query")

    # Find existing playlist ID from UserPlaylists or JellyfinPlaylistId
    playlist_id = None
    user_playlists = config.get("UserPlaylists") or []
    for entry in user_playlists:
        if entry.get("JellyfinPlaylistId"):
            playlist_id = entry["JellyfinPlaylistId"]
            break
    if not playlist_id:
        playlist_id = config.get("JellyfinPlaylistId")

    # Verify the playlist still exists in Jellyfin
    if playlist_id:
        existing_item = jf.get_item_by_id(playlist_id)
        if not existing_item:
            logger.warning(f"[SmartLists] Playlist {playlist_id} for '{name}' no longer exists, will create new")
            playlist_id = None

    if playlist_id:
        # Update existing playlist — compare ordered lists to detect changes
        current_entries = jf.get_playlist_items(playlist_id)
        current_ordered_ids = [entry["Id"] for entry in current_entries]

        if current_ordered_ids == item_ids:
            # No changes needed — same items in same order
            logger.info(f"[SmartLists] '{name}': no changes needed ({len(item_ids)} items)")
        else:
            # Items or order changed — clear and re-add in correct order
            if current_entries:
                all_entry_ids = [entry.get("PlaylistItemId", entry["Id"]) for entry in current_entries]
                jf.remove_from_playlist(playlist_id, all_entry_ids)
            if item_ids:
                result = jf.add_to_playlist(playlist_id, item_ids)
                logger.info(f"[SmartLists] '{name}': add_to_playlist returned {result}")

            added = set(item_ids) - set(current_ordered_ids)
            removed = set(current_ordered_ids) - set(item_ids)
            logger.info(f"[SmartLists] Updated '{name}': +{len(added)} -{len(removed)} items (total {len(item_ids)})")

        stats["updated"] += 1
    else:
        # Create new private playlist with items
        playlist_id = jf.create_playlist(name, item_ids if item_ids else None, user_id=user_id, is_public=False)
        if playlist_id:
            # Save playlist ID back to config
            if user_id:
                config["UserPlaylists"] = [{"UserId": user_id, "JellyfinPlaylistId": playlist_id}]
            config["JellyfinPlaylistId"] = playlist_id
            config_file = folder / "config.json"
            config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
            logger.info(f"[SmartLists] Created playlist '{name}' (ID: {playlist_id}) with {len(item_ids)} items")
            stats["created"] += 1
        else:
            logger.error(f"[SmartLists] Failed to create playlist for '{name}'")
            stats["errors"] += 1
            return

    stats["processed"] += 1
    stats["item_counts"][name] = len(item_ids)


VALID_SORT_BY = {"releasedate", "name", "datecreated", "communityrating", "random"}
SORT_BY_DISPLAY = {
    "ReleaseDate": "releasedate",
    "SortName": "name",
    "DateCreated": "datecreated",
    "CommunityRating": "communityrating",
    "Random": "random",
}
# Reverse mapping for config
SORT_BY_TO_CONFIG = {v: k for k, v in SORT_BY_DISPLAY.items()}


def update_playlist_sort(name: str, sort_by: str, sort_order: str, db, user_id: int = None) -> dict:
    """Update sort order for a per-user playlist config on disk and re-populate in Jellyfin."""
    from services.jellyfin import JellyfinService

    if sort_by not in VALID_SORT_BY:
        return {"success": False, "message": f"Invalid sort_by: {sort_by}"}
    if sort_order not in ("Ascending", "Descending"):
        return {"success": False, "message": f"Invalid sort_order: {sort_order}"}

    if user_id is not None:
        try:
            smartlists_path = _user_smartlists_path(db, user_id)
        except ValueError:
            return {"success": False, "message": "User not found"}
    else:
        smartlists_path = Path(get_setting(db, "smartlists_path", "/data/smartlists"))

    existing = _scan_existing(smartlists_path)

    if name not in existing:
        return {"success": False, "message": f"Playlist '{name}' not found on disk"}

    folder, config = existing[name]
    config_sort_by = SORT_BY_TO_CONFIG.get(sort_by, "ReleaseDate")

    # Update sort in config
    config["Order"] = {
        "SortOptions": [{"SortBy": config_sort_by, "SortOrder": sort_order}]
    }
    config_file = folder / "config.json"
    config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
    logger.info(f"[SmartLists] Updated sort for '{name}': {config_sort_by} {sort_order}")

    # Re-populate the Jellyfin playlist with the new sort order
    jellyfin_url = get_setting(db, "jellyfin_url")
    jellyfin_key = get_setting(db, "jellyfin_api_key")
    jf_user_id = _get_jellyfin_user_id(db, user_id) if user_id else get_setting(db, "jellyfin_user_id")

    if jellyfin_url and jellyfin_key and jf_user_id:
        try:
            jf = JellyfinService(jellyfin_url, jellyfin_key, jf_user_id)

            # Find playlist ID
            playlist_id = None
            for entry in config.get("UserPlaylists", []):
                if entry.get("JellyfinPlaylistId"):
                    playlist_id = entry["JellyfinPlaylistId"]
                    break

            if playlist_id:
                # Clear all items and re-add in new sort order
                current_entries = jf.get_playlist_items(playlist_id)
                if current_entries:
                    entry_ids = [e.get("PlaylistItemId", e["Id"]) for e in current_entries]
                    jf.remove_from_playlist(playlist_id, entry_ids)

                # Query items in new sort order and add
                query = _build_query_params(config)
                items = jf.query_items(**query)
                items = _resort_by_db_date(items, config, db)
                item_ids = [item["Id"] for item in items]
                if item_ids:
                    jf.add_to_playlist(playlist_id, item_ids)

                logger.info(f"[SmartLists] Re-populated '{name}' with {len(item_ids)} items in new sort order")
        except Exception as e:
            logger.warning(f"[SmartLists] Failed to re-populate '{name}' after sort change: {e}")

    # Notify all clients of the sort change
    bump_playlist_version()
    _notify_jellyfin_plugin(db)

    return {"success": True, "sort_by": sort_by, "sort_order": sort_order}


def remove_item_from_playlists(db: Session, jellyfin_item_id: str, user_id: int) -> dict:
    """Remove a specific Jellyfin item from all of a user's playlists.

    Much faster than a full refresh — only fetches items for each playlist
    and removes the matching entry. Skips playlists that don't contain the item.
    """
    from services.jellyfin import JellyfinService

    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")
    jf_user_id = _get_jellyfin_user_id(db, user_id)

    if not jellyfin_url or not jellyfin_key or not jf_user_id:
        return {"removed_from": 0, "error": "Jellyfin not configured"}

    jf = JellyfinService(jellyfin_url, jellyfin_key, jf_user_id)

    try:
        smartlists_path = _user_smartlists_path(db, user_id)
    except ValueError:
        return {"removed_from": 0, "error": "User not found"}

    existing = _scan_existing(smartlists_path)
    if not existing:
        return {"removed_from": 0}

    removed_from = 0
    for name, (folder, config) in existing.items():
        if not config.get("Enabled", True) or config.get("Type") != "Playlist":
            continue

        # Find the Jellyfin playlist ID from config
        playlist_id = None
        for up in config.get("UserPlaylists", []):
            if up.get("UserId") == jf_user_id:
                playlist_id = up.get("JellyfinPlaylistId")
                break
        if not playlist_id:
            continue

        try:
            items = jf.get_playlist_items(playlist_id)
            entry_ids = [
                item["PlaylistItemId"] for item in items
                if item.get("Id") == jellyfin_item_id and "PlaylistItemId" in item
            ]
            if entry_ids:
                jf.remove_from_playlist(playlist_id, entry_ids)
                removed_from += 1
                logger.info(f"[SmartLists] Removed item {jellyfin_item_id} from playlist '{name}'")
        except Exception as e:
            logger.warning(f"[SmartLists] Failed to check/remove from '{name}': {e}")

    return {"removed_from": removed_from}


def add_item_to_matching_playlists(db: Session, jellyfin_item_id: str, item_tags: list,
                                    media_type: str) -> dict:
    """Directly add a Jellyfin item to all matching playlists for all users.

    Instead of querying Jellyfin by tag (which requires waiting for indexing),
    this matches the item's known tags against playlist expressions and adds
    the item directly. Much faster and avoids the tag-indexing race condition.
    """
    from services.jellyfin import JellyfinService

    if not jellyfin_item_id or not item_tags:
        return {"added_to": 0}

    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")
    if not jellyfin_url or not jellyfin_key:
        return {"added_to": 0, "error": "Jellyfin not configured"}

    # Normalize media type for matching config MediaTypes
    jf_media_type = "Movie" if media_type == "movie" else "Series"
    item_tags_set = set(item_tags)

    users = db.query(TentacleUser).all()
    total_added = 0

    for user in users:
        try:
            smartlists_path = _user_smartlists_path(db, user.id)
        except ValueError:
            continue

        jf_user_id = _get_jellyfin_user_id(db, user.id)
        if not jf_user_id:
            continue

        jf = JellyfinService(jellyfin_url, jellyfin_key, jf_user_id)
        existing = _scan_existing(smartlists_path)

        for name, (folder, config) in existing.items():
            if not config.get("Enabled", True) or config.get("Type") != "Playlist":
                continue

            # Check media type matches
            config_types = config.get("MediaTypes", [])
            if jf_media_type not in config_types:
                continue

            # Check if item's tags match any of the playlist's tag expressions
            matches = False
            for expr_set in config.get("ExpressionSets", []):
                for expr in expr_set.get("Expressions", []):
                    member = (expr.get("MemberName") or "").lower()
                    operator = (expr.get("Operator") or "").lower()
                    value = expr.get("TargetValue", "")
                    if member == "tags" and operator == "contains" and value in item_tags_set:
                        matches = True
                        break
                if matches:
                    break

            if not matches:
                continue

            # Find Jellyfin playlist ID
            playlist_id = None
            for up in config.get("UserPlaylists", []):
                if up.get("JellyfinPlaylistId"):
                    playlist_id = up["JellyfinPlaylistId"]
                    break

            if not playlist_id:
                continue

            # Check if item is already in the playlist
            try:
                current_items = jf.get_playlist_items(playlist_id)
                current_ids = {item["Id"] for item in current_items}
                if jellyfin_item_id in current_ids:
                    continue  # Already there

                # Add item, then re-sort entire playlist to match user's sort order
                jf.add_to_playlist(playlist_id, [jellyfin_item_id])
                try:
                    query = _build_query_params(config)
                    sorted_items = jf.query_items(**query)
                    sorted_items = _resort_by_db_date(sorted_items, config, db)
                    sorted_ids = [item["Id"] for item in sorted_items]
                    if sorted_ids:
                        all_entries = jf.get_playlist_items(playlist_id)
                        entry_ids = [e.get("PlaylistItemId", e["Id"]) for e in all_entries]
                        if entry_ids:
                            jf.remove_from_playlist(playlist_id, entry_ids)
                        jf.add_to_playlist(playlist_id, sorted_ids)
                except Exception as sort_err:
                    logger.warning(f"[SmartLists] Re-sort after add failed for '{name}': {sort_err}")

                total_added += 1
                logger.info(f"[SmartLists] Added item {jellyfin_item_id} to playlist '{name}' for user {user.display_name}")
            except Exception as e:
                logger.warning(f"[SmartLists] Failed to add to '{name}': {e}")

    if total_added > 0:
        bump_playlist_version()
    return {"added_to": total_added}

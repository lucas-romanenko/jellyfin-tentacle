"""
Tentacle - SmartLists Service
Creates and syncs Jellyfin SmartList config files to disk.
Also generates tentacle-home.json for the Tentacle Jellyfin plugin.
"""

import uuid
import json
import logging
import requests
from pathlib import Path
from sqlalchemy.orm import Session

from models.database import get_setting, TagRule

logger = logging.getLogger(__name__)

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


def _build_config(name: str, tag: str, media_types: list, folder_id: str,
                  enabled: bool = True, jellyfin_user_id: str = "",
                  expressions: list = None) -> dict:
    user_playlists = [{"UserId": jellyfin_user_id, "JellyfinPlaylistId": ""}] if jellyfin_user_id else []

    if expressions is None:
        expressions = [{"MemberName": "Tags", "Operator": "Contains", "TargetValue": tag}]

    return {
        "Public": True,
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
                    "SortBy": "ReleaseDate",
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


def _create_jellyfin_playlist(name: str, user_id: str, jellyfin_url: str, jellyfin_key: str) -> str:
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
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("Id", "")
    except Exception as e:
        logger.warning(f"Could not create Jellyfin playlist '{name}': {e}")
        return ""


def get_desired_smartlists(db: Session) -> list:
    """Build the full list of SmartList definitions from:
    1. Enabled auto playlists (source, list, built-in)
    2. Custom playlists (tag rules)
    """
    from models.database import ListSubscription, ListItem, AutoPlaylistToggle, Movie, Series
    smartlists = []
    existing_tags = set()

    # ── Auto playlists (source-based, from enabled toggles) ──
    toggles = {t.key: t.enabled for t in db.query(AutoPlaylistToggle).all()}

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

    # Built-in playlists
    builtin_map = {
        "builtin:recently_added_movies": ("Recently Added Movies", ["Movie"]),
        "builtin:recently_added_tv": ("Recently Added TV", ["Series"]),
        "builtin:downloaded_movies": ("Downloaded Movies", ["Movie"]),
        "builtin:downloaded_tv": ("Downloaded TV", ["Series"]),
    }
    for bkey, (bname, bmedia) in builtin_map.items():
        if toggles.get(bkey) and bname not in existing_tags:
            smartlists.append({"name": bname, "tag": bname, "media_type": bmedia, "enabled": True, "source": "auto"})
            existing_tags.add(bname)

    # ── List playlists (use ListSubscription.playlist_enabled) ──
    enabled_lists = db.query(ListSubscription).filter(
        ListSubscription.playlist_enabled == True,
        ListSubscription.active == True,
    ).all()
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
    active_rules = db.query(TagRule).filter(TagRule.active == True).all()
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


def sync_smartlists(db: Session) -> dict:
    """Sync SmartList config files to disk. Returns {created, updated, total}."""
    smartlists_path_str = get_setting(db, "smartlists_path", "/data/smartlists")
    smartlists_path = Path(smartlists_path_str)

    if not smartlists_path.exists():
        try:
            smartlists_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Cannot create SmartLists path {smartlists_path}: {e}")
            return {"created": 0, "updated": 0, "total": 0, "error": str(e)}

    desired = get_desired_smartlists(db)
    existing = _scan_existing(smartlists_path)
    jellyfin_user_id = get_setting(db, "jellyfin_user_id", "")
    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")

    created = 0
    updated = 0

    for sl in desired:
        name = sl["name"]
        tag = sl["tag"]
        media_types = sl["media_type"]
        enabled = sl["enabled"]
        expressions = sl.get("expressions")  # None for tag-based, list for native

        if name in existing:
            # Update existing
            folder, old_data = existing[name]
            folder_id = old_data.get("Id", str(uuid.uuid4()))
            config = _build_config(name, tag, media_types, folder_id, enabled, jellyfin_user_id, expressions=expressions)

            # Preserve user-managed fields from existing config
            for field in PRESERVED_FIELDS:
                if field in old_data:
                    config[field] = old_data[field]

            # Preserve UserPlaylists entries that have a linked JellyfinPlaylistId
            old_playlists = old_data.get("UserPlaylists", [])
            for entry in old_playlists:
                if entry.get("JellyfinPlaylistId"):
                    config["UserPlaylists"] = old_playlists
                    break
            else:
                # No linked playlists — create one if we have Jellyfin credentials
                if jellyfin_user_id and jellyfin_url and jellyfin_key:
                    playlist_id = _create_jellyfin_playlist(name, jellyfin_user_id, jellyfin_url, jellyfin_key)
                    if playlist_id:
                        config["UserPlaylists"] = [{"UserId": jellyfin_user_id, "JellyfinPlaylistId": playlist_id}]

            config_file = folder / "config.json"
            config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
            updated += 1
        else:
            # Create new folder
            folder_id = str(uuid.uuid4())
            folder = smartlists_path / folder_id
            folder.mkdir(parents=True, exist_ok=True)

            config = _build_config(name, tag, media_types, folder_id, enabled, jellyfin_user_id, expressions=expressions)

            # Create Jellyfin playlist for the new SmartList
            if jellyfin_user_id and jellyfin_url and jellyfin_key:
                playlist_id = _create_jellyfin_playlist(name, jellyfin_user_id, jellyfin_url, jellyfin_key)
                if playlist_id:
                    config["UserPlaylists"] = [{"UserId": jellyfin_user_id, "JellyfinPlaylistId": playlist_id}]

            config_file = folder / "config.json"
            config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
            created += 1

    # Clean up orphaned SmartList folders (deleted tag rules)
    desired_names = {sl["name"] for sl in desired}
    orphaned = {name: (folder, data) for name, (folder, data) in existing.items() if name not in desired_names}
    removed = 0

    # Safety check: if more than half of existing playlists would be removed,
    # something is likely wrong (DB issue, toggle reset, etc.) — skip cleanup
    if orphaned and existing and len(orphaned) > len(existing) / 2:
        logger.warning(
            f"Skipping orphan cleanup: {len(orphaned)}/{len(existing)} playlists would be removed "
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
                    jf = JellyfinService(jellyfin_url, jellyfin_key, jellyfin_user_id)
                    jf.delete_item(playlist_id)
                except Exception as e:
                    logger.warning(f"Could not delete Jellyfin playlist for '{name}': {e}")

            # Remove the folder from disk
            try:
                import shutil
                shutil.rmtree(folder)
                logger.info(f"Removed orphaned SmartList folder: {name}")
                removed += 1
            except Exception as e:
                logger.warning(f"Could not remove folder for '{name}': {e}")

    logger.info(f"SmartLists sync: {created} created, {updated} updated, {removed} removed, {len(desired)} total")

    # After syncing configs, populate playlists via Jellyfin API directly
    playlist_stats = refresh_smartlist_playlists(db)

    # Generate and upload artwork for all playlists
    artwork_stats = {}
    try:
        from routers.collections import sync_playlist_artwork
        artwork_stats = sync_playlist_artwork(db)
    except Exception as e:
        logger.warning(f"Artwork sync failed: {e}")

    # Generate tentacle-home.json
    write_home_config(db)

    return {
        "created": created, "updated": updated, "removed": removed, "total": len(desired),
        "playlists": playlist_stats, "artwork": artwork_stats,
    }


def _get_smartlists_with_playlist_ids(db: Session) -> list:
    """Scan existing SmartList configs and return those with a non-empty JellyfinPlaylistId."""
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


def write_home_config(db: Session) -> dict:
    """Generate and write tentacle-home.json based on current SmartLists.

    Preserves existing row order, hero pick, and built-in Jellyfin sections.
    Only playlist rows are validated against disk (built-in sections are always kept).
    Returns the config dict that was written.
    """
    home_config_path = get_setting(db, "home_config_path", "/data/tentacle-home.json")
    home_row_limit = int(get_setting(db, "home_row_limit", "20") or "20")
    smartlists = _get_smartlists_with_playlist_ids(db)

    if not smartlists:
        logger.warning("No SmartLists with JellyfinPlaylistId found, skipping home config write")
        return {}

    existing_config = get_home_config(db)
    existing_rows = existing_config.get("rows", []) if existing_config else []
    existing_hero = existing_config.get("hero") if existing_config else None

    # Set of playlist_ids from the disk scan (current truth)
    current_ids = {sl["playlist_id"] for sl in smartlists}
    # Lookup for display names
    name_by_id = {sl["playlist_id"]: sl["name"] for sl in smartlists}

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

    # Hero: preserve existing pick only if its playlist still exists, otherwise disabled
    if existing_hero and existing_hero.get("playlist_id") in current_ids:
        hero = existing_hero
    else:
        hero = {"enabled": False, "playlist_id": "", "display_name": "", "sort_by": "random", "sort_order": "Descending", "require_logo": True}

    config = {
        "hero": hero,
        "rows": rows,
    }

    try:
        path = Path(home_config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        logger.info(f"Wrote tentacle-home.json with {len(rows)} rows to {home_config_path}")
    except Exception as e:
        logger.error(f"Failed to write tentacle-home.json: {e}")
        return {}

    return config


def get_home_config(db: Session) -> dict:
    """Read and return the current tentacle-home.json contents."""
    home_config_path = get_setting(db, "home_config_path", "/data/tentacle-home.json")
    path = Path(home_config_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to read tentacle-home.json: {e}")
        return {}



def _notify_jellyfin_plugin(db: Session):
    """POST to the Tentacle Jellyfin plugin refresh endpoint. Fail silently if not installed."""
    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")
    if not jellyfin_url or not jellyfin_key:
        return
    try:
        r = requests.post(
            f"{jellyfin_url.rstrip('/')}/Tentacle/Refresh",
            headers={"X-Emby-Token": jellyfin_key},
            timeout=5,
        )
        if r.ok:
            logger.info("Notified Tentacle Jellyfin plugin to refresh")
        else:
            logger.debug(f"Tentacle plugin refresh returned {r.status_code} (plugin may not be installed)")
    except Exception:
        logger.debug("Tentacle Jellyfin plugin not reachable (plugin may not be installed)")


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
                params["genres"].append(value)
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


def refresh_smartlist_playlists(db: Session) -> dict:
    """Read SmartList configs from disk, query Jellyfin for matching items,
    and create/update playlists. This replaces the C# SmartLists plugin entirely.

    Returns {processed, created, updated, errors}.
    """
    from services.jellyfin import JellyfinService

    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")
    jellyfin_uid = get_setting(db, "jellyfin_user_id", "")
    smartlists_path = Path(get_setting(db, "smartlists_path", "/data/smartlists"))

    if not jellyfin_url or not jellyfin_key:
        return {"error": "Jellyfin not configured", "processed": 0}

    jf = JellyfinService(jellyfin_url, jellyfin_key, jellyfin_uid)

    if not jf.test_connection():
        return {"error": "Jellyfin connection failed", "processed": 0}

    existing = _scan_existing(smartlists_path)
    if not existing:
        return {"error": "No SmartList configs found on disk", "processed": 0}

    stats = {"processed": 0, "created": 0, "updated": 0, "errors": 0}

    for name, (folder, config) in existing.items():
        if not config.get("Enabled", True) or config.get("Type") != "Playlist":
            continue

        try:
            _process_single_playlist(jf, folder, config, jellyfin_uid, stats)
        except Exception as e:
            logger.error(f"[SmartLists] Failed to process '{name}': {e}")
            stats["errors"] += 1

    logger.info(
        f"[SmartLists] Playlist refresh: {stats['processed']} processed, "
        f"{stats['created']} created, {stats['updated']} updated, "
        f"{stats['errors']} errors"
    )
    return stats


def _process_single_playlist(jf, folder: Path, config: dict, user_id: str, stats: dict):
    """Process a single SmartList config: query items, create/update playlist."""
    name = config.get("Name", "Unknown")

    # Query Jellyfin for matching items
    query = _build_query_params(config)
    items = jf.query_items(**query)
    item_ids = [item["Id"] for item in items]

    logger.debug(f"[SmartLists] '{name}': {len(item_ids)} matching items")

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
        # Update existing playlist — diff current items vs desired
        current_entries = jf.get_playlist_items(playlist_id)
        current_ids = {entry["Id"] for entry in current_entries}
        current_entry_map = {entry["Id"]: entry.get("PlaylistItemId", entry["Id"]) for entry in current_entries}

        desired_ids = set(item_ids)

        # Remove items no longer matching
        to_remove_entry_ids = [
            current_entry_map[cid] for cid in current_ids - desired_ids
            if cid in current_entry_map
        ]
        if to_remove_entry_ids:
            jf.remove_from_playlist(playlist_id, to_remove_entry_ids)

        # Add new items
        to_add = [iid for iid in item_ids if iid not in current_ids]
        if to_add:
            jf.add_to_playlist(playlist_id, to_add)

        logger.info(f"[SmartLists] Updated '{name}': +{len(to_add)} -{len(to_remove_entry_ids)} items (total {len(item_ids)})")
        stats["updated"] += 1
    else:
        # Create new playlist with items
        playlist_id = jf.create_playlist(name, item_ids if item_ids else None)
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


def update_playlist_sort(name: str, sort_by: str, sort_order: str, db) -> dict:
    """Update sort order for a playlist config on disk and re-populate in Jellyfin."""
    from services.jellyfin import JellyfinService

    if sort_by not in VALID_SORT_BY:
        return {"success": False, "message": f"Invalid sort_by: {sort_by}"}
    if sort_order not in ("Ascending", "Descending"):
        return {"success": False, "message": f"Invalid sort_order: {sort_order}"}

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
    jellyfin_user_id = get_setting(db, "jellyfin_user_id")

    if jellyfin_url and jellyfin_key and jellyfin_user_id:
        try:
            jf = JellyfinService(jellyfin_url, jellyfin_key, jellyfin_user_id)

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
                item_ids = [item["Id"] for item in items]
                if item_ids:
                    jf.add_to_playlist(playlist_id, item_ids)

                logger.info(f"[SmartLists] Re-populated '{name}' with {len(item_ids)} items in new sort order")
        except Exception as e:
            logger.warning(f"[SmartLists] Failed to re-populate '{name}' after sort change: {e}")

    return {"success": True, "sort_by": sort_by, "sort_order": sort_order}

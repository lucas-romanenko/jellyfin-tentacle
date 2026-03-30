"""
Tentacle - Collections Router
Generates and uploads artwork for Jellyfin playlists.
"""

import logging
import base64
import requests
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.database import get_db, get_setting, TagRule
from services.artwork import generate_playlist_poster, _get_source_tag_from_rule, _detect_source_from_name

# Track which artwork files have been uploaded (path → True)
# Persists across calls within the same container lifetime
_uploaded_artwork: dict[str, bool] = {}

from routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/collections", tags=["collections"], dependencies=[Depends(require_admin)])


def upload_playlist_artwork(jellyfin_url: str, jellyfin_key: str, playlist_id: str, image_path: str) -> bool:
    """Upload a poster image to a Jellyfin playlist."""
    try:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        r = requests.post(
            f"{jellyfin_url.rstrip('/')}/Items/{playlist_id}/Images/Primary",
            headers={
                "X-Emby-Token": jellyfin_key,
                "Content-Type": "image/png",
            },
            data=image_data,
            timeout=15
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Failed to upload artwork for playlist {playlist_id}: {e}")
        return False


def sync_playlist_artwork(db: Session) -> dict:
    """Generate and upload artwork for all playlists that have a Jellyfin playlist ID."""
    from services.smartlists import _get_smartlists_with_playlist_ids

    jellyfin_url = get_setting(db, "jellyfin_url")
    jellyfin_key = get_setting(db, "jellyfin_api_key")
    logodev_token = get_setting(db, "logodev_api_key", "")

    if not jellyfin_url or not jellyfin_key:
        return {"error": "Jellyfin not configured", "updated": 0}

    playlists = _get_smartlists_with_playlist_ids(db)
    if not playlists:
        return {"updated": 0, "total": 0}

    # Build a map of output_tag → source_tag from active TagRules
    rules = db.query(TagRule).filter(TagRule.active == True).all()
    source_by_tag = {}
    for rule in rules:
        st = _get_source_tag_from_rule(rule)
        if st:
            source_by_tag[rule.output_tag] = st

    updated = 0
    skipped = 0
    errors = 0

    for pl in playlists:
        name = pl["name"]
        playlist_id = pl["playlist_id"]
        source_tag = source_by_tag.get(name) or _detect_source_from_name(name)

        image_path = generate_playlist_poster(
            name,
            source_tag=source_tag,
            logodev_token=logodev_token or None,
        )
        if not image_path:
            errors += 1
            continue

        # Skip upload if this exact artwork was already uploaded
        cache_key = f"{playlist_id}:{image_path}"
        if cache_key in _uploaded_artwork:
            skipped += 1
            continue

        if upload_playlist_artwork(jellyfin_url, jellyfin_key, playlist_id, image_path):
            _uploaded_artwork[cache_key] = True
            logger.info(f"Uploaded artwork for playlist: {name}")
            updated += 1
        else:
            errors += 1

    return {"success": True, "updated": updated, "skipped": skipped, "errors": errors, "total": len(playlists)}


@router.post("/sync-artwork")
def sync_artwork(db: Session = Depends(get_db)):
    """Generate and upload artwork for all Jellyfin playlists."""
    return sync_playlist_artwork(db)

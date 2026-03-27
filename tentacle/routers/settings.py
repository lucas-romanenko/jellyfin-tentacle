"""
Tentacle - Settings Router
Handles all settings API endpoints
"""

import os
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Dict
import requests

from models.database import get_db, Setting, get_setting, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    settings: Dict[str, str]


class ConnectionTest(BaseModel):
    type: str  # tmdb | radarr | jellyfin
    url: Optional[str] = None
    api_key: Optional[str] = None
    bearer_token: Optional[str] = None


@router.get("")
def get_settings(db: Session = Depends(get_db)):
    settings = db.query(Setting).all()
    result = {s.key: s.value for s in settings}
    # Mask sensitive values
    for key in ["tmdb_bearer_token", "tmdb_api_key", "radarr_api_key", "sonarr_api_key", "jellyfin_api_key", "trakt_client_id"]:
        if result.get(key):
            result[key] = result[key][:8] + "..." + result[key][-4:]
    return result


@router.get("/raw")
def get_settings_raw(db: Session = Depends(get_db)):
    """Get settings without masking - for internal use"""
    settings = db.query(Setting).all()
    return {s.key: s.value for s in settings}


@router.post("")
def update_settings(body: SettingsUpdate, db: Session = Depends(get_db)):
    sensitive_keys = {"tmdb_bearer_token", "tmdb_api_key", "radarr_api_key", "sonarr_api_key", "jellyfin_api_key", "trakt_client_id"}
    was_setup_complete = get_setting(db, "setup_complete") == "true"

    for key, value in body.settings.items():
        # Don't overwrite sensitive keys if they look masked
        if key in sensitive_keys and value and "..." in value:
            continue
        set_setting(db, key, value)

    # Mark setup complete if all required fields are filled
    required = ["jellyfin_url", "jellyfin_api_key"]
    all_set = all(get_setting(db, k) for k in required)
    if all_set:
        set_setting(db, "setup_complete", "true")

    # First-time setup: auto-scan Radarr/Sonarr if configured, so existing content is picked up
    if all_set and not was_setup_complete:
        _trigger_post_setup_scan()

    return {"success": True}


def _trigger_post_setup_scan():
    """After setup wizard completes, scan Radarr/Sonarr in the background
    so the user's existing library is immediately available."""
    import threading
    import logging

    logger = logging.getLogger(__name__)

    def _post_setup_background():
        from models.database import SessionLocal, get_setting, set_setting, log_activity
        from datetime import datetime
        db = SessionLocal()
        try:
            radarr_url = get_setting(db, "radarr_url")
            radarr_key = get_setting(db, "radarr_api_key")
            if radarr_url and radarr_key:
                logger.info("[Post-setup] Auto-scanning Radarr library...")
                from services.radarr import scan_radarr_library
                result = scan_radarr_library(db)
                set_setting(db, "last_radarr_scan", datetime.utcnow().isoformat())
                n = result.get("new", 0) if isinstance(result, dict) else 0
                if n:
                    log_activity(db, "radarr_scan", f"Post-setup Radarr scan — {n} movie{'s' if n != 1 else ''} imported")
                logger.info(f"[Post-setup] Radarr scan complete: {n} new movies")

            sonarr_url = get_setting(db, "sonarr_url")
            sonarr_key = get_setting(db, "sonarr_api_key")
            if sonarr_url and sonarr_key:
                logger.info("[Post-setup] Auto-scanning Sonarr library...")
                from services.sonarr import scan_sonarr_library
                result = scan_sonarr_library(db)
                set_setting(db, "last_sonarr_scan", datetime.utcnow().isoformat())
                n = result.get("new", 0) if isinstance(result, dict) else 0
                if n:
                    log_activity(db, "sonarr_scan", f"Post-setup Sonarr scan — {n} series imported")
                logger.info(f"[Post-setup] Sonarr scan complete: {n} new series")

            # Run full Jellyfin pipeline if anything was scanned
            if (radarr_url and radarr_key) or (sonarr_url and sonarr_key):
                from services.jellyfin import run_full_jellyfin_pipeline
                run_full_jellyfin_pipeline(db, log_prefix="Post-setup")
                logger.info("[Post-setup] Jellyfin pipeline complete")

        except Exception as e:
            logger.error(f"[Post-setup] Auto-scan failed: {e}", exc_info=True)
        finally:
            db.close()

    thread = threading.Thread(target=_post_setup_background, daemon=True)
    thread.start()
    logging.getLogger(__name__).info("[Post-setup] Triggered background Radarr/Sonarr scan after setup wizard")


@router.post("/test")
def test_connection(body: ConnectionTest, db: Session = Depends(get_db)):
    if body.type == "tmdb":
        from services.tmdb import get_tmdb_token
        token = body.bearer_token if (body.bearer_token and "..." not in body.bearer_token) else get_tmdb_token(db)
        if not token:
            raise HTTPException(400, "No TMDB token configured")
        try:
            r = requests.get(
                "https://api.themoviedb.org/3/configuration",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            r.raise_for_status()
            return {"success": True, "message": "TMDB connection successful"}
        except Exception as e:
            raise HTTPException(400, f"TMDB connection failed: {str(e)}")

    elif body.type == "radarr":
        url = body.url or get_setting(db, "radarr_url")
        key = body.api_key or get_setting(db, "radarr_api_key")
        if not url or not key:
            raise HTTPException(400, "Radarr URL and API key required")
        try:
            r = requests.get(
                f"{url.rstrip('/')}/api/v3/system/status",
                headers={"X-Api-Key": key},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            return {"success": True, "message": f"Radarr {data.get('version', '')} connected"}
        except Exception as e:
            raise HTTPException(400, f"Radarr connection failed: {str(e)}")

    elif body.type == "sonarr":
        url = body.url or get_setting(db, "sonarr_url")
        key = body.api_key or get_setting(db, "sonarr_api_key")
        if not url or not key:
            raise HTTPException(400, "Sonarr URL and API key required")
        try:
            r = requests.get(
                f"{url.rstrip('/')}/api/v3/system/status",
                headers={"X-Api-Key": key},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            return {"success": True, "message": f"Sonarr {data.get('version', '')} connected"}
        except Exception as e:
            raise HTTPException(400, f"Sonarr connection failed: {str(e)}")

    elif body.type == "jellyfin":
        url = body.url or get_setting(db, "jellyfin_url")
        key = body.api_key or get_setting(db, "jellyfin_api_key")
        if not url or not key:
            raise HTTPException(400, "Jellyfin URL and API key required")
        try:
            r = requests.get(
                f"{url.rstrip('/')}/System/Info",
                headers={"X-Emby-Token": key},
                timeout=10
            )
            if r.status_code == 401:
                raise HTTPException(401, "Invalid API key — generate a new one in Jellyfin Dashboard → API Keys")
            r.raise_for_status()
            data = r.json()
            return {"success": True, "message": f"Jellyfin {data.get('Version', '')} connected"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Jellyfin connection failed: {str(e)}")

    raise HTTPException(400, "Unknown connection type")


@router.get("/paths")
def check_paths():
    """Check which media paths are mounted and accessible."""
    paths = [
        {"key": "data", "path": "/data", "label": "Database & Config", "required": True,
         "mount_example": "./tentacle-data:/data"},
        {"key": "vod", "path": "/mnt/media/vod", "label": "VOD Content",
         "mount_example": "/your/vod:/mnt/media/vod"},
        {"key": "movies", "path": "/mnt/media/movies", "label": "Radarr Movies",
         "mount_example": "/your/movies:/mnt/media/movies"},
        {"key": "tv", "path": "/mnt/media/tv", "label": "Sonarr TV Shows",
         "mount_example": "/your/tv:/mnt/media/tv"},
    ]
    result = {}
    for info in paths:
        p = Path(info["path"])
        mounted = p.exists() and p.is_dir()
        writable = mounted and os.access(str(p), os.W_OK)
        result[info["key"]] = {
            "path": info["path"],
            "label": info["label"],
            "mounted": mounted,
            "writable": writable,
            "mount_example": info["mount_example"],
        }
        if info.get("required"):
            result[info["key"]]["required"] = True
    return result


@router.get("/stale-files")
def check_stale_files(db: Session = Depends(get_db)):
    """Check for .strm files in VOD folders that Tentacle didn't create.
    Only relevant on first startup when DB is empty but VOD folders have content."""
    # Don't show if user already dismissed or has synced content
    if get_setting(db, "stale_files_dismissed") == "true":
        return {"show": False}

    from models.database import Movie, Series, SyncRun
    has_content = db.query(Movie).filter(Movie.source.like("provider_%")).count() > 0
    has_series = db.query(Series).filter(Series.source.like("provider_%")).count() > 0
    has_synced = db.query(SyncRun).filter(SyncRun.status == "completed").count() > 0
    if has_content or has_series or has_synced:
        return {"show": False}

    # Scan VOD folders for existing .strm / .nfo files
    movies_path = Path("/mnt/media/vod/Movies")
    series_path = Path("/mnt/media/vod/Series")
    strm_count = 0
    nfo_count = 0
    for vod_dir in [movies_path, series_path]:
        if vod_dir.exists():
            strm_count += len(list(vod_dir.rglob("*.strm")))
            nfo_count += len(list(vod_dir.rglob("*.nfo")))

    if strm_count == 0:
        return {"show": False}

    return {"show": True, "strm_count": strm_count, "nfo_count": nfo_count}


@router.post("/stale-files/delete")
def delete_stale_files(db: Session = Depends(get_db)):
    """Delete all .strm and .nfo files in VOD folders, then remove empty directories."""
    import shutil
    movies_path = Path("/mnt/media/vod/Movies")
    series_path = Path("/mnt/media/vod/Series")
    deleted_strm = 0
    deleted_nfo = 0

    for vod_dir in [movies_path, series_path]:
        if not vod_dir.exists():
            continue
        for f in vod_dir.rglob("*.strm"):
            f.unlink(missing_ok=True)
            deleted_strm += 1
        for f in vod_dir.rglob("*.nfo"):
            f.unlink(missing_ok=True)
            deleted_nfo += 1
        # Remove empty directories bottom-up
        for dirpath in sorted(vod_dir.rglob("*"), reverse=True):
            if dirpath.is_dir():
                try:
                    dirpath.rmdir()  # only removes if empty
                except OSError:
                    pass

    set_setting(db, "stale_files_dismissed", "true")
    return {"success": True, "deleted_strm": deleted_strm, "deleted_nfo": deleted_nfo}


@router.post("/stale-files/dismiss")
def dismiss_stale_files(db: Session = Depends(get_db)):
    """Permanently dismiss the stale files warning."""
    set_setting(db, "stale_files_dismissed", "true")
    return {"success": True}


class WebhookTest(BaseModel):
    url: str


@router.post("/test-webhook")
def test_webhook(body: WebhookTest):
    """Proxy a webhook test through the backend to avoid mixed-content browser issues."""
    try:
        r = requests.post(
            body.url,
            json={"eventType": "Test"},
            timeout=10
        )
        r.raise_for_status()
        return {"success": True, "message": "Webhook test successful"}
    except Exception as e:
        raise HTTPException(400, f"Webhook test failed: {str(e)}")


class JellyfinLogin(BaseModel):
    username: str
    password: str


@router.post("/jellyfin-login")
def jellyfin_login(body: JellyfinLogin, db: Session = Depends(get_db)):
    """Authenticate with Jellyfin and save user ID/name."""
    url = get_setting(db, "jellyfin_url")
    if not url:
        raise HTTPException(400, "Jellyfin URL not configured")
    try:
        r = requests.post(
            f"{url.rstrip('/')}/Users/AuthenticateByName",
            headers={
                "Authorization": 'MediaBrowser Client="Tentacle", Device="Server", DeviceId="tentacle", Version="1.0"',
                "Content-Type": "application/json",
            },
            json={"Username": body.username, "Pw": body.password},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        user_id = data["User"]["Id"]
        user_name = data["User"]["Name"]
        set_setting(db, "jellyfin_user_id", user_id)
        set_setting(db, "jellyfin_user_name", user_name)
        return {"success": True, "user_id": user_id, "username": user_name}
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            raise HTTPException(401, "Invalid username or password")
        raise HTTPException(400, f"Jellyfin login failed: {e}")
    except Exception as e:
        raise HTTPException(400, f"Jellyfin login failed: {e}")

"""
Tentacle - Settings Router
Handles all settings API endpoints
"""

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

    return {"success": True}


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

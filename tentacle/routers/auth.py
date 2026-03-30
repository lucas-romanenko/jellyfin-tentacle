"""
Tentacle - Auth Router
Handles user authentication via Jellyfin, session management, and user listing.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import requests

from models.database import (
    get_db, TentacleUser, get_setting, set_setting,
    migrate_orphaned_data_to_user,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "tentacle_session"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_session_secret(db: Session) -> str:
    return get_setting(db, "session_secret", "fallback-secret-change-me")


def _sign_session(user_id: int, secret: str) -> str:
    """Create a signed session token: user_id.signature"""
    import hmac, hashlib
    msg = str(user_id).encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"{user_id}.{sig}"


def _verify_session(token: str, secret: str) -> Optional[int]:
    """Verify a signed session token and return user_id or None."""
    import hmac, hashlib
    if not token or "." not in token:
        return None
    try:
        uid_str, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), uid_str.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return int(uid_str)
    except (ValueError, TypeError):
        pass
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> TentacleUser:
    """Extract authenticated user from session cookie. Raises 401 if not logged in."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Not authenticated")
    secret = _get_session_secret(db)
    user_id = _verify_session(token, secret)
    if user_id is None:
        raise HTTPException(401, "Invalid session")
    user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
    if not user:
        raise HTTPException(401, "User not found")
    return user


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[TentacleUser]:
    """Get current user or None (no 401). For endpoints that support both cookie and query param auth."""
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


def get_user_from_request(request: Request, db: Session = Depends(get_db)) -> TentacleUser:
    """Get user from session cookie OR from ?userId= query param (for plugin/API calls)."""
    # Try cookie first
    try:
        return get_current_user(request, db)
    except HTTPException:
        pass
    # Try userId query param (Jellyfin user ID, not internal ID)
    jf_user_id = request.query_params.get("userId")
    if jf_user_id:
        user = db.query(TentacleUser).filter(TentacleUser.jellyfin_user_id == jf_user_id).first()
        if user:
            return user
    raise HTTPException(401, "Not authenticated")


def require_admin(request: Request, db: Session = Depends(get_db)) -> Optional[TentacleUser]:
    """Require admin role. Allows access in bootstrap mode (no users yet)."""
    # Bootstrap mode: if no users exist, allow unauthenticated access
    if db.query(TentacleUser).count() == 0:
        return None
    user = get_current_user(request, db)
    if not user.is_admin:
        raise HTTPException(403, "Admin access required")
    return user


# ─── Models ──────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str = ""


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/users")
def get_jellyfin_users(db: Session = Depends(get_db)):
    """Fetch Jellyfin users for the login picker. No auth required.
    Uses authenticated /Users endpoint (API key) so hidden users are included.
    Falls back to /Users/Public if no API key configured.
    """
    jf_url = get_setting(db, "jellyfin_url")
    if not jf_url:
        raise HTTPException(400, "Jellyfin URL not configured")
    jf_key = get_setting(db, "jellyfin_api_key", "")
    try:
        if jf_key:
            r = requests.get(
                f"{jf_url.rstrip('/')}/Users",
                headers={"X-Emby-Token": jf_key},
                timeout=10,
            )
        else:
            r = requests.get(f"{jf_url.rstrip('/')}/Users/Public", timeout=10)
        r.raise_for_status()
        users = r.json()
        return [
            {
                "id": u["Id"],
                "name": u["Name"],
                "has_password": u.get("HasPassword", True),
                "image_tag": u.get("PrimaryImageTag"),
                "jellyfin_url": jf_url.rstrip("/"),
            }
            for u in users
        ]
    except Exception as e:
        logger.error(f"Failed to fetch Jellyfin users: {e}")
        raise HTTPException(502, f"Could not reach Jellyfin: {e}")


@router.post("/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """Authenticate with Jellyfin and create a Tentacle session."""
    jf_url = get_setting(db, "jellyfin_url")
    if not jf_url:
        raise HTTPException(400, "Jellyfin URL not configured")

    # Authenticate via Jellyfin
    try:
        r = requests.post(
            f"{jf_url.rstrip('/')}/Users/AuthenticateByName",
            headers={
                "Authorization": 'MediaBrowser Client="Tentacle", Device="Server", DeviceId="tentacle", Version="1.0"',
                "Content-Type": "application/json",
            },
            json={"Username": body.username, "Pw": body.password},
            timeout=10,
        )
        r.raise_for_status()
    except requests.HTTPError:
        raise HTTPException(401, "Invalid username or password")
    except Exception as e:
        raise HTTPException(502, f"Could not reach Jellyfin: {e}")

    data = r.json()
    jf_user_id = data["User"]["Id"]
    jf_user_name = data["User"]["Name"]
    jf_image_tag = data["User"].get("PrimaryImageTag")

    # Create or update TentacleUser
    user = db.query(TentacleUser).filter(TentacleUser.jellyfin_user_id == jf_user_id).first()
    is_first_user = db.query(TentacleUser).count() == 0

    if not user:
        user = TentacleUser(
            jellyfin_user_id=jf_user_id,
            display_name=jf_user_name,
            is_admin=is_first_user,  # First user becomes admin
            profile_image_tag=jf_image_tag,
        )
        db.add(user)
        db.flush()

        if is_first_user:
            # Migrate existing data from before multi-user to this admin
            migrate_orphaned_data_to_user(db, user.id)
            # Also update the legacy settings for backwards compat
            set_setting(db, "jellyfin_user_id", jf_user_id)
            set_setting(db, "jellyfin_user_name", jf_user_name)
            logger.info(f"First user '{jf_user_name}' promoted to admin, orphaned data migrated")
    else:
        user.display_name = jf_user_name
        user.profile_image_tag = jf_image_tag

    db.commit()

    # Set session cookie
    secret = _get_session_secret(db)
    token = _sign_session(user.id, secret)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )

    return {
        "id": user.id,
        "jellyfin_user_id": user.jellyfin_user_id,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "profile_image_tag": user.profile_image_tag,
    }


@router.post("/logout")
def logout(response: Response):
    """Clear the session cookie."""
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"success": True}


@router.get("/me")
def get_me(user: TentacleUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the currently authenticated user."""
    jf_url = get_setting(db, "jellyfin_url", "")
    return {
        "id": user.id,
        "jellyfin_user_id": user.jellyfin_user_id,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "profile_image_tag": user.profile_image_tag,
        "jellyfin_url": jf_url.rstrip("/") if jf_url else "",
    }


@router.post("/promote/{user_id}")
def promote_user(user_id: int, admin: TentacleUser = Depends(require_admin), db: Session = Depends(get_db)):
    """Promote a user to admin. Only admins can do this."""
    target = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    target.is_admin = True
    db.commit()
    return {"success": True, "display_name": target.display_name}

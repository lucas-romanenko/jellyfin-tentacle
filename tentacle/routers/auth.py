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
    jf_is_admin = data["User"].get("Policy", {}).get("IsAdministrator", False)

    # Create or update TentacleUser
    user = db.query(TentacleUser).filter(TentacleUser.jellyfin_user_id == jf_user_id).first()
    is_first_user = db.query(TentacleUser).count() == 0

    if not user:
        user = TentacleUser(
            jellyfin_user_id=jf_user_id,
            display_name=jf_user_name,
            is_admin=jf_is_admin,  # Sync admin status from Jellyfin
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
            logger.info(f"First user '{jf_user_name}' set as admin, orphaned data migrated")
    else:
        user.display_name = jf_user_name
        user.profile_image_tag = jf_image_tag
        user.is_admin = jf_is_admin  # Sync admin status on every login

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


@router.get("/managed-users")
def get_managed_users(admin: TentacleUser = Depends(require_admin), db: Session = Depends(get_db)):
    """List all Jellyfin users with their admin status. Admin only."""
    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key", "")
    if not jf_url or not jf_key:
        raise HTTPException(400, "Jellyfin not configured")
    try:
        r = requests.get(
            f"{jf_url.rstrip('/')}/Users",
            headers={"X-Emby-Token": jf_key},
            timeout=10,
        )
        r.raise_for_status()
        users = r.json()
        return [
            {
                "id": u["Id"],
                "name": u["Name"],
                "is_admin": u.get("Policy", {}).get("IsAdministrator", False),
                "image_tag": u.get("PrimaryImageTag"),
                "has_logged_in": db.query(TentacleUser).filter(
                    TentacleUser.jellyfin_user_id == u["Id"]
                ).first() is not None,
            }
            for u in users
        ]
    except requests.RequestException as e:
        raise HTTPException(502, f"Could not reach Jellyfin: {e}")


class SetAdminRequest(BaseModel):
    jellyfin_user_id: str
    is_admin: bool


@router.post("/set-admin")
def set_user_admin(body: SetAdminRequest, admin: TentacleUser = Depends(require_admin), db: Session = Depends(get_db)):
    """Toggle a Jellyfin user's admin status. Updates both Jellyfin and Tentacle."""
    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key", "")
    if not jf_url or not jf_key:
        raise HTTPException(400, "Jellyfin not configured")

    # Prevent removing your own admin
    if body.jellyfin_user_id == admin.jellyfin_user_id and not body.is_admin:
        raise HTTPException(400, "Cannot remove your own admin status")

    try:
        # Fetch current user policy from Jellyfin
        r = requests.get(
            f"{jf_url.rstrip('/')}/Users/{body.jellyfin_user_id}",
            headers={"X-Emby-Token": jf_key},
            timeout=10,
        )
        r.raise_for_status()
        jf_user = r.json()
        policy = jf_user.get("Policy", {})
        policy["IsAdministrator"] = body.is_admin

        # Update policy in Jellyfin
        requests.post(
            f"{jf_url.rstrip('/')}/Users/{body.jellyfin_user_id}/Policy",
            headers={"X-Emby-Token": jf_key, "Content-Type": "application/json"},
            json=policy,
            timeout=10,
        ).raise_for_status()

        # Update TentacleUser if they've logged in before
        tentacle_user = db.query(TentacleUser).filter(
            TentacleUser.jellyfin_user_id == body.jellyfin_user_id
        ).first()
        if tentacle_user:
            tentacle_user.is_admin = body.is_admin
            db.commit()

        return {"success": True, "is_admin": body.is_admin}
    except requests.HTTPError as e:
        raise HTTPException(502, f"Jellyfin API error: {e}")
    except requests.RequestException as e:
        raise HTTPException(502, f"Could not reach Jellyfin: {e}")

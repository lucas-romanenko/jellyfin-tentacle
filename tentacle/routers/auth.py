"""
Tentacle - Auth Router
Handles user authentication via Jellyfin, session management, and user listing.
"""

import logging
import time
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

# Cache mapping a Jellyfin access token → (normalized_jellyfin_user_id, expiry).
# The ?api_key= auth path resolves the token's owner from Jellyfin once and then
# trusts it for a short window, avoiding a /Users/Me round-trip on every request.
_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_CACHE_TTL = 300  # 5 minutes
# What /Users/Me said about each verified token owner (name, admin flag), so a
# Jellyfin user who has never opened the dashboard can be given a Tentacle row
# on their first plugin call instead of being refused.
_token_profiles: dict[str, dict] = {}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _build_playlists_for_new_user(user_id: int) -> None:
    """Create a just-created user's playlists in the background."""
    import threading

    def _run():
        from models.database import SessionLocal
        from services.smartlists import (
            _notify_jellyfin_plugin, bump_playlist_version, refresh_smartlist_playlists,
            sync_smartlists, write_home_config,
        )
        db = SessionLocal()
        try:
            sync_smartlists(db, user_id=user_id)
            refresh_smartlist_playlists(db, user_id=user_id)
            write_home_config(db, user_id=user_id)
            bump_playlist_version()
            _notify_jellyfin_plugin(db)
            logger.info(f"Built playlists for new user {user_id}")
        except Exception as e:
            logger.warning(f"Could not build playlists for new user {user_id}: {e}")
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name=f"new-user-playlists-{user_id}").start()


def _get_session_secret(db: Session) -> str:
    return get_setting(db, "session_secret", "fallback-secret-change-me")


def _sign_session(user_id: int, secret: str, version: int = 0,
                  issued_at: Optional[int] = None) -> str:
    """Signed session token: user_id.issued_at.version.signature.

    The issue time lets the server enforce the cookie lifetime itself (a
    browser's max-age is only advice, and a copied cookie has none), and the
    version lets logout revoke tokens already handed out.
    """
    import hmac, hashlib
    issued_at = int(time.time()) if issued_at is None else int(issued_at)
    msg = f"{user_id}.{issued_at}.{version}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def _verify_session(token: str, secret: str) -> Optional[tuple]:
    """(user_id, issued_at, version) for a validly signed token, else None.

    Tokens in the old two-part format are refused: they carry no issue time,
    so there is no way to tell a fresh one from one copied a year ago. Their
    owners log in once more.
    """
    import hmac, hashlib
    if not token or token.count(".") != 3:
        return None
    try:
        msg, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        uid, iat, ver = (int(x) for x in msg.split("."))
        return uid, iat, ver
    except (ValueError, TypeError):
        return None


def _issue_session(db: Session, user: TentacleUser) -> str:
    return _sign_session(user.id, _get_session_secret(db), user.session_version or 0)


# How long a session trusts the admin / disabled state it last saw in Jellyfin.
_SESSION_RECHECK_SECONDS = 300
_session_checks: dict = {}  # jellyfin_user_id -> monotonic time of last good check


def _refresh_from_jellyfin(db: Session, user: TentacleUser) -> bool:
    """Re-read the user's Jellyfin account at most every few minutes.

    False when Jellyfin says the account is gone or disabled. The admin flag is
    copied across either way, so rights removed in Jellyfin's dashboard are
    removed here too rather than lasting until the next login. When Jellyfin
    cannot be asked (down, no API key) the last known state stands: locking
    every user out of Tentacle because Jellyfin is restarting would be worse,
    and nothing Tentacle does needs Jellyfin to be up to be refused.
    """
    now = time.monotonic()
    last = _session_checks.get(user.jellyfin_user_id)
    if last is not None and now - last < _SESSION_RECHECK_SECONDS:
        return True
    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key", "")
    if not jf_url or not jf_key:
        return True
    try:
        r = requests.get(f"{jf_url.rstrip('/')}/Users/{user.jellyfin_user_id}",
                         headers={"X-Emby-Token": jf_key}, timeout=5)
    except requests.RequestException as e:
        logger.warning(f"Could not re-check {user.display_name} with Jellyfin: {e}")
        return True
    if r.status_code == 404:
        _session_checks.pop(user.jellyfin_user_id, None)
        return False
    if r.status_code != 200:
        return True
    policy = (r.json() or {}).get("Policy") or {}
    if policy.get("IsDisabled"):
        _session_checks.pop(user.jellyfin_user_id, None)
        return False
    is_admin = bool(policy.get("IsAdministrator", False))
    if bool(user.is_admin) != is_admin:
        logger.info(f"{user.display_name}: admin {'granted' if is_admin else 'removed'} in Jellyfin")
        user.is_admin = is_admin
        db.commit()
    _session_checks[user.jellyfin_user_id] = now
    return True


def _user_from_cookie(request: Request, db: Session) -> Optional[TentacleUser]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    parsed = _verify_session(token, _get_session_secret(db))
    if parsed is None:
        return None
    user_id, issued_at, version = parsed
    if not (0 <= time.time() - issued_at <= COOKIE_MAX_AGE):
        return None
    user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
    if not user or version != (user.session_version or 0):
        return None
    if not _refresh_from_jellyfin(db, user):
        return None
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> TentacleUser:
    """Extract authenticated user from session cookie. Raises 401 if not logged in."""
    if not request.cookies.get(COOKIE_NAME):
        raise HTTPException(401, "Not authenticated")
    user = _user_from_cookie(request, db)
    if user is None:
        raise HTTPException(401, "Invalid session")
    return user


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[TentacleUser]:
    """Get current user or None (no 401). For endpoints that support both cookie and query param auth."""
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


def _resolve_token_user(db: Session, api_key: str) -> Optional[str]:
    """Return the normalized Jellyfin user id that owns `api_key`, or None.

    Calls Jellyfin's user-scoped /Users/Me with the token — only the real owner of
    a valid access token gets a 200 here, so this turns the api_key into proof of
    identity (not an unverified claim). Results are cached for `_TOKEN_CACHE_TTL`
    so high-frequency plugin polling does not hit Jellyfin on every request.
    """
    if not api_key:
        return None
    now = time.time()
    cached = _token_cache.get(api_key)
    if cached and cached[1] > now:
        return cached[0]
    jf_url = get_setting(db, "jellyfin_url")
    if not jf_url:
        return None
    try:
        r = requests.get(
            f"{jf_url.rstrip('/')}/Users/Me",
            headers={"X-Emby-Token": api_key},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        profile = r.json() or {}
        token_uid = str(profile.get("Id", "")).replace("-", "")
        if token_uid:
            # Opportunistically prune expired entries to bound memory growth.
            if len(_token_cache) > 512:
                for k, (_, exp) in list(_token_cache.items()):
                    if exp <= now:
                        _token_cache.pop(k, None)
            _token_cache[api_key] = (token_uid, now + _TOKEN_CACHE_TTL)
            _token_profiles[token_uid] = {
                "name": profile.get("Name") or token_uid,
                "is_admin": bool((profile.get("Policy") or {}).get("IsAdministrator", False)),
                "image_tag": profile.get("PrimaryImageTag"),
            }
            return token_uid
    except Exception as e:
        logger.warning(f"Jellyfin token validation failed: {e}")
    return None


def get_user_from_request(request: Request, db: Session = Depends(get_db)) -> TentacleUser:
    """Get user from session cookie OR from a *verified* ?api_key= access token.

    The userId is only a claim — identity is established by resolving the api_key
    (the caller's Jellyfin access token) to its real owner via Jellyfin. Without a
    valid token, anyone who could reach the backend could impersonate any user or
    admin simply by passing their userId, so a bare userId is never trusted. When
    a userId is also supplied it must match the token's owner. The cookie path
    remains HMAC-verified.
    """
    # Try cookie first (HMAC-signed session)
    try:
        return get_current_user(request, db)
    except HTTPException:
        pass
    # Query-param path: identity comes from the verified Jellyfin access token
    api_key = request.query_params.get("api_key")
    token_uid = _resolve_token_user(db, api_key) if api_key else None
    if token_uid:
        # If a userId was also claimed, it must belong to the same token owner.
        jf_user_id = request.query_params.get("userId")
        if jf_user_id and jf_user_id.replace("-", "") != token_uid:
            raise HTTPException(401, "Not authenticated")
        user = db.query(TentacleUser).filter(TentacleUser.jellyfin_user_id == token_uid).first()
        if user is None:
            user = _provision_plugin_user(db, token_uid)
        if user:
            # Same re-check as the cookie path (#80): the token proves who the
            # caller is, but admin rights removed in Jellyfin — or an account
            # disabled there — must stop working here too, not only in a browser.
            if not _refresh_from_jellyfin(db, user):
                raise HTTPException(401, "Not authenticated")
            return user
    raise HTTPException(401, "Not authenticated")


def _provision_plugin_user(db: Session, token_uid: str) -> Optional[TentacleUser]:
    """Create the Tentacle row for a Jellyfin user the token has just proved.

    Every route the plugin proxies needs a session, and a session needs a
    TentacleUser row — which only the dashboard login used to create. A
    household member who only ever uses Jellyfin therefore got "No results" in
    the Jellyfin search and an empty Discover tab. Their identity is exactly as
    verified as a login (Jellyfin answered /Users/Me for their token), so give
    them the row the login would have, with the admin flag Jellyfin reports.

    Never on an install with no users yet: the first Tentacle login becomes the
    owner and inherits the pre-multi-user data, and that must stay a deliberate
    dashboard login, not whichever TV happened to poll first.
    """
    if db.query(TentacleUser).count() == 0:
        return None
    profile = _token_profiles.get(token_uid) or {}
    user = TentacleUser(
        jellyfin_user_id=token_uid,
        display_name=profile.get("name") or token_uid,
        is_admin=bool(profile.get("is_admin", False)),
        profile_image_tag=profile.get("image_tag"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"Created Tentacle user '{user.display_name}' from a verified Jellyfin token")
    _build_playlists_for_new_user(user.id)
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> Optional[TentacleUser]:
    """Require admin role. Allows access in bootstrap mode (no users yet).
    Supports both session cookie and ?userId= query param (for plugin API calls)."""
    # Bootstrap mode: if no users exist, allow unauthenticated access
    if db.query(TentacleUser).count() == 0:
        return None
    user = get_user_from_request(request, db)
    if not user.is_admin:
        raise HTTPException(403, "Admin access required")
    return user


def _has_internal_secret(request: Request, db: Session) -> bool:
    """True if the request carries the shared internal secret (header or ?secret=).

    Used to authenticate trusted server-to-server callers — the Jellyfin plugin's
    delete/plugin-keys calls and Radarr/Sonarr webhooks — which cannot present a
    user session. Constant-time compared against the stored secret.
    """
    import hmac
    provided = request.headers.get("X-Tentacle-Secret") or request.query_params.get("secret")
    if not provided:
        return False
    secret = get_setting(db, "internal_secret", "")
    return bool(secret) and hmac.compare_digest(provided, secret)


def require_internal_or_admin(request: Request, db: Session = Depends(get_db)):
    """Allow trusted server-to-server callers (valid internal secret) OR an admin.

    Bootstrap mode (no users yet) is allowed so first-run setup is not blocked.
    """
    if db.query(TentacleUser).count() == 0:
        return None
    if _has_internal_secret(request, db):
        return None
    return require_admin(request, db)


# ─── Models ──────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str = ""


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/users")
def get_jellyfin_users(db: Session = Depends(get_db)):
    """Fetch Jellyfin users for the login picker. No auth required.

    Uses the unauthenticated /Users/Public endpoint, which only returns accounts
    Jellyfin itself shows on its login screen. The privileged /Users endpoint is
    NOT used here — calling it (with the server API key) over an unauthenticated
    route enumerated every account including admin-hidden ones plus their ids,
    which is exactly the data an attacker needs.
    """
    jf_url = get_setting(db, "jellyfin_url")
    if not jf_url:
        raise HTTPException(400, "Jellyfin URL not configured")
    try:
        browser_url = (get_setting(db, "jellyfin_public_url") or "").strip().rstrip("/") or jf_url.rstrip("/")
        r = requests.get(f"{jf_url.rstrip('/')}/Users/Public", timeout=10)
        r.raise_for_status()
        users = r.json()
        return [
            {
                "id": u["Id"],
                "name": u["Name"],
                # HasPassword is part of Jellyfin's own public login payload and the
                # picker needs it to decide whether to prompt for a password.
                "has_password": u.get("HasPassword", True),
                "image_tag": u.get("PrimaryImageTag"),
                # The BROWSER loads the avatar from this, so it has to be the address a
                # browser can reach: jellyfin_url is often a docker-internal name.
                "jellyfin_url": browser_url,
            }
            for u in users
        ]
    except Exception as e:
        logger.error(f"Failed to fetch Jellyfin users: {e}")
        raise HTTPException(502, f"Could not reach Jellyfin: {e}")


@router.post("/login")
def login(body: LoginRequest, response: Response, request: Request, db: Session = Depends(get_db)):
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
        created = True
    else:
        user.display_name = jf_user_name
        user.profile_image_tag = jf_image_tag
        user.is_admin = jf_is_admin  # Sync admin status on every login
        created = False

    db.commit()

    if created:
        # Give the new user their playlists now. Playlists are per-user and are
        # created for the users that exist when something publishes; a user who
        # logs in afterwards used to have none until the nightly sync — a day
        # with an empty "add row" list on a fresh install, where the channel
        # was typically added before anyone had logged in at all.
        _build_playlists_for_new_user(user.id)

    # Set session cookie. Mark Secure when the request reached us over HTTPS
    # (Cloudflare tunnel sets X-Forwarded-Proto) so the session token is not sent
    # in cleartext over the public domain — but stay non-Secure for plain-HTTP LAN
    # access (http://<ip>:8888) so local logins keep working.
    token = _issue_session(db, user)
    _session_checks[user.jellyfin_user_id] = time.monotonic()
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=(forwarded_proto == "https"),
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
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Clear the session cookie and revoke the sessions it came from.

    Bumping the user's session version ends every session they hold, on every
    device — the only way to end the one a copied cookie is using. Logging out
    of one browser signing out the others is the usual price of that.
    """
    user = _user_from_cookie(request, db)
    if user is not None:
        user.session_version = (user.session_version or 0) + 1
        db.commit()
        _session_checks.pop(user.jellyfin_user_id, None)
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
    first_user = db.query(TentacleUser).order_by(TentacleUser.id).first()
    first_jf_id = first_user.jellyfin_user_id if first_user else None
    try:
        r = requests.get(
            f"{jf_url.rstrip('/')}/Users",
            headers={"X-Emby-Token": jf_key},
            timeout=10,
        )
        if r.status_code == 401:
            raise HTTPException(502, "Jellyfin API key is invalid — generate a new one in Jellyfin Dashboard → API Keys")
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
                "is_owner": u["Id"] == first_jf_id,
            }
            for u in users
        ]
    except HTTPException:
        raise
    except requests.ConnectionError:
        raise HTTPException(502, f"Cannot reach Jellyfin at {jf_url} — check the URL and ensure Jellyfin is running")
    except requests.Timeout:
        raise HTTPException(504, "Jellyfin connection timed out — the server may be under heavy load")
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

    # Prevent removing admin from the initial setup user (lowest ID = first login)
    if not body.is_admin:
        first_user = db.query(TentacleUser).order_by(TentacleUser.id).first()
        if first_user and first_user.jellyfin_user_id == body.jellyfin_user_id:
            raise HTTPException(400, "Cannot remove admin from the initial setup user")

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
        logger.info(f"Setting admin={body.is_admin} for user {body.jellyfin_user_id} (was {policy.get('IsAdministrator')})")
        policy["IsAdministrator"] = body.is_admin

        # Update policy in Jellyfin
        pr = requests.post(
            f"{jf_url.rstrip('/')}/Users/{body.jellyfin_user_id}/Policy",
            headers={"X-Emby-Token": jf_key, "Content-Type": "application/json"},
            json=policy,
            timeout=10,
        )
        logger.info(f"Jellyfin policy update response: {pr.status_code}")
        pr.raise_for_status()

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

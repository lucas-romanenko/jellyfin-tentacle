"""Telling someone new what's what (Roadmap 1e): the message an admin sends
when they add someone, and the welcome a person sees at their first
sign-in to Tentacle.

Family "don't know what's what", so both say the same thing in the same
order. Tentacle comes first, because it's where they choose what they see
(their channels, their home screen, asking for movies and shows); then
where they watch (Jellyfin's address: phones, laptops, computers, and on an
iPhone or iPad the website, not an app); the TV app last, as an extra.
Every address comes from the household's own settings (family.addresses),
so every install's message shows its own server.

The welcome is shown once (TentacleUser.welcomed_at), every step can be
skipped, and a kid's is the same with only what a kid has (no requests).
"""
import logging
import threading
import time
from datetime import datetime
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import TentacleUser, get_setting

logger = logging.getLogger(__name__)


def display_name(name: str) -> str:
    """A name as people write it: "lucas" → "Lucas"; "DJ", "van Dam" stay."""
    name = (name or "").strip()
    return name[:1].upper() + name[1:] if name and name == name.lower() else name


def server_name(db: Session, admin: Optional[TentacleUser] = None) -> str:
    """Whose server it is, in the person's words: "Lucas's server"."""
    from services.family import admin_of
    admin = admin or admin_of(db)
    return f"{display_name(admin.display_name)}'s server" if admin else "your family's server"


def message(db: Session, admin: Optional[TentacleUser], user: TentacleUser, password: str,
            new_password: bool = False) -> str:
    """The ready-to-send message for someone just added (or given a new
    password): plain text for a chat or an e-mail."""
    from services.family import addresses
    a = addresses(db)
    name = display_name(user.display_name)
    kid = bool(user.kids_level)
    home_only = "" if a["tentacle_away"] else " (at home, on your Wi-Fi)"
    lines = []
    if new_password:
        lines += [f"Hi {name}, here's your new password for {server_name(db, admin)}.", ""]
    else:
        lines += [f"Hi {name}! You have an account on {server_name(db, admin)}. Here's how to start.", ""]
    lines += [f"1. First open Tentacle: {a['tentacle']}{home_only}" if a["tentacle"] else "1. First open Tentacle.",
              "It's where you set up your home screen." if kid else
              "It's where you pick your channels, set up your home screen and ask for movies and shows.",
              "",
              "Sign in with:",
              "Name: " + user.display_name,
              "Password: " + password,
              "It's the same name and password for everything below.",
              ""]
    jf_home_only = "" if a["jellyfin_away"] else " (at home, on your Wi-Fi)"
    lines += [f"2. Then watch at {a['jellyfin']}{jf_home_only}" if a["jellyfin"] else "2. Then watch in Jellyfin.",
              "On a phone, laptop or computer, open it in the browser. On an iPhone or iPad, use the website too.",
              ""]
    if a["tv_app"]:
        lines += ["Got an Android TV or Google TV? Install the free Downloader app, open it and type "
                  f"{a['tv_app']} to get the Tentacle TV app."]
    return "\n".join(lines).strip() + "\n"


def state(db: Session, user: TentacleUser) -> dict:
    """What the welcome shows this person (GET /api/welcome)."""
    from services import approvals, family
    from services.smartlists import get_home_config
    owner = family.admin_of(db)
    show = user.welcomed_at is None and not (owner is not None and owner.id == user.id)
    a = family.addresses(db)
    try:
        cfg = get_home_config(db, user_id=user.id) or {}
    except Exception:
        cfg = {}
    rows = [r.get("display_name") for r in sorted(cfg.get("rows") or [], key=lambda r: r.get("order", 0))
            if r.get("display_name")]
    return {
        "show": show,
        "name": display_name(user.display_name),
        "server": server_name(db),
        "admin": display_name(owner.display_name) if owner else "your admin",
        "kids": bool(user.kids_level),
        "live_tv": not user.live_tv_off,
        "requests": approvals.request_mode(db, user),
        "home_rows": rows[:8],
        "jellyfin_address": a["jellyfin"],
        "tv_app_address": a["tv_app"],
        "counts": counts(db, user) if show else None,
    }


# ── "You now have access to 12,345 movies, 1,234 TV shows and 1,730 live TV
# channels!" (the hello step). Movies and shows: the titles this person can
# open in Jellyfin, downloaded and provider (VOD) alike, a title with both
# copies once (counted by TMDB id); a kid or someone with only some
# libraries by what Jellyfin shows them (family.visible_titles), everyone
# else from Discover's cached TMDB -> item maps. Channels: their lineup
# (the pool after folding and hiding; a kid's kids channels), none when
# Live TV is off for them. Reading a large lineup and the maps takes a
# second or more, so the counts are made in the background and kept: when
# an admin adds someone, at start-up and hourly for everyone not welcomed
# yet, and when the welcome asks. An answer older than COUNTS_FRESH_S is
# still given (and counted again behind it).
COUNTS_WAIT_S = 0.25          # inside GET /api/welcome
COUNTS_LATE_WAIT_S = 4.0      # GET /api/welcome/counts, when the first answer had none
COUNTS_FRESH_S = 600
COUNTS_KEEP_S = 6 * 3600
_counts_lock = threading.Lock()
_counts_cache = {}            # user id -> (time, {"movies", "shows", "channels"})
_counts_running = {}          # user id -> Future
_counts_pool = None


def _count(db_factory, user_id: int) -> dict:
    from routers.library import _sees_only_some
    from services.family import visible_titles
    db = db_factory()
    try:
        user = db.get(TentacleUser, user_id)
        if user is None:
            return {}
        if _sees_only_some(db, user):
            seen = visible_titles(db, user)
            out = {"movies": sum(1 for t, _ in seen if t == "movie"),
                   "shows": sum(1 for t, _ in seen if t == "series")}
        else:
            from routers import discover
            # A map being refreshed is used meanwhile (stale-while-revalidate).
            deadline = time.time() + 60
            out = {"movies": len(discover._search_jellyfin_ids("movie", deadline)),
                   "shows": len(discover._search_jellyfin_ids("series", deadline))}
        out["channels"] = 0
        if not user.live_tv_off:
            try:
                from routers.my_livetv import my_lineup
                out["channels"] = len(my_lineup(db, user))
            except Exception as e:
                logger.info(f"[Welcome] Channels for {user.display_name} not counted: {e}")
    finally:
        db.close()
    with _counts_lock:
        _counts_cache[user_id] = (time.time(), out)
    return out


def warm_counts(user_id: int):
    """Count this person's titles and channels in the background (once at a time)."""
    from concurrent.futures import ThreadPoolExecutor
    from models.database import SessionLocal
    global _counts_pool
    with _counts_lock:
        fut = _counts_running.get(user_id)
        if fut is not None and not fut.done():
            return fut
        if _counts_pool is None:
            _counts_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="welcome-counts")
        fut = _counts_pool.submit(_count, SessionLocal, user_id)
        _counts_running[user_id] = fut
        return fut


def warm_everyone_due() -> int:
    """Counts for everyone the welcome is still due for (start-up, hourly)."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        ids = [i for (i,) in db.query(TentacleUser.id).filter(TentacleUser.welcomed_at.is_(None)).all()]
    finally:
        db.close()
    for i in ids:
        warm_counts(i)
    return len(ids)


def counts(db: Session, user: TentacleUser, wait: float = COUNTS_WAIT_S) -> Optional[dict]:
    """{"movies", "shows", "channels"} for this person, or None when they
    aren't counted yet (asked again by GET /api/welcome/counts)."""
    with _counts_lock:
        hit = _counts_cache.get(user.id)
    age = time.time() - hit[0] if hit else None
    if hit and age < COUNTS_KEEP_S:
        if age >= COUNTS_FRESH_S:
            warm_counts(user.id)
        out = hit[1]
    else:
        try:
            out = warm_counts(user.id).result(timeout=wait)
        except Exception:
            return None
    return dict(out) if out else None


def done(db: Session, user: TentacleUser) -> None:
    if user.welcomed_at is None:
        user.welcomed_at = datetime.utcnow()
        db.commit()
        logger.info(f"[Welcome] {user.display_name} finished the welcome")


class PasswordError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def change_password(db: Session, user: TentacleUser, current: str, new: str) -> None:
    """Someone picks their own password: the one they signed in with is
    checked with Jellyfin first (a wrong one counts towards the lock, as
    any wrong password does), then Jellyfin takes the new one."""
    from routers.auth import _JF_CLIENT
    from services.family import FamilyError, check_password, dashed, jellyfin
    try:
        check_password(new)
    except FamilyError as e:
        raise PasswordError(e.message)
    if new == current:
        raise PasswordError("That's the password you have now. Pick another, or keep it.")
    url = (get_setting(db, "jellyfin_url", "") or "").rstrip("/")
    if not url:
        raise PasswordError("Jellyfin isn't connected yet.", 409)
    try:
        r = requests.post(f"{url}/Users/AuthenticateByName", json={"Username": user.display_name, "Pw": current or ""},
                          headers={"Authorization": _JF_CLIENT, "Content-Type": "application/json"}, timeout=10)
    except requests.RequestException:
        raise PasswordError("Jellyfin can't be reached right now. Try again in a moment.", 502)
    if r.status_code == 401:
        raise PasswordError("That isn't the password you signed in with.", 403)
    if not r.ok:
        raise PasswordError(f"Jellyfin answered HTTP {r.status_code}. Try again in a moment.", 502)
    try:
        jellyfin(db).post("/Users/Password", {"CurrentPw": current, "NewPw": new, "ResetPassword": False},
                          userId=dashed(user.jellyfin_user_id))
    except FamilyError as e:
        raise PasswordError(e.message, e.status)
    except requests.RequestException:
        raise PasswordError("Jellyfin didn't take the new password. Try again in a moment.", 502)
    logger.info(f"[Welcome] {user.display_name} picked their own password")

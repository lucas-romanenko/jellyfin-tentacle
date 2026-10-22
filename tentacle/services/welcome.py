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
    }


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

"""Family accounts (Roadmap 1c): invites, kids' profiles, the defaults new
people start with, and signing a TV in with a code.

Who it's for: an admin running a home server who gives accounts to family.
Everything here is the admin's, except signing one's own TV in.

- **Add someone** (Roadmap 1e, three steps on People): who (name, age
  limit, place, a password made from the name that the admin may change),
  what they get (Live TV, requests, the home screen and favourites they
  start with: the household's defaults, or for kids the ready-made kids
  setup) and a message to send them (services/welcome.py writes it).
- **Kids profile:** one switch per person with an age level. It sets
  Jellyfin's own parental controls (MaxParentalRating, unrated items
  blocked), and Live TV becomes the household's kids channels only (the
  other channels of the one lineup are blocked by tag, Roadmap 1b), or none
  at all while no kids channels are picked. The policy fields it changes
  are saved first and put back when it's switched off.
- **TV sign-in code:** the TV app shows a Quick Connect code; typing it in
  Tentacle signs that TV in as the person (Jellyfin's
  /QuickConnect/Authorize with userId, which Tentacle's admin key may do).
"""
import json
import logging
import re
import secrets
import threading
import unicodedata
from datetime import datetime
from typing import Optional

import requests
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.database import TentacleUser, get_setting, set_setting
from services import livetv_lineups as L

logger = logging.getLogger(__name__)

# Jellyfin's rating scores are ages and the same in every country's table
# (Emby.Server.Implementations/Localization/Ratings): G/TV-Y 0, TV-Y7 7,
# PG/TV-PG 10, PG-13 13, TV-14 14, R/TV-MA 17.
KIDS_LEVELS = {
    "young": {"score": 7, "name": "Young kids", "allows": "up to TV-Y7"},
    "kids": {"score": 10, "name": "Kids", "allows": "up to PG"},
    "teens": {"score": 14, "name": "Teens", "allows": "up to PG-13"},
}
# Unrated movies and shows are hidden from kids: about a third of a typical
# VOD library has no rating, and "unknown" isn't "safe". Guide programmes
# aren't: Tentacle's guide carries no ratings, so blocking them left kids an
# empty guide, and their channels are the kids channels an admin picked.
KIDS_BLOCK_UNRATED = ["Movie", "Series", "Trailer"]
# What kids profiles blocked before (Oct 2026), taken off with them.
_KIDS_BLOCKED_BEFORE = set(KIDS_BLOCK_UNRATED) | {"LiveTvProgram"}
POLICY_FIELDS = ("MaxParentalRating", "MaxParentalSubRating", "BlockUnratedItems", "EnableLiveTvAccess")

KIDS_CHANNELS_KEY = "livetv_kids_channels"   # JSON list of channel keys (livetv_lineups.pool)
DEFAULTS_KEY = "family_defaults"             # JSON {"home_from": user id, "channels_from": user id}
# What new kids start with: {"home": "kids" | "default" | user id,
# "channels": "kids" | "none"}. Until an admin picks, the ready-made kids
# setup: the kids home (services/kids_start.py) and the kids channels starred.
KIDS_DEFAULTS_KEY = "family_kids_defaults"
KIDS_DEFAULTS = {"home": "kids", "channels": "kids"}

NAME_RE = re.compile(r"^[\w\-'. @]{1,64}$", re.UNICODE)


class FamilyError(Exception):
    """A refusal with words for the admin (or the person) and an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def jellyfin(db):
    from services.livetv_tuners import jellyfin_for
    jf = jellyfin_for(db)
    if jf is None:
        raise FamilyError("Jellyfin isn't connected yet (Settings → Services).", 409)
    return jf


def dashed(user_id: str) -> str:
    u = L.norm_user_id(user_id)
    return f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}" if len(u) == 32 else user_id


def _jf_error(e: requests.RequestException, what: str) -> FamilyError:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return FamilyError(f"Jellyfin didn't {what} (HTTP {e.response.status_code}).", 502)
    return FamilyError("Jellyfin can't be reached right now.", 502)


# ── Passwords ───────────────────────────────────────────────────────────────

# Jellyfin turns an account off after this many wrong passwords in a row
# (its LoginAttemptsBeforeLockout); People shows it locked, with Unlock.
# Admins are left as they are: a locked-out admin couldn't unlock anyone.
LOCKOUT_AFTER = 5
PASSWORD_MAX = 128


def password_base(name: str) -> str:
    """The name part of a made-up password: lowercase letters and digits
    ("Grandma Rose" → "grandmarose", "Zoë" → "zoe"); "tv" for a name with
    none (another alphabet)."""
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", plain)[:16] or "tv"


def make_password(name: str = "") -> str:
    """Their name in lowercase, five random digits and "!" (Cam →
    "cam12457!", Lucas's design): easy to read out and to type on a TV
    remote. The admin can change it before the account is made, and the
    person can pick their own at their first sign-in. The welcome.js
    wizard makes the same shape in the browser."""
    return f"{password_base(name)}{secrets.randbelow(100000):05d}!"


def check_password(password: str) -> str:
    if not isinstance(password, str) or len(password.strip()) < 4:
        raise FamilyError("Use a password of at least 4 characters.")
    if len(password) > PASSWORD_MAX:
        raise FamilyError(f"Use a password of at most {PASSWORD_MAX} characters.")
    return password


# ── Defaults new people start with ──────────────────────────────────────────

def defaults(db: Session) -> dict:
    try:
        d = json.loads(get_setting(db, DEFAULTS_KEY, "") or "{}")
    except ValueError:
        d = {}
    return {"home_from": d.get("home_from") if isinstance(d.get("home_from"), int) else None,
            "channels_from": d.get("channels_from") if isinstance(d.get("channels_from"), int) else None}


def kids_defaults(db: Session) -> dict:
    """What new kids start with (People → New kids start with)."""
    try:
        d = json.loads(get_setting(db, KIDS_DEFAULTS_KEY, "") or "{}")
    except ValueError:
        d = {}
    home = d.get("home")
    if not (home in ("kids", "default") or isinstance(home, int)):
        home = KIDS_DEFAULTS["home"]
    channels = d.get("channels") if d.get("channels") in ("kids", "none") else KIDS_DEFAULTS["channels"]
    return {"home": home, "channels": channels}


def save_defaults(db: Session, home_from: Optional[int], channels_from: Optional[int],
                  kids: Optional[dict] = None) -> dict:
    for uid in (home_from, channels_from):
        if uid is not None and not db.query(TentacleUser).filter(TentacleUser.id == uid).first():
            raise FamilyError("That person isn't in Tentacle.", 404)
    set_setting(db, DEFAULTS_KEY, json.dumps({"home_from": home_from, "channels_from": channels_from}))
    if kids is not None:
        home = start_choice(db, kids.get("home"), kid=True)
        channels = kids.get("channels")
        if channels not in ("kids", "none"):
            raise FamilyError("Pick the kids channels or none.")
        set_setting(db, KIDS_DEFAULTS_KEY, json.dumps({"home": home, "channels": channels}))
    return {**defaults(db), "kids": kids_defaults(db)}


def start_choice(db: Session, value, kid: bool):
    """A home screen to start someone on: "default" (the default home),
    "kids" (the kids home, kids only) or a person's id (the same as theirs)."""
    if value in ("default", "kids"):
        if value == "kids" and not kid:
            raise FamilyError("The kids home is for kids.")
        return value
    try:
        uid = int(value)
    except (TypeError, ValueError):
        raise FamilyError("Pick a home screen to start with.")
    if not db.query(TentacleUser).filter(TentacleUser.id == uid).first():
        raise FamilyError("That person isn't in Tentacle.", 404)
    return uid


def copy_home(db: Session, template: TentacleUser, user: TentacleUser) -> int:
    """Start `user` on `template`'s home screen: the same Tentacle playlists
    switched on, then the same rows (built-in sections, and playlist rows
    matched by name to the new person's own playlists), hero and toolbar.
    Playlists only the template has (their own lists, their Downloads) are
    left out. Returns the number of rows written. Runs before the new
    person's first home config exists."""
    from models.database import AutoPlaylistToggle
    from services.smartlists import (
        _get_smartlists_with_playlist_ids, _notify_jellyfin_plugin, _user_home_config_path,
        bump_playlist_version, get_home_config, refresh_smartlist_playlists, sync_smartlists,
        write_home_config, write_home_json,
    )
    for t in db.query(AutoPlaylistToggle).filter(AutoPlaylistToggle.user_id == template.id).all():
        if not db.query(AutoPlaylistToggle).filter(AutoPlaylistToggle.user_id == user.id,
                                                   AutoPlaylistToggle.key == t.key).first():
            db.add(AutoPlaylistToggle(user_id=user.id, key=t.key, enabled=t.enabled))
    db.commit()
    sync_smartlists(db, user_id=user.id)
    refresh_smartlist_playlists(db, user_id=user.id)
    src = get_home_config(db, user_id=template.id) or {}
    mine = _get_smartlists_with_playlist_ids(db, user_id=user.id)
    by_name = {}
    for sl in mine:
        by_name.setdefault(sl["name"], []).append(sl["playlist_id"])
    rows = []
    for r in src.get("rows") or []:
        if r.get("type") == "builtin":
            rows.append({k: v for k, v in r.items() if k != "unresolved_since"})
        elif len(by_name.get(r.get("display_name"), [])) == 1:
            rows.append({**{k: v for k, v in r.items() if k != "unresolved_since"},
                         "type": "playlist", "playlist_id": by_name[r["display_name"]][0]})
    for i, r in enumerate(rows, 1):
        r["order"] = i
    cfg = {k: v for k, v in src.items() if k in ("toolbar",)}
    cfg["rows"] = rows
    hero = dict(src.get("hero") or {})
    if hero:
        ids = by_name.get(hero.get("display_name"), [])
        if hero.get("playlist_id") and len(ids) != 1:
            hero = {**hero, "enabled": False, "playlist_id": "", "display_name": ""}
        elif ids:
            hero["playlist_id"] = ids[0]
        cfg["hero"] = hero
    write_home_json(_user_home_config_path(db, user.id), cfg)
    write_home_config(db, user_id=user.id)
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    return len(rows)


def starter_favourites(db: Session) -> Optional[TentacleUser]:
    """Whose favourite channels new people start with (Family → "New people
    start with"), None when nobody is chosen: then they start with no
    favourites and the whole lineup."""
    d = defaults(db)
    if not d["channels_from"]:
        return None
    return db.query(TentacleUser).filter(TentacleUser.id == d["channels_from"]).first()


def copy_channels(db: Session, template: TentacleUser, user: TentacleUser) -> int:
    """Start `user` with `template`'s favourite channels: there is one
    lineup, so the same Jellyfin channels."""
    from services import livetv_tuners as T
    jf = T.jellyfin_for(db)
    if jf is None:
        return 0
    try:
        theirs = T.favourite_ids(jf, L.norm_user_id(template.jellyfin_user_id))
        have = T.favourite_ids(jf, L.norm_user_id(user.jellyfin_user_id))
        for item_id in theirs - have:
            T.set_favourite(jf, L.norm_user_id(user.jellyfin_user_id), item_id, True)
        return len(theirs - have)
    except requests.RequestException as e:
        logger.warning(f"[Family] {user.display_name}'s starting favourites not set: {e}")
        return 0


def starting_point(db: Session, user: TentacleUser, home=None, channels=None) -> dict:
    """What someone starts with: {"home": "default" | "kids" | user id,
    "channels": "none" | "kids" | user id}. The admin's choice in Add someone
    when given, else the household's (kids: New kids start with)."""
    if user.kids_level:
        d = kids_defaults(db)
        return {"home": d["home"] if home is None else home, "channels": d["channels"] if channels is None else channels}
    d = defaults(db)
    return {"home": (d["home_from"] or "default") if home is None else home,
            "channels": (d["channels_from"] or "none") if channels is None else channels}


def start_new_person(user_id: int, home=None, channels=None) -> None:
    """In the background: the home screen and favourite channels they start
    with (starting_point), else the usual new-person playlists
    (routers.auth._build_playlists_for_new_user)."""
    def _run():
        from models.database import SessionLocal
        from services import kids_start, livetv_tuners
        db = SessionLocal()
        try:
            user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
            if user is None:
                return
            start = starting_point(db, user, home, channels)
            if start["channels"] == "kids" and user.kids_level:
                n = kids_start.star_kids_channels(db, user)
                logger.info(f"[Family] {user.display_name} starts with the {n} kids channel(s) as favourites")
            elif isinstance(start["channels"], int) and start["channels"] != user.id and not user.kids_level:
                chan_t = db.query(TentacleUser).filter(TentacleUser.id == start["channels"]).first()
                if chan_t is not None:
                    n = copy_channels(db, chan_t, user)
                    logger.info(f"[Family] {user.display_name} starts with {chan_t.display_name}'s "
                                f"{n} favourite channel(s)")
            # Their blocked tags now (a kids profile), not at the hourly check.
            livetv_tuners.schedule_sync(f"{user.display_name} joined")
            home_t = (db.query(TentacleUser).filter(TentacleUser.id == start["home"]).first()
                      if isinstance(start["home"], int) else None)
            if start["home"] == "kids" and user.kids_level:
                n = kids_start.kids_home(db, user)
                logger.info(f"[Family] {user.display_name} starts with the kids home ({n} rows)")
            elif home_t is not None and home_t.id != user.id:
                n = copy_home(db, home_t, user)
                logger.info(f"[Family] {user.display_name} starts with {home_t.display_name}'s home screen ({n} rows)")
            else:
                from routers.auth import _build_playlists_for_new_user
                _build_playlists_for_new_user(user.id)
            # What their welcome will count, ready before they first sign in.
            from services import welcome
            welcome.warm_counts(user.id)
        except Exception as e:
            logger.warning(f"[Family] Could not set up the defaults for user {user_id}: {e}")
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name=f"family-new-{user_id}").start()


# ── Invites ─────────────────────────────────────────────────────────────────

TENTACLE_PUBLIC_KEY = "tentacle_public_url"    # Tentacle's address for browsers (optional)
JELLYFIN_PUBLIC_KEY = "jellyfin_public_url"     # Jellyfin's, for browsers (Settings → Services)
ADDRESSES_ASKED_KEY = "invite_addresses_asked"  # "1" once Add someone asked about the addresses

_LOCAL_SUFFIXES = (".lan", ".local", ".home", ".internal", ".localdomain", ".home.arpa", ".localhost")


def _host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def reachable_away(url: str) -> bool:
    """Whether an address could work away from home (pure): a name with a
    dot that isn't a home-network one, or a public IP. http://192.168.1.10,
    http://jellyfin:8096 and http://nas.lan are home-only."""
    import ipaddress
    host = _host(url)
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    return "." in host and host != "localhost" and not host.endswith(_LOCAL_SUFFIXES)


def opens_in_a_browser(url: str) -> bool:
    """Whether a browser on the home network can open an address (pure): not
    a Docker name like http://jellyfin:8096 or localhost, which only the
    server itself reaches."""
    import ipaddress
    host = _host(url)
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return "." in host


def addresses(db: Session) -> dict:
    """Where people open Tentacle and Jellyfin in a browser, for the message
    and the welcome: the "for your browser" addresses (Settings → Services),
    else the ones Tentacle uses when a browser can open them. `*_away` says
    whether it works away from home too; `ask` is true until the admin has
    answered Add someone's one question about them (asked once)."""
    from models.database import tentacle_address
    setting = lambda k: (get_setting(db, k, "") or "").strip().rstrip("/")
    jf = setting(JELLYFIN_PUBLIC_KEY) or next(
        (u for u in (setting("jellyfin_url"),) if opens_in_a_browser(u)), "")
    tent = setting(TENTACLE_PUBLIC_KEY) or next(
        (u for u in (setting("external_url"), tentacle_address(db)) if reachable_away(u)), "") or next(
        (u for u in (setting("external_url"), tentacle_address(db)) if opens_in_a_browser(u)), "")
    jf_away, tent_away = reachable_away(jf), reachable_away(tent)
    return {"jellyfin": jf, "tentacle": tent, "tv_app": f"{tent}/tv" if tent else "",
            "jellyfin_away": jf_away, "tentacle_away": tent_away,
            "ask": not (jf and tent and jf_away and tent_away) and setting(ADDRESSES_ASKED_KEY) != "1"}


def save_addresses(db: Session, tentacle: Optional[str], jellyfin: Optional[str]) -> dict:
    """Add someone's one question, answered: the addresses family open (on
    the internet, or at home only). Not asked again; Settings → Services
    has them."""
    for key, val in ((TENTACLE_PUBLIC_KEY, tentacle), (JELLYFIN_PUBLIC_KEY, jellyfin)):
        if val is None:
            continue
        val = val.strip().rstrip("/")
        if val and not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", val, re.I):
            raise FamilyError("Type the whole address, starting with https:// (or http://).")
        if val:
            set_setting(db, key, val)
    set_setting(db, ADDRESSES_ASKED_KEY, "1")
    return addresses(db)


def invite_card(db: Session, name: str, password: Optional[str]) -> dict:
    a = addresses(db)
    return {"name": name, "password": password,
            **{f"{k}_address": a[k] for k in ("jellyfin", "tentacle", "tv_app")}}


def invite(db: Session, admin: TentacleUser, name: str, kids_level: Optional[str] = None,
           request_mode: Optional[str] = None, location: Optional[str] = None,
           password: Optional[str] = None, live_tv: bool = True, home=None, channels=None) -> dict:
    from services import livetv_locations, welcome
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise FamilyError("Use a name of letters, numbers, spaces, dots, dashes or apostrophes (up to 64).")
    if kids_level and kids_level not in KIDS_LEVELS:
        raise FamilyError("Unknown kids level.")
    if request_mode not in (None, "ask", "direct"):
        raise FamilyError("Unknown request setting.")
    if location and not livetv_locations.get(location):
        raise FamilyError("Unknown place.")
    password = check_password(password) if password else make_password(name)
    if home is not None:
        home = start_choice(db, home, kid=bool(kids_level))
    if channels is not None and channels not in ("none", "kids"):
        try:
            channels = int(channels)
        except (TypeError, ValueError):
            raise FamilyError("Pick whose favourite channels they start with.")
        if kids_level or not db.query(TentacleUser).filter(TentacleUser.id == channels).first():
            raise FamilyError("Pick whose favourite channels they start with.")
    if channels == "kids" and not kids_level:
        channels = "none"
    jf = jellyfin(db)
    try:
        users = jf.get("/Users") or []
    except requests.RequestException as e:
        raise _jf_error(e, "list its accounts")
    if any((u.get("Name") or "").lower() == name.lower() for u in users):
        raise FamilyError(f"Someone called {name} already has an account.", 409)
    try:
        created = jf.post("/Users/New", {"Name": name, "Password": password}) or {}
    except requests.RequestException as e:
        raise _jf_error(e, "make the account")
    uid = L.norm_user_id(created.get("Id") or "")
    if len(uid) != 32:
        raise FamilyError("Jellyfin made the account but didn't say its id.", 502)
    # Jellyfin's defaults hide new accounts from its login screens and leave
    # them unable to record; an invited person starts on the household's
    # (services/person_access.py). A policy POST replaces the whole policy,
    # so it starts from Jellyfin's.
    from services.person_access import new_person_policy
    from services.youtube import follows
    try:
        policy = _policy(jf, _Uid(uid))
        if policy:
            policy = new_person_policy(policy, bool(kids_level), live_tv)
            # Nobody's YouTube channels: they add their own (services/youtube/follows.py).
            policy["BlockedTags"] = follows.with_blocked(policy.get("BlockedTags"), follows.blocked_for(db, uid))
            jf.post(f"/Users/{dashed(uid)}/Policy", policy)
    except (requests.RequestException, FamilyError) as e:
        logger.warning(f"[Family] {name}'s starting settings weren't set in Jellyfin: {e}")
    row = db.query(TentacleUser).filter(TentacleUser.jellyfin_user_id == uid).first()
    if row is None:
        row = TentacleUser(jellyfin_user_id=uid, display_name=created.get("Name") or name, is_admin=False)
        db.add(row)
    def _fill(r):
        r.invited_by, r.invited_at = admin.display_name, datetime.utcnow()
        r.request_mode = request_mode or "ask"
        r.livetv_location = _own_place(db, location)
        r.live_tv_off = not live_tv
        r.welcomed_at = None
    _fill(row)
    try:
        db.commit()
    except IntegrityError:   # the plugin made the row in between (a first plugin call)
        db.rollback()
        row = db.query(TentacleUser).filter(TentacleUser.jellyfin_user_id == uid).first()
        _fill(row)
        db.commit()
    logger.info(f"[Family] {admin.display_name} invited {row.display_name}"
                f"{' (kids: ' + kids_level + ')' if kids_level else ''}{'' if live_tv else ', no Live TV'}")
    if kids_level:
        set_kids(db, row, kids_level)
    start_new_person(row.id, home, channels)
    card = invite_card(db, row.display_name, password)
    card["message"] = welcome.message(db, admin, row, password)
    return {"user": person(db, row, created), "invite": card}


# ── Where someone watches from ──────────────────────────────────────────────

def _own_place(db: Session, location: Optional[str]) -> Optional[str]:
    """A place to store for someone: None for the household's own (they follow it)."""
    from services import livetv_locations
    slug = (location or "").strip().lower() or None
    if slug is None or not livetv_locations.get(slug):
        return None
    return None if slug == livetv_locations.household(db) else slug


def set_location(db: Session, user: TentacleUser, location: Optional[str]) -> dict:
    """Where someone watches Live TV from (an admin sets it; "" / None: the
    household's). Their local and regional channels follow: the lineup
    carries their place's variants, their favourites move to them and they
    no longer see other places' (services/livetv_tuners.py). The first
    admin's place is the household's: people without one follow it."""
    from services import livetv_locations, livetv_tuners
    slug = (location or "").strip().lower() or None
    if slug and not livetv_locations.get(slug):
        raise FamilyError("Unknown place.")
    owner = (db.query(TentacleUser).filter(TentacleUser.is_admin == True)  # noqa: E712
             .order_by(TentacleUser.id).first())
    household = owner is not None and owner.id == user.id
    if household:
        # The household's place: kept as the admin's own, never "none".
        slug = slug or livetv_locations.household(db)
    elif slug == livetv_locations.household(db):
        slug = None
    if user.livetv_location == slug:
        return person(db, user)
    user.livetv_location = slug
    db.commit()
    where = livetv_locations.public(livetv_locations.of_person(db, user))
    logger.info(f"[Family] {user.display_name} watches Live TV from {where['label'] if where else 'the household'}")
    # The household's place chooses every network's default variant (and so
    # its guide): Jellyfin reads the guide again then.
    livetv_tuners.schedule_sync(f"{user.display_name}'s place", refresh=household, delay=1)
    return person(db, user)


def new_password(db: Session, user: TentacleUser) -> dict:
    """A fresh password for someone who lost theirs (admins only; never an admin's)."""
    if user.is_admin:
        raise FamilyError("Admins change their own password in Jellyfin.", 403)
    from services import welcome
    jf = jellyfin(db)
    password = make_password(user.display_name)
    try:
        jf.post("/Users/Password", {"NewPw": password, "ResetPassword": False}, userId=dashed(user.jellyfin_user_id))
    except requests.RequestException as e:
        raise _jf_error(e, "change the password")
    logger.info(f"[Family] New password for {user.display_name}")
    card = invite_card(db, user.display_name, password)
    card["message"] = welcome.message(db, admin_of(db), user, password, new_password=True)
    return card


def admin_of(db: Session) -> Optional[TentacleUser]:
    """The household's admin: the owner (Tentacle's own Jellyfin account),
    else the first admin."""
    owner = (get_setting(db, "jellyfin_user_id", "") or "").replace("-", "")
    admins = db.query(TentacleUser).filter(TentacleUser.is_admin == True).order_by(TentacleUser.id).all()  # noqa: E712
    return next((a for a in admins if a.jellyfin_user_id == owner), admins[0] if admins else None)


# ── Kids profiles ───────────────────────────────────────────────────────────

def kids_channels(db: Session) -> list:
    try:
        keys = json.loads(get_setting(db, KIDS_CHANNELS_KEY, "") or "[]")
    except ValueError:
        keys = []
    known = {c["key"] for c in L.pool(db)}
    return L.resolve_keys(db, [k for k in keys if isinstance(k, str)], known)


def kids_blocked_tags(tags) -> list:
    """BlockedTags with every channel that isn't a kids channel blocked."""
    tags = list(tags or [])
    return tags if L.ALL_TAG in tags else tags + [L.ALL_TAG]


def kids_policy(policy: dict, level: str, live_tv: bool) -> dict:
    """A Jellyfin policy with the kids profile on (pure)."""
    p = dict(policy)
    p["MaxParentalRating"] = KIDS_LEVELS[level]["score"]
    p["MaxParentalSubRating"] = None
    p["BlockUnratedItems"] = sorted(set(p.get("BlockUnratedItems") or []) | set(KIDS_BLOCK_UNRATED))
    p["EnableLiveTvAccess"] = bool(live_tv)
    if live_tv:
        # Only the kids channels from the first moment, not once the Live TV
        # sync gets to it (services/livetv_tuners.py keeps it so).
        p["BlockedTags"] = kids_blocked_tags(p.get("BlockedTags"))
    p["EnableLiveTvManagement"] = False
    p["IsAdministrator"] = False
    return p


def restored_policy(policy: dict, saved: dict) -> dict:
    """The policy with the fields the kids profile changed put back (pure)."""
    p = dict(policy)
    for k in POLICY_FIELDS:
        if k in saved:
            p[k] = saved[k]
    if "EnableLiveTvManagement" in saved:
        p["EnableLiveTvManagement"] = saved["EnableLiveTvManagement"]
    return p


class _Uid:
    """Just enough of a user for _policy(), before the Tentacle row exists."""
    def __init__(self, uid: str):
        self.jellyfin_user_id = uid


def set_on_sign_in(db: Session, user: TentacleUser, shown: bool) -> bool:
    """Show or hide someone on the sign-in screens (Jellyfin's own and
    Tentacle's): Jellyfin's IsHidden, the one place it is kept."""
    jf = jellyfin(db)
    policy = _policy(jf, user)
    if not policy:
        raise FamilyError("Jellyfin didn't say how that account is set up.", 502)
    if bool(policy.get("IsHidden")) == (not shown):
        return shown
    try:
        jf.post(f"/Users/{dashed(user.jellyfin_user_id)}/Policy", {**policy, "IsHidden": not shown})
    except requests.RequestException as e:
        raise _jf_error(e, "change the sign-in screen")
    logger.info(f"[Family] {user.display_name} {'shown on' if shown else 'hidden from'} the sign-in screen")
    return shown


def _policy(jf, user: TentacleUser) -> dict:
    try:
        return (jf.get(f"/Users/{dashed(user.jellyfin_user_id)}") or {}).get("Policy") or {}
    except requests.RequestException as e:
        raise _jf_error(e, "answer")


def set_kids(db: Session, user: TentacleUser, level: Optional[str]) -> dict:
    """Switch someone's kids profile on (at `level`) or off (None)."""
    from services import livetv_tuners
    if level and level not in KIDS_LEVELS:
        raise FamilyError("Unknown kids level.")
    jf = jellyfin(db)
    policy = _policy(jf, user)
    uid = L.norm_user_id(user.jellyfin_user_id)
    if level:
        if user.is_admin or policy.get("IsAdministrator"):
            raise FamilyError("An admin can't have a kids profile. Take admin away first.")
        if not user.kids_saved_policy:
            saved = {k: policy.get(k) for k in POLICY_FIELDS}
            saved["EnableLiveTvManagement"] = policy.get("EnableLiveTvManagement")
            saved["request_mode"] = user.request_mode
            try:
                saved["toolbar"] = _kids_toolbar(db, user, True)
            except Exception as e:   # no home config yet: the defaults give one
                logger.info(f"[Family] Navbar for {user.display_name} not changed: {e}")
            user.kids_saved_policy = json.dumps(saved)
        keys = kids_channels(db)
        new = kids_policy(policy, level, bool(keys) and not user.live_tv_off)
        try:
            jf.post(f"/Users/{dashed(uid)}/Policy", new)
        except requests.RequestException as e:
            raise _jf_error(e, "take the age limit")
        user.kids_level = level
        user.request_mode = "ask"
        db.commit()
        logger.info(f"[Family] Kids profile on for {user.display_name}: {KIDS_LEVELS[level]['name']}, "
                    f"{len(keys)} kids channels")
    else:
        try:
            saved = json.loads(user.kids_saved_policy or "{}")
        except ValueError:
            saved = {}
        new = restored_policy(policy, saved) if saved else {
            **policy, "MaxParentalRating": None, "MaxParentalSubRating": None,
            "BlockUnratedItems": [b for b in policy.get("BlockUnratedItems") or [] if b not in _KIDS_BLOCKED_BEFORE],
            "EnableLiveTvAccess": True}
        if user.live_tv_off:
            new["EnableLiveTvAccess"] = False
        try:
            jf.post(f"/Users/{dashed(uid)}/Policy", new)
        except requests.RequestException as e:
            raise _jf_error(e, "lift the age limit")
        if saved.get("request_mode"):
            user.request_mode = saved["request_mode"]
        try:
            _kids_toolbar(db, user, False, saved.get("toolbar") or {})
        except Exception as e:
            logger.info(f"[Family] Navbar for {user.display_name} not put back: {e}")
        user.kids_level = None
        user.kids_saved_policy = None
        db.commit()
        logger.info(f"[Family] Kids profile off for {user.display_name}")
    forget_visible(uid)
    livetv_tuners.schedule_sync(f"kids profile for {user.display_name}")
    return person(db, user)


def set_kids_channels(db: Session, keys: list) -> dict:
    """The household's kids channels; every kid's Live TV follows."""
    from services import livetv_tuners
    known = [c["key"] for c in L.pool(db)]
    seen, ordered = set(), []
    for k in keys or []:
        if k in known and k not in seen:
            seen.add(k)
            ordered.append(k)
    set_setting(db, KIDS_CHANNELS_KEY, json.dumps(ordered))
    kids = db.query(TentacleUser).filter(TentacleUser.kids_level.isnot(None)).all()
    if kids:
        jf = jellyfin(db)
        for kid in kids:
            uid = L.norm_user_id(kid.jellyfin_user_id)
            try:
                policy = _policy(jf, kid)
                new = {**policy, "EnableLiveTvAccess": bool(ordered) and not kid.live_tv_off}
                if ordered:
                    new["BlockedTags"] = kids_blocked_tags(policy.get("BlockedTags"))
                if new != policy:
                    jf.post(f"/Users/{dashed(uid)}/Policy", new)
            except FamilyError as e:
                logger.warning(f"[Family] Kids channels for {kid.display_name} not set: {e.message}")
                continue
            except requests.RequestException as e:
                logger.warning(f"[Family] Kids channels for {kid.display_name} not set: {e}")
                continue
        # The channels' tags follow; the refresh carries them onto programmes.
        livetv_tuners.schedule_sync("kids channels changed", refresh=True)
    return {"keys": ordered, "kids": len(kids)}


# What a kid may see in Tentacle's own pages: Tentacle keeps no age ratings,
# so its Library asks Jellyfin which titles the kid's account can open (their
# parental limit applied by Jellyfin itself), cached a few minutes.
_VISIBLE_TTL = 300
_visible_cache = {}   # jellyfin user id -> (time, {(media_type, tmdb_id)})
_visible_lock = threading.Lock()


def visible_titles(db: Session, user: TentacleUser) -> set:
    """{("movie"|"series", tmdb_id)} the person can open in Jellyfin. Empty when
    Jellyfin can't say: a kid then sees nothing rather than everything."""
    import time
    uid = L.norm_user_id(user.jellyfin_user_id)
    now = time.time()
    with _visible_lock:
        hit = _visible_cache.get(uid)
        if hit and now - hit[0] < _VISIBLE_TTL:
            return hit[1]
    out = set()
    try:
        jf = jellyfin(db)
        res = jf.get("/Items", userId=dashed(uid), Recursive="true", IncludeItemTypes="Movie,Series",
                     Fields="ProviderIds", EnableImages="false", EnableUserData="false") or {}
        for it in res.get("Items") or []:
            tmdb = (it.get("ProviderIds") or {}).get("Tmdb")
            if tmdb and str(tmdb).isdigit():
                out.add(("series" if it.get("Type") == "Series" else "movie", int(tmdb)))
    except (FamilyError, requests.RequestException) as e:
        logger.info(f"[Family] What {user.display_name} may see wasn't read from Jellyfin: {e}")
        return set()
    with _visible_lock:
        _visible_cache[uid] = (now, out)
    return out


def forget_visible(user_id: str) -> None:
    with _visible_lock:
        _visible_cache.pop(L.norm_user_id(user_id), None)


KIDS_TOOLBAR_OFF = ("discover", "activity")


def _kids_toolbar(db: Session, user: TentacleUser, on: bool, saved: Optional[dict] = None) -> dict:
    """Discover (TMDB's whole catalogue, any rating) and Activity off in a
    kid's Jellyfin and TV navbar; back as they were when the profile goes.
    Returns the toolbar switches as they were."""
    from services.smartlists import (
        _notify_jellyfin_plugin, _user_home_config_path, bump_playlist_version, get_home_config,
        write_home_json,
    )
    cfg = get_home_config(db, user_id=user.id) or {}
    toolbar = cfg.get("toolbar")
    if not toolbar:
        return {}
    before = {t.get("id"): bool(t.get("enabled")) for t in toolbar}
    for t in toolbar:
        if t.get("id") in KIDS_TOOLBAR_OFF:
            t["enabled"] = False if on else bool((saved or {}).get(t["id"], True))
    write_home_json(_user_home_config_path(db, user.id), cfg)
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    return {k: v for k, v in before.items() if k in KIDS_TOOLBAR_OFF}


# ── TV sign-in codes ────────────────────────────────────────────────────────

def tv_sign_in(db: Session, user: TentacleUser, code: str) -> dict:
    code = re.sub(r"\s+", "", code or "")
    if not re.fullmatch(r"[0-9A-Za-z]{4,12}", code):
        raise FamilyError("Type the code the TV shows (6 digits).")
    jf = jellyfin(db)
    try:
        jf.post("/QuickConnect/Authorize", None, code=code, userId=dashed(user.jellyfin_user_id))
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 404:
            raise FamilyError("No TV is showing that code. Check the code on the TV (it changes each time) "
                              "and type it again.", 404)
        if status == 401:
            raise FamilyError("Sign-in codes are switched off in Jellyfin. An admin can turn them on "
                              "in Tentacle → Settings → People & sign-in.", 409)
        raise _jf_error(e, "take the code")
    except requests.RequestException as e:
        raise _jf_error(e, "take the code")
    logger.info(f"[Family] A TV was signed in as {user.display_name} with a code")
    return {"ok": True, "name": user.display_name}


def quick_connect_enabled(db: Session) -> Optional[bool]:
    try:
        return bool(jellyfin(db).get("/QuickConnect/Enabled"))
    except (FamilyError, requests.RequestException):
        return None


def enable_quick_connect(db: Session) -> bool:
    jf = jellyfin(db)
    try:
        cfg = jf.get("/System/Configuration") or {}
        if not cfg.get("QuickConnectAvailable"):
            jf.post("/System/Configuration", {**cfg, "QuickConnectAvailable": True})
            logger.info("[Family] Turned Jellyfin's Quick Connect (TV sign-in codes) on")
    except requests.RequestException as e:
        raise _jf_error(e, "turn sign-in codes on")
    return True


# ── People, for the admin's page ────────────────────────────────────────────

def person(db: Session, user: TentacleUser, jf_user: Optional[dict] = None) -> dict:
    policy = (jf_user or {}).get("Policy") or {}
    return {
        "id": user.id,
        "jellyfin_user_id": user.jellyfin_user_id,
        "name": user.display_name,
        "is_admin": bool(user.is_admin),
        "kids_level": user.kids_level,
        "request_mode": "direct" if user.is_admin else (user.request_mode or "ask"),
        "invited_by": user.invited_by or "",
        "invited_at": user.invited_at.isoformat() + "Z" if user.invited_at else None,
        "disabled": bool(policy.get("IsDisabled")),
        # Jellyfin's "hide this user from login screens", which is on for every
        # account made in Jellyfin's dashboard: such people are missing from
        # the sign-in screens until an admin shows them.
        "on_sign_in": not policy.get("IsHidden", True) if jf_user else None,
        "has_image": bool(user.profile_image_tag),
        "location": _place_of(db, user),
        # Jellyfin turned the account off after too many wrong passwords.
        "locked": is_locked(policy),
        "live_tv": not user.live_tv_off,
        "welcomed": user.welcomed_at is not None,
    }


def is_locked(policy: dict) -> bool:
    """Off because of wrong passwords (Jellyfin's own lockout), not by an admin (pure)."""
    p = policy or {}
    limit = p.get("LoginAttemptsBeforeLockout")
    return bool(p.get("IsDisabled")) and isinstance(limit, int) and limit > 0 \
        and int(p.get("InvalidLoginAttemptCount") or 0) >= limit


def _place_of(db: Session, user: TentacleUser) -> Optional[dict]:
    from services import livetv_locations
    p = livetv_locations.public(livetv_locations.of_person(db, user))
    if p is not None:
        p["own"] = bool(livetv_locations.get(user.livetv_location))
    return p

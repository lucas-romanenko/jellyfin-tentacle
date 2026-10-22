"""What each person may do in Jellyfin, set from Tentacle's People page
(Roadmap 1d), and the two server-wide sign-in settings an admin needs.

Family never open Jellyfin's dashboard, so the Jellyfin settings a home
server's admin actually changes per person live here, in plain words:

- **Can record shows** = Jellyfin's "Allow Live TV recording management"
  (EnableLiveTvManagement). Jellyfin makes new accounts without it, though
  recordings are the household's (Roadmap 1b), so invites turn it on.
- **Away from home** = "Allow remote connections to this server"
  (EnableRemoteAccess): off means only on the home network.
- **Libraries** = library access (EnableAllFolders / EnabledFolders).
- **Watching hours** = parental access schedules (AccessSchedules): one range
  every day on the server's clock. Jellyfin checks a range as
  start <= now <= end with no wrap past midnight, so an evening that runs
  past midnight is stored as two ranges (start-24 and 0-end).
- **Turned off** = "Disable this user" (IsDisabled). Jellyfin sets it itself
  after too many wrong passwords; turning the account on clears that count,
  or the next typo locks it again.

Jellyfin is the one truth: nothing here is kept in Tentacle's database. The
People page reads every person's policy from Jellyfin, and each change is
read back from Jellyfin before it's shown. A policy POST replaces the whole
policy, so every change starts from a fresh read.

Admins aren't changed from here (Jellyfin lets admins do everything, and an
admin's own settings are theirs); kids can't record (a kids profile turns it
off and puts it back when it ends, services/family.py).

Server-wide: TV sign-in codes (Quick Connect) and the quality cap for
everyone watching away from home (RemoteClientBitrateLimit).
"""
import logging
import threading
import time
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import TentacleUser
from services import livetv_lineups as L
from services.family import FamilyError, _jf_error, dashed, jellyfin

logger = logging.getLogger(__name__)

EVERY_DAY = "Everyday"

# What "Quality away from home" offers, in bits per second (0 = no limit).
AWAY_QUALITY = [
    {"bps": 0, "name": "No limit"},
    {"bps": 40_000_000, "name": "High (40 Mbps)"},
    {"bps": 20_000_000, "name": "Good (20 Mbps)"},
    {"bps": 10_000_000, "name": "Medium (10 Mbps)"},
    {"bps": 4_000_000, "name": "Low (4 Mbps)"},
]


def _norm(i) -> str:
    return str(i or "").replace("-", "").lower()


# ── Reading a policy ─────────────────────────────────────────────────────────

def hours_of(schedules) -> Optional[dict]:
    """{"start": h, "end": h} for the person's watching hours, None for any
    time. Two every-day ranges meeting at midnight are one evening; anything
    else Jellyfin allows (other days, several ranges) is shown as "custom"."""
    s = [x for x in schedules or [] if isinstance(x, dict)]
    if not s:
        return None
    if all(x.get("DayOfWeek") == EVERY_DAY for x in s):
        spans = sorted((float(x.get("StartHour") or 0), float(x.get("EndHour") or 0)) for x in s)
        if len(spans) == 1:
            return {"start": spans[0][0], "end": spans[0][1]}
        if len(spans) == 2 and spans[0][0] == 0 and spans[1][1] >= 24:
            return {"start": spans[1][0], "end": spans[0][1]}
    return {"custom": True}


def access_of(policy: dict) -> dict:
    """The People page's view of a Jellyfin policy (pure)."""
    p = policy or {}
    all_libs = p.get("EnableAllFolders") is not False
    return {
        "can_record": bool(p.get("EnableLiveTvManagement")),
        "away": p.get("EnableRemoteAccess") is not False,
        "libraries": None if all_libs else sorted(_norm(f) for f in p.get("EnabledFolders") or []),
        "hours": hours_of(p.get("AccessSchedules")),
        "disabled": bool(p.get("IsDisabled")),
    }


# ── Changing a policy ────────────────────────────────────────────────────────

def _hour(v) -> float:
    try:
        h = float(v)
    except (TypeError, ValueError):
        raise FamilyError("Pick a time for the watching hours.")
    if not 0 <= h <= 24 or (h * 2) != int(h * 2):
        raise FamilyError("Watching hours go in half hours between 0:00 and 24:00.")
    return h


def schedules_for(hours: Optional[dict], user_id: str) -> list:
    """Jellyfin AccessSchedules for one every-day range (pure); [] = any time."""
    if not hours:
        return []
    start, end = _hour(hours.get("start")), _hour(hours.get("end"))
    if start == end:
        raise FamilyError("The watching hours need a start and an end that differ.")
    uid = dashed(user_id)
    one = lambda a, b: {"DayOfWeek": EVERY_DAY, "StartHour": a, "EndHour": b, "UserId": uid}
    if start < end:
        return [one(start, end)]
    return [one(start, 24), one(0, end)]   # past midnight: two ranges


def changed_policy(policy: dict, change: dict, user_id: str, library_ids=None) -> dict:
    """`policy` with the People page's `change` applied (pure). Keys of
    `change` that are absent stay as they are."""
    p = dict(policy)
    if "can_record" in change:
        p["EnableLiveTvManagement"] = bool(change["can_record"])
    if "away" in change:
        p["EnableRemoteAccess"] = bool(change["away"])
    if "libraries" in change:
        libs = change["libraries"]
        if libs is None:
            p["EnableAllFolders"], p["EnabledFolders"] = True, []
        else:
            libs = [_norm(i) for i in libs]
            if library_ids is not None:
                known = {_norm(i) for i in library_ids}
                libs = [i for i in libs if i in known]
            if not libs:
                raise FamilyError("Pick at least one library, or turn the account off instead.")
            p["EnableAllFolders"], p["EnabledFolders"] = False, sorted(set(libs))
    if "hours" in change:
        p["AccessSchedules"] = schedules_for(change["hours"], user_id)
    if "disabled" in change:
        p["IsDisabled"] = bool(change["disabled"])
        if not change["disabled"]:
            p["InvalidLoginAttemptCount"] = 0
    return p


def new_person_policy(policy: dict, kid: bool) -> dict:
    """The defaults a newly invited person starts with (pure): on the sign-in
    screen, can record shows (not kids), can watch away from home, every
    library, any time. Jellyfin's own defaults hide new accounts and leave
    them unable to record."""
    p = dict(policy)
    p.update({"IsHidden": False, "EnableLiveTvManagement": not kid, "EnableRemoteAccess": True,
              "EnableAllFolders": True, "EnabledFolders": [], "AccessSchedules": [], "IsDisabled": False})
    return p


def libraries(db: Session) -> list:
    """[{"id", "name"}] of Jellyfin's libraries; [] when Jellyfin can't say."""
    try:
        folders = jellyfin(db).get("/Library/VirtualFolders") or []
    except (FamilyError, requests.RequestException) as e:
        logger.info(f"[People] Jellyfin's libraries weren't read: {e}")
        return []
    return [{"id": _norm(f.get("ItemId")), "name": f.get("Name") or "Library"}
            for f in folders if f.get("ItemId")]


def read_user(jf, user: TentacleUser) -> dict:
    try:
        return jf.get(f"/Users/{dashed(user.jellyfin_user_id)}") or {}
    except requests.RequestException as e:
        raise _jf_error(e, "answer")


def set_access(db: Session, admin: TentacleUser, user: TentacleUser, change: dict) -> dict:
    """Apply `change` to `user`'s Jellyfin policy; returns the Jellyfin user
    as read back afterwards."""
    if not change:
        raise FamilyError("Nothing to change.")
    jf = jellyfin(db)
    before = read_user(jf, user)
    policy = before.get("Policy") or {}
    if user.is_admin or policy.get("IsAdministrator"):
        raise FamilyError("Admins can do everything in Jellyfin. Take admin away first to set limits.", 403)
    if L.norm_user_id(user.jellyfin_user_id) == L.norm_user_id(admin.jellyfin_user_id):
        raise FamilyError("You can't change your own account here.", 403)
    if change.get("can_record") and user.kids_level:
        raise FamilyError("Kids can't record shows. Take the age limit away first.")
    lib_ids = None
    if change.get("libraries") is not None:
        lib_ids = [l["id"] for l in libraries(db)]
        if not lib_ids:
            raise FamilyError("Jellyfin didn't list its libraries. Try again in a moment.", 502)
    new = changed_policy(policy, change, user.jellyfin_user_id, lib_ids)
    if new != policy:
        try:
            jf.post(f"/Users/{dashed(user.jellyfin_user_id)}/Policy", new)
        except requests.RequestException as e:
            raise _jf_error(e, "save it")
    after = read_user(jf, user)
    forget(user.jellyfin_user_id)
    from services.family import forget_visible
    forget_visible(user.jellyfin_user_id)
    logger.info(f"[People] {admin.display_name} changed {user.display_name}: "
                + ", ".join(f"{k}={'(set)' if k == 'libraries' and v else v}" for k, v in change.items()))
    return after


# ── What Tentacle's own pages show someone with a few libraries ─────────────

_LIMITED_TTL = 300
_limited = {}   # jellyfin user id -> (time, bool)
_limited_lock = threading.Lock()


def library_limited(db: Session, user: TentacleUser) -> bool:
    """True when the person sees only some libraries: Tentacle's Library then
    shows only what Jellyfin lets them open, as for a kid. Read from Jellyfin
    every few minutes; while Jellyfin can't say, the last answer stands."""
    uid = L.norm_user_id(user.jellyfin_user_id)
    now = time.time()
    with _limited_lock:
        hit = _limited.get(uid)
    if hit and now - hit[0] < _LIMITED_TTL:
        return hit[1]
    try:
        policy = read_user(jellyfin(db), user).get("Policy") or {}
        val = policy.get("EnableAllFolders") is False and not policy.get("IsAdministrator")
    except FamilyError:
        return hit[1] if hit else False
    with _limited_lock:
        _limited[uid] = (now, val)
    return val


def forget(user_id: str) -> None:
    with _limited_lock:
        _limited.pop(L.norm_user_id(user_id), None)


# ── Server-wide ──────────────────────────────────────────────────────────────

def server_settings(db: Session) -> dict:
    """Read from Jellyfin each time: {"quick_connect", "away_quality", "choices"}."""
    jf = jellyfin(db)
    try:
        cfg = jf.get("/System/Configuration") or {}
    except requests.RequestException as e:
        raise _jf_error(e, "answer")
    return {"quick_connect": bool(cfg.get("QuickConnectAvailable")),
            "away_quality": int(cfg.get("RemoteClientBitrateLimit") or 0),
            "choices": AWAY_QUALITY}


def save_server_settings(db: Session, admin: TentacleUser, quick_connect: Optional[bool] = None,
                         away_quality: Optional[int] = None) -> dict:
    if away_quality is not None and away_quality not in {c["bps"] for c in AWAY_QUALITY}:
        raise FamilyError("Pick one of the qualities offered.")
    jf = jellyfin(db)
    try:
        cfg = jf.get("/System/Configuration") or {}
        new = dict(cfg)
        if quick_connect is not None:
            new["QuickConnectAvailable"] = bool(quick_connect)
        if away_quality is not None:
            new["RemoteClientBitrateLimit"] = int(away_quality)
        if new != cfg:
            jf.post("/System/Configuration", new)
            logger.info(f"[People] {admin.display_name} set TV sign-in codes={new.get('QuickConnectAvailable')}, "
                        f"away quality={new.get('RemoteClientBitrateLimit')}")
    except requests.RequestException as e:
        raise _jf_error(e, "save it")
    return server_settings(db)

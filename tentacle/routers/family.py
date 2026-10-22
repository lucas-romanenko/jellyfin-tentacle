"""Family: the admin's People page (invites, kids profiles, how requests
work per person, the defaults new people start with), signing a TV in with
a code, requests waiting for an admin, and plain-words status for everyone.

The work is in services/family.py, services/approvals.py and
services/admin_alerts.py.
"""
import logging
from typing import List, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import TentacleUser, get_db
from routers.auth import get_user_from_request, require_admin
from services import admin_alerts, approvals, family as F, gone_people, livetv_lineups as L, person_access as PA, tv_app
from services import welcome

logger = logging.getLogger(__name__)
router = APIRouter()


def _refusal(e: F.FamilyError) -> HTTPException:
    return HTTPException(e.status, e.message)


def _row(db: Session, user_id: int) -> TentacleUser:
    row = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
    if row is None:
        raise HTTPException(404, "That person isn't in Tentacle.")
    return row


def _ensure_rows(db: Session, jf_users: list) -> None:
    """Give every Jellyfin account a Tentacle row, so an admin can set up
    someone who hasn't opened Tentacle yet. A new row starts on the
    household's defaults, as at a first sign-in."""
    from sqlalchemy.exc import IntegrityError
    known = {u.jellyfin_user_id for u in db.query(TentacleUser).all()}
    for u in jf_users:
        uid = L.norm_user_id(u.get("Id") or "")
        if len(uid) != 32 or uid in known:
            continue
        row = TentacleUser(jellyfin_user_id=uid, display_name=u.get("Name") or uid,
                           is_admin=bool((u.get("Policy") or {}).get("IsAdministrator")),
                           profile_image_tag=u.get("PrimaryImageTag"))
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        F.start_new_person(row.id)


# ── The People page (admins) ────────────────────────────────────────────────

@router.get("/api/family/people", dependencies=[Depends(require_admin)])
def people(db: Session = Depends(get_db)):
    try:
        jf = F.jellyfin(db)
        jf_users = jf.get("/Users") or []
    except F.FamilyError as e:
        raise _refusal(e)
    except requests.RequestException:
        raise HTTPException(502, "Jellyfin can't be reached right now.")
    _ensure_rows(db, jf_users)
    gone_people.forget_gone(db, jf_users)
    # Accounts made in Jellyfin's dashboard lock after wrong passwords too.
    PA.ensure_lockout(db, jf_users)
    by_id = {L.norm_user_id(u["Id"]): u for u in jf_users if u.get("Id")}
    try:
        devices = (jf.get("/Devices") or {}).get("Items") or []
    except requests.RequestException:
        devices = []
    tv_versions = tv_app.versions_by_user(devices)
    newest = tv_app.latest()
    owner = db.query(TentacleUser).order_by(TentacleUser.id).first()
    out = []
    pool = L.pool(db)
    lineup = L.lineup(db, pool)
    from routers.my_livetv import _favourites, my_lineup
    from services import livetv_locations
    for row in db.query(TentacleUser).order_by(TentacleUser.display_name).all():
        jf_user = by_id.get(row.jellyfin_user_id)
        if jf_user is None:
            continue    # gone from Jellyfin
        if bool((jf_user.get("Policy") or {}).get("IsAdministrator")) != bool(row.is_admin):
            row.is_admin = bool(jf_user["Policy"]["IsAdministrator"])
            db.commit()
        p = F.person(db, row, jf_user)
        tv = tv_versions.get(row.jellyfin_user_id.lower())
        p["is_owner"] = bool(owner and owner.id == row.id)
        p["tv_app"] = {"version": tv, "up_to_date": bool(tv and newest and newest["version"]
                                                         and tv_app.version_key(tv) >= tv_app.version_key(newest["version"]))}
        # What they may do, straight from Jellyfin (never a copy kept here).
        p["access"] = PA.access_of(jf_user.get("Policy") or {})
        p["last_active"] = jf_user.get("LastActivityDate")
        p["image_tag"] = jf_user.get("PrimaryImageTag")
        # Favourite channels (one lineup for everyone, with their place's
        # channels); None when Jellyfin can't say.
        favs = None if row.kids_level else _favourites(db, row.jellyfin_user_id, my_lineup(db, row, lineup))
        p["favourite_count"] = len(favs) if favs is not None else None
        out.append(p)
    kids_keys = F.kids_channels(db)
    names = {c["key"]: c["name"] for c in pool}
    return {
        "people": out,
        "defaults": {**F.defaults(db), "kids": F.kids_defaults(db)},
        "kids_levels": [{"id": k, **v} for k, v in F.KIDS_LEVELS.items()],
        "kids_channels": len(kids_keys),
        "pool_channels": len(L.view_for(lineup, None)),
        # Where people watch Live TV from: the places to choose from, and the
        # household's (the first admin's), which an invite starts with.
        "locations": livetv_locations.choices(db),
        "household_location": livetv_locations.public(livetv_locations.household(db)),
        "kids_channel_names": [names[k] for k in kids_keys[:6] if k in names],
        "libraries": PA.libraries(db),
        "quick_connect": F.quick_connect_enabled(db),
        "addresses": F.addresses(db),
        "tv_app": {"newest": newest["version"] if newest else None},
        "pending_requests": approvals.pending_count(db),
    }


class InviteBody(BaseModel):
    name: str
    kids_level: Optional[str] = None
    request_mode: Optional[str] = None
    location: Optional[str] = None   # a place's slug (services/livetv_locations.py); none: the household's
    password: Optional[str] = None   # none: one made from the name
    live_tv: bool = True
    # What they start with; none: the household's (New people / New kids start with).
    home: Optional[str] = None       # "default" | "kids" | a person's id
    channels: Optional[str] = None   # "none" | "kids" | a person's id


@router.post("/api/family/invite")
def invite(body: InviteBody, admin: TentacleUser = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        return F.invite(db, admin, body.name, kids_level=body.kids_level or None,
                        request_mode=body.request_mode or None, location=body.location or None,
                        password=body.password or None, live_tv=body.live_tv,
                        home=body.home or None, channels=body.channels or None)
    except F.FamilyError as e:
        raise _refusal(e)


class AddressesBody(BaseModel):
    tentacle: Optional[str] = None
    jellyfin: Optional[str] = None


@router.put("/api/family/addresses", dependencies=[Depends(require_admin)])
def save_addresses(body: AddressesBody, db: Session = Depends(get_db)):
    """Add someone's one question: the addresses family open Tentacle and
    Jellyfin at (asked once)."""
    try:
        return F.save_addresses(db, body.tentacle, body.jellyfin)
    except F.FamilyError as e:
        raise _refusal(e)


class PersonBody(BaseModel):
    # Only the fields sent change. kids_level "" or null switches the profile off.
    kids_level: Optional[str] = None
    set_kids: bool = False
    request_mode: Optional[str] = None
    on_sign_in: Optional[bool] = None
    location: Optional[str] = None   # a place's slug; "" = the household's


@router.put("/api/family/people/{user_id}")
def update_person(user_id: int, body: PersonBody, admin: TentacleUser = Depends(require_admin),
                  db: Session = Depends(get_db)):
    row = _row(db, user_id)
    try:
        if body.set_kids:
            F.set_kids(db, row, body.kids_level or None)
        if body.request_mode is not None:
            if body.request_mode not in ("ask", "direct"):
                raise HTTPException(400, "Unknown request setting.")
            if row.kids_level and body.request_mode == "direct":
                raise HTTPException(400, "A kid's requests always wait for an admin.")
            row.request_mode = body.request_mode
            db.commit()
            logger.info(f"[Family] {admin.display_name} set {row.display_name}'s requests to {body.request_mode}")
        if body.on_sign_in is not None:
            F.set_on_sign_in(db, row, body.on_sign_in)
        if body.location is not None:
            F.set_location(db, row, body.location)
    except F.FamilyError as e:
        raise _refusal(e)
    out = F.person(db, row)
    if body.on_sign_in is not None:
        out["on_sign_in"] = body.on_sign_in
    return out


class AccessBody(BaseModel):
    # Only the fields sent change. libraries null = every library; hours
    # null = any time, else {"start": h, "end": h} in half hours.
    can_record: Optional[bool] = None
    away: Optional[bool] = None
    live_tv: Optional[bool] = None
    libraries: Optional[List[str]] = None
    hours: Optional[dict] = None
    disabled: Optional[bool] = None


@router.put("/api/family/people/{user_id}/access")
def update_access(user_id: int, body: AccessBody, admin: TentacleUser = Depends(require_admin),
                  db: Session = Depends(get_db)):
    """What someone may do in Jellyfin; answers with the person as Jellyfin
    has them afterwards."""
    row = _row(db, user_id)
    change = body.model_dump(exclude_unset=True)
    for k in ("can_record", "away", "disabled", "live_tv"):
        if k in change and change[k] is None:
            raise HTTPException(400, f"{k} needs true or false.")
    if len(change.get("libraries") or []) > 500:
        raise HTTPException(400, "Too many libraries")
    try:
        jf_user = PA.set_access(db, admin, row, change)
    except F.FamilyError as e:
        raise _refusal(e)
    p = F.person(db, row, jf_user)
    p["access"] = PA.access_of(jf_user.get("Policy") or {})
    return p


@router.get("/api/family/server", dependencies=[Depends(require_admin)])
def server_settings(db: Session = Depends(get_db)):
    """Settings → People & sign-in, read from Jellyfin each time."""
    try:
        return PA.server_settings(db)
    except F.FamilyError as e:
        raise _refusal(e)


class ServerBody(BaseModel):
    quick_connect: Optional[bool] = None
    away_quality: Optional[int] = None


@router.put("/api/family/server")
def save_server_settings(body: ServerBody, admin: TentacleUser = Depends(require_admin),
                         db: Session = Depends(get_db)):
    try:
        return PA.save_server_settings(db, admin, body.quick_connect, body.away_quality)
    except F.FamilyError as e:
        raise _refusal(e)


@router.post("/api/family/people/{user_id}/password")
def reset_password(user_id: int, admin: TentacleUser = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        return F.new_password(db, _row(db, user_id))
    except F.FamilyError as e:
        raise _refusal(e)


class CodeBody(BaseModel):
    code: str


@router.post("/api/family/people/{user_id}/tv-code")
def sign_in_tv_for(user_id: int, body: CodeBody, admin: TentacleUser = Depends(require_admin),
                   db: Session = Depends(get_db)):
    try:
        return F.tv_sign_in(db, _row(db, user_id), body.code)
    except F.FamilyError as e:
        raise _refusal(e)


class DefaultsBody(BaseModel):
    home_from: Optional[int] = None
    channels_from: Optional[int] = None
    kids: Optional[dict] = None   # {"home": "kids" | "default" | id, "channels": "kids" | "none"}


@router.put("/api/family/defaults", dependencies=[Depends(require_admin)])
def save_defaults(body: DefaultsBody, db: Session = Depends(get_db)):
    try:
        return F.save_defaults(db, body.home_from, body.channels_from, body.kids)
    except F.FamilyError as e:
        raise _refusal(e)


@router.get("/api/family/kids-channels", dependencies=[Depends(require_admin)])
def kids_channels(db: Session = Depends(get_db)):
    chosen = set(F.kids_channels(db))
    return {"channels": [{"key": c["key"], "name": c["name"], "logo_url": c["logo_url"],
                          "kids": c["key"] in chosen} for c in L.pool(db)],
            "keys": F.kids_channels(db)}


class KidsChannelsBody(BaseModel):
    keys: List[str] = []


@router.put("/api/family/kids-channels", dependencies=[Depends(require_admin)])
def save_kids_channels(body: KidsChannelsBody, db: Session = Depends(get_db)):
    if len(body.keys) > 5000:
        raise HTTPException(400, "Too many channels")
    try:
        return F.set_kids_channels(db, body.keys)
    except F.FamilyError as e:
        raise _refusal(e)


@router.post("/api/family/quick-connect", dependencies=[Depends(require_admin)])
def turn_on_codes(db: Session = Depends(get_db)):
    try:
        return {"ok": F.enable_quick_connect(db)}
    except F.FamilyError as e:
        raise _refusal(e)


# ── Everyone ────────────────────────────────────────────────────────────────

@router.post("/api/family/tv-code")
def sign_in_my_tv(body: CodeBody, user: TentacleUser = Depends(get_user_from_request),
                  db: Session = Depends(get_db)):
    """Sign a TV in as yourself with the code it shows."""
    try:
        return F.tv_sign_in(db, user, body.code)
    except F.FamilyError as e:
        raise _refusal(e)


@router.get("/api/family/me")
def me(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    """What this person's account does differently: kids profile, how their
    requests work, and the plain-words problems of the moment."""
    level = F.KIDS_LEVELS.get(user.kids_level or "")
    return {
        "kids": {"level": user.kids_level, "name": level["name"], "allows": level["allows"]} if level else None,
        **approvals.request_mode(db, user),
        "messages": admin_alerts.family_messages(db),
        "tv_app_address": F.addresses(db)["tv_app"],
    }


@router.get("/api/welcome")
def welcome_state(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    """The first-sign-in welcome: whether to show it, and what it says."""
    return welcome.state(db, user)


@router.post("/api/welcome/done")
def welcome_done(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    welcome.done(db, user)
    return {"ok": True}


class MyPasswordBody(BaseModel):
    current: str = ""
    new: str


@router.post("/api/me/pw")
def change_my_password(body: MyPasswordBody, user: TentacleUser = Depends(get_user_from_request),
                       db: Session = Depends(get_db)):
    """Pick your own password (the welcome's first step)."""
    try:
        welcome.change_password(db, user, body.current, body.new)
    except welcome.PasswordError as e:
        raise HTTPException(e.status, e.message)
    return {"ok": True}


@router.get("/api/family/status")
def status(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    """Plain-words problems for everyone (plugin, TV app): {"messages": [...]}."""
    return {"messages": admin_alerts.family_messages(db)}


# ── Requests ────────────────────────────────────────────────────────────────

@router.get("/api/requests/mode")
def my_request_mode(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    return approvals.request_mode(db, user)


@router.get("/api/requests/mine")
def my_requests(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    return approvals.listing(db, user=user)


@router.delete("/api/requests/mine/{request_id}")
def take_back(request_id: int, user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    if not approvals.cancel(db, request_id, user):
        raise HTTPException(404, "That request isn't waiting any more.")
    return {"ok": True}


@router.get("/api/requests", dependencies=[Depends(require_admin)])
def all_requests(db: Session = Depends(get_db)):
    return approvals.listing(db)


@router.get("/api/requests/count", dependencies=[Depends(require_admin)])
def waiting_count(db: Session = Depends(get_db)):
    return {"pending": approvals.pending_count(db)}


@router.post("/api/requests/{request_id}/approve")
def approve(request_id: int, admin: TentacleUser = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        return approvals.approve(db, request_id, admin)
    except approvals.NotPending:
        raise HTTPException(409, "Someone already answered this request.")


class DeclineBody(BaseModel):
    note: str = ""


@router.post("/api/requests/{request_id}/decline")
def decline(request_id: int, body: DeclineBody, admin: TentacleUser = Depends(require_admin),
            db: Session = Depends(get_db)):
    try:
        return approvals.decline(db, request_id, admin, body.note)
    except approvals.NotPending:
        raise HTTPException(409, "Someone already answered this request.")


# ── The TV app download (no sign-in: typed into Downloader on a TV) ─────────

tv_router = APIRouter()


@tv_router.get("/tv", include_in_schema=False)
def tv_app_download():
    """Tentacle's address + /tv in the Downloader app gets the newest TV app."""
    newest = tv_app.latest()
    return RedirectResponse(newest["url"] if newest and newest.get("url") else tv_app.RELEASES_PAGE, status_code=302)

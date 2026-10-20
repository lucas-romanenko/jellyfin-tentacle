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
from services import admin_alerts, approvals, family as F, livetv_lineups as L, tv_app

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
    by_id = {L.norm_user_id(u["Id"]): u for u in jf_users if u.get("Id")}
    try:
        devices = (jf.get("/Devices") or {}).get("Items") or []
    except requests.RequestException:
        devices = []
    tv_versions = tv_app.versions_by_user(devices)
    newest = tv_app.latest()
    owner = db.query(TentacleUser).order_by(TentacleUser.id).first()
    out = []
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
        p["last_active"] = jf_user.get("LastActivityDate")
        p["image_tag"] = jf_user.get("PrimaryImageTag")
        p["has_own_channels"] = L.has_own_list(db, row.jellyfin_user_id)
        out.append(p)
    kids_keys = F.kids_channels(db)
    names = {c["key"]: c["name"] for c in L.pool(db)}
    return {
        "people": out,
        "defaults": F.defaults(db),
        "kids_levels": [{"id": k, **v} for k, v in F.KIDS_LEVELS.items()],
        "kids_channels": len(kids_keys),
        "kids_channel_names": [names[k] for k in kids_keys[:6] if k in names],
        "quick_connect": F.quick_connect_enabled(db),
        "addresses": F.addresses(db),
        "tv_app": {"newest": newest["version"] if newest else None},
        "pending_requests": approvals.pending_count(db),
    }


class InviteBody(BaseModel):
    name: str
    kids_level: Optional[str] = None
    request_mode: Optional[str] = None


@router.post("/api/family/invite")
def invite(body: InviteBody, admin: TentacleUser = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        return F.invite(db, admin, body.name, kids_level=body.kids_level or None,
                        request_mode=body.request_mode or None)
    except F.FamilyError as e:
        raise _refusal(e)


class PersonBody(BaseModel):
    # Only the fields sent change. kids_level "" or null switches the profile off.
    kids_level: Optional[str] = None
    set_kids: bool = False
    request_mode: Optional[str] = None


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
    except F.FamilyError as e:
        raise _refusal(e)
    return F.person(db, row)


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


@router.put("/api/family/defaults", dependencies=[Depends(require_admin)])
def save_defaults(body: DefaultsBody, db: Session = Depends(get_db)):
    try:
        return F.save_defaults(db, body.home_from, body.channels_from)
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

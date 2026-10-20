"""My channels: each person picks their own Live TV channels (any signed-in
user, for themselves only), and the admins' view of who uses what.

The household's channels (the pool) are the admin's: channel groups switched
on per provider (Sources → IPTV providers). People see channels only, never
groups. A person sees either all of them or a list of their own; their
favourites are Jellyfin's, on their own channels. Every change is carried to
Jellyfin by services/livetv_tuners.py a few seconds later.
"""
import logging
from typing import List

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import LiveUserLineup, TentacleUser, get_db
from routers.auth import get_user_from_request, require_admin
from services import livetv_lineups as L
from services import livetv_tuners as T

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_CHANNELS = 5000


def _favourites(db, user_id: str, tuner_url: str, entries: list):
    """The keys of someone's favourite channels, or None when Jellyfin can't
    say (not connected, or their channels aren't set up there yet)."""
    jf = T.jellyfin_for(db)
    if jf is None or not tuner_url:
        return None
    try:
        ids = L.item_ids_for(tuner_url, entries)
        return {ids[i] for i in T.favourite_ids(jf, user_id) if i in ids}
    except requests.RequestException as e:
        logger.info(f"[LiveTV] Favourites for {user_id} not read from Jellyfin: {e}")
        return None


def _state(db, user: TentacleUser) -> dict:
    uid = L.norm_user_id(user.jellyfin_user_id)
    channels = L.pool(db)
    if user.kids_level:
        # A kids profile sees the household's kids channels only, chosen by
        # an admin (services/family.py); their list is those, in that order.
        from services import family
        kids = set(family.kids_channels(db))
        channels = [c for c in channels if c["key"] in kids]
    row = L.lineup_row(db, uid)
    own = bool(row and row.own_list)
    mine = L.own_lineup(db, uid, channels)
    shown = mine if own else [{**c, "number": i} for i, c in enumerate(channels, 1)]
    # Favourites are on the channels Jellyfin has now, which until the next
    # sync are the ones of the list set up there.
    tuner_url = T.tuner_url_for(db, uid)
    set_up = mine if (row and row.synced_own_list) else [{**c, "number": i} for i, c in enumerate(channels, 1)]
    favs = _favourites(db, uid, tuner_url, set_up)
    my_keys = {c["key"] for c in mine}
    numbers = {c["key"]: c["number"] for c in shown}
    kids_profile = None
    if user.kids_level:
        from services.approvals import approver_name
        kids_profile = {"chosen_by": approver_name(db)}
    return {
        "available": bool(channels),
        "kids": kids_profile,
        "own_list": own,
        "total": len(channels),
        "count": len(shown),
        "favourites_known": favs is not None,
        "setting_up": T.status.get("running") or bool(row and bool(row.own_list) != bool(row.synced_own_list)),
        "channels": [{
            "key": c["key"], "name": c["name"], "logo_url": c["logo_url"],
            "in_list": c["key"] in my_keys, "number": numbers.get(c["key"]),
            "favourite": bool(favs and c["key"] in favs),
        } for c in channels],
        "my_list": [c["key"] for c in mine],
    }


@router.get("/api/my/channels")
def my_channels(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    return _state(db, user)


class MyChannelsBody(BaseModel):
    own_list: bool
    keys: List[str] = []


@router.put("/api/my/channels")
def save_my_channels(body: MyChannelsBody, user: TentacleUser = Depends(get_user_from_request),
                     db: Session = Depends(get_db)):
    if len(body.keys) > MAX_CHANNELS:
        raise HTTPException(400, "Too many channels")
    if user.kids_level:
        raise HTTPException(403, "Your channels are picked for you. Ask a grown-up to change them.")
    L.save_own_list(db, user.jellyfin_user_id, body.own_list, body.keys)
    T.schedule_sync(f"{user.display_name}'s channels", refresh=True)
    return _state(db, user)


class FavouriteBody(BaseModel):
    key: str
    favourite: bool


@router.put("/api/my/channels/favourite")
def set_my_favourite(body: FavouriteBody, user: TentacleUser = Depends(get_user_from_request),
                     db: Session = Depends(get_db)):
    """Star or unstar one of your channels (Jellyfin's favourite, so every
    app shows it)."""
    uid = L.norm_user_id(user.jellyfin_user_id)
    row = L.lineup_row(db, uid)
    channels = L.pool(db)
    entries = (L.own_lineup(db, uid, channels) if (row and row.synced_own_list)
               else [{**c, "number": i} for i, c in enumerate(channels, 1)])
    tuner_url = T.tuner_url_for(db, uid)
    jf = T.jellyfin_for(db)
    by_key = {k: i for i, k in L.item_ids_for(tuner_url, entries).items()} if tuner_url else {}
    item_id = by_key.get(body.key)
    if jf is None or not item_id:
        raise HTTPException(409, "This channel isn't in Jellyfin yet. Try again in a minute.")
    try:
        T.set_favourite(jf, uid, item_id, body.favourite)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise HTTPException(409, "This channel isn't in Jellyfin yet. Try again in a minute.")
        raise HTTPException(502, "Jellyfin didn't take the change")
    except requests.RequestException:
        raise HTTPException(502, "Jellyfin can't be reached")
    return {"ok": True}


# ── Admins: who uses what ───────────────────────────────────────────────────

@router.get("/api/live/people", dependencies=[Depends(require_admin)])
def live_people(db: Session = Depends(get_db)):
    """Everyone's Live TV at a glance, and how Jellyfin is set up."""
    channels = L.pool(db)
    rows = {r.jellyfin_user_id: r for r in db.query(LiveUserLineup).all()}
    people = []
    for u in db.query(TentacleUser).order_by(TentacleUser.display_name).all():
        uid = L.norm_user_id(u.jellyfin_user_id)
        row = rows.get(uid)
        own = bool(row and row.own_list)
        people.append({
            "name": u.display_name,
            "own_list": own,
            "count": len(L.own_lineup(db, uid, channels)) if own else len(channels),
            "changed": row.updated_at.isoformat() + "Z" if row and row.updated_at else None,
        })
    st = T.last_status(db)
    return {
        "total": len(channels),
        "people": people,
        "setup": {
            "running": bool(st.get("running")),
            "last_at": st.get("last_at"),
            "ok": st.get("last_ok"),
            "message": st.get("last_message") or "",
        },
    }


@router.post("/api/live/setup", dependencies=[Depends(require_admin)])
def live_setup_now():
    """Set Live TV up in Jellyfin again now (normally automatic)."""
    T.schedule_sync("set up again from the dashboard", refresh=True, delay=0)
    return {"ok": True}

"""My channels: each person's favourite Live TV channels (any signed-in
user, for themselves only), and the admins' view of who has what.

The household has one lineup, the admin's (the pool: channel groups switched
on per provider, duplicates folded, services/livetv_lineups.py), one tuner
and one guide in Jellyfin. What is personal is the view: everyone stars
their favourites, which are Jellyfin's own favourites (so every app shows
them, and the guide can be filtered to them). Quick picks ("Sports",
"News", ...) star or unstar a type's main channels at once. Kids profiles
see only the household's kids channels.
"""
import logging
from typing import List

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import TentacleUser, get_db
from routers.auth import get_user_from_request, require_admin
from services import livetv_lineups as L
from services import livetv_tuners as T

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_CHANNELS = 5000

# Pool entry fields a picker may show (the catalog and the checker add some).
_PICK_FIELDS = ("types", "main_types", "has_guide", "event", "variants", "variant_name", "check", "new")


def _favourites(db, user_id: str, entries: list):
    """The keys of someone's favourite channels, or None when Jellyfin can't
    say (not connected, or the lineup isn't set up there yet)."""
    jf = T.jellyfin_for(db)
    url = T.all_tuner_url(db)
    if jf is None or not url:
        return None
    try:
        ids = L.item_ids_for(url, entries)
        return {ids[i] for i in T.favourite_ids(jf, user_id) if i in ids}
    except requests.RequestException as e:
        logger.info(f"[LiveTV] Favourites for {user_id} not read from Jellyfin: {e}")
        return None


def my_lineup(db, user: TentacleUser, entries: list = None) -> list:
    """The lineup as this person sees it: their place's channels (CTV ·
    Vancouver instead of CTV, services/livetv_locations.py), and a kids
    profile only the household's kids channels (chosen by an admin,
    services/family.py)."""
    from services import livetv_locations
    entries = L.view_for(L.lineup(db) if entries is None else entries, livetv_locations.of_person(db, user))
    if user.kids_level:
        from services import family
        kids = set(family.kids_channels(db))
        entries = [c for c in entries if L.base_key(c["key"]) in kids]
    return entries


def _state(db, user: TentacleUser) -> dict:
    """The lineup with my stars, my favourites first in lineup order."""
    uid = L.norm_user_id(user.jellyfin_user_id)
    entries = my_lineup(db, user)
    favs = _favourites(db, uid, entries)
    kids_profile = None
    if user.kids_level:
        from services.approvals import approver_name
        kids_profile = {"chosen_by": approver_name(db)}

    def entry(c):
        e = {"key": c["key"], "name": c["name"], "logo_url": c["logo_url"], "number": c["number"],
             "favourite": bool(favs and c["key"] in favs)}
        e.update({f: c[f] for f in _PICK_FIELDS if f in c})
        return e

    out = [entry(c) for c in entries]
    from services import livetv_locations
    place = livetv_locations.public(livetv_locations.of_person(db, user))
    if place is not None:
        place["own"] = bool(livetv_locations.get(user.livetv_location))
    return {
        "available": bool(entries),
        "kids": kids_profile,
        # Where they watch from (an admin sets it): their local channels' place.
        "location": place,
        "total": len(entries),
        "count": sum(1 for e in out if e["favourite"]),
        "favourites_known": favs is not None,
        "setting_up": bool(T.status.get("running")) or favs is None,
        "mine": [e for e in out if e["favourite"]],
        "channels": out,
    }


@router.get("/api/my/channels")
def my_channels(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    return _state(db, user)


class FavouritesBody(BaseModel):
    keys: List[str]
    favourite: bool


def _set_favourites(db, user: TentacleUser, keys: list, on: bool) -> int:
    """Star (or unstar) channels for this person in Jellyfin; returns how
    many changed. Channels Jellyfin hasn't made yet are refused (409)."""
    uid = L.norm_user_id(user.jellyfin_user_id)
    entries = my_lineup(db, user)
    url = T.all_tuner_url(db)
    jf = T.jellyfin_for(db)
    if jf is None or not url:
        raise HTTPException(409, "Live TV isn't set up in Jellyfin yet. Try again in a minute.")
    by_key = {k: i for i, k in L.item_ids_for(url, entries).items()}
    # A network is their place's channel of it ("n7" is "n7@vancouver" for
    # someone in Vancouver), whichever of its keys is asked for.
    mine = {L.base_key(k): k for k in by_key}
    asked = L.resolve_keys(db, [L.base_key(k) for k in keys], set(mine))
    wanted = [by_key[mine[k]] for k in dict.fromkeys(asked)]
    if not wanted:
        raise HTTPException(404, "No such channel")
    try:
        now = T.favourite_ids(jf, uid)
        changed = 0
        for item_id in wanted:
            if (item_id in now) == on:
                continue
            T.set_favourite(jf, uid, item_id, on)
            changed += 1
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise HTTPException(409, "This channel isn't in Jellyfin yet. Try again in a minute.")
        raise HTTPException(502, "Jellyfin didn't take the change")
    except requests.RequestException:
        raise HTTPException(502, "Jellyfin can't be reached")
    return changed


@router.put("/api/my/channels/favourites")
def set_my_favourites(body: FavouritesBody, user: TentacleUser = Depends(get_user_from_request),
                      db: Session = Depends(get_db)):
    """Star or unstar several channels at once (a quick pick)."""
    if len(body.keys) > MAX_CHANNELS:
        raise HTTPException(400, "Too many channels")
    changed = _set_favourites(db, user, body.keys, body.favourite)
    return {**_state(db, user), "changed": changed}


class FavouriteBody(BaseModel):
    key: str
    favourite: bool


@router.put("/api/my/channels/favourite")
def set_my_favourite(body: FavouriteBody, user: TentacleUser = Depends(get_user_from_request),
                     db: Session = Depends(get_db)):
    """Star or unstar one channel (Jellyfin's favourite, so every app shows
    it)."""
    _set_favourites(db, user, [body.key], body.favourite)
    return {"ok": True}


# ── Admins: who has what ────────────────────────────────────────────────────

@router.get("/api/live/people", dependencies=[Depends(require_admin)])
def live_people(db: Session = Depends(get_db)):
    """Everyone's Live TV at a glance (how many favourites each has, kids
    profiles' channels), and how Jellyfin is set up."""
    entries = L.lineup(db)
    from services import family, livetv_locations
    kids = set(family.kids_channels(db))
    kids_count = sum(1 for c in L.view_for(entries, None) if c["key"] in kids)
    people = []
    for u in db.query(TentacleUser).order_by(TentacleUser.display_name).all():
        theirs = my_lineup(db, u, entries)
        favs = _favourites(db, L.norm_user_id(u.jellyfin_user_id), theirs)
        people.append({
            "name": u.display_name,
            "kids": bool(u.kids_level),
            "channels": len(theirs),
            "favourites": len(favs) if favs is not None else None,
            "location": livetv_locations.public(livetv_locations.of_person(db, u)),
        })
    st = T.last_status(db)
    return {
        "total": len(L.view_for(entries, None)),
        "kids_channels": kids_count,
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

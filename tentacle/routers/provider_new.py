"""New from your provider (admin): Live TV groups and VOD categories a
provider added since the admin last reviewed (services/provider_new.py).
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db
from routers.auth import require_admin
from services import provider_new as N

router = APIRouter(dependencies=[Depends(require_admin)])

MAX_IDS = 5000


class GroupIds(BaseModel):
    group_ids: List[int]


class AddBody(BaseModel):
    group_ids: List[int]
    channel_ids: List[int] = []


class CategoryIds(BaseModel):
    category_ids: List[int]


def _limit(ids: list) -> list:
    if len(ids) > MAX_IDS:
        raise HTTPException(400, f"At most {MAX_IDS} at once")
    return ids


@router.get("/api/new-from-provider/counts")
def new_counts(db: Session = Depends(get_db)):
    """The sidebar badge: groups and categories waiting for a review."""
    return N.counts(db)


@router.get("/api/live/new-groups")
def new_groups(dismissed: bool = False, country: str = "", lang: str = "", db: Session = Depends(get_db)):
    """New Live TV groups (and the dismissed ones, asked for), each with its
    country, a clean title and whether it is probably for this household
    (`country` / `lang`: the admin's browser locale, a hint). Starts
    reading the channels of new groups not read lately, in the background;
    `loading` says how far it is."""
    out = N.live_overview(db, dismissed=dismissed, country=country[:8], lang=lang[:8])
    if not out["loading"]["running"] and N.needs_reading(db):
        N.read_new_in_background()
        out["loading"] = dict(N.loading, running=True)
    return out


@router.post("/api/live/new-groups/channels")
def new_group_channels(body: GroupIds, db: Session = Depends(get_db)):
    """Step 2: the channels of the ticked groups, folded, with their marks."""
    return N.channels_of(db, _limit(body.group_ids))


@router.post("/api/live/new-groups/add")
def new_groups_add(body: AddBody, db: Session = Depends(get_db)):
    """Add the picked channels to the household's channels; the groups are
    reviewed (unpicked channels stay out)."""
    if not body.group_ids:
        raise HTTPException(400, "No group")
    return N.add_to_pool(db, _limit(body.group_ids), _limit(body.channel_ids))


@router.post("/api/live/new-groups/dismiss")
def new_groups_dismiss(body: GroupIds, db: Session = Depends(get_db)):
    return {"dismissed": N.dismiss(db, _limit(body.group_ids))}


@router.post("/api/live/new-groups/restore")
def new_groups_restore(body: GroupIds, db: Session = Depends(get_db)):
    return {"restored": N.restore(db, _limit(body.group_ids))}


@router.get("/api/new-from-provider/categories")
def new_categories(dismissed: bool = False, db: Session = Depends(get_db)):
    return N.vod_overview(db, dismissed=dismissed)


@router.post("/api/new-from-provider/categories/add")
def new_categories_add(body: CategoryIds, db: Session = Depends(get_db)):
    """Add to the library: the categories sync with tonight's VOD sync."""
    return {"added": N.vod_decide(db, _limit(body.category_ids), add=True)}


@router.post("/api/new-from-provider/categories/dismiss")
def new_categories_dismiss(body: CategoryIds, db: Session = Depends(get_db)):
    return {"dismissed": N.vod_decide(db, _limit(body.category_ids), add=False)}


@router.post("/api/new-from-provider/categories/restore")
def new_categories_restore(body: CategoryIds, db: Session = Depends(get_db)):
    return {"restored": N.vod_restore(db, _limit(body.category_ids))}

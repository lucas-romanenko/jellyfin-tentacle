"""The channel catalog, for the admin (Sources → Live TV): folded networks and
their variants, splitting and merging, choosing the variant, ticking single
channels, correcting types. People read the catalog through the pool
(routers/my_livetv.py). See services/livetv_catalog.py.

Also the stream of a folded network, /api/live/net/<id>: the variant the
admin chose, through the same handler as /api/live/stream/<id> (so one
provider connection per channel, shared, and every stream limit), and
unauthenticated like it: Jellyfin's tuner reads it.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db
from routers.auth import require_admin
from services import livetv_catalog as C

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
stream_router = APIRouter()


def _changed(what: str) -> None:
    from routers.livetv import _pool_changed
    _pool_changed(what)


def _run(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except C.CatalogError as e:
        raise HTTPException(400, str(e))


@router.get("/api/live/catalog/types")
def catalog_types():
    return {"types": C.TYPES}


@router.get("/api/live/catalog/pool")
def catalog_pool(db: Session = Depends(get_db)):
    """The pool as people choose from it (folded), with "no event" channels
    the people's pickers hide."""
    from services.livetv_lineups import pool
    entries = pool(db)
    return {"channels": entries, "count": len(entries),
            "networks": sum(1 for e in entries if e["key"].startswith("n")),
            "no_event": sum(1 for e in entries if e.get("event") == "no_event")}


@router.get("/api/live/catalog/networks")
def catalog_networks(include_empty: bool = False, db: Session = Depends(get_db)):
    """Every folded network with its variants (switched on first)."""
    nets = C.networks_list(db, include_empty=include_empty)
    return {"networks": nets, "count": len(nets)}


@router.post("/api/live/catalog/refresh")
def catalog_refresh(db: Session = Depends(get_db)):
    """Fold again now (it happens by itself when the channel list changes)."""
    stats = C.refresh(db)
    _changed("channel catalog refreshed")
    return stats


class VariantBody(BaseModel):
    channel_id: Optional[int] = None   # None: back to the default


@router.put("/api/live/catalog/networks/{nid}/variant")
def catalog_set_variant(nid: int, body: VariantBody, db: Session = Depends(get_db)):
    _run(C.set_variant, db, nid, body.channel_id)
    _changed("a network's variant changed")
    return {"success": True, "variant_id": C.chosen_variants(db).get(nid)}


class NetworkBody(BaseModel):
    name: Optional[str] = None       # "" = the automatic name again
    excluded: Optional[bool] = None  # True: the network is not in the pool


@router.put("/api/live/catalog/networks/{nid}")
def catalog_update_network(nid: int, body: NetworkBody, db: Session = Depends(get_db)):
    if body.name is not None:
        _run(C.rename_network, db, nid, body.name)
    if body.excluded is not None:
        _run(C.set_excluded, db, nid, body.excluded)
    _changed("a network changed")
    return {"success": True}


class SplitBody(BaseModel):
    channel_ids: Optional[List[int]] = None  # none: dissolve the whole network
    name: Optional[str] = None               # the moved channels become this network


@router.post("/api/live/catalog/networks/{nid}/split")
def catalog_split(nid: int, body: SplitBody, db: Session = Depends(get_db)):
    res = _run(C.split, db, nid, body.channel_ids, body.name)
    C.ensure_fresh(db)
    _changed("a network split")
    return {"success": True, **res}


class MergeBody(BaseModel):
    keys: List[str]                  # "n<id>" / "c<id>"; the first network keeps its key
    name: Optional[str] = None


@router.post("/api/live/catalog/merge")
def catalog_merge(body: MergeBody, db: Session = Depends(get_db)):
    res = _run(C.merge, db, body.keys, body.name)
    C.ensure_fresh(db)
    _changed("channels merged into one network")
    return {"success": True, **res}


class EnabledBody(BaseModel):
    enabled: bool


@router.put("/api/live/catalog/channels/{channel_id}/enabled")
def catalog_tick(channel_id: int, body: EnabledBody, db: Session = Depends(get_db)):
    """Tick / untick one channel within an enabled group (a single channel or
    one variant of a network)."""
    _run(C.set_channel_enabled, db, channel_id, body.enabled)
    _changed("a channel switched on or off")
    return {"success": True}


class TypesBody(BaseModel):
    key: str                          # "c<id>" or "n<id>"
    types: Optional[List[str]] = None  # None / []: the automatic types again


@router.put("/api/live/catalog/types")
def catalog_set_types(body: TypesBody, db: Session = Depends(get_db)):
    types = _run(C.set_types, db, body.key, body.types)
    return {"success": True, "types": types}


@router.get("/api/live/catalog/logos")
def catalog_logos(key: str, db: Session = Depends(get_db)):
    """Logos to pick from for a channel ("c<id>") or network ("n<id>")."""
    return _run(C.logo_choices, db, key)


class LogoBody(BaseModel):
    key: str                   # "c<id>" or "n<id>"
    url: Optional[str] = None  # None / "": the automatic logo again


@router.put("/api/live/catalog/logo")
def catalog_set_logo(body: LogoBody, db: Session = Depends(get_db)):
    """The admin's logo: Jellyfin's channel image follows at the Live TV
    sync a few seconds later (services/livetv_tuners.py), then open clients."""
    url = _run(C.set_logo, db, body.key, body.url)
    _changed("a channel's logo changed")
    return {"success": True, "custom_logo_url": url}


# ── The network stream ──────────────────────────────────────────────────────

def _variant(db: Session, nid: int) -> int:
    cid = C.network_channel(db, nid)
    if cid is None:
        raise HTTPException(404, "Channel not found")
    return cid


@stream_router.head("/api/live/net/{nid}")
async def network_stream_head(nid: int, db: Session = Depends(get_db)):
    from routers.livetv import stream_head
    return await stream_head(_variant(db, nid), db)


@stream_router.get("/api/live/net/{nid}")
async def network_stream(nid: int, db: Session = Depends(get_db)):
    from routers.livetv import stream_proxy
    return await stream_proxy(_variant(db, nid), db)


def _place_variant(db: Session, nid: int, place: str) -> int:
    cid = C.place_channel(db, nid, place)
    if cid is None:
        raise HTTPException(404, "Channel not found")
    return cid


@stream_router.head("/api/live/net/{nid}/at/{place}")
async def network_place_stream_head(nid: int, place: str, db: Session = Depends(get_db)):
    from routers.livetv import stream_head
    return await stream_head(_place_variant(db, nid, place), db)


@stream_router.get("/api/live/net/{nid}/at/{place}")
async def network_place_stream(nid: int, place: str, db: Session = Depends(get_db)):
    """A network as people in a place watch it (its variant there, else the
    network's): the lineup's "CTV · Vancouver" (services/livetv_lineups.py)."""
    from routers.livetv import stream_proxy
    return await stream_proxy(_place_variant(db, nid, place), db)

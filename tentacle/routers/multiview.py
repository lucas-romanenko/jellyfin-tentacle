"""Multi-view: 2-4 Live TV channels in one picture (services/multiview.py).

Signed-in people, for themselves: what they may start now (free provider
connections), start one on channels from their own lineup, change it (swap
or remove a channel), keep it alive while a channel plays full screen, and
close it. The picture itself is one MPEG-TS stream at an unguessable
address (the session id), like the tuner's own: a player can't send
credentials.
"""
import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import LiveChannel, SessionLocal, TentacleUser, get_db
from routers.auth import get_user_from_request
from services import multiview as MV

logger = logging.getLogger(__name__)
router = APIRouter()


class StartBody(BaseModel):
    # Jellyfin channel item ids (what the TV app's guide has) or pool keys.
    item_ids: List[str] = []
    keys: List[str] = []


def _uid(user: TentacleUser) -> str:
    return (user.jellyfin_user_id or "").replace("-", "")


def _picks(db: Session, user: TentacleUser, body: StartBody) -> list:
    """[(channel_id, name, key, item_id)] for the asked channels, in order,
    from this person's own lineup only."""
    from routers.my_livetv import my_lineup
    from services import livetv_lineups as L
    from services import livetv_tuners as T
    entries = my_lineup(db, user)
    url = T.all_tuner_url(db)
    by_item = L.item_ids_for(url, entries) if url else {}
    item_of = {k: i for i, k in by_item.items()}
    by_key = {e["key"]: e for e in entries}
    by_base = {L.base_key(e["key"]): e for e in entries}
    norm = [i.replace("-", "").lower() for i in body.item_ids]
    asked = [(by_item.get(i), i) for i in norm] + [(k, None) for k in body.keys]
    if not asked:
        raise HTTPException(400, "Pick 2 to 4 channels")
    out, seen = [], set()
    for key, item_id in asked:
        entry = by_key.get(key) if key else None
        if entry is None and key:
            entry = by_base.get(L.base_key(key))
        if entry is None:
            raise HTTPException(404, "One of the channels isn't in your Live TV")
        cid = MV.channel_id_of_path(db, entry.get("stream_path") or "")
        if cid is None:
            raise HTTPException(409, f"{entry['name']} can't be in multi-view")
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, entry["name"], entry["key"], item_id or item_of.get(entry["key"]) or ""))
    if not MV.MIN_TILES <= len(out) <= MV.MAX_TILES:
        raise HTTPException(400, f"Pick {MV.MIN_TILES} to {MV.MAX_TILES} different channels")
    return out


def _soon() -> int:
    db = SessionLocal()
    try:
        return MV.upcoming_recordings(db)
    finally:
        db.close()


def _own(session_id: str, user: TentacleUser) -> MV.Session:
    s = MV.get(session_id)
    if s is None or s.user_id != _uid(user):
        raise HTTPException(404, "This multi-view has ended")
    return s


def _unavailable() -> Optional[str]:
    if not MV._ffmpeg():
        return "Multi-view needs ffmpeg on the Tentacle server."
    return None


@router.get("/api/live/multiview")
async def multiview_state(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    """Whether this person can start a multi-view now, and how many channels
    it can have: free connections (recordings starting soon set aside); a
    session of theirs that runs gives its connections back to the count."""
    reason = _unavailable()
    mine = MV.of_user(_uid(user))
    soon = await asyncio.to_thread(_soon)
    check = MV.free_connections(db, [], soon)
    free = check["free"]
    if free is not None:
        free += MV.own_connections(_uid(user))   # theirs come back when it is replaced
    max_tiles = MV.MAX_TILES if free is None else max(0, min(MV.MAX_TILES, free))
    can = reason is None and max_tiles >= MV.MIN_TILES
    message = reason or ("" if can else MV.refusal_message(MV.MIN_TILES, max(free or 0, 0), soon))
    return {"available": reason is None, "can_start": can, "max_tiles": max_tiles, "min_tiles": MV.MIN_TILES,
            "free": free, "limit": check["limit"], "recordings_soon": soon, "message": message,
            "session": mine[0].describe() if mine else None}


async def _start(db: Session, user: TentacleUser, body: StartBody) -> dict:
    reason = _unavailable()
    if reason:
        raise HTTPException(503, reason)
    picks = _picks(db, user, body)
    channels = db.query(LiveChannel).filter(LiveChannel.id.in_([p[0] for p in picks])).all()
    mine = MV.of_user(_uid(user))
    keep = {t.channel_id for s in mine for t in s.tiles}
    soon = await asyncio.to_thread(_soon)
    # A channel of their running multi-view is open already: it needs no new connection.
    check = MV.free_connections(db, channels, soon, keep=keep)
    if not check["ok"]:
        raise HTTPException(409, check["message"])
    try:
        session = await MV.start(_uid(user), picks, SessionLocal)
    except MV.MultiviewError as e:
        raise HTTPException(e.status, str(e))
    return session.describe()


@router.post("/api/live/multiview")
async def multiview_start(body: StartBody, user: TentacleUser = Depends(get_user_from_request),
                          db: Session = Depends(get_db)):
    """Start a multi-view on 2-4 channels (replaces this person's running one)."""
    return await _start(db, user, body)


@router.put("/api/live/multiview/{session_id}")
async def multiview_change(session_id: str, body: StartBody,
                           user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    """The same multi-view with other channels (one swapped or removed): the
    channels in both stay open throughout, the ones left go first (their
    connections count as free for the new ones), then the picture restarts."""
    s = _own(session_id, user)
    picks = _picks(db, user, body)
    held = {t.channel_id for t in s.tiles}
    wanted = {p[0] for p in picks}
    new = db.query(LiveChannel).filter(LiveChannel.id.in_(list(wanted - held))).all()
    if new:
        soon = await asyncio.to_thread(_soon)
        check = MV.free_connections(db, new, soon)
        freed = MV.own_connections(_uid(user), only=held - wanted)
        if not check["ok"] and check["free"] is not None and check["needed"] > check["free"] + freed:
            raise HTTPException(409, check["message"])
    try:
        await s.change(picks, SessionLocal)
    except MV.MultiviewError as e:
        if s.alive:
            raise HTTPException(e.status, f"{e} The others stay on.")
        await MV.close(s.id)
        raise HTTPException(e.status, str(e))
    return s.describe()


@router.post("/api/live/multiview/{session_id}/keepalive")
async def multiview_keepalive(session_id: str, user: TentacleUser = Depends(get_user_from_request)):
    """Still here (a channel plays full screen, the picture isn't read): the
    session stays. Answers its state (a channel whose stream ended: live false)."""
    s = _own(session_id, user)
    s.touch()
    return s.describe()


@router.delete("/api/live/multiview/{session_id}")
async def multiview_close(session_id: str, user: TentacleUser = Depends(get_user_from_request)):
    """Close it: every channel's connection is given back (unless someone
    else watches or records that channel)."""
    _own(session_id, user)
    await MV.close(session_id)
    return {"closed": True}


@router.get("/api/live/multiview/{session_id}/stream.ts")
async def multiview_stream(session_id: str):
    """The picture. Open like the tuner's streams: the id is the secret."""
    s = MV.get(session_id)
    if s is None:
        raise HTTPException(404, "This multi-view has ended")
    return StreamingResponse(s.read(), media_type="video/mp2t",
                             headers={"Cache-Control": "no-cache, no-store", "Connection": "close"})

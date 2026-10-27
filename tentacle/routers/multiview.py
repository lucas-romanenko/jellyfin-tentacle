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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import LiveChannel, SessionLocal, TentacleUser, get_db
from routers.auth import get_user_from_request
from services import multiview as MV
from services import web_stream as WS

logger = logging.getLogger(__name__)
router = APIRouter()


class StartBody(BaseModel):
    # Jellyfin channel item ids (what the TV app's guide has) or pool keys.
    item_ids: List[str] = []
    keys: List[str] = []


def _uid(user: TentacleUser) -> str:
    return (user.jellyfin_user_id or "").replace("-", "")


def _picks(db: Session, user: TentacleUser, body: StartBody, count: bool = True) -> list:
    """[(channel_id, name, key, item_id)] for the asked channels, in order,
    from this person's own lineup only (`count`: 2 to 4 of them)."""
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
    if count and not MV.MIN_TILES <= len(out) <= MV.MAX_TILES:
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


def _device(device_id: str) -> str:
    """The Jellyfin device the caller plays on (the plugin passes the device id
    of the caller's token). Like userId, a claim: it only picks among the
    signed-in person's own sessions."""
    return (device_id or "").strip()[:128]


@router.get("/api/live/multiview")
async def multiview_state(deviceId: str = "", user: TentacleUser = Depends(get_user_from_request),
                          db: Session = Depends(get_db)):
    """Whether this person can start a multi-view now on this device, and how
    many channels it can have: free connections (recordings starting soon set
    aside); their session on this device gives its connections back to the
    count (it is the one a new one replaces)."""
    reason = _unavailable()
    device = _device(deviceId)
    mine = MV.of_user(_uid(user), device)
    soon = await asyncio.to_thread(_soon)
    check = MV.free_connections(db, [], soon)
    free = check["free"]
    if free is not None:
        free += MV.own_connections(_uid(user), device_id=device)   # theirs come back when it is replaced
    max_tiles = MV.MAX_TILES if free is None else max(0, min(MV.MAX_TILES, free))
    can = reason is None and max_tiles >= MV.MIN_TILES
    message = reason or ("" if can else MV.refusal_message(MV.MIN_TILES, max(free or 0, 0), soon))
    return {"available": reason is None, "can_start": can, "max_tiles": max_tiles, "min_tiles": MV.MIN_TILES,
            "free": free, "limit": check["limit"], "recordings_soon": soon, "message": message,
            "session": mine[0].describe() if mine else None}


async def _start(db: Session, user: TentacleUser, body: StartBody, device: str = "") -> dict:
    reason = _unavailable()
    if reason:
        raise HTTPException(503, reason)
    picks = _picks(db, user, body)
    channels = db.query(LiveChannel).filter(LiveChannel.id.in_([p[0] for p in picks])).all()
    mine = MV.of_user(_uid(user), device)
    keep = {t.channel_id for s in mine for t in s.tiles}
    soon = await asyncio.to_thread(_soon)
    # A channel of their running multi-view is open already: it needs no new connection.
    check = MV.free_connections(db, channels, soon, keep=keep)
    if not check["ok"]:
        raise HTTPException(409, check["message"])
    try:
        session = await MV.start(_uid(user), picks, SessionLocal, device)
    except MV.MultiviewError as e:
        raise HTTPException(e.status, str(e))
    return session.describe()


@router.post("/api/live/multiview")
async def multiview_start(body: StartBody, deviceId: str = "", user: TentacleUser = Depends(get_user_from_request),
                          db: Session = Depends(get_db)):
    """Start a multi-view on 2-4 channels (replaces this person's running one
    on this device; one on another device plays on)."""
    return await _start(db, user, body, _device(deviceId))


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
        freed = MV.own_connections(_uid(user), only=held - wanted, device_id=s.device_id)
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


# ── For web browsers (Jellyfin Web): fragmented MP4 (services/web_stream.py) ──

_MP4_HEADERS = {"Cache-Control": "no-cache, no-store", "Connection": "close"}


@router.get("/api/live/multiview/{session_id}/web.mp4")
async def multiview_web(session_id: str, audio: int = 0):
    """The picture for a browser: fMP4 with only tile `audio`'s sound (a
    browser can't switch sound tracks). Open like stream.ts: the id is the secret."""
    s = MV.get(session_id)
    if s is None:
        raise HTTPException(404, "This multi-view has ended")
    ffmpeg = MV._ffmpeg()
    if not ffmpeg:
        raise HTTPException(503, "Multi-view needs ffmpeg on the Tentacle server.")
    audio = max(0, min(int(audio), len(s.tiles) - 1))
    return StreamingResponse(WS.fmp4(s.read(), WS.multiview_args(ffmpeg, audio)),
                             media_type="video/mp4", headers=_MP4_HEADERS)


@router.get("/api/live/preview/{item_id}/stream.mp4")
async def preview_stream(item_id: str, user: TentacleUser = Depends(get_user_from_request),
                         db: Session = Depends(get_db)):
    """The guide's muted live preview of one channel of this person's own
    lineup, for a browser: small silent fMP4. Joins the channel like the
    tuner (a channel already open costs no connection); a new connection
    only while /api/live/preview says a preview may run."""
    from routers import livetv as LT
    ffmpeg = MV._ffmpeg()
    if not ffmpeg:
        raise HTTPException(503, "Previews need ffmpeg on the Tentacle server.")
    cid, name, _key, _item = _picks(db, user, StartBody(item_ids=[item_id]), count=False)[0]
    shared = LT._shared_streams.get(cid)
    if shared is None or shared._closed:
        answer = await LT.live_preview(previewing=False, user=user, db=db)
        if not answer.get("previews"):
            raise HTTPException(409, "No connection is free for a preview right now")
    try:
        resp = await LT.stream_proxy(cid, db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"{name} didn't open: {e}")
    body = getattr(resp, "body_iterator", None)
    if body is None or (getattr(resp, "headers", {}) or {}).get("x-tentacle-reason"):
        await _leave(resp)
        raise HTTPException(503, f"{name} isn't coming through right now")

    return _PreviewResponse(WS.fmp4(body, WS.preview_args(ffmpeg)), resp)


class _PreviewResponse(StreamingResponse):
    """A preview that ALWAYS gives its channel back, however it ends.

    Giving it back from the fMP4 generator's end was not enough: a browser
    that dropped the request while the channel was opening (the next channel
    focused, the guide closed, Play pressed) is gone when the response
    starts, Starlette cancels it before the generator ever runs, and a
    generator that never started never runs its `finally`. The subscription
    stayed, and the provider connection with it, for as long as the channel
    played (27 Oct 2026: Cartoon Network for 25 minutes on a one-connection
    account). Same shape as livetv._SubscriberResponse."""

    def __init__(self, body, joined):
        super().__init__(body, media_type="video/mp4", headers=_MP4_HEADERS)
        self._joined = joined

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A generator that did start closes ffmpeg in its own finally;
            # one that never did has nothing running.
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                try:
                    await close()
                except BaseException:
                    pass
            await _leave(self._joined)


async def _leave(resp) -> None:
    """Give a stream_proxy subscription back (the upstream closes when it was the last)."""
    shared = getattr(resp, "_shared", None)
    if shared is not None:
        try:
            await shared.unsubscribe(resp._q)
        except Exception as e:
            logger.info(f"[Preview] unsubscribe: {e}")

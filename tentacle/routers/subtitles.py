"""Subtitles & skipping: each person's choices, the household's starting
points (admins), and the subtitles Tentacle gets ready (services/subtitles.py)."""
import asyncio
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import SubtitleFile, SubtitleJob, TentacleUser, get_db
from routers.auth import get_user_from_request, require_admin
from services import live_updates, subtitles, watch_prefs

logger = logging.getLogger(__name__)
router = APIRouter()

# How long the plugin's request for a subtitle that isn't ready waits here
# (Jellyfin's player waits on it; the plugin tells the person meanwhile).
WAIT_SECONDS = 20 * 60


class _Prefs(BaseModel):
    subtitle_languages: Optional[List[str]] = None
    subtitle_mode: Optional[str] = None
    skip_intro: Optional[str] = None
    skip_credits: Optional[str] = None


def _set(body: _Prefs) -> dict:
    return {k: v for k, v in body.model_dump().items() if v is not None}


def _answer(db, user: TentacleUser) -> dict:
    return {**watch_prefs.for_person(db, user), "kid": bool(user.kids_level),
            "languages": [{"code": c, "name": n, "own_name": o} for c, n, o in watch_prefs.LANGUAGES]}


def _tell(user: TentacleUser) -> None:
    """Their open apps pick the change up (the plugin's menus, the TV app, Jellyfin Web)."""
    live_updates.changed("watch", user_ids=[user.id], clients=True)


@router.get("/api/watch/prefs")
def my_prefs(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    return _answer(db, user)


@router.put("/api/watch/prefs")
async def save_my_prefs(body: _Prefs, db: Session = Depends(get_db),
                        user: TentacleUser = Depends(get_user_from_request)):
    try:
        watch_prefs.save_person(db, user, _set(body))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await asyncio.to_thread(watch_prefs.sync_to_jellyfin, db, user)
    _tell(user)
    return _answer(db, user)


@router.post("/api/watch/prefs/reset")
async def reset_my_prefs(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    watch_prefs.reset_person(db, user)
    await asyncio.to_thread(watch_prefs.sync_to_jellyfin, db, user)
    _tell(user)
    return _answer(db, user)


class _Defaults(BaseModel):
    grownups: Optional[_Prefs] = None
    kids: Optional[_Prefs] = None


@router.get("/api/watch/defaults", dependencies=[Depends(require_admin)])
def get_defaults(db: Session = Depends(get_db)):
    return {"grownups": watch_prefs.defaults(db), "kids": watch_prefs.defaults(db, kids=True),
            "languages": [{"code": c, "name": n, "own_name": o} for c, n, o in watch_prefs.LANGUAGES]}


@router.put("/api/watch/defaults", dependencies=[Depends(require_admin)])
async def save_defaults(body: _Defaults, db: Session = Depends(get_db)):
    try:
        if body.grownups is not None:
            watch_prefs.save_defaults(db, _set(body.grownups))
        if body.kids is not None:
            watch_prefs.save_defaults(db, _set(body.kids), kids=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Everyone who never chose follows the starting point.
    await asyncio.to_thread(watch_prefs.sync_everyone, db)
    live_updates.changed("watch", everyone=True, clients=True)
    return get_defaults(db)


# ── the subtitles themselves ────────────────────────────────────────────────

@router.post("/api/subtitles/playing/{item_id}")
async def playing(item_id: str, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """The plugin: someone started this title (its season's next episodes follow)."""
    try:
        n = await asyncio.to_thread(subtitles.on_play, db, item_id)
    except Exception as e:
        logger.info(f"[Subtitles] {item_id} not queued: {type(e).__name__}")
        n = 0
    return {"queued": n}


@router.get("/api/subtitles/{item_id}/status")
def status(item_id: str, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    return subtitles.status(db, item_id)


@router.get("/api/subtitles/{item_id}/embedded")
async def embedded_tracks(item_id: str, wait: int = 0, db: Session = Depends(get_db),
                          user: TentacleUser = Depends(get_user_from_request)):
    """The plugin, when Jellyfin is asked for a title's embedded subtitle it
    doesn't have: which of its tracks (stream indexes) Tentacle has a copy
    of. A title not read yet goes first in line and is waited for (at most
    `wait` seconds)."""
    item_id = item_id.replace("-", "").lower()

    def tracks():
        return sorted(int(f.source_ref) for f in db.query(SubtitleFile).filter(
            SubtitleFile.jellyfin_item_id == item_id, SubtitleFile.source == "embedded").all()
            if (f.source_ref or "").isdigit() and os.path.exists(f.path))
    have = tracks()
    job = db.query(SubtitleJob).filter(SubtitleJob.jellyfin_item_id == item_id).first()
    if not have or job is None or job.state != "done":
        job = await subtitles.wait_for(db, item_id, min(max(wait, 0), WAIT_SECONDS))
        db.expire_all()
        have = tracks()
    return {"state": job.state if job else "none", "tracks": have}


@router.get("/api/subtitles/{item_id}/stream/{index}.srt")
async def embedded(item_id: str, index: int, wait: int = 0, db: Session = Depends(get_db),
                   user: TentacleUser = Depends(get_user_from_request)):
    """The plugin, when Jellyfin is asked for an embedded subtitle of a
    title: Tentacle's copy of that track, first in line if it isn't ready
    (waiting up to `wait` seconds). 404 when the title has no such text track
    in the household's languages, 503 while it is still being read."""
    item_id = item_id.replace("-", "").lower()

    def found():
        return (db.query(SubtitleFile).filter(SubtitleFile.jellyfin_item_id == item_id,
                                              SubtitleFile.source == "embedded",
                                              SubtitleFile.source_ref == str(index)).first())
    row = found()
    if row is None:
        job = await subtitles.wait_for(db, item_id, min(max(wait, 0), WAIT_SECONDS))
        db.expire_all()
        row = found()
        if row is None:
            if job is not None and job.state in ("queued", "running"):
                raise HTTPException(503, "Still reading this title's subtitles")
            raise HTTPException(404, "Tentacle has no copy of that subtitle track")
    try:
        text = await asyncio.to_thread(lambda: open(row.path, "rb").read())
    except OSError:
        db.delete(row)
        db.commit()
        raise HTTPException(404, "That subtitle file is gone")
    return Response(content=text, media_type="application/x-subrip")


class _Another(BaseModel):
    language: str


@router.post("/api/subtitles/{item_id}/another")
async def another(item_id: str, body: _Another, db: Session = Depends(get_db),
                  user: TentacleUser = Depends(get_user_from_request)):
    """"Subtitles wrong? Get another": the next one from the subtitle site,
    lined up with the sound. Answers when it is in place (or there is none)."""
    lang = watch_prefs.normalize(body.language)
    if not lang:
        raise HTTPException(400, "Which language?")
    result = await subtitles.get_another(db, item_id, lang)
    if result is None:
        raise HTTPException(404, f"No other {watch_prefs.name(lang)} subtitles were found for this title.")
    live_updates.changed("watch", user_ids=[user.id], clients=True)
    return {"done": result}


@router.get("/api/subtitles/queue", dependencies=[Depends(require_admin)])
def queue_list(db: Session = Depends(get_db)):
    rows = (db.query(SubtitleJob).order_by(SubtitleJob.state.desc(), SubtitleJob.priority, SubtitleJob.queued_at)
            .limit(200).all())
    return [{"item_id": j.jellyfin_item_id, "name": j.name, "state": j.state, "priority": j.priority,
             "method": j.method, "error": j.error, "languages": j.languages,
             "finished_at": j.finished_at.isoformat() if j.finished_at else None} for j in rows]


# ── Skip intro / Skip credits (services/segments.py) ────────────────────────

@router.get("/api/segments/{item_id}")
def segments_for(item_id: str, db: Session = Depends(get_db)):
    """The plugin's media segment provider (Jellyfin calls it with no person
    behind it): where this item's intro and end credits are. Times only."""
    from services import segments
    return {"segments": segments.for_item(db, item_id)}

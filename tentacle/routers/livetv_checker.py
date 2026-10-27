"""
Tentacle - Live TV stream checker status (admin only).

GET /api/live/checker/status: what the background checker
(services/livetv_checker.py) is doing -- checking / paused (and why) /
idle / backing off a provider --, progress over the pool and over every
enabled channel, the last check; "paused_by_admin" (state "off") when an
admin paused it.
PUT /api/live/checker/enabled {"enabled": bool}: the admin's switch (off =
the checker opens no channel until it's switched on again).
"""
import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import SessionLocal, get_db
from routers.auth import require_admin
from services import livetv_checker

router = APIRouter(prefix="/api/live/checker", tags=["livetv"], dependencies=[Depends(require_admin)])


@router.get("/status")
def checker_status(db: Session = Depends(get_db)):
    return livetv_checker.summary(db)


class EnabledBody(BaseModel):
    enabled: bool


@router.put("/enabled")
async def checker_enabled(body: EnabledBody):
    await livetv_checker.set_enabled(body.enabled)

    def summary():
        db = SessionLocal()
        try:
            return livetv_checker.summary(db)
        finally:
            db.close()
    return await asyncio.to_thread(summary)

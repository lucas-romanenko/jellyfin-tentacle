"""
Tentacle - Live TV stream checker status (admin only).

GET /api/live/checker/status: what the background checker
(services/livetv_checker.py) is doing -- checking / paused (and why) /
idle / backing off a provider --, progress over the pool and over every
enabled channel, the last check.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.database import get_db
from routers.auth import require_admin
from services import livetv_checker

router = APIRouter(prefix="/api/live/checker", tags=["livetv"], dependencies=[Depends(require_admin)])


@router.get("/status")
def checker_status(db: Session = Depends(get_db)):
    return livetv_checker.summary(db)

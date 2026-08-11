"""
Tentacle - Health Router
Library housekeeping: deletion audit log, download health, missing content,
stream health. Admin-only.
"""

import logging
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.database import get_db, DeletionLog
from routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/health", tags=["health"], dependencies=[Depends(require_admin)])


# ─── Deletion audit log ───────────────────────────────────────────────────────

@router.get("/deletions")
def get_deletions(limit: int = 200, db: Session = Depends(get_db)):
    """Recent deletion-audit entries, newest first."""
    limit = max(1, min(limit, 1000))
    entries = db.query(DeletionLog).order_by(DeletionLog.created_at.desc()).limit(limit).all()
    return [{
        "id": e.id,
        "kind": e.kind,
        "name": e.name,
        "media_type": e.media_type,
        "size_bytes": e.size_bytes,
        "reason": e.reason,
        "detail": e.detail,
        "user_name": e.user_name,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    } for e in entries]

"""
Tentacle - Health Router
Library housekeeping: deletion audit log, download health, missing content,
stream health. Admin-only.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db, get_setting, set_setting, DeletionLog
from routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/health", tags=["health"], dependencies=[Depends(require_admin)])


# ─── Download health ──────────────────────────────────────────────────────────

class DownloadActionRequest(BaseModel):
    source: str  # radarr | sonarr
    queue_id: int
    delete_file: bool = True


class DownloadImportRequest(BaseModel):
    source: str
    download_id: str
    movie_id: int | None = None
    episode_id: int | None = None


class DownloadTogglesRequest(BaseModel):
    auto_fix: bool | None = None
    auto_import: bool | None = None


def _validate_source(source: str):
    if source not in ("radarr", "sonarr"):
        raise HTTPException(400, "source must be radarr or sonarr")


@router.post("/downloads/fix")
def fix_stuck_download(body: DownloadActionRequest, db: Session = Depends(get_db)):
    """Cancel + blocklist a stuck download and grab a replacement release."""
    _validate_source(body.source)
    from services.download_health import resolve_stuck_download
    result = resolve_stuck_download(db, body.source, body.queue_id, reason="manual")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Fix failed"))
    return result


@router.post("/downloads/remove")
def remove_queued_download(body: DownloadActionRequest, db: Session = Depends(get_db)):
    """Dequeue without blocklisting or re-grabbing."""
    _validate_source(body.source)
    from services.download_health import remove_download
    result = remove_download(db, body.source, body.queue_id, delete_file=body.delete_file, reason="manual")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Remove failed"))
    return result


@router.post("/downloads/manual-import")
def manual_import_download(body: DownloadImportRequest, db: Session = Depends(get_db)):
    """Force-import a blocked download when the arr's own lookup is unambiguous."""
    _validate_source(body.source)
    from services.download_health import attempt_manual_import
    try:
        result = attempt_manual_import(db, body.source, body.download_id,
                                       movie_id=body.movie_id, episode_id=body.episode_id)
    except Exception as e:
        raise HTTPException(502, f"Manual import failed: {e}")
    return result


@router.get("/downloads/settings")
def get_download_settings(db: Session = Depends(get_db)):
    return {
        "auto_fix": get_setting(db, "downloads_auto_fix_enabled", "false") == "true",
        "auto_import": get_setting(db, "downloads_auto_import_enabled", "false") == "true",
    }


@router.post("/downloads/settings")
def save_download_settings(body: DownloadTogglesRequest, db: Session = Depends(get_db)):
    if body.auto_fix is not None:
        set_setting(db, "downloads_auto_fix_enabled", "true" if body.auto_fix else "false")
    if body.auto_import is not None:
        set_setting(db, "downloads_auto_import_enabled", "true" if body.auto_import else "false")
    return {"success": True}


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

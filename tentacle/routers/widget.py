"""
Tentacle - Homepage Widget Router
Provides a status endpoint compatible with Homepage dashboard's customapi widget
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime
from models.database import get_db, Movie, Series, SyncRun, Provider, Duplicate

router = APIRouter(prefix="/api/widget", tags=["widget"])


@router.get("/status")
def widget_status(db: Session = Depends(get_db)):
    """
    Homepage dashboard customapi widget endpoint.
    Returns a clean JSON summary of Tentacle status.
    """
    total_movies = db.query(Movie).count()
    total_series = db.query(Series).count()
    active_providers = db.query(Provider).filter(Provider.active == True).count()
    pending_dups = db.query(Duplicate).filter(Duplicate.resolution == "pending").count()

    last_run = db.query(SyncRun).filter(
        SyncRun.status == "completed"
    ).order_by(SyncRun.completed_at.desc()).first()

    running_run = db.query(SyncRun).filter(
        SyncRun.status == "running"
    ).first()

    status = "syncing" if running_run else ("ok" if last_run else "idle")

    last_sync_str = "Never"
    movies_added = 0
    if last_run and last_run.completed_at:
        last_sync_str = last_run.completed_at.strftime("%Y-%m-%d %H:%M")
        movies_added = last_run.movies_new or 0

    return {
        "status": status,
        "movies": total_movies,
        "series": total_series,
        "providers": active_providers,
        "last_sync": last_sync_str,
        "last_sync_new": movies_added,
        "pending_duplicates": pending_dups,
        "needs_attention": pending_dups > 0,
    }

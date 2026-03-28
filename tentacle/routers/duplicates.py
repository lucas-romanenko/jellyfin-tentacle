"""
Tentacle - Duplicates Router
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime
from pathlib import Path
from models.database import get_db, get_setting, Duplicate, Movie, Series

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])


def _apply_resolution(dup: Duplicate, resolution: str, db: Session):
    """
    Act on the resolution decision:
    keep_radarr = delete VOD .strm + .nfo files, remove VOD DB record
    keep_vod    = delete from Radarr (API + files), remove Radarr DB record
    keep_both   = do nothing
    """
    if resolution == "keep_both":
        return

    sources = dup.sources or []

    if resolution == "keep_radarr":
        # Delete VOD strm/nfo files
        for source in sources:
            src = source.get("source", "")
            path = source.get("path", "")
            if src.startswith("provider_") and path:
                _delete_vod_files(path)

        # Remove VOD DB record
        if dup.media_type == "movie":
            vod = db.query(Movie).filter(
                Movie.tmdb_id == dup.tmdb_id,
                Movie.source != "radarr"
            ).first()
            if vod:
                db.delete(vod)
                logger.info(f"Removed VOD DB record for tmdb:{dup.tmdb_id}")
        else:
            vod = db.query(Series).filter(
                Series.tmdb_id == dup.tmdb_id,
                Series.source != "radarr"
            ).first()
            if vod:
                db.delete(vod)
                logger.info(f"Removed VOD DB record for tmdb:{dup.tmdb_id}")

    elif resolution == "keep_vod":
        # Delete from Radarr via API (removes from Radarr + deletes files on disk)
        _delete_from_radarr(dup.tmdb_id, db)

        # Remove Radarr DB record
        radarr_movie = db.query(Movie).filter(
            Movie.tmdb_id == dup.tmdb_id,
            Movie.source == "radarr"
        ).first()
        if radarr_movie:
            db.delete(radarr_movie)
            logger.info(f"Removed Radarr DB record for tmdb:{dup.tmdb_id}")

    db.commit()


def _delete_vod_files(strm_path: str):
    """Delete a VOD .strm file and its companion .nfo, plus empty parent folder."""
    try:
        strm = Path(strm_path)
        if strm.exists() and strm.suffix == ".strm":
            strm.unlink()
            logger.info(f"Deleted VOD strm: {strm_path}")

            # Delete companion .nfo file (same name, different extension)
            nfo = strm.with_suffix(".nfo")
            if nfo.exists():
                nfo.unlink()
                logger.info(f"Deleted companion NFO: {nfo}")

            # Remove parent folder if empty
            parent = strm.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                logger.info(f"Removed empty folder: {parent}")
    except Exception as e:
        logger.warning(f"Could not delete VOD files at {strm_path}: {e}")


def _delete_from_radarr(tmdb_id: int, db: Session):
    """Delete a movie from Radarr via its API."""
    from services.radarr import RadarrService

    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")
    if not radarr_url or not radarr_key:
        logger.warning(f"Cannot delete tmdb:{tmdb_id} from Radarr — not configured")
        return

    radarr = RadarrService(radarr_url, radarr_key)
    radarr.delete_movie(tmdb_id, delete_files=True)


class ResolveRequest(BaseModel):
    resolution: str  # keep_radarr | keep_vod | keep_both


class ResolveAllRequest(BaseModel):
    resolution: str


@router.get("")
def get_duplicates(db: Session = Depends(get_db)):
    dups = db.query(Duplicate).order_by(Duplicate.detected_at.desc()).all()
    pending = sum(1 for d in dups if d.resolution == "pending")
    resolved = sum(1 for d in dups if d.resolution != "pending")

    # Enrich with movie/series title and poster from DB
    enriched = []
    for d in dups:
        entry = {
            "id": d.id,
            "tmdb_id": d.tmdb_id,
            "media_type": d.media_type,
            "sources": d.sources,
            "resolution": d.resolution,
            "detected_at": d.detected_at,
            "resolved_at": d.resolved_at,
            "title": None,
            "poster_path": None,
        }
        if d.media_type == "movie":
            movie = db.query(Movie).filter(Movie.tmdb_id == d.tmdb_id).first()
            if movie:
                entry["title"] = movie.title
                entry["poster_path"] = movie.poster_path
        else:
            series = db.query(Series).filter(Series.tmdb_id == d.tmdb_id).first()
            if series:
                entry["title"] = series.title
                entry["poster_path"] = series.poster_path
        enriched.append(entry)

    return {
        "total": len(dups),
        "pending": pending,
        "resolved": resolved,
        "duplicates": enriched,
    }


@router.post("/{dup_id}/resolve")
def resolve_duplicate(dup_id: int, body: ResolveRequest, db: Session = Depends(get_db)):
    dup = db.query(Duplicate).filter(Duplicate.id == dup_id).first()
    if not dup:
        raise HTTPException(404, "Duplicate not found")

    _apply_resolution(dup, body.resolution, db)

    # Mark as resolved (keep in DB for stats/history)
    dup.resolution = body.resolution
    dup.resolved_at = datetime.now(datetime.timezone.utc)
    db.commit()

    return {"success": True}


@router.post("/resolve-all")
def resolve_all(body: ResolveAllRequest, db: Session = Depends(get_db)):
    pending = db.query(Duplicate).filter(Duplicate.resolution == "pending").all()
    count = len(pending)

    # Apply resolution to each duplicate (delete files, clean up DB)
    for dup in pending:
        try:
            _apply_resolution(dup, body.resolution, db)
        except Exception as e:
            logger.error(f"Failed to apply resolution for tmdb:{dup.tmdb_id}: {e}")
        dup.resolution = body.resolution
        dup.resolved_at = datetime.now(datetime.timezone.utc)
    db.commit()

    return {"success": True, "count": count}

"""
Tentacle - Duplicates Router
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timezone
from models.database import get_db, get_setting, Duplicate, Movie, Series, log_deletion
from routers.auth import require_admin
from services.duplicates import delete_vod_files, convert_record_to_downloaded
from services.media_files import delete_series_files

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/duplicates", tags=["duplicates"], dependencies=[Depends(require_admin)])


def _apply_resolution(dup: Duplicate, resolution: str, db: Session):
    """
    Act on the resolution decision:
    keep_radarr = delete VOD .strm + .nfo files, CONVERT the DB row to a
                  downloaded-only record (tmdb_id is unique — one row per
                  title; deleting it would make the nightly VOD sync
                  re-import the provider copy as brand new)
    keep_vod    = delete from Radarr (API + files), clear radarr_path
    keep_both   = do nothing
    """
    if resolution == "keep_both":
        return

    sources = dup.sources or []
    model = Movie if dup.media_type == "movie" else Series
    downloaded_source = "radarr" if dup.media_type == "movie" else "sonarr"

    title = None
    record = db.query(model).filter(model.tmdb_id == dup.tmdb_id).first()
    if record is not None:
        title = record.title

    is_series = dup.media_type != "movie"

    if resolution == "keep_radarr":
        # Delete VOD strm/nfo files. A series' VOD path is its show folder, not
        # a .strm, so it needs the folder-aware helper (extension-based, safe in
        # merged folders); the movie helper silently ignored it.
        for source in sources:
            src = source.get("source", "")
            path = source.get("path", "")
            if src.startswith("provider_") and path:
                if is_series:
                    delete_series_files(path)
                else:
                    delete_vod_files(path)

        # Convert the provider-owned row into a downloaded-only row.
        # The VOD sync then skips this title forever (source is radarr/sonarr)
        # and the Radarr/Sonarr scan sees an existing record — not a new movie.
        if record and record.source != downloaded_source:
            convert_record_to_downloaded(record, dup.media_type)
        log_deletion(db, kind="duplicate-resolve", name=title or f"tmdb:{dup.tmdb_id}",
                     media_type=dup.media_type, reason="manual",
                     detail="Kept downloaded copy — VOD .strm/.nfo files deleted")

    elif resolution == "keep_vod":
        # Delete the downloaded copy from the *arr that owns it (API + files).
        # Never Radarr for a series: TMDB movie and TV ids are separate number
        # spaces, so a series' id names an unrelated film in Radarr.
        if is_series:
            deleted_ok = _delete_from_sonarr(dup.tmdb_id, db)
            arr = "Sonarr"
        else:
            deleted_ok = _delete_from_radarr(dup.tmdb_id, db)
            arr = "Radarr"

        # Only touch the DB if the API delete actually succeeded — otherwise
        # the files are still on disk and the resolution should be retryable.
        if not deleted_ok:
            db.rollback()
            raise HTTPException(502, f"Failed to delete tmdb:{dup.tmdb_id} from {arr} — files may still exist")

        # The (single) row is the VOD one — just clear the downloaded-copy path
        path_attr = "sonarr_path" if is_series else "radarr_path"
        if record is not None and getattr(record, path_attr, None):
            setattr(record, path_attr, None)

        # Legacy state: a radarr-only row (shouldn't exist alongside VOD due to
        # the unique constraint, but clean up if the row itself is radarr-owned)
        radarr_movie = None if is_series else db.query(Movie).filter(
            Movie.tmdb_id == dup.tmdb_id,
            Movie.source == "radarr"
        ).first()
        if radarr_movie:
            db.delete(radarr_movie)
            logger.info(f"Removed Radarr DB record for tmdb:{dup.tmdb_id}")
        log_deletion(db, kind="duplicate-resolve", name=title or f"tmdb:{dup.tmdb_id}",
                     media_type=dup.media_type, reason="manual",
                     detail=f"Kept VOD copy — downloaded files deleted from {arr}")

    db.commit()


def _delete_from_radarr(tmdb_id: int, db: Session) -> bool:
    """Delete a movie from Radarr via its API. Returns True on success.

    If Radarr is not configured there is nothing to delete on the *arr side, so
    we treat it as success (the caller still removes the DB record).
    """
    from services.radarr import RadarrService

    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")
    if not radarr_url or not radarr_key:
        logger.warning(f"Cannot delete tmdb:{tmdb_id} from Radarr — not configured")
        return True

    try:
        radarr = RadarrService(radarr_url, radarr_key)
        return bool(radarr.delete_movie(tmdb_id, delete_files=True))
    except Exception as e:
        logger.error(f"Failed to delete tmdb:{tmdb_id} from Radarr: {e}")
        return False


def _delete_from_sonarr(tmdb_id: int, db: Session) -> bool:
    """Delete a series from Sonarr via its API. Returns True on success.

    Same contract as _delete_from_radarr: not configured means nothing to
    delete on the *arr side.
    """
    from services.sonarr import SonarrService

    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        logger.warning(f"Cannot delete tmdb:{tmdb_id} from Sonarr — not configured")
        return True

    try:
        sonarr = SonarrService(sonarr_url, sonarr_key)
        return bool(sonarr.delete_series(tmdb_id, delete_files=True))
    except Exception as e:
        logger.error(f"Failed to delete tmdb:{tmdb_id} from Sonarr: {e}")
        return False


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
    dup.resolved_at = datetime.now(timezone.utc)
    db.commit()

    return {"success": True}


@router.post("/resolve-all")
def resolve_all(body: ResolveAllRequest, db: Session = Depends(get_db)):
    pending = db.query(Duplicate).filter(Duplicate.resolution == "pending").all()
    total = len(pending)

    # Apply resolution to each duplicate (delete files, clean up DB). Only mark a
    # duplicate resolved if its resolution actually succeeded — failed ones stay
    # pending so they can be retried instead of being silently dropped.
    resolved = 0
    failed = 0
    for dup in pending:
        try:
            _apply_resolution(dup, body.resolution, db)
        except Exception as e:
            logger.error(f"Failed to apply resolution for tmdb:{dup.tmdb_id}: {e}")
            failed += 1
            continue
        dup.resolution = body.resolution
        dup.resolved_at = datetime.now(timezone.utc)
        resolved += 1
    db.commit()

    return {"success": failed == 0, "count": resolved, "total": total, "failed": failed}

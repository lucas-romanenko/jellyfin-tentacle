"""
Tentacle - Duplicates Router
"""

import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timezone
from models.database import get_db, get_setting, Duplicate, Movie, Series, log_deletion
from routers.auth import require_admin
from services.duplicates import (
    delete_vod_files, convert_record_to_downloaded, is_downloaded_file, arr_folder_is_vod_folder,
    carry_user_data, UserDataCarryError,
)
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
    keep_vod    = delete the downloaded files from Radarr/Sonarr (never the
                  VOD folder), remove the title there, clear radarr_path
    keep_both   = do nothing
    Before either deletes a copy, every Jellyfin user's played state, resume
    point and favourite on it are merged onto the copy that stays.
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
        if is_series:
            _require_series_download(dup, db)
        _carry_user_data(dup, record, "download", db)
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
        _require_vod_copy(dup, record)
        # Delete the downloaded copy from the *arr that owns it: its files
        # through the file API, then the title. Never Radarr for a series:
        # TMDB movie and TV ids are separate number spaces, so a series' id
        # names an unrelated film in Radarr.
        _carry_user_data(dup, record, "vod", db)
        arr = _delete_downloaded_copy(dup, record, db)

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


def _carry_user_data(dup: Duplicate, record, keep: str, db: Session) -> None:
    """Users' watched state on the removed copy goes to the kept one first;
    when that can't be done, nothing is deleted."""
    try:
        carry_user_data(db, dup, record, keep)
    except UserDataCarryError as e:
        db.rollback()
        raise HTTPException(e.status, e.message)


def _require_vod_copy(dup: Duplicate, record) -> None:
    """Refuse Keep VOD when the VOD copy it would keep is not on disk.

    Keep VOD deletes the download; without a VOD copy that deletes the title.
    It happens after a Keep Downloaded that a restart interrupted once it had
    deleted the .strm (the duplicate is pending again), and for a title that
    was downloaded first, whose provider .strm the VOD sync never wrote. The
    VOD copy is a provider source's path, or the row's own strm_path while a
    provider owns the row: a film's .strm file, a show folder holding at least
    one .strm. These are Tentacle's own paths, so the check is reliable."""
    paths = [s.get("path") for s in dup.sources or []
             if (s.get("source") or "").startswith("provider_") and s.get("path")]
    if record is not None and (record.source or "").startswith("provider_") and record.strm_path:
        paths.append(record.strm_path)
    for p in paths:
        path = Path(p)
        try:
            if dup.media_type == "movie":
                if path.suffix.lower() == ".strm" and path.is_file():
                    return
            elif path.is_dir() and any(f.suffix.lower() == ".strm" and f.is_file() for f in path.rglob("*")):
                return
        except OSError as e:  # can't look: not proof that the copy is there
            logger.warning(f"Keep VOD: could not check {p}: {e}")
    raise HTTPException(409, "The VOD copy of this title isn't on disk (it was never written, or it was "
                             "deleted), so Keep VOD would leave nothing to play. Nothing was deleted; use "
                             "Keep Downloaded or Keep Both.")


def _require_series_download(dup: Duplicate, db: Session) -> None:
    """Refuse Keep Downloaded for a show Sonarr holds no real download of.

    Sonarr 4 lists Tentacle's .strm files as episode files, so a show Sonarr
    holds at the VOD folder looks downloaded without one. Keep Downloaded
    would then delete every .strm, tvshow.nfo and the empty folders, and hand
    the row to Sonarr so the VOD sync skips the show for good. Checked on
    every resolve (a retry or Resolve All too); nothing is deleted unless
    Sonarr lists at least one file that is not a .strm."""
    from services.duplicates import series_has_real_download
    url, key = get_setting(db, "sonarr_url"), get_setting(db, "sonarr_api_key")
    if not url or not key:
        raise HTTPException(409, "Sonarr isn't configured, so Tentacle can't check that this show was "
                                 "downloaded. Nothing was deleted.")
    from services.sonarr import SonarrService
    sonarr = SonarrService(url, key)
    try:
        show = sonarr.get_series_by_tmdb(dup.tmdb_id, raise_errors=True)
    except Exception as e:
        logger.error(f"Keep Downloaded: could not read tmdb:{dup.tmdb_id} from Sonarr: {e}")
        raise HTTPException(502, "Couldn't reach Sonarr to check the download. Nothing was deleted; try again.")
    has = series_has_real_download(sonarr, show.get("id")) if show else False
    if has is None:
        raise HTTPException(502, "Couldn't read Sonarr's episode files. Nothing was deleted; try again.")
    if not has:
        raise HTTPException(409, "Nothing downloaded for this show: Sonarr only lists the VOD .strm files. "
                                 "Nothing was deleted; use Keep Both to dismiss it.")


def _delete_downloaded_copy(dup: Duplicate, record, db: Session) -> str:
    """Delete the downloaded copy of a duplicate from Radarr/Sonarr, keeping
    the VOD copy. Returns the arr's name; raises HTTPException (and changes
    nothing in Tentacle) when the download may still be on disk.

    Never deleteFiles=true on a folder that is also the VOD folder (the merged
    layout): Radarr/Sonarr then delete the title's WHOLE folder, .strm and
    .nfo included, and only after answering 200, so a folder they can't empty
    fails unseen. Instead the imported files are deleted one by one (a failure
    is an HTTP error Tentacle sees), never a .strm (Sonarr 4 lists Tentacle's
    .strm files as episode files), then the title is removed without its
    files. In separate folders the title goes with deleteFiles=true, which
    also removes the arr's leftover extras and the empty folder.

    Not configured means there is nothing to delete on the arr side.
    """
    is_series = dup.media_type != "movie"
    arr = "Sonarr" if is_series else "Radarr"
    prefix = "sonarr" if is_series else "radarr"
    url, key = get_setting(db, f"{prefix}_url"), get_setting(db, f"{prefix}_api_key")
    if not url or not key:
        logger.warning(f"Cannot delete tmdb:{dup.tmdb_id} from {arr}: not configured")
        return arr

    def fail(msg):
        db.rollback()
        raise HTTPException(502, f"{msg}; nothing was changed. Try again.")

    try:
        if is_series:
            from services.sonarr import SonarrService
            svc = SonarrService(url, key)
            title = svc.get_series_by_tmdb(dup.tmdb_id, raise_errors=True)
        else:
            from services.radarr import RadarrService
            svc = RadarrService(url, key)
            title = svc.get_movie_by_tmdb(dup.tmdb_id)
    except Exception as e:
        logger.error(f"Keep VOD: could not read tmdb:{dup.tmdb_id} from {arr}: {e}")
        fail(f"Couldn't reach {arr}")
    if not title:
        fail(f"tmdb:{dup.tmdb_id} is not in {arr}, or {arr} could not be read")

    try:
        files = svc.get_episode_files(title["id"]) if is_series else svc.get_movie_files(title["id"])
    except Exception as e:
        logger.error(f"Keep VOD: could not list {arr}'s files for tmdb:{dup.tmdb_id}: {e}")
        fail(f"Couldn't read {arr}'s files for this title")
    downloaded = [f for f in files or [] if is_downloaded_file(f.get("path"))]
    try:
        if is_series:
            svc.delete_episode_files([f["id"] for f in downloaded])
        else:
            for f in downloaded:
                svc.delete_movie_file(f["id"])
    except Exception as e:
        logger.error(f"Keep VOD: {arr} could not delete the downloaded files of tmdb:{dup.tmdb_id}: {e}")
        fail(f"{arr} could not delete the downloaded files (they may still be on disk)")

    # With nothing downloaded there is nothing for deleteFiles to remove, and
    # a folder that is also the VOD folder must never go with the title.
    delete_files = bool(downloaded) and not arr_folder_is_vod_folder(dup.media_type, title.get("path"), record)
    ok = (svc.delete_series_by_id(title["id"], delete_files=delete_files) if is_series
          else svc.delete_movie_by_id(title["id"], delete_files=delete_files))
    if not ok:
        fail(f"{arr} refused to remove tmdb:{dup.tmdb_id}" + (" (its downloaded files are already deleted)"
                                                              if downloaded else ""))
    logger.info(f"Keep VOD: removed tmdb:{dup.tmdb_id} from {arr}: {len(downloaded)} downloaded file(s) deleted, "
                f"{len(files or []) - len(downloaded)} .strm kept, deleteFiles={delete_files}")
    return arr


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

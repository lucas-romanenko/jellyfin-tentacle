"""
Tentacle - Duplicates Router
"""

import logging
import threading
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timezone
from models.database import get_db, get_setting, set_setting, DownloadRequest, Duplicate, Movie, Series, log_deletion
from routers.auth import require_admin
from services.duplicates import (
    delete_vod_files, convert_record_to_downloaded, is_downloaded_file, arr_folder_is_vod_folder,
    carry_user_data, UserDataCarryError, watch_pending_user_data, vod_copy_on_disk, resolving,
    vod_only_episodes,
)
from services.media_files import delete_series_files

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/duplicates", tags=["duplicates"], dependencies=[Depends(require_admin)])

# A duplicate is resolved once (#328). Each resolution re-reads its duplicate
# under this lock and goes ahead only while it is still pending, so Keep
# Downloaded in one tab and Keep VOD in a stale one (or Resolve All racing a
# single button) can't delete one copy each. One uvicorn worker, so a
# process lock covers every request; Resolve All takes it per duplicate.
_resolve_lock = threading.Lock()
_LABELS = {"keep_radarr": "Kept Downloaded", "keep_vod": "Kept VOD", "keep_both": "Kept Both"}


def _apply_resolution(dup: Duplicate, resolution: str, db: Session, reason: str = "manual", why: str = None):
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
    `reason` and `why` go to the Deletion Log: "manual" from the page, the
    automatic rule's own (services/duplicates_auto.py) with its reason.
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
        else:
            _require_movie_download(dup, db)
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
                     media_type=dup.media_type, reason=reason,
                     detail="Kept downloaded copy — VOD .strm/.nfo files deleted" + (f" ({why})" if why else ""))

    elif resolution == "keep_vod":
        # Keep VOD deletes the download: without the VOD copy on disk that
        # leaves nothing to play. A Keep Downloaded a restart cut short after
        # it deleted the .strm leaves the duplicate pending that way (#332).
        if not vod_copy_on_disk(dup, record):
            raise HTTPException(409, "The VOD copy of this title isn't on disk (it was deleted, or never "
                                     "written), so Keep VOD would leave nothing to play. Nothing was deleted; "
                                     "use Keep Downloaded or Keep Both.")
        # Delete the downloaded copy from the *arr that owns it: its files
        # through the file API, then the title. Never Radarr for a series:
        # TMDB movie and TV ids are separate number spaces, so a series' id
        # names an unrelated film in Radarr.
        _carry_user_data(dup, record, "vod", db)
        arr = _delete_downloaded_copy(dup, record, db)
        # The arr's delete webhooks ran inside that call (#515): they may have
        # released the row to VOD only or deleted it. Read it as it is now;
        # not expire_all(), which would drop _resolve's merged dup.sources.
        record = db.query(model).filter(model.tmdb_id == dup.tmdb_id).populate_existing().first()

        if is_series:
            # The show left Sonarr: the VOD row goes back to VOD only and the
            # request goes, as on SeriesDelete, whose webhook may never come
            # or come late (#617, the films' #549).
            if record is not None and record.source != "sonarr" and (
                    record.sonarr_path or record.downloaded_at is not None):
                from services.sonarr import release_vod_series_download
                release_vod_series_download(db, record, drop_duplicate=False)
                db.query(DownloadRequest).filter(DownloadRequest.tmdb_id == dup.tmdb_id,
                                                 DownloadRequest.media_type == "series").delete()
            elif record is not None and record.sonarr_path:
                record.sonarr_path = None
        elif record is not None and record.source != "radarr":
            # The film left Radarr: the row goes back to VOD only and the
            # request goes, as on MovieDelete. Done here, not left to Radarr's
            # delete webhook, which may never come or come late (#549).
            from services.radarr import release_vod_download
            release_vod_download(db, record, drop_duplicate=False)
            db.query(DownloadRequest).filter(DownloadRequest.tmdb_id == dup.tmdb_id,
                                             DownloadRequest.media_type == "movie").delete()

        # Legacy state: a radarr-only row (shouldn't exist alongside VOD due to
        # the unique constraint, but clean up if the row itself is radarr-owned)
        radarr_movie = record if not is_series and record is not None and record.source == "radarr" else None
        if radarr_movie:
            db.delete(radarr_movie)
            logger.info(f"Removed Radarr DB record for tmdb:{dup.tmdb_id}")
        log_deletion(db, kind="duplicate-resolve", name=title or f"tmdb:{dup.tmdb_id}",
                     media_type=dup.media_type, reason=reason,
                     detail=f"Kept VOD copy — downloaded files deleted from {arr}" + (f" ({why})" if why else ""))

    db.commit()
    # Both copies in one folder: the saved watched state goes to the item
    # Jellyfin makes for the kept copy once it has seen the delete (#333).
    watch_pending_user_data(db, dup)


def _carry_user_data(dup: Duplicate, record, keep: str, db: Session) -> None:
    """Users' watched state on the removed copy goes to the kept one first;
    when that can't be done, nothing is deleted."""
    try:
        carry_user_data(db, dup, record, keep)
    except UserDataCarryError as e:
        db.rollback()
        raise HTTPException(e.status, e.message)


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
    # A show Sonarr holds only some episodes of (Download More for one
    # season) is a duplicate too; Keep Downloaded deletes every .strm and the
    # VOD sync never brings them back, so the episodes only VOD has would be
    # gone for good (#800).
    only_vod = vod_only_episodes(sonarr, show.get("id"), dup)
    if only_vod is None:
        raise HTTPException(502, "Couldn't read Sonarr's episode files. Nothing was deleted; try again.")
    if only_vod:
        n = len(only_vod)
        raise HTTPException(409, f"{n} episode{'s' if n != 1 else ''} of this show exist{'' if n != 1 else 's'} "
                                 f"only as VOD streams (first: {only_vod[0]}); Keep Downloaded would delete "
                                 f"{'them' if n != 1 else 'it'} for good. Nothing was deleted; use Keep Both, "
                                 "or download the rest first.")


def _require_movie_download(dup: Duplicate, db: Session) -> None:
    """Refuse Keep Downloaded for a film Radarr holds no downloaded file of,
    as _require_series_download does for shows. Keep Downloaded deletes the
    VOD copy; without a download that deletes the film. It happens after a
    Keep VOD that failed half-way (Radarr deleted the file, then removing the
    title failed), when the file was deleted in Radarr, and for a duplicate
    between two providers (Resolve All sends Keep Downloaded for every one).
    Radarr not configured refuses too, as Sonarr not configured does for shows."""
    if not any((s.get("source") or "") == "radarr" for s in dup.sources or []):
        raise HTTPException(409, "Both copies of this film are VOD streams: there is no download to keep. "
                                 "Nothing was deleted; use Keep Both to dismiss it.")
    url, key = get_setting(db, "radarr_url"), get_setting(db, "radarr_api_key")
    if not url or not key:
        raise HTTPException(409, "Radarr isn't configured, so Tentacle can't check that this film was "
                                 "downloaded. Nothing was deleted.")
    from services.radarr import RadarrService
    radarr = RadarrService(url, key)
    movie = radarr.get_movie_by_tmdb(dup.tmdb_id)
    if not movie:
        raise HTTPException(409, "Radarr doesn't list this film (or couldn't be read), so there is no download "
                                 "to keep. Nothing was deleted.")
    try:
        files = radarr.get_movie_files(movie["id"])
    except Exception as e:
        logger.error(f"Keep Downloaded: could not read Radarr's files for tmdb:{dup.tmdb_id}: {e}")
        raise HTTPException(502, "Couldn't read Radarr's files for this film. Nothing was deleted; try again.")
    if not any(is_downloaded_file(f.get("path")) for f in files or []):
        raise HTTPException(409, "Nothing downloaded for this film: Radarr has no file for it. "
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


Resolution = Literal["keep_radarr", "keep_vod", "keep_both"]


class ResolveRequest(BaseModel):
    resolution: Resolution  # anything else (or "pending", which reopened it) is a 422


class ResolveAllRequest(BaseModel):
    resolution: Resolution


def _current_resolution(db: Session, dup_id: int):
    """The duplicate's resolution as committed now (None when it is gone),
    not the session's copy, which may predate another request's commit."""
    return db.query(Duplicate.resolution).filter(Duplicate.id == dup_id).scalar()


def _pending_twins(dup: Duplicate, db: Session) -> list:
    """The title's other pending rows. Nothing makes (tmdb_id, media_type)
    unique: two scans at once can each write one (#504)."""
    return db.query(Duplicate).filter(
        Duplicate.id != dup.id,
        Duplicate.tmdb_id == dup.tmdb_id,
        Duplicate.media_type == dup.media_type,
        Duplicate.resolution == "pending",
    ).all()


def _resolve(dup: Duplicate, resolution: str, db: Session, reason: str = "manual", why: str = None) -> None:
    """Resolve a pending duplicate and its pending twins together, under
    _resolve_lock: a twin left pending could later be resolved the other way
    and delete the copy this resolution kept (#504). The twins' sources are
    merged in first, so a copy only a twin knew about is acted on and checked
    too. On a refusal nothing is marked resolved."""
    twins = _pending_twins(dup, db)
    if twins:
        merged = list(dup.sources or [])
        seen = {(s.get("source"), s.get("path")) for s in merged}
        for twin in twins:
            for s in twin.sources or []:
                if (s.get("source"), s.get("path")) not in seen:
                    seen.add((s.get("source"), s.get("path")))
                    merged.append(s)
        dup.sources = merged  # a new list, so the JSON column is saved

    # The arr's delete webhooks, sent inside Keep VOD's delete calls, leave
    # the title's duplicates to this resolution meanwhile (#515). Should it
    # fail after the arr deleted the download (#506), the duplicate stays
    # pending: a retry of Keep VOD finishes it, or Keep Both dismisses it.
    with resolving(dup.media_type, dup.tmdb_id):
        _apply_resolution(dup, resolution, db, reason=reason, why=why)

        # Mark as resolved (keep in DB for stats/history)
        now = datetime.now(timezone.utc)
        for d in (dup, *twins):
            d.resolution = resolution
            d.resolved_at = now
            d.resolved_by = "rule" if reason != "manual" else None
            d.resolved_why = why
        db.commit()


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
            "resolved_by": d.resolved_by,
            "resolved_why": d.resolved_why,
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
    with _resolve_lock:
        current = _current_resolution(db, dup_id)
        if current is None:
            raise HTTPException(404, "Duplicate not found")
        if current != "pending":
            raise HTTPException(409, f"This duplicate was already resolved ({_LABELS.get(current, current)}), "
                                     "in another tab or by Resolve All. Nothing was deleted.")
        dup = db.query(Duplicate).filter(Duplicate.id == dup_id).first()
        _resolve(dup, body.resolution, db)

    return {"success": True}


@router.post("/resolve-all")
def resolve_all(body: ResolveAllRequest, db: Session = Depends(get_db)):
    pending = db.query(Duplicate).filter(Duplicate.resolution == "pending").all()
    total = len(pending)

    # Apply resolution to each duplicate (delete files, clean up DB). Only mark a
    # duplicate resolved if its resolution actually succeeded — failed ones stay
    # pending so they can be retried instead of being silently dropped.
    # A duplicate resolved since the list was read (another tab, another
    # Resolve All) is skipped, never resolved again.
    resolved = 0
    failed = 0
    skipped = 0
    for dup_id in [d.id for d in pending]:
        with _resolve_lock:
            if _current_resolution(db, dup_id) != "pending":
                skipped += 1
                continue
            dup = db.query(Duplicate).filter(Duplicate.id == dup_id).first()
            tmdb_id = dup.tmdb_id   # read now: the log line must not fail on a stale dup (#515)
            try:
                # Commits each one: a later failure rolls the session back, which
                # would turn this one (one copy already deleted) back into
                # "pending", and resolving it the other way would then delete the
                # copy that is left. Its twins are marked with it, then skipped.
                _resolve(dup, body.resolution, db)
            except Exception as e:
                logger.error(f"Failed to apply resolution for tmdb:{tmdb_id}: {e}")
                failed += 1
                continue
            resolved += 1
    db.commit()

    return {"success": failed == 0, "count": resolved, "total": total, "failed": failed, "skipped": skipped}


# ── The automatic rule (services/duplicates_auto.py, #872) ──────────────────

class RuleSettings(BaseModel):
    mode: Literal["off", "download", "stream"]
    cap: int = 10
    protected_indexers: str = ""
    qbittorrent_url: str = ""
    qbittorrent_username: str = ""
    qbittorrent_password: Optional[str] = None   # None or the masked form: unchanged
    qbittorrent_tag: str = ""


class RuleRun(BaseModel):
    confirm: Optional[int] = None   # the number of changes the admin was shown (over the cap)


class PreferDownload(BaseModel):
    media_type: Literal["movie", "series"]
    tmdb_id: int
    prefer: bool


@router.get("/rule")
def get_rule(db: Session = Depends(get_db)):
    from services import duplicates_auto as rule
    from services.secret_mask import mask
    m = rule.mode(db)
    return {
        "mode": m,
        "cap": rule.cap(db),
        "protected_indexers": get_setting(db, rule.SETTING_INDEXERS, "") or "",
        "qbittorrent_url": get_setting(db, "qbittorrent_url", "") or "",
        "qbittorrent_username": get_setting(db, "qbittorrent_username", "") or "",
        "qbittorrent_password": mask(get_setting(db, "qbittorrent_password", "") or "", whole=True),
        "qbittorrent_tag": get_setting(db, rule.SETTING_QBIT_TAG, "") or "",
        "every_minutes": rule.RUN_MINUTES,
        "recycle_bins": rule.recycle_bins(db) if m == "stream" else {},
        "last": rule.last_run(db),
    }


@router.put("/rule")
def put_rule(body: RuleSettings, db: Session = Depends(get_db)):
    from services import duplicates_auto as rule
    from services.secret_mask import is_shown_form
    if body.cap < 0 or body.cap > 10000:
        raise HTTPException(400, "The cap is a number of titles from 0 to 10000.")
    url = body.qbittorrent_url.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "qBittorrent's address starts with http:// or https://")
    before = rule.mode(db)
    for key, value in ((rule.SETTING_MODE, body.mode), (rule.SETTING_CAP, str(body.cap)),
                       (rule.SETTING_INDEXERS, body.protected_indexers.strip()),
                       ("qbittorrent_url", url), ("qbittorrent_username", body.qbittorrent_username.strip()),
                       (rule.SETTING_QBIT_TAG, body.qbittorrent_tag.strip())):
        set_setting(db, key, value)
    stored = get_setting(db, "qbittorrent_password", "") or ""
    if body.qbittorrent_password is not None and not is_shown_form(body.qbittorrent_password, stored):
        set_setting(db, "qbittorrent_password", body.qbittorrent_password)
    if before != body.mode:
        logger.info(f"Duplicates rule: {before} -> {body.mode}")
    return get_rule(db)


@router.get("/rule/preview")
def preview_rule(mode: Optional[str] = None, db: Session = Depends(get_db)):
    """What the rule would do now in `mode` (download / stream): read only."""
    from services import duplicates_auto as rule
    return rule.preview(db, mode)


@router.post("/rule/run")
def run_rule(body: RuleRun, db: Session = Depends(get_db)):
    """Run the rule now in its saved mode; `confirm` applies a held run."""
    from services import duplicates_auto as rule
    out = rule.run(db, confirm=body.confirm)
    if out.get("state") == "off":
        raise HTTPException(409, "The automatic rule is off. Nothing was changed.")
    if out.get("state") == "busy":
        raise HTTPException(409, "The rule is running already. Nothing else was changed.")
    return out


@router.get("/prefer-download")
def list_prefer_download(db: Session = Depends(get_db)):
    rows = [{"media_type": "movie", "tmdb_id": m.tmdb_id, "title": m.title}
            for m in db.query(Movie).filter(Movie.prefer_download.is_(True)).all()]
    rows += [{"media_type": "series", "tmdb_id": s.tmdb_id, "title": s.title}
             for s in db.query(Series).filter(Series.prefer_download.is_(True)).all()]
    return sorted(rows, key=lambda r: (r["title"] or "").casefold())


@router.put("/prefer-download")
def set_prefer_download(body: PreferDownload, db: Session = Depends(get_db)):
    model = Movie if body.media_type == "movie" else Series
    row = db.query(model).filter(model.tmdb_id == body.tmdb_id).first()
    if row is None:
        raise HTTPException(404, "That title isn't in the library.")
    row.prefer_download = body.prefer
    db.commit()
    return {"success": True, "prefer": body.prefer}

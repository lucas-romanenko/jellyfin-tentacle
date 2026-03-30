"""
Tentacle - Sync Router
API endpoints for triggering syncs and viewing history
"""

import json
import queue
import asyncio
import threading
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from models.database import get_db, Provider, SyncRun, Movie, Series, Duplicate, ActivityLog, get_setting, set_setting, log_activity
from services.sync import sync_provider
from services.tagger import refresh_recently_added_tags
from routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sync", tags=["sync"], dependencies=[Depends(require_admin)])

# Track running syncs and their progress
_running_syncs: dict = {}
_sync_progress: dict = {}  # provider_id -> {phase, category, stats}
_sync_subscribers: dict = {}  # provider_id -> [queue.Queue]
_cancel_flags: dict = {}  # provider_id -> threading.Event (set = cancelled)
_sync_lock = threading.Lock()


class SyncRequest(BaseModel):
    provider_id: int
    sync_type: str = "full"  # full | movies | series


def _notify_sync_progress(provider_id: int, phase: str, category: str, stats: dict):
    """Progress callback — updates state and notifies SSE subscribers"""
    progress = {"phase": phase, "category": category, "stats": stats}
    _sync_progress[provider_id] = progress

    with _sync_lock:
        for q in _sync_subscribers.get(provider_id, []):
            try:
                q.put_nowait(progress)
            except queue.Full:
                pass


def _run_sync_background(provider_id: int, sync_type: str):
    """Run sync in background thread. Always runs the full Jellyfin pipeline after VOD sync."""
    from models.database import SessionLocal
    cancel_event = threading.Event()
    _cancel_flags[provider_id] = cancel_event
    db = SessionLocal()
    # Cumulative stats across phases
    cumulative = {"movies_new": 0, "movies_existing": 0, "series_new": 0, "series_existing": 0}
    try:
        provider = db.query(Provider).filter(Provider.id == provider_id).first()
        if not provider:
            return

        def progress_cb(phase, category, stats, item_title=None, item_pos=None, item_total=None):
            if phase == "movies":
                cumulative["movies_new"] = stats.get("new", 0)
                cumulative["movies_existing"] = stats.get("existing", 0)
            elif phase == "series":
                cumulative["series_new"] = stats.get("new", 0)
                cumulative["series_existing"] = stats.get("existing", 0)
            progress = dict(cumulative)
            if item_title:
                progress["item_title"] = item_title
            if item_pos and item_total:
                progress["item_pos"] = item_pos
                progress["item_total"] = item_total
            _notify_sync_progress(provider_id, phase, category, progress)

        def cancel_check():
            return cancel_event.is_set()

        run = sync_provider(provider, sync_type, db, progress_callback=progress_cb, cancel_check=cancel_check)

        cumulative["movies_new"] = run.movies_new or 0
        cumulative["movies_existing"] = run.movies_existing or 0
        cumulative["series_new"] = run.series_new or 0
        cumulative["series_existing"] = run.series_existing or 0

        # Activity log for VOD sync
        if run.status == "completed":
            mn = run.movies_new or 0
            me = run.movies_existing or 0
            ms = run.movies_skipped or 0
            sn = run.series_new or 0
            se = run.series_existing or 0
            ss = run.series_skipped or 0
            total_scanned = mn + me + ms + sn + se + ss
            total_matched = mn + me + sn + se
            total_new = mn + sn
            total_skipped = ms + ss

            parts = []
            if total_new:
                sub = []
                if mn: sub.append(f"{mn} movie{'s' if mn != 1 else ''}")
                if sn: sub.append(f"{sn} series")
                parts.append(f"{', '.join(sub)} added")
            if total_skipped:
                parts.append(f"{total_skipped} skipped (no TMDB match)")

            if parts:
                msg = f"VOD sync — {total_scanned} streams scanned, {', '.join(parts)}"
            elif total_scanned:
                msg = f"VOD sync — {total_scanned} streams scanned, {total_matched} already in library, no new content"
            else:
                msg = "VOD sync completed — no streams found in enabled categories"
            log_activity(db, "vod_sync", msg)

        # Full pipeline: Jellyfin library scan → wait for indexing → push tags → refresh playlists
        if run.status == "completed" and not cancel_event.is_set():
            _notify_sync_progress(provider_id, "jellyfin_scan", "Scanning Jellyfin library...", cumulative)
            try:
                from services.jellyfin import run_full_jellyfin_pipeline
                pipeline_stats = run_full_jellyfin_pipeline(db, log_prefix="VOD sync")
                logger.info(f"[VOD sync] Pipeline complete: {pipeline_stats}")
            except Exception as e:
                logger.error(f"[VOD sync] Jellyfin pipeline failed: {e}")

            # Notify about new auto playlists available
            try:
                from routers.smartlists import _compute_auto_playlists
                auto_pls = _compute_auto_playlists(db)
                new_available = [p for p in auto_pls if not p["enabled"] and p["category"] == "source"]
                if new_available:
                    names = [p["name"] for p in new_available[:3]]
                    suffix = f" and {len(new_available) - 3} more" if len(new_available) > 3 else ""
                    log_activity(db, "new_playlists",
                                 f"{len(new_available)} new playlist{'s' if len(new_available) != 1 else ''} available: {', '.join(names)}{suffix}")
            except Exception as e:
                logger.debug(f"[VOD sync] Auto playlist check failed: {e}")

        phase = "complete" if run.status == "completed" else "cancelled" if run.status == "cancelled" else "error"
        _notify_sync_progress(provider_id, phase, "", {
            **cumulative,
            "duration_seconds": run.duration_seconds,
        })
        logger.info(f"Background sync {run.status}: run #{run.id}")
    except Exception as e:
        logger.error(f"Background sync failed: {e}", exc_info=True)
        _notify_sync_progress(provider_id, "error", "", {"error": str(e)})
    finally:
        _running_syncs.pop(provider_id, None)
        _cancel_flags.pop(provider_id, None)
        db.close()
        # Keep progress around for 10s so the frontend can poll the final state
        def _cleanup_progress():
            import time
            time.sleep(10)
            _sync_progress.pop(provider_id, None)
        threading.Thread(target=_cleanup_progress, daemon=True).start()


@router.post("/trigger")
def trigger_sync(body: SyncRequest, db: Session = Depends(get_db)):
    """Trigger a sync for a provider"""
    provider = db.query(Provider).filter(Provider.id == body.provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")

    if not provider.active:
        raise HTTPException(400, "Provider is not active")

    # Check both in-memory and DB for running sync
    if body.provider_id in _running_syncs:
        raise HTTPException(400, "A sync is already running for this provider")
    db_running = db.query(SyncRun).filter(
        SyncRun.provider_id == body.provider_id,
        SyncRun.status == "running"
    ).first()
    if db_running:
        raise HTTPException(400, "A sync is already running for this provider")

    # TMDB always available (built-in token as fallback)

    _running_syncs[body.provider_id] = True

    thread = threading.Thread(
        target=_run_sync_background,
        args=(body.provider_id, body.sync_type),
        daemon=True
    )
    thread.start()

    return {"success": True, "message": f"Sync started for {provider.name}"}


@router.post("/cancel")
def cancel_sync(body: SyncRequest, db: Session = Depends(get_db)):
    """Cancel a running sync for a provider"""
    # Check DB for running sync (source of truth)
    db_run = db.query(SyncRun).filter(
        SyncRun.provider_id == body.provider_id,
        SyncRun.status == "running"
    ).first()
    if not db_run:
        raise HTTPException(400, "No sync is running for this provider")

    cancel_event = _cancel_flags.get(body.provider_id)
    if cancel_event:
        cancel_event.set()
        logger.info(f"Cancel requested for provider {body.provider_id}")
        return {"success": True, "message": "Cancel signal sent"}

    # No cancel flag but DB shows running — force-fail it (orphaned from restart)
    logger.warning(f"No cancel flag for provider {body.provider_id}, force-failing orphaned run #{db_run.id}")
    db_run.status = "cancelled"
    db_run.error_message = "Cancelled by user (no active sync thread)"
    db_run.completed_at = datetime.utcnow()
    if db_run.started_at:
        db_run.duration_seconds = int((db_run.completed_at - db_run.started_at).total_seconds())
    db.commit()
    return {"success": True, "message": "Orphaned sync cancelled"}


@router.get("/status")
def get_sync_status(db: Session = Depends(get_db)):
    """Get status of all running and recent syncs"""
    # Check DB for running syncs (works regardless of how sync was started)
    running_runs = db.query(SyncRun).filter(
        SyncRun.status == "running"
    ).order_by(SyncRun.started_at.desc()).all()

    # Auto-fail syncs stuck longer than 4 hours
    stuck_cutoff = datetime.utcnow() - timedelta(hours=4)
    actually_running = []
    for run in running_runs:
        if run.started_at and run.started_at < stuck_cutoff:
            logger.warning(f"Auto-failing stuck sync run #{run.id} (started {run.started_at})")
            run.status = "failed"
            run.error_message = "Automatically failed: exceeded 4 hour timeout"
            run.completed_at = datetime.utcnow()
            run.duration_seconds = int((run.completed_at - run.started_at).total_seconds())
        else:
            actually_running.append(run)
    if len(actually_running) != len(running_runs):
        db.commit()

    running = []
    for run in actually_running:
        provider = db.query(Provider).filter(Provider.id == run.provider_id).first()
        running.append({
            "provider_id": run.provider_id,
            "provider_name": provider.name if provider else "Unknown",
            "run_id": run.id,
            "sync_type": run.sync_type,
            "started_at": run.started_at,
            "movies_new": run.movies_new,
            "series_new": run.series_new,
            "cancellable": run.provider_id in _cancel_flags,
        })

    # Last completed run per provider
    recent = []
    providers = db.query(Provider).filter(Provider.active == True).all()
    for p in providers:
        last_run = db.query(SyncRun).filter(
            SyncRun.provider_id == p.id,
            SyncRun.status == "completed"
        ).order_by(SyncRun.completed_at.desc()).first()

        if last_run:
            recent.append({
                "provider_id": p.id,
                "provider_name": p.name,
                "run_id": last_run.id,
                "sync_type": last_run.sync_type,
                "completed_at": last_run.completed_at,
                "duration_seconds": last_run.duration_seconds,
                "movies_new": last_run.movies_new,
                "movies_existing": last_run.movies_existing,
                "series_new": last_run.series_new,
                "series_existing": last_run.series_existing,
            })

    # Get the very last run status (any status) for cancel detection
    last_run_any = db.query(SyncRun).order_by(SyncRun.id.desc()).first()
    last_status = last_run_any.status if last_run_any else None

    return {
        "running": running,
        "recent": recent,
        "is_running": len(running) > 0,
        "last_status": last_status,
    }


@router.get("/history")
def get_sync_history(
    provider_id: Optional[int] = None,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Get full sync history"""
    query = db.query(SyncRun)
    if provider_id:
        query = query.filter(SyncRun.provider_id == provider_id)

    total = query.count()
    runs = query.order_by(SyncRun.started_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "runs": [
            {
                "id": r.id,
                "provider_id": r.provider_id,
                "provider_name": r.provider.name if r.provider else "Unknown",
                "status": r.status,
                "sync_type": r.sync_type,
                "movies_new": r.movies_new,
                "movies_existing": r.movies_existing,
                "movies_failed": r.movies_failed,
                "movies_skipped": r.movies_skipped,
                "series_new": r.series_new,
                "series_existing": r.series_existing,
                "series_failed": r.series_failed,
                "series_skipped": r.series_skipped,
                "new_movies": r.new_movies or [],
                "new_series": r.new_series or [],
                "category_stats": r.category_stats or {},
                "error_message": r.error_message,
                "started_at": r.started_at,
                "completed_at": r.completed_at,
                "duration_seconds": r.duration_seconds,
            }
            for r in runs
        ]
    }


@router.get("/feed")
def get_new_additions_feed(
    limit: int = 20,
    db: Session = Depends(get_db)
):
    """Get recent additions feed from last sync run"""
    last_run = db.query(SyncRun).filter(
        SyncRun.status == "completed"
    ).order_by(SyncRun.completed_at.desc()).first()

    if not last_run:
        return {"items": [], "last_sync": None}

    items = []
    for m in (last_run.new_movies or [])[:limit // 2]:
        items.append({**m, "type": "movie"})
    for s in (last_run.new_series or [])[:limit // 2]:
        items.append({**s, "type": "series"})

    # Sort by added_at
    items.sort(key=lambda x: x.get("added_at", ""), reverse=True)

    return {
        "items": items[:limit],
        "last_sync": last_run.completed_at,
        "last_run_id": last_run.id,
    }


@router.get("/stats")
def get_library_stats(db: Session = Depends(get_db)):
    """Overall library statistics"""
    total_movies = db.query(Movie).count()
    total_series = db.query(Series).count()
    downloaded_movies = db.query(Movie).filter(Movie.source == "radarr").count()
    vod_movies = db.query(Movie).filter(Movie.source.like("provider_%")).count()
    pending_duplicates = db.query(Duplicate).filter(Duplicate.resolution == "pending").count()
    active_providers = db.query(Provider).filter(Provider.active == True).count()

    # Per-source breakdown
    source_stats = {}
    movies = db.query(Movie.source_tag, Movie.tmdb_id).all()
    for tag, _ in movies:
        if tag:
            source_stats[tag] = source_stats.get(tag, 0) + 1

    return {
        "total_movies": total_movies,
        "total_series": total_series,
        "downloaded_movies": downloaded_movies,
        "vod_movies": vod_movies,
        "pending_duplicates": pending_duplicates,
        "active_providers": active_providers,
        "source_breakdown": source_stats,
    }


@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    """Dashboard overview — status cards, recent downloads, library stats"""

    # --- Status timestamps ---
    last_vod_sync = None
    last_vod_status = "never"
    last_vod_run = db.query(SyncRun).filter(
        SyncRun.status.in_(["completed", "failed"])
    ).order_by(SyncRun.completed_at.desc()).first()
    if last_vod_run:
        last_vod_sync = last_vod_run.completed_at.isoformat() if last_vod_run.completed_at else None
        last_vod_status = last_vod_run.status
        last_vod_new = (last_vod_run.movies_new or 0) + (last_vod_run.series_new or 0)
    else:
        last_vod_new = 0

    last_radarr_scan = get_setting(db, "last_radarr_scan", "")
    last_sonarr_scan = get_setting(db, "last_sonarr_scan", "")
    last_jellyfin_push = get_setting(db, "last_jellyfin_push", "")

    # --- Library counts ---
    total_movies = db.query(Movie).count()
    total_series = db.query(Series).count()
    radarr_movies = db.query(Movie).filter(Movie.source == "radarr").count()
    sonarr_series = db.query(Series).filter(Series.source == "sonarr").count()
    vod_movies = db.query(Movie).filter(Movie.source.like("provider_%")).count()
    vod_series = db.query(Series).filter(Series.source.like("provider_%")).count()
    pending_duplicates = db.query(Duplicate).filter(Duplicate.resolution == "pending").count()

    # --- Recent downloads (Radarr + Sonarr, last 10) ---
    recent_radarr = db.query(Movie).filter(
        Movie.source == "radarr"
    ).order_by(Movie.date_added.desc()).limit(10).all()

    recent_sonarr = db.query(Series).filter(
        Series.source == "sonarr"
    ).order_by(Series.date_added.desc()).limit(10).all()

    recent_downloads = []
    for m in recent_radarr:
        recent_downloads.append({
            "title": m.title, "year": m.year, "type": "movie",
            "poster": m.poster_path, "date_added": m.date_added.isoformat() if m.date_added else None,
            "source": "radarr",
        })
    for s in recent_sonarr:
        recent_downloads.append({
            "title": s.title, "year": s.year, "type": "series",
            "poster": s.poster_path, "date_added": s.date_added.isoformat() if s.date_added else None,
            "source": "sonarr",
        })
    recent_downloads.sort(key=lambda x: x.get("date_added") or "", reverse=True)
    recent_downloads = recent_downloads[:10]

    # --- Running state ---
    is_syncing = len(_running_syncs) > 0
    radarr_scanning = False
    sonarr_scanning = False
    try:
        from routers.radarr import _scan_running as radarr_running
        radarr_scanning = radarr_running
    except ImportError:
        pass
    try:
        from routers.sonarr import _scan_running as sonarr_running
        sonarr_scanning = sonarr_running
    except ImportError:
        pass

    # --- Config flags (what's configured) ---
    radarr_configured = bool(get_setting(db, "radarr_url") and get_setting(db, "radarr_api_key"))
    sonarr_configured = bool(get_setting(db, "sonarr_url") and get_setting(db, "sonarr_api_key"))
    has_providers = db.query(Provider).filter(Provider.active == True).count() > 0
    has_playlists = bool(get_setting(db, "last_jellyfin_push"))
    from services.smartlists import get_home_config
    home_config = get_home_config(db)
    has_home_screen = bool(home_config and home_config.get("rows"))

    return {
        "status": {
            "vod_sync": {"timestamp": last_vod_sync, "status": last_vod_status, "new_items": last_vod_new},
            "radarr_scan": {"timestamp": last_radarr_scan or None},
            "sonarr_scan": {"timestamp": last_sonarr_scan or None},
            "jellyfin_push": {"timestamp": last_jellyfin_push or None},
        },
        "library": {
            "total_movies": total_movies, "total_series": total_series,
            "radarr_movies": radarr_movies, "sonarr_series": sonarr_series,
            "vod_movies": vod_movies, "vod_series": vod_series,
            "pending_duplicates": pending_duplicates,
        },
        "recent_downloads": recent_downloads,
        "running": {
            "vod_sync": is_syncing,
            "radarr_scan": radarr_scanning,
            "sonarr_scan": sonarr_scanning,
        },
        "config": {
            "radarr": radarr_configured,
            "sonarr": sonarr_configured,
            "has_providers": has_providers,
            "has_playlists": has_playlists,
            "has_home_screen": has_home_screen,
        },
    }


@router.get("/activity")
def get_activity(limit: int = 15, db: Session = Depends(get_db)):
    """Recent activity feed for dashboard"""
    entries = db.query(ActivityLog).order_by(
        ActivityLog.created_at.desc()
    ).limit(limit).all()
    return [{
        "id": e.id,
        "event": e.event,
        "message": e.message,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    } for e in entries]


@router.post("/refresh-tags")
def refresh_tags(db: Session = Depends(get_db)):
    """Manually trigger tag refresh (recently added window) and push to Jellyfin"""
    m, s = refresh_recently_added_tags(db)

    # Push tags to Jellyfin for all radarr movies
    jf_tagged = 0
    jf_not_found = 0
    jf_errors = 0
    jellyfin_url = get_setting(db, "jellyfin_url")
    jellyfin_key = get_setting(db, "jellyfin_api_key")
    jellyfin_uid = get_setting(db, "jellyfin_user_id", "")
    if jellyfin_url and jellyfin_key:
        from services.jellyfin import JellyfinService
        jf = JellyfinService(jellyfin_url, jellyfin_key, jellyfin_uid)

        # Push tags for ALL movies (VOD + Radarr)
        all_movies = db.query(Movie).all()
        logger.info(f"[Refresh Tags] Pushing tags to Jellyfin for {len(all_movies)} movies")
        jf_movie_lookup, jf_movie_title_lookup = jf.get_tmdb_lookup_with_fallback("Movie")
        logger.info(f"[Refresh Tags] Movie lookup: {len(jf_movie_lookup)} by TMDB, {len(jf_movie_title_lookup)} by title")

        for movie in all_movies:
            if not movie.tags:
                continue
            try:
                jf_item = jf_movie_lookup.get(movie.tmdb_id)
                if not jf_item and movie.title:
                    norm_title = JellyfinService._normalize_title(movie.title)
                    jf_item = jf_movie_title_lookup.get((norm_title, str(movie.year or "")))
                if jf_item:
                    if jf.set_item_tags(jf_item["Id"], movie.tags):
                        jf_tagged += 1
                    else:
                        jf_errors += 1
                else:
                    jf_not_found += 1
            except Exception as e:
                jf_errors += 1
                logger.error(f"[Refresh Tags] Jellyfin tag sync failed for '{movie.title}': {e}")

        # Push tags for ALL series
        all_series = db.query(Series).all()
        logger.info(f"[Refresh Tags] Pushing tags to Jellyfin for {len(all_series)} series")
        jf_series_lookup, jf_series_title_lookup = jf.get_tmdb_lookup_with_fallback("Series")
        logger.info(f"[Refresh Tags] Series lookup: {len(jf_series_lookup)} by TMDB, {len(jf_series_title_lookup)} by title")

        for series in all_series:
            if not series.tags:
                continue
            try:
                jf_item = jf_series_lookup.get(series.tmdb_id)
                if not jf_item and series.title:
                    norm_title = JellyfinService._normalize_title(series.title)
                    jf_item = jf_series_title_lookup.get((norm_title, str(series.year or "")))
                if jf_item:
                    if jf.set_item_tags(jf_item["Id"], series.tags):
                        jf_tagged += 1
                    else:
                        jf_errors += 1
                else:
                    jf_not_found += 1
            except Exception as e:
                jf_errors += 1
                logger.error(f"[Refresh Tags] Jellyfin tag sync failed for '{series.title}': {e}")

        logger.info(f"[Refresh Tags] Jellyfin sync complete: {jf_tagged} tagged, {jf_not_found} not found, {jf_errors} errors")
    else:
        logger.info("[Refresh Tags] Jellyfin not configured — skipping tag push")

    # Rewrite NFOs for VOD content so Jellyfin picks up new tags
    from services.nfo import write_movie_nfo, write_series_nfo
    nfos_written = 0
    vod_movies = db.query(Movie).filter(Movie.source != "radarr", Movie.nfo_path.isnot(None)).all()
    for movie in vod_movies:
        try:
            metadata = {
                "tmdb_id": movie.tmdb_id, "title": movie.title, "year": movie.year,
                "overview": movie.overview, "runtime": movie.runtime, "rating": movie.rating,
                "genres": movie.genres or [], "poster_path": movie.poster_path,
                "backdrop_path": movie.backdrop_path,
            }
            write_movie_nfo(Path(movie.nfo_path), metadata, movie.tags or [])
            nfos_written += 1
        except Exception:
            pass
    vod_series = db.query(Series).filter(Series.source != "radarr", Series.nfo_path.isnot(None)).all()
    for series in vod_series:
        try:
            metadata = {
                "tmdb_id": series.tmdb_id, "title": series.title, "year": series.year,
                "overview": series.overview, "genres": series.genres or [],
                "poster_path": series.poster_path, "backdrop_path": series.backdrop_path,
                "status": getattr(series, "status", None),
            }
            write_series_nfo(Path(series.nfo_path), metadata, series.tags or [])
            nfos_written += 1
        except Exception:
            pass
    if nfos_written:
        logger.info(f"[Refresh Tags] Rewrote {nfos_written} NFO files")

    # Refresh playlist contents so new tags take effect (don't rebuild configs/home)
    try:
        from services.smartlists import refresh_smartlist_playlists
        refresh_smartlist_playlists(db)
        logger.info("[Refresh Tags] Playlist contents refreshed")
    except Exception as e:
        logger.warning(f"[Refresh Tags] Playlist refresh failed: {e}")

    # Track last Jellyfin push timestamp
    if jf_tagged > 0:
        set_setting(db, "last_jellyfin_push", datetime.utcnow().isoformat())
        log_activity(db, "jellyfin_push", f"Pushed tags to Jellyfin — {jf_tagged} items updated")

    return {"success": True, "updated_movies": m, "updated_series": s, "jellyfin_tagged": jf_tagged}


# ── Sync Progress ─────────────────────────────────────────────────────────

@router.get("/progress/{provider_id}/poll")
def poll_sync_progress(provider_id: int, db: Session = Depends(get_db)):
    """Simple JSON endpoint for current sync progress"""
    # Check in-memory first, fall back to DB
    running = provider_id in _running_syncs
    if not running:
        db_run = db.query(SyncRun).filter(
            SyncRun.provider_id == provider_id,
            SyncRun.status == "running"
        ).first()
        running = db_run is not None
    progress = _sync_progress.get(provider_id)
    if progress:
        return {"running": running, **progress}
    return {"running": running}


@router.get("/progress/{provider_id}")
async def stream_sync_progress(provider_id: int):
    """SSE endpoint for real-time sync progress"""
    if provider_id not in _running_syncs:
        return {"running": False}

    async def event_generator():
        q: queue.Queue = queue.Queue(maxsize=100)
        with _sync_lock:
            _sync_subscribers.setdefault(provider_id, []).append(q)

        loop = asyncio.get_event_loop()
        try:
            while provider_id in _running_syncs:
                try:
                    progress = await loop.run_in_executor(None, lambda: q.get(timeout=1.0))
                    yield f"data: {json.dumps(progress)}\n\n"
                    if progress.get("phase") in ("complete", "error"):
                        break
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _sync_lock:
                subs = _sync_subscribers.get(provider_id, [])
                if q in subs:
                    subs.remove(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

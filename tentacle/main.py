"""
Tentacle - Main Application
FastAPI app with all routers
"""

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os
import logging
from datetime import datetime

from models.database import create_tables, SessionLocal, seed_defaults, Setting, Provider, SyncRun
from routers import settings, providers, sync as sync_router, library, duplicates, lists as lists_router, widget, radarr as radarr_router, sonarr as sonarr_router, tags as tags_router, collections as collections_router, smartlists as smartlists_router, discover as discover_router, livetv as livetv_router, auth as auth_router
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = BackgroundScheduler()


def run_scheduled_sync():
    """Run sync for all active providers on schedule"""
    from services.sync import sync_provider
    from services.tagger import refresh_recently_added_tags
    from services.radarr import scan_radarr_library
    from services.tmdb import TMDBService
    from models.database import get_setting
    db = SessionLocal()
    try:
        from models.database import ListSubscription, TentacleUser
        from routers.lists import fetch_list_tmdb_ids, store_list_items, apply_list_tags_to_library, enrich_items_with_tmdb, _get_tmdb_service
        from services.tmdb import get_tmdb_token
        bearer = get_tmdb_token(db)
        trakt_cid = get_setting(db, "trakt_client_id") or ""
        tmdb = _get_tmdb_service(db)
        active_lists = db.query(ListSubscription).filter(ListSubscription.active == True).all()
        for lst in active_lists:
            try:
                items = fetch_list_tmdb_ids(lst, bearer_token=bearer, trakt_client_id=trakt_cid)
                if items:
                    if tmdb:
                        enrich_items_with_tmdb(items, tmdb)
                    store_list_items(lst, items, db)
                    apply_list_tags_to_library(items, lst.tag, db)
                    lst.last_fetched = datetime.utcnow()
                    lst.last_item_count = len(items)
                    logger.info(f"List '{lst.name}' refreshed: {len(items)} items")
            except Exception as e:
                logger.warning(f"Failed to refresh list '{lst.name}': {e}")
        db.commit()

        from routers.sync import _running_syncs, _cancel_flags, _notify_sync_progress, _sync_progress
        import threading
        active_providers = db.query(Provider).filter(Provider.active == True).all()
        for provider in active_providers:
            logger.info(f"Scheduled sync starting for {provider.name}")
            cancel_event = threading.Event()
            _running_syncs[provider.id] = True
            _cancel_flags[provider.id] = cancel_event

            def progress_cb(phase, category, stats):
                _notify_sync_progress(provider.id, phase, category, stats)

            def cancel_check():
                return cancel_event.is_set()

            try:
                run = sync_provider(provider, "full", db, progress_callback=progress_cb, cancel_check=cancel_check)
                phase = "complete" if run.status == "completed" else "cancelled" if run.status == "cancelled" else "error"
                _notify_sync_progress(provider.id, phase, "", {})
            finally:
                _running_syncs.pop(provider.id, None)
                _cancel_flags.pop(provider.id, None)
                _sync_progress.pop(provider.id, None)

        logger.info("Scheduled Radarr scan starting")
        scan_radarr_library(db)

        logger.info("Scheduled Sonarr scan starting")
        from services.sonarr import scan_sonarr_library
        scan_sonarr_library(db)

        logger.info("Refreshing recently added tags")
        refresh_recently_added_tags(db)

        # Run full Jellyfin pipeline: library scan → wait → push tags → refresh playlists → home config
        logger.info("Running Jellyfin pipeline (scan, tags, playlists)")
        from services.jellyfin import run_full_jellyfin_pipeline
        pipeline_stats = run_full_jellyfin_pipeline(db, log_prefix="Nightly sync")
        logger.info(f"Jellyfin pipeline complete: {pipeline_stats}")

        bearer = get_tmdb_token(db)
        data_dir = get_setting(db, "data_dir", "/data")
        if bearer:
            tmdb = TMDBService(bearer, data_dir)
            tmdb.cleanup_cache()

        logger.info("Syncing playlist artwork")
        from routers.collections import sync_playlist_artwork
        sync_playlist_artwork(db)

        # Rebuild smartlist configs (global) and per-user home layouts
        from services.smartlists import sync_smartlists, write_home_config, _notify_jellyfin_plugin
        # Disk configs + Jellyfin playlists are global — sync once
        sync_smartlists(db)
        # Home configs are per-user
        users = db.query(TentacleUser).all()
        for u in users:
            try:
                write_home_config(db, user_id=u.id)
            except Exception as e:
                logger.warning(f"Per-user home config failed for {u.display_name}: {e}")
        if users:
            _notify_jellyfin_plugin(db)

        logger.info("Scheduled sync complete")
    except Exception as e:
        logger.error(f"Scheduled sync failed: {e}")
    finally:
        db.close()


def setup_scheduler(db):
    """Setup cron scheduler from settings"""
    s = db.query(Setting).filter(Setting.key == "sync_schedule").first()
    cron = s.value if s and s.value else "0 3 * * *"

    try:
        parts = cron.strip().split()
        if len(parts) == 5:
            trigger = CronTrigger(
                minute=parts[0], hour=parts[1],
                day=parts[2], month=parts[3], day_of_week=parts[4]
            )
            scheduler.add_job(run_scheduled_sync, trigger, id="main_sync", replace_existing=True)
            logger.info(f"Sync scheduled: {cron}")
    except Exception as e:
        logger.warning(f"Could not parse cron schedule '{cron}': {e}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Tentacle starting up...")
    create_tables()
    db = SessionLocal()
    try:
        seed_defaults(db)

        stuck_runs = db.query(SyncRun).filter(SyncRun.status == "running").all()
        for run in stuck_runs:
            run.status = "failed"
            run.error_message = "Interrupted by container restart"
            run.completed_at = datetime.utcnow()
            if run.started_at:
                run.duration_seconds = int((run.completed_at - run.started_at).total_seconds())
        if stuck_runs:
            db.commit()
            logger.info(f"Cleaned up {len(stuck_runs)} stuck sync run(s) from previous restart")

        setup_scheduler(db)

        # Validate Jellyfin connection
        from models.database import get_setting
        jf_url = get_setting(db, "jellyfin_url")
        jf_key = get_setting(db, "jellyfin_api_key")
        jf_uid = get_setting(db, "jellyfin_user_id", "")
        if jf_url and jf_key:
            from services.jellyfin import JellyfinService
            jf = JellyfinService(jf_url, jf_key, jf_uid)
            if not jf.test_connection():
                logger.warning("[Startup] Jellyfin API key is invalid — tagging via API will fail. Update in Settings → Connections.")
            else:
                logger.info("[Startup] Jellyfin connection OK")
    finally:
        db.close()

    from services.logstream import setup_sse_logging
    setup_sse_logging()

    scheduler.start()
    logger.info("Tentacle ready")
    yield
    scheduler.shutdown(wait=False)
    logger.info("Tentacle shutting down")


app = FastAPI(
    title="Tentacle",
    description="Unified media library manager",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(settings.router)
app.include_router(providers.router)
app.include_router(sync_router.router)
app.include_router(library.router)
app.include_router(duplicates.router)
app.include_router(lists_router.router)
app.include_router(widget.router)
app.include_router(radarr_router.router)
app.include_router(sonarr_router.router)
app.include_router(tags_router.router)
app.include_router(collections_router.router)
app.include_router(smartlists_router.router)
app.include_router(discover_router.router)
app.include_router(livetv_router.router)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    return FileResponse(
        "static/index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)}
    )
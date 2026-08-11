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

from models.database import create_tables, SessionLocal, seed_defaults, Setting, Provider, SyncRun, Movie, Series
from routers import settings, providers, sync as sync_router, library, duplicates, lists as lists_router, widget, radarr as radarr_router, sonarr as sonarr_router, tags as tags_router, collections as collections_router, smartlists as smartlists_router, discover as discover_router, livetv as livetv_router, auth as auth_router, activity as activity_router, notifications as notifications_router, health as health_router
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

scheduler = BackgroundScheduler()


def run_scheduled_sync():
    """Run sync for all active providers on schedule"""
    from services.sync import sync_provider
    from services.tagger import refresh_recently_added_tags
    from services.radarr import scan_radarr_library
    from services.tmdb import TMDBService
    from models.database import get_setting, log_activity
    db = SessionLocal()
    try:
        from models.database import ListSubscription
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
                    store_stats = store_list_items(lst, items, db)
                    apply_list_tags_to_library(items, lst.tag, db)
                    lst.last_fetched = datetime.utcnow()
                    lst.last_item_count = len(items)
                    stored = store_stats.get("stored", len(items)) if store_stats else len(items)
                    new_count = store_stats.get("new", 0) if store_stats else 0
                    removed_count = store_stats.get("removed", 0) if store_stats else 0
                    if new_count or removed_count:
                        log_activity(db, "list_fetch", f"Fetched '{lst.name}' — {stored} items (+{new_count} new, -{removed_count} removed)")
                    else:
                        log_activity(db, "list_fetch", f"Fetched '{lst.name}' — {stored} items (no changes)")
                    logger.info(f"List '{lst.name}' refreshed: {len(items)} items")
            except Exception as e:
                logger.warning(f"Failed to refresh list '{lst.name}': {e}")
        db.commit()

        from routers.sync import _running_syncs, _cancel_flags, _notify_sync_progress, _sync_progress, _sync_lock
        import threading
        active_providers = db.query(Provider).filter(Provider.active == True).all()
        for provider in active_providers:
            # Respect the same running-guard the manual sync endpoint uses, so the
            # nightly run never starts a second concurrent sync for a provider a
            # user (or a previous nightly job) is already syncing. Check-and-set
            # atomically under _sync_lock to avoid a TOCTOU race.
            with _sync_lock:
                if provider.id in _running_syncs:
                    logger.info(f"Scheduled sync skipping {provider.name} — sync already running")
                    continue
                _running_syncs[provider.id] = True
            db_running = db.query(SyncRun).filter(
                SyncRun.provider_id == provider.id,
                SyncRun.status == "running",
            ).first()
            if db_running:
                logger.info(f"Scheduled sync skipping {provider.name} — sync already running (DB)")
                _running_syncs.pop(provider.id, None)
                continue

            logger.info(f"Scheduled sync starting for {provider.name}")
            cancel_event = threading.Event()
            _cancel_flags[provider.id] = cancel_event

            def progress_cb(phase, category, stats, _pid=provider.id, **kwargs):
                _notify_sync_progress(_pid, phase, category, stats)

            def cancel_check(_ev=cancel_event):
                return _ev.is_set()

            try:
                run = sync_provider(provider, "full", db, progress_callback=progress_cb, cancel_check=cancel_check)
                phase = "complete" if run.status == "completed" else "cancelled" if run.status == "cancelled" else "error"
                _notify_sync_progress(provider.id, phase, "", {})
            except Exception as e:
                logger.error(f"Scheduled sync failed for {provider.name}: {e}")
                log_activity(db, "sync", f"Scheduled sync failed for {provider.name}: {e}")
            finally:
                _running_syncs.pop(provider.id, None)
                _cancel_flags.pop(provider.id, None)
                _sync_progress.pop(provider.id, None)

        logger.info("Scheduled Radarr scan starting")
        try:
            radarr_stats = scan_radarr_library(db)
            new_count = radarr_stats.get("new", 0) if radarr_stats else 0
            if new_count:
                log_activity(db, "radarr_scan", f"Radarr scan — {new_count} new movie(s) imported")
            logger.info(f"Radarr scan complete: {radarr_stats}")
        except Exception as e:
            logger.error(f"Radarr scan failed: {e}")
            log_activity(db, "radarr_scan", f"Radarr scan failed: {e}")

        logger.info("Scheduled Sonarr scan starting")
        try:
            from services.sonarr import scan_sonarr_library
            sonarr_stats = scan_sonarr_library(db)
            new_count = sonarr_stats.get("new", 0) if sonarr_stats else 0
            if new_count:
                log_activity(db, "sonarr_scan", f"Sonarr scan — {new_count} new series imported")
            logger.info(f"Sonarr scan complete: {sonarr_stats}")
        except Exception as e:
            logger.error(f"Sonarr scan failed: {e}")
            log_activity(db, "sonarr_scan", f"Sonarr scan failed: {e}")

        logger.info("Refreshing recently added tags")
        try:
            refresh_recently_added_tags(db)
        except Exception as e:
            logger.error(f"Tag refresh failed: {e}")

        # Run full Jellyfin pipeline: library scan → wait → push tags → refresh playlists → home config
        logger.info("Running Jellyfin pipeline (scan, tags, playlists)")
        try:
            from services.jellyfin import run_full_jellyfin_pipeline
            pipeline_stats = run_full_jellyfin_pipeline(db, log_prefix="Nightly sync")
            tags_pushed = pipeline_stats.get("tags_pushed", 0)
            if tags_pushed:
                log_activity(db, "jellyfin_push", f"Jellyfin pipeline — {tags_pushed} tag(s) pushed, playlists refreshed")
            logger.info(f"Jellyfin pipeline complete: {pipeline_stats}")
        except Exception as e:
            logger.error(f"Jellyfin pipeline failed: {e}")
            log_activity(db, "jellyfin_push", f"Jellyfin pipeline failed: {e}")

        try:
            bearer = get_tmdb_token(db)
            data_dir = get_setting(db, "data_dir", "/data")
            if bearer:
                tmdb = TMDBService(bearer, data_dir)
                tmdb.cleanup_cache()
        except Exception as e:
            logger.error(f"TMDB cache cleanup failed: {e}")

        # Discover new provider content: VOD categories + Live TV groups.
        # New entries are stored disabled/non-whitelisted and the user gets a
        # persistent dashboard notice + activity feed events.
        logger.info("Discovering new provider categories and Live TV groups")
        try:
            from services.discovery import discover_new_provider_content
            discovered = discover_new_provider_content(db)
            if discovered["vod_new"] or discovered["live_new"]:
                logger.info(f"Discovery: {len(discovered['vod_new'])} new VOD categories, {len(discovered['live_new'])} new Live TV groups")
        except Exception as e:
            logger.error(f"Provider content discovery failed: {e}")

        # EPG sync for Live TV providers + Jellyfin guide refresh
        logger.info("Syncing Live TV EPG data")
        try:
            from models.database import LiveChannel
            from routers.livetv import _run_epg_sync_background
            live_providers = db.query(Provider).filter(Provider.live_tv_enabled == True, Provider.active == True).all()
            epg_synced = False
            for lp in live_providers:
                all_channels = db.query(LiveChannel).filter(LiveChannel.provider_id == lp.id).all()
                if not all_channels:
                    continue
                enabled_count = sum(1 for ch in all_channels if ch.enabled)
                provider_data = {
                    "id": lp.id,
                    "provider_type": lp.provider_type or "xtream",
                    "server_url": lp.server_url,
                    "username": lp.username,
                    "password": lp.password,
                    "user_agent": lp.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
                    "epg_url": lp.epg_url,
                    "channels": [
                        {"stream_id": ch.stream_id, "epg_channel_id": ch.epg_channel_id, "name": ch.name}
                        for ch in all_channels
                    ],
                    "enabled_count": enabled_count,
                }
                logger.info(f"EPG sync for '{lp.name}': {len(all_channels)} channels ({enabled_count} enabled)")
                # Only trigger the Jellyfin guide refresh if the sync actually
                # produced data — refreshing after a failed sync makes Jellyfin
                # re-ingest a draining/stale guide for nothing.
                if _run_epg_sync_background(provider_data):
                    epg_synced = True

            # Trigger Jellyfin guide refresh so it picks up new EPG data
            if epg_synced:
                try:
                    import requests as req
                    jf_url = get_setting(db, "jellyfin_url")
                    jf_key = get_setting(db, "jellyfin_api_key")
                    if jf_url and jf_key:
                        headers = {"X-Emby-Token": jf_key}
                        tasks = req.get(f"{jf_url}/ScheduledTasks", headers=headers, timeout=10).json()
                        guide_task = next((t for t in tasks if t.get("Key") == "RefreshGuide"), None)
                        if guide_task:
                            req.post(f"{jf_url}/ScheduledTasks/Running/{guide_task['Id']}", headers=headers, timeout=10)
                            logger.info("Jellyfin guide refresh triggered after EPG sync")
                except Exception as e:
                    logger.error(f"Jellyfin guide refresh failed: {e}")
        except Exception as e:
            logger.error(f"EPG sync failed: {e}")

        # Sweep orphaned downloads (radarr/sonarr records no longer in Jellyfin)
        logger.info("Sweeping orphaned downloads")
        try:
            from services.jellyfin import sweep_orphaned_downloads
            orphans = sweep_orphaned_downloads(db)
            if orphans:
                from models.database import log_activity
                log_activity(db, "orphan_sweep", f"Removed {orphans} orphaned download(s) from DB")
        except Exception as e:
            logger.error(f"Orphan sweep failed: {e}")

        # Sweep VOD DB records whose .strm files no longer exist on disk
        logger.info("Sweeping orphaned VOD records")
        try:
            from pathlib import Path as _Path
            vod_orphans = 0
            swept_titles = []
            for m in db.query(Movie).filter(Movie.source.like("provider_%"), Movie.strm_path.isnot(None)).all():
                if not _Path(m.strm_path).exists():
                    logger.info(f"[VOD sweep] Removing orphaned movie: {m.title} (missing: {m.strm_path})")
                    swept_titles.append(m.title)
                    db.delete(m)
                    vod_orphans += 1
            for s in db.query(Series).filter(Series.source.like("provider_%"), Series.strm_path.isnot(None)).all():
                if not _Path(s.strm_path).exists():
                    logger.info(f"[VOD sweep] Removing orphaned series: {s.title} (missing: {s.strm_path})")
                    swept_titles.append(s.title)
                    db.delete(s)
                    vod_orphans += 1
            if vod_orphans:
                db.commit()
                log_activity(db, "vod_sweep", f"Removed {vod_orphans} orphaned VOD record(s) with missing files")
                from models.database import log_deletion
                log_deletion(db, kind="vod-sweep", name=f"{vod_orphans} VOD record(s)", reason="auto",
                             detail="DB records removed — .strm files missing on disk: " + ", ".join(swept_titles[:20])
                                    + ("…" if len(swept_titles) > 20 else ""))
                logger.info(f"VOD sweep: removed {vod_orphans} orphaned record(s)")
        except Exception as e:
            logger.error(f"VOD sweep failed: {e}")

        # Per-user: sync smartlist configs + write home configs
        logger.info("Syncing per-user playlists and home configs")
        try:
            from models.database import TentacleUser
            from services.smartlists import sync_smartlists, write_home_config, migrate_global_smartlists_to_user, refresh_smartlist_playlists
            users = db.query(TentacleUser).all()
            for user in users:
                try:
                    migrate_global_smartlists_to_user(db, user.id)
                    sync_smartlists(db, user_id=user.id)
                    stats = refresh_smartlist_playlists(db, user_id=user.id) or {}
                    write_home_config(db, user_id=user.id)
                    logger.info(
                        f"[Nightly] Playlists rebuilt for user {user.id}: "
                        f"{stats.get('processed', 0)} processed, {stats.get('created', 0)} created, "
                        f"{stats.get('updated', 0)} updated, {stats.get('errors', 0)} errors"
                    )
                    counts = stats.get("item_counts") or {}
                    if counts:
                        detail = ", ".join(f"{n}={c}" for n, c in sorted(counts.items()))
                        logger.info(f"[Nightly] Playlist item counts (user {user.id}): {detail}")
                    if stats.get("errors"):
                        logger.warning(
                            f"[Nightly] {stats['errors']} playlist(s) failed for user {user.id} "
                            f"— see [SmartLists] lines above"
                        )
                except Exception as e:
                    logger.error(f"Per-user sync failed for user {user.id}: {e}")
            # Notify plugin to clear caches once at end
            try:
                from services.smartlists import _notify_jellyfin_plugin
                _notify_jellyfin_plugin(db)
            except Exception:
                pass
            logger.info(f"Per-user sync complete for {len(users)} user(s)")
        except Exception as e:
            logger.error(f"Per-user sync failed: {e}")

        logger.info("Syncing playlist artwork")
        try:
            from routers.collections import sync_playlist_artwork
            sync_playlist_artwork(db)
        except Exception as e:
            logger.error(f"Artwork sync failed: {e}")

        logger.info("Scheduled sync complete")
    except Exception as e:
        logger.error(f"Scheduled sync failed: {e}", exc_info=True)
        # Surface the failure in the activity feed so it isn't silently swallowed.
        try:
            log_activity(db, "sync", f"Scheduled nightly sync failed: {e}")
        except Exception:
            pass
    finally:
        db.close()


def run_native_playlist_refresh():
    """Periodic: re-query only native (genre/rating/year) playlists so they pick up
    items whose Jellyfin metadata arrived after the last full refresh — without waiting
    for the 3am sync. Reuses the incremental, add-before-remove refresh path (no-op when
    unchanged); tag-based playlists are excluded to avoid the tag-indexing race. Notifies
    both clients (web version + Android WebSocket) only when something actually changed."""
    db = SessionLocal()
    try:
        from services.smartlists import refresh_native_playlists, _notify_jellyfin_plugin
        result = refresh_native_playlists(db)
        if result.get("updated", 0) or result.get("created", 0):
            _notify_jellyfin_plugin(db)
            logger.info(f"[Native refresh] genre/rating playlists updated: {result}")
    except Exception as e:
        logger.warning(f"[Native refresh] failed: {e}")
    finally:
        db.close()


def reschedule_main_sync(cron: str = None) -> bool:
    """(Re)schedule the daily sync from a 5-field cron string. Reads the
    sync_schedule setting when cron is None. Safe to call at runtime — the job is
    replaced in place, so schedule changes take effect without a restart."""
    if cron is None:
        db = SessionLocal()
        try:
            s = db.query(Setting).filter(Setting.key == "sync_schedule").first()
            cron = s.value if s and s.value else "0 3 * * *"
        finally:
            db.close()
    parts = (cron or "").strip().split()
    if len(parts) != 5:
        logger.warning(f"Invalid sync schedule '{cron}' — expected 5 cron fields")
        return False
    try:
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1],
            day=parts[2], month=parts[3], day_of_week=parts[4]
        )
        scheduler.add_job(run_scheduled_sync, trigger, id="main_sync", replace_existing=True)
        logger.info(f"Sync scheduled: {cron}")
        return True
    except Exception as e:
        logger.warning(f"Could not schedule sync '{cron}': {e}")
        return False


def get_schedule_info() -> dict:
    """Current sync schedule as a friendly time + the effective timezone + next run."""
    from datetime import datetime
    db = SessionLocal()
    try:
        s = db.query(Setting).filter(Setting.key == "sync_schedule").first()
        cron = s.value if s and s.value else "0 3 * * *"
    finally:
        db.close()
    parts = cron.strip().split()
    time_str = "03:00"
    if len(parts) >= 2:
        try:
            time_str = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        except Exception:
            pass
    tz = scheduler.timezone
    try:
        tz_abbr = datetime.now(tz).strftime("%Z")
    except Exception:
        tz_abbr = ""
    next_run_human = None
    try:
        job = scheduler.get_job("main_sync")
        if job and job.next_run_time:
            next_run_human = job.next_run_time.strftime("%a %b %-d, %-I:%M %p %Z")
    except Exception:
        pass
    return {
        "cron": cron,
        "time": time_str,
        "timezone": str(tz),
        "timezone_abbr": tz_abbr,
        "next_run_human": next_run_human,
    }


def setup_scheduler(db):
    """Setup cron scheduler from settings"""
    reschedule_main_sync()

    # Keep native (genre/rating/year) playlists current between nightly syncs, so a
    # genre row fills in within minutes of its content getting Jellyfin metadata
    # instead of waiting until 3am. Cheap: incremental + no-op when nothing changed.
    scheduler.add_job(
        run_native_playlist_refresh,
        IntervalTrigger(minutes=15),
        id="native_playlist_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Native playlist refresh scheduled: every 15 min")

    # Download health: advance stall tracking every 5 min; auto-fix/auto-import
    # only act when their toggles are enabled (Health page, default off).
    from services.download_health import run_download_health_check
    scheduler.add_job(
        run_download_health_check,
        IntervalTrigger(minutes=5),
        id="download_health",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Download health check scheduled: every 5 min")

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

        # One-time migration: move global smartlists to per-user directories
        from models.database import get_setting, TentacleUser as _TU
        from services.smartlists import migrate_global_smartlists_to_user
        for _u in db.query(_TU).all():
            try:
                migrate_global_smartlists_to_user(db, _u.id)
            except Exception as e:
                logger.warning(f"SmartLists migration failed for {_u.display_name}: {e}")

        # Validate Jellyfin connection
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

def _build_cors_origins() -> list[str]:
    """Allowlist of browser origins permitted to call the API.

    Restricts CORS from the previous wildcard to: the configured Jellyfin origin
    (the web plugin runs inside Jellyfin's page), the dashboard's own external
    host if set, and localhost for local dev. The Jellyfin plugin and Android TV
    app call the API server-to-server (no browser Origin / CORS preflight), so
    they are unaffected. allow_credentials stays False, so this is purely about
    which web origins' JS may read responses.
    """
    from urllib.parse import urlparse
    origins: set[str] = {
        "http://localhost:8888",
        "http://127.0.0.1:8888",
    }
    try:
        db = SessionLocal()
        try:
            from models.database import get_setting
            for key in ("jellyfin_url", "external_url", "hdhr_base_url"):
                val = (get_setting(db, key, "") or "").strip().rstrip("/")
                if not val:
                    continue
                p = urlparse(val)
                if p.scheme and p.netloc:
                    origins.add(f"{p.scheme}://{p.netloc}")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Could not build CORS allowlist from settings: {e}")
    return sorted(origins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(settings.router)
app.include_router(settings.plugin_router)
app.include_router(providers.router)
app.include_router(sync_router.router)
app.include_router(library.router)
app.include_router(duplicates.router)
app.include_router(lists_router.router)
app.include_router(widget.router)
app.include_router(radarr_router.router)
app.include_router(radarr_router.webhook_router)
app.include_router(sonarr_router.router)
app.include_router(sonarr_router.webhook_router)
app.include_router(tags_router.router)
app.include_router(collections_router.router)
app.include_router(smartlists_router.router)
app.include_router(discover_router.router)
app.include_router(activity_router.router)
app.include_router(livetv_router.router)
app.include_router(notifications_router.router)
app.include_router(health_router.router)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/favicon.ico")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    # Served from root (not /static) so the service worker gets root scope and
    # can control SPA navigations. Service-Worker-Allowed pins the scope to "/".
    return FileResponse(
        "static/sw.js",
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


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
    return {"status": "ok"}


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
    # Log the full detail server-side, but return a generic message to the client
    # so internal paths, library/version info, or secrets in exception text aren't
    # leaked to callers.
    logger.error(f"Unhandled exception on {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )
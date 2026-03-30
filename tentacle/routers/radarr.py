"""
Tentacle - Radarr Router
Radarr library scanning, quality profiles, and provider migration
"""

import threading
import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from models.database import get_db, Provider, Movie, ListItem, ListSubscription, get_setting, log_activity
from services.radarr import scan_radarr_library, RadarrService
from services.nfo import update_nfo_tags, write_movie_nfo, make_folder_name
from services.migration import migrate_provider, preview_migration
from services.logstream import log_event_generator, get_recent_logs, emit_library_event

from routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/radarr", tags=["radarr"], dependencies=[Depends(require_admin)])

_scan_running = False


class MigrateRequest(BaseModel):
    from_provider_id: int
    to_provider_id: int
    dry_run: bool = False


def _run_scan_background():
    global _scan_running
    from models.database import SessionLocal, set_setting, log_activity
    from datetime import datetime
    db = SessionLocal()
    try:
        result = scan_radarr_library(db)
        set_setting(db, "last_radarr_scan", datetime.utcnow().isoformat())
        n = result.get("new", 0) if isinstance(result, dict) else 0
        msg = f"Radarr scan — {n} new movie{'s' if n != 1 else ''}" if n else "Radarr scan — no new movies"
        log_activity(db, "radarr_scan", msg)
    except Exception as e:
        logger.error(f"Background Radarr scan failed: {e}", exc_info=True)
    finally:
        _scan_running = False
        db.close()


@router.post("/scan")
def trigger_radarr_scan(db: Session = Depends(get_db)):
    """Scan Radarr library and apply Downloaded tags"""
    global _scan_running

    if _scan_running:
        raise HTTPException(400, "Radarr scan already running")

    radarr_url = get_setting(db, "radarr_url")
    if not radarr_url:
        raise HTTPException(400, "Radarr not configured in Settings")

    _scan_running = True
    thread = threading.Thread(target=_run_scan_background, daemon=True)
    thread.start()

    return {"success": True, "message": "Radarr scan started"}


@router.get("/scan/status")
def get_scan_status():
    return {"running": _scan_running}


_nfo_running = False


@router.post("/write-nfos")
def write_nfos(db: Session = Depends(get_db)):
    """Force rewrite NFO files for all Radarr movies in the DB"""
    global _nfo_running
    from pathlib import Path
    from services.tagger import apply_tag_rules, get_list_tags_for_tmdb_id

    if _nfo_running:
        raise HTTPException(400, "NFO write already running")

    _nfo_running = True
    try:
        radarr_movies_path = "/media/movies"
        logger.info(f"[NFO] Writing NFOs using base path: {radarr_movies_path}")

        movies = db.query(Movie).filter(Movie.source == "radarr").all()
        written = 0
        skipped = 0

        for db_movie in movies:
            try:
                folder_name = make_folder_name(db_movie.title, db_movie.year)
                movie_folder = Path(radarr_movies_path) / folder_name
                logger.debug(f"[NFO] {db_movie.title} → {movie_folder}")

                if not movie_folder.exists():
                    logger.debug(f"[NFO] Skipped {db_movie.title} — folder not found: {movie_folder}")
                    skipped += 1
                    continue

                # Build tag list
                tags = []
                if db_movie.source_tag:
                    tags.append(db_movie.source_tag)

                metadata = {
                    "genres": db_movie.genres or [],
                    "rating": db_movie.rating or 0,
                    "year": db_movie.year,
                    "runtime": db_movie.runtime or 0,
                }
                rule_tags = apply_tag_rules(metadata, "movie", "radarr", db_movie.source_tag, db)
                for rt in rule_tags:
                    if rt not in tags:
                        tags.append(rt)

                list_tags = get_list_tags_for_tmdb_id(db_movie.tmdb_id, "movie", db)
                for lt in list_tags:
                    if lt not in tags:
                        tags.append(lt)

                db_movie.tags = tags

                nfo_metadata = {
                    "title": db_movie.title,
                    "tmdb_id": db_movie.tmdb_id,
                    "year": db_movie.year,
                    "overview": db_movie.overview,
                    "runtime": db_movie.runtime,
                    "rating": db_movie.rating,
                    "genres": db_movie.genres or [],
                    "poster_path": db_movie.poster_path,
                    "backdrop_path": db_movie.backdrop_path,
                }

                # Match NFO filename to actual video file
                video_file = None
                for ext in ('.mkv', '.mp4', '.avi', '.m4v'):
                    files = list(movie_folder.glob(f'*{ext}'))
                    if files:
                        video_file = files[0]
                        break
                nfo_path = video_file.with_suffix('.nfo') if video_file else movie_folder / f"{folder_name}.nfo"
                if write_movie_nfo(nfo_path, nfo_metadata, tags):
                    db_movie.nfo_path = str(nfo_path)
                    written += 1
                else:
                    skipped += 1

            except Exception as e:
                logger.debug(f"NFO write failed for {db_movie.title}: {e}")
                skipped += 1

        db.commit()

        # Trigger Jellyfin scan
        if written > 0:
            jellyfin_url = get_setting(db, "jellyfin_url")
            jellyfin_key = get_setting(db, "jellyfin_api_key")
            jellyfin_uid = get_setting(db, "jellyfin_user_id", "")
            if jellyfin_url and jellyfin_key:
                try:
                    from services.jellyfin import JellyfinService
                    jf = JellyfinService(jellyfin_url, jellyfin_key, jellyfin_uid)
                    jf.trigger_library_scan()
                except Exception:
                    pass

        logger.info(f"NFO rewrite complete: {written} written, {skipped} skipped")
        return {"success": True, "written": written, "skipped": skipped}
    finally:
        _nfo_running = False


webhook_router = APIRouter(prefix="/api/radarr", tags=["radarr"])


@webhook_router.post("/webhook")
def radarr_webhook(payload: dict, db: Session = Depends(get_db)):
    """Radarr webhook — triggered on Download, MovieAdded, MovieDelete, MovieFileDelete events."""
    event_type = payload.get("eventType", "unknown")
    logger.info(f"[Radarr webhook] Received event: {event_type}")
    valid_events = ("Download", "MovieAdded", "MovieDelete", "MovieFileDelete", "Test")
    if event_type not in valid_events:
        return {"status": "ignored", "event": event_type}

    # Handle Radarr test ping
    if event_type == "Test":
        logger.info("[Radarr webhook] Test event received")
        return {"status": "ok"}

    movie_data = payload.get("movie", {})
    tmdb_id = movie_data.get("tmdbId")
    title = movie_data.get("title", "Unknown")
    logger.info(f"[Radarr webhook] {event_type} for '{title}' (tmdb:{tmdb_id})")

    if not tmdb_id:
        return {"status": "skipped", "reason": "no tmdbId"}

    # MovieFileDelete with upgrade reason — file is being replaced, ignore
    if event_type == "MovieFileDelete":
        delete_reason = payload.get("deleteReason", "")
        if delete_reason == "upgrade":
            logger.info(f"[Radarr webhook] MovieFileDelete upgrade for '{title}' — ignoring")
            return {"status": "ignored", "reason": "upgrade"}
        # Non-upgrade file deletion — remove from DB
        deleted = db.query(Movie).filter(Movie.tmdb_id == tmdb_id, Movie.source == "radarr").delete()
        db.commit()
        if deleted:
            emit_library_event("movie_removed", {"tmdb_id": tmdb_id, "title": title, "media_type": "movie"})
        logger.info(f"[Radarr webhook] MovieFileDelete for '{title}' (tmdb:{tmdb_id}) — removed {deleted} from DB")
        if deleted:
            log_activity(db, "radarr_remove", f"Removed '{title}' from Radarr library")
        return {"status": "deleted", "tmdb_id": tmdb_id}

    # MovieDelete — remove from DB
    if event_type == "MovieDelete":
        deleted = db.query(Movie).filter(Movie.tmdb_id == tmdb_id, Movie.source == "radarr").delete()
        db.commit()
        if deleted:
            emit_library_event("movie_removed", {"tmdb_id": tmdb_id, "title": title, "media_type": "movie"})
            log_activity(db, "radarr_remove", f"Removed '{title}' from Radarr library")
        logger.info(f"[Radarr webhook] MovieDelete for '{title}' (tmdb:{tmdb_id}) — removed {deleted} from DB")
        return {"status": "deleted", "tmdb_id": tmdb_id}

    # Download / MovieAdded — scan and tag
    def _webhook_background(tmdb_id, title):
        import time
        from models.database import SessionLocal, get_setting
        from pathlib import Path
        from services.jellyfin import JellyfinService
        db = SessionLocal()
        try:
            scan_radarr_library(db)

            db_movie = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
            if not db_movie:
                logger.warning(f"[Radarr webhook] Movie tmdb:{tmdb_id} not found after scan")
                return

            list_items = db.query(ListItem).filter(ListItem.tmdb_id == tmdb_id).all()
            if not list_items:
                logger.info(f"[Radarr webhook] '{title}' not in any lists")
            else:
                list_ids = [li.list_id for li in list_items]
                subscriptions = db.query(ListSubscription).filter(
                    ListSubscription.id.in_(list_ids),
                    ListSubscription.active == True
                ).all()

                tags = list(db_movie.tags or [])
                tagged_from = []
                for sub in subscriptions:
                    if sub.tag not in tags:
                        tags.append(sub.tag)
                        tagged_from.append(sub.name)

                if tagged_from:
                    db_movie.tags = tags
                    if db_movie.nfo_path:
                        update_nfo_tags(Path(db_movie.nfo_path), tags)
                    db.commit()
                    logger.info(f"[Radarr webhook] Tagged '{title}' with {tagged_from}")
                else:
                    logger.info(f"[Radarr webhook] '{title}' already has all list tags")

            # Push tags to Jellyfin via API.
            # scan_radarr_library() already triggered a library scan and pushed tags
            # for movies it found in Jellyfin. But for newly downloaded movies, Jellyfin
            # may not have indexed them yet. Wait for Jellyfin to scan, then retry.
            jf_url = get_setting(db, "jellyfin_url")
            jf_key = get_setting(db, "jellyfin_api_key")
            jf_uid = get_setting(db, "jellyfin_user_id", "")
            if jf_url and jf_key and db_movie.tags:
                jf = JellyfinService(jf_url, jf_key, jf_uid)
                movie_title = db_movie.title or title
                movie_year = str(db_movie.year or "")

                # Retry loop: wait for Jellyfin to index the new movie
                jf_item = None
                max_attempts = 5
                for attempt in range(max_attempts):
                    jf_item = jf.search_by_tmdb_id(
                        tmdb_id, "Movie", title=movie_title, year=movie_year
                    )
                    if jf_item:
                        break
                    if attempt < max_attempts - 1:
                        wait = 15 * (attempt + 1)  # 15s, 30s, 45s, 60s
                        logger.info(
                            f"[Radarr webhook] '{title}' not in Jellyfin yet, "
                            f"retrying in {wait}s (attempt {attempt + 1}/{max_attempts})"
                        )
                        time.sleep(wait)
                        # Re-trigger scan in case it finished before file was ready
                        if attempt == 1:
                            try:
                                jf.trigger_library_scan()
                            except Exception:
                                pass

                if jf_item:
                    # Merge with existing Jellyfin tags rather than replacing
                    existing_jf_tags = set(jf_item.get("Tags", []))
                    desired_tags = set(db_movie.tags)
                    merged = list(existing_jf_tags | desired_tags)
                    if jf.set_item_tags(jf_item["Id"], merged):
                        logger.info(f"[Radarr webhook] Pushed tags to Jellyfin for '{title}': {merged}")
                    else:
                        logger.warning(f"[Radarr webhook] Failed to set tags on '{title}' in Jellyfin")

                    # Refresh metadata so Jellyfin fetches posters/info from TMDB
                    if jf.refresh_item_metadata(jf_item["Id"]):
                        logger.info(f"[Radarr webhook] Triggered metadata refresh for '{title}'")
                else:
                    logger.warning(
                        f"[Radarr webhook] '{title}' (tmdb:{tmdb_id}) not found in Jellyfin "
                        f"after {max_attempts} attempts — tags will be pushed on next scheduled scan"
                    )

            # Refresh SmartList playlists so the movie appears immediately
            # in IMDB TOP 250, Downloaded Movies, etc.
            try:
                from services.smartlists import refresh_smartlist_playlists
                refresh_smartlist_playlists(db)
                logger.info(f"[Radarr webhook] Refreshed SmartList playlists after processing '{title}'")
            except Exception as e:
                logger.warning(f"[Radarr webhook] Playlist refresh failed: {e}")

            # Activity log
            from models.database import log_activity as _log_act
            _log_act(db, "radarr_add", f"Radarr downloaded '{db_movie.title}'")

            # Emit library event for the newly processed movie
            emit_library_event("movie_added", {
                "tmdb_id": tmdb_id,
                "title": db_movie.title,
                "year": db_movie.year,
                "poster_path": db_movie.poster_path,
                "source": db_movie.source,
                "source_tag": db_movie.source_tag,
                "tags": list(db_movie.tags or []),
                "media_type": "movie",
                "in_library": True,
            })
        except Exception as e:
            logger.error(f"[Radarr webhook] Background processing failed: {e}", exc_info=True)
        finally:
            db.close()

    thread = threading.Thread(target=_webhook_background, args=(tmdb_id, title), daemon=True)
    thread.start()

    return {"status": "processing", "event": event_type, "tmdb_id": tmdb_id}


@router.get("/rootfolders")
def get_root_folders(db: Session = Depends(get_db)):
    """Get Radarr root folders"""
    import requests
    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")
    if not radarr_url or not radarr_key:
        raise HTTPException(400, "Radarr not configured")
    r = requests.get(
        f"{radarr_url.rstrip('/')}/api/v3/rootfolder",
        headers={"X-Api-Key": radarr_key},
        timeout=10,
    )
    r.raise_for_status()
    return [{"path": f["path"], "freeSpace": f["freeSpace"]} for f in r.json()]


@router.get("/quality-profiles")
def get_quality_profiles(db: Session = Depends(get_db)):
    """Get Radarr quality profiles for settings"""
    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")

    if not radarr_url or not radarr_key:
        raise HTTPException(400, "Radarr not configured")

    radarr = RadarrService(radarr_url, radarr_key)
    profiles = radarr.get_quality_profiles()
    return [{"id": p["id"], "name": p["name"]} for p in profiles]


@router.get("/migration/preview")
def preview_migration_endpoint(
    from_id: int,
    to_id: int,
    db: Session = Depends(get_db)
):
    """Preview a provider migration without making changes"""
    from_provider = db.query(Provider).filter(Provider.id == from_id).first()
    to_provider = db.query(Provider).filter(Provider.id == to_id).first()

    if not from_provider or not to_provider:
        raise HTTPException(404, "Provider not found")

    return preview_migration(from_provider, to_provider, db)


@router.post("/migration/run")
def run_migration(body: MigrateRequest, db: Session = Depends(get_db)):
    """Migrate content from one provider to another"""
    result = migrate_provider(
        body.from_provider_id,
        body.to_provider_id,
        db,
        dry_run=body.dry_run
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return {"success": True, **result}


# ── SSE Log Stream ────────────────────────────────────────────────────────

@router.get("/logs/stream")
async def stream_logs(last_id: int = 0):
    """SSE endpoint for real-time log streaming"""
    return StreamingResponse(
        log_event_generator(last_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


@router.get("/logs/recent")
def get_recent_log_entries(limit: int = 200):
    """Get recent log entries (non-streaming)"""
    return {"logs": get_recent_logs(limit)}

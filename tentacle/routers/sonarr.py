"""
Tentacle - Sonarr Router
Sonarr library scanning, webhooks, and NFO management
"""

import threading
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models.database import get_db, Series, ListItem, ListSubscription, get_setting, log_activity
from services.sonarr import scan_sonarr_library, SonarrService
from services.nfo import update_nfo_tags, write_series_nfo
from services.logstream import emit_library_event

from routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sonarr", tags=["sonarr"], dependencies=[Depends(require_admin)])

_scan_running = False


def _run_scan_background():
    global _scan_running
    from models.database import SessionLocal, set_setting, log_activity
    from datetime import datetime
    db = SessionLocal()
    try:
        result = scan_sonarr_library(db)
        set_setting(db, "last_sonarr_scan", datetime.utcnow().isoformat())
        n = result.get("new", 0) if isinstance(result, dict) else 0
        msg = f"Sonarr scan — {n} new series" if n else "Sonarr scan — no new series"
        log_activity(db, "sonarr_scan", msg)
    except Exception as e:
        logger.error(f"Background Sonarr scan failed: {e}", exc_info=True)
    finally:
        _scan_running = False
        db.close()


@router.post("/scan")
def trigger_sonarr_scan(db: Session = Depends(get_db)):
    """Scan Sonarr library and apply Downloaded TV tags"""
    global _scan_running

    if _scan_running:
        raise HTTPException(400, "Sonarr scan already running")

    sonarr_url = get_setting(db, "sonarr_url")
    if not sonarr_url:
        raise HTTPException(400, "Sonarr not configured in Settings")

    _scan_running = True
    thread = threading.Thread(target=_run_scan_background, daemon=True)
    thread.start()

    return {"success": True, "message": "Sonarr scan started"}


@router.get("/scan/status")
def get_scan_status():
    return {"running": _scan_running}


@router.post("/write-nfos")
def write_nfos(db: Session = Depends(get_db)):
    """Force rewrite NFO files for all Sonarr series in the DB"""
    from pathlib import Path
    from services.tagger import apply_tag_rules, get_list_tags_for_tmdb_id

    sonarr_series_path = "/media/shows"
    logger.info(f"[NFO] Writing series NFOs using base path: {sonarr_series_path}")

    all_series = db.query(Series).filter(Series.source == "sonarr").all()
    written = 0
    skipped = 0

    for db_series in all_series:
        try:
            # Use sonarr_path if available, otherwise construct from base path
            if db_series.sonarr_path:
                series_folder = Path(db_series.sonarr_path)
            else:
                from services.nfo import make_folder_name
                folder_name = make_folder_name(db_series.title, db_series.year)
                series_folder = Path(sonarr_series_path) / folder_name

            if not series_folder.exists():
                skipped += 1
                continue

            # Build tag list
            tags = []
            if db_series.source_tag:
                tags.append(db_series.source_tag)

            metadata = {
                "genres": db_series.genres or [],
                "rating": db_series.rating or 0,
                "year": db_series.year,
                "runtime": 0,
            }
            rule_tags = apply_tag_rules(metadata, "series", "sonarr", db_series.source_tag, db)
            for rt in rule_tags:
                if rt not in tags:
                    tags.append(rt)

            list_tags = get_list_tags_for_tmdb_id(db_series.tmdb_id, "series", db)
            for lt in list_tags:
                if lt not in tags:
                    tags.append(lt)

            db_series.tags = tags

            nfo_metadata = {
                "title": db_series.title,
                "tmdb_id": db_series.tmdb_id,
                "year": db_series.year,
                "overview": db_series.overview,
                "rating": db_series.rating,
                "genres": db_series.genres or [],
                "poster_path": db_series.poster_path,
                "backdrop_path": db_series.backdrop_path,
                "status": db_series.status,
            }

            nfo_path = series_folder / "tvshow.nfo"
            if write_series_nfo(nfo_path, nfo_metadata, tags):
                db_series.nfo_path = str(nfo_path)
                written += 1
            else:
                skipped += 1

        except Exception as e:
            logger.debug(f"NFO write failed for {db_series.title}: {e}")
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

    logger.info(f"Series NFO rewrite complete: {written} written, {skipped} skipped")
    return {"success": True, "written": written, "skipped": skipped}


webhook_router = APIRouter(prefix="/api/sonarr", tags=["sonarr"])


@webhook_router.post("/webhook")
def sonarr_webhook(payload: dict, db: Session = Depends(get_db)):
    """Sonarr webhook — triggered on Download, SeriesAdd, SeriesDelete, EpisodeFileDelete events."""
    event_type = payload.get("eventType", "unknown")
    logger.info(f"[Sonarr webhook] Received event: {event_type}")
    valid_events = ("Download", "SeriesAdd", "SeriesDelete", "EpisodeFileDelete", "Test")
    if event_type not in valid_events:
        return {"status": "ignored", "event": event_type}

    # Handle Sonarr test ping
    if event_type == "Test":
        logger.info("[Sonarr webhook] Test event received")
        return {"status": "ok"}

    series_data = payload.get("series", {})
    tmdb_id = series_data.get("tmdbId") or 0
    title = series_data.get("title", "Unknown")
    logger.info(f"[Sonarr webhook] {event_type} for '{title}' (tmdb:{tmdb_id})")

    # EpisodeFileDelete with upgrade reason — file is being replaced, ignore
    if event_type == "EpisodeFileDelete":
        delete_reason = payload.get("deleteReason", "")
        if delete_reason == "upgrade":
            logger.info(f"[Sonarr webhook] EpisodeFileDelete upgrade for '{title}' — ignoring")
            return {"status": "ignored", "reason": "upgrade"}
        # Non-upgrade file deletion — just log, don't remove series
        # (series may still have other episodes)
        logger.info(f"[Sonarr webhook] EpisodeFileDelete for '{title}' — will re-scan")

    # SeriesDelete — remove from DB or clear sonarr state
    if event_type == "SeriesDelete":
        if tmdb_id:
            # Clear following state on any matching series (hybrid VOD+Sonarr)
            hybrid = db.query(Series).filter(Series.tmdb_id == tmdb_id, Series.source != "sonarr").first()
            if hybrid:
                hybrid.sonarr_monitored = False
                hybrid.sonarr_path = None
            deleted = db.query(Series).filter(Series.tmdb_id == tmdb_id, Series.source == "sonarr").delete()
            db.commit()
            if deleted:
                emit_library_event("series_removed", {"tmdb_id": tmdb_id, "title": title, "media_type": "series"})
                log_activity(db, "sonarr_remove", f"Removed '{title}' from Sonarr library")
            logger.info(f"[Sonarr webhook] SeriesDelete for '{title}' (tmdb:{tmdb_id}) — removed {deleted} from DB")
        return {"status": "deleted", "tmdb_id": tmdb_id}

    # Download / SeriesAdd / EpisodeFileDelete — scan and tag
    def _webhook_background(tmdb_id, title):
        import time
        from models.database import SessionLocal, get_setting
        from services.jellyfin import JellyfinService
        db = SessionLocal()
        try:
            scan_sonarr_library(db)

            if not tmdb_id:
                logger.warning(f"[Sonarr webhook] No TMDB ID for '{title}' — scan completed but cannot tag")
                return

            db_series = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
            if not db_series:
                logger.warning(f"[Sonarr webhook] Series tmdb:{tmdb_id} not found after scan")
                return

            list_items = db.query(ListItem).filter(ListItem.tmdb_id == tmdb_id).all()
            if not list_items:
                logger.info(f"[Sonarr webhook] '{title}' not in any lists")
            else:
                list_ids = [li.list_id for li in list_items]
                subscriptions = db.query(ListSubscription).filter(
                    ListSubscription.id.in_(list_ids),
                    ListSubscription.active == True
                ).all()

                tags = list(db_series.tags or [])
                tagged_from = []
                for sub in subscriptions:
                    if sub.tag not in tags:
                        tags.append(sub.tag)
                        tagged_from.append(sub.name)

                if tagged_from:
                    db_series.tags = tags
                    if db_series.nfo_path:
                        from pathlib import Path
                        update_nfo_tags(Path(db_series.nfo_path), tags)
                    db.commit()
                    logger.info(f"[Sonarr webhook] Tagged '{title}' with {tagged_from}")
                else:
                    logger.info(f"[Sonarr webhook] '{title}' already has all list tags")

            # Push tags to Jellyfin via API
            jf_url = get_setting(db, "jellyfin_url")
            jf_key = get_setting(db, "jellyfin_api_key")
            jf_uid = get_setting(db, "jellyfin_user_id", "")
            if jf_url and jf_key and db_series.tags:
                jf = JellyfinService(jf_url, jf_key, jf_uid)
                series_title = db_series.title or title
                series_year = str(db_series.year or "")

                # Retry loop: wait for Jellyfin to index the new series/episode
                jf_item = None
                max_attempts = 5
                for attempt in range(max_attempts):
                    jf_item = jf.search_by_tmdb_id(
                        tmdb_id, "Series", title=series_title, year=series_year
                    )
                    if jf_item:
                        break
                    if attempt < max_attempts - 1:
                        wait = 15 * (attempt + 1)
                        logger.info(
                            f"[Sonarr webhook] '{title}' not in Jellyfin yet, "
                            f"retrying in {wait}s (attempt {attempt + 1}/{max_attempts})"
                        )
                        time.sleep(wait)
                        if attempt == 1:
                            try:
                                jf.trigger_library_scan()
                            except Exception:
                                pass

                if jf_item:
                    existing_jf_tags = set(jf_item.get("Tags", []))
                    desired_tags = set(db_series.tags)
                    merged = list(existing_jf_tags | desired_tags)
                    if jf.set_item_tags(jf_item["Id"], merged):
                        logger.info(f"[Sonarr webhook] Pushed tags to Jellyfin for '{title}': {merged}")
                    else:
                        logger.warning(f"[Sonarr webhook] Failed to set tags on '{title}' in Jellyfin")

                    if jf.refresh_item_metadata(jf_item["Id"]):
                        logger.info(f"[Sonarr webhook] Triggered metadata refresh for '{title}'")
                else:
                    logger.warning(
                        f"[Sonarr webhook] '{title}' (tmdb:{tmdb_id}) not found in Jellyfin "
                        f"after {max_attempts} attempts — tags will be pushed on next scheduled scan"
                    )

            # Refresh SmartList playlists
            try:
                from services.smartlists import refresh_smartlist_playlists
                refresh_smartlist_playlists(db)
                logger.info(f"[Sonarr webhook] Refreshed SmartList playlists after processing '{title}'")
            except Exception as e:
                logger.warning(f"[Sonarr webhook] Playlist refresh failed: {e}")

            # Activity log
            from models.database import log_activity as _log_act
            _log_act(db, "sonarr_add", f"Sonarr downloaded '{db_series.title}'")

            emit_library_event("series_added", {
                "tmdb_id": tmdb_id,
                "title": db_series.title,
                "year": db_series.year,
                "poster_path": db_series.poster_path,
                "source": db_series.source,
                "source_tag": db_series.source_tag,
                "tags": list(db_series.tags or []),
                "media_type": "series",
                "in_library": True,
            })
        except Exception as e:
            logger.error(f"[Sonarr webhook] Background processing failed: {e}", exc_info=True)
        finally:
            db.close()

    thread = threading.Thread(target=_webhook_background, args=(tmdb_id, title), daemon=True)
    thread.start()

    return {"status": "processing", "event": event_type, "tmdb_id": tmdb_id}


@router.get("/rootfolders")
def get_root_folders(db: Session = Depends(get_db)):
    """Get Sonarr root folders"""
    import requests
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        raise HTTPException(400, "Sonarr not configured")
    r = requests.get(
        f"{sonarr_url.rstrip('/')}/api/v3/rootfolder",
        headers={"X-Api-Key": sonarr_key},
        timeout=10,
    )
    r.raise_for_status()
    return [{"path": f["path"], "freeSpace": f["freeSpace"]} for f in r.json()]


@router.get("/quality-profiles")
def get_quality_profiles(db: Session = Depends(get_db)):
    """Get Sonarr quality profiles"""
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")

    if not sonarr_url or not sonarr_key:
        raise HTTPException(400, "Sonarr not configured")

    sonarr = SonarrService(sonarr_url, sonarr_key)
    profiles = sonarr.get_quality_profiles()
    return [{"id": p["id"], "name": p["name"]} for p in profiles]

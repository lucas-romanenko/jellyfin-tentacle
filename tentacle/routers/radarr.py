"""
Tentacle - Radarr Router
Radarr library scanning, quality profiles, and provider migration
"""

import re
import threading
import logging
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from models.database import get_db, Provider, Movie, ListItem, ListSubscription, DownloadRequest, Duplicate, get_setting, log_activity
from services.radarr import scan_radarr_library, RadarrService, file_loss_looks_like_an_outage
from services.nfo import update_nfo_tags, write_movie_nfo, make_folder_name, refresh_arr_nfo
from services.tagger import tentacle_owned_tags
from services.migration import migrate_provider, preview_migration
from services.logstream import log_event_generator, get_recent_logs, emit_library_event

from routers.auth import require_admin, _has_internal_secret


def _check_webhook_auth(request: Request, db: Session) -> None:
    """Opt-in webhook authentication.

    If a `webhook_secret` setting is configured, require the caller to present a
    matching `?secret=` (or X-Tentacle-Secret header) — otherwise reject 401 so
    forged delete/scan events can't be injected. If no secret is configured
    (default), allow the call but log a one-line warning recommending setup, so
    existing Radarr/Sonarr installs keep working with no change.
    """
    import hmac
    secret = get_setting(db, "webhook_secret", "")
    if not secret:
        logger.warning(
            "[Radarr webhook] Received unauthenticated webhook — set a 'webhook_secret' "
            "in settings and add ?secret=... to the webhook URL to reject forged events."
        )
        return
    provided = request.headers.get("X-Tentacle-Secret") or request.query_params.get("secret") or ""
    if not (provided and hmac.compare_digest(provided, secret)):
        logger.warning("[Radarr webhook] Rejected webhook with missing/invalid secret")
        raise HTTPException(401, "Invalid or missing webhook secret")

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/radarr", tags=["radarr"], dependencies=[Depends(require_admin)])

# Per-movie lock to prevent duplicate webhook processing (e.g. Download + MovieAdded
# firing close together for the same movie). Only one background thread per tmdb_id.
_webhook_locks: dict[int, threading.Lock] = {}
_webhook_locks_guard = threading.Lock()

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
        owned = tentacle_owned_tags(db)

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
                if refresh_arr_nfo(nfo_path, write_movie_nfo, nfo_metadata, tags, owned):
                    written += 1
                else:
                    skipped += 1
                if nfo_path.exists():
                    db_movie.nfo_path = str(nfo_path)

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


def _release_vod_row(db: Session, tmdb_id: int):
    """The download of a VOD title was deleted: the provider row stops claiming
    it (#378). Returns the tags taken off (maybe none), or None when there is
    no such row (a radarr row is deleted instead)."""
    from services.radarr import release_download_claim
    row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id, Movie.source.like("provider_%"),
                                 Movie.radarr_path.isnot(None), Movie.radarr_path != "").first()
    if row is None:
        return None
    return release_download_claim(db, row)


def _after_vod_row_released(db: Session, tmdb_id: int, title: str, removed_tags: set, arr_folder):
    log_activity(db, "radarr_remove", f"Download of '{title}' deleted; its VOD copy stays")
    threading.Thread(target=_vod_row_released_background, args=(tmdb_id, sorted(removed_tags), arr_folder),
                     daemon=True).start()


def _vod_row_released_background(tmdb_id: int, removed_tags: list, arr_folder):
    """Background: the playlists see the change now, not at the nightly run.
    The deleted download's entry leaves every playlist (only the item in its
    folder, #296). The VOD item, found by its .strm, loses the tags the row
    lost (Jellyfin may still list the download item for a while, and the
    nightly tag push only takes a tag off where one item has the TMDB id),
    then every user's playlists of those tags are refreshed. Whatever fails
    here, the nightly pipeline does again."""
    from routers.library import _cleanup_playlists_all_users
    _cleanup_playlists_all_users(tmdb_id, "movie", arr_folder=arr_folder)
    if not removed_tags:
        return
    from models.database import SessionLocal
    from services.jellyfin import JellyfinService
    db = SessionLocal()
    try:
        row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
        jf_url, jf_key = get_setting(db, "jellyfin_url"), get_setting(db, "jellyfin_api_key")
        if row is None or not row.strm_path or not (jf_url and jf_key):
            return
        jf = JellyfinService(jf_url, jf_key)
        tail = "/".join(row.strm_path.replace("\\", "/").split("/")[-2:]).lower()
        vod_ids = []
        for item in jf._fetch_all_items("Movie"):
            if (item.get("ProviderIds") or {}).get("Tmdb") != str(tmdb_id):
                continue
            path = ((jf.get_item_by_id(item["Id"]) or {}).get("Path") or "").replace("\\", "/").lower()
            if path.endswith("/" + tail):
                vod_ids.append(item["Id"])
        if not vod_ids:
            logger.info(f"[Radarr webhook] tmdb:{tmdb_id}: VOD item not in Jellyfin yet; the nightly run updates it")
            return
        owned = tentacle_owned_tags(db)
        for item_id in vod_ids:
            jf.set_item_owned_tags(item_id, list(row.tags or []), owned)
        from services.smartlists import refresh_smartlist_playlists, _notify_jellyfin_plugin
        result = refresh_smartlist_playlists(db, only_names=removed_tags)
        if result.get("changed") or result.get("created"):
            _notify_jellyfin_plugin(db)
        logger.info(f"[Radarr webhook] tmdb:{tmdb_id}: VOD copy untagged {removed_tags}; playlists refreshed")
    except Exception as e:
        logger.warning(f"[Radarr webhook] tmdb:{tmdb_id}: could not update Jellyfin after its download was "
                       f"deleted ({e}); the nightly run does it")
    finally:
        db.close()


webhook_router = APIRouter(prefix="/api/radarr", tags=["radarr"])


def _remove_downloaded_movie(db: Session, tmdb_id: int, title: str, arr_folder: Optional[str],
                             clean_up_if_gone: bool = False) -> int:
    """A download's file is gone: drop its row, its request (unless a "Bad
    copy" replacement is coming) and its duplicate tombstones, and take it out
    of every user's playlists. Returns the rows deleted; raises on a DB error
    (rolled back). clean_up_if_gone: the playlist clean-up runs even when the
    row was already gone (a scan removed it while the report waited; the scan
    does no playlist clean-up), by the download's folder only."""
    from services.bad_copy import is_replacing
    replacing = is_replacing(db, "movie", tmdb_id)
    try:
        deleted = db.query(Movie).filter(Movie.tmdb_id == tmdb_id, Movie.source == "radarr").delete()
        if not replacing:  # "Bad copy": the request stands while another copy comes
            db.query(DownloadRequest).filter(DownloadRequest.tmdb_id == tmdb_id, DownloadRequest.media_type == "movie").delete()
        # Clear duplicate tombstones — deleting the downloaded copy is a
        # clean slate; the title may legitimately re-import from VOD later
        db.query(Duplicate).filter(Duplicate.tmdb_id == tmdb_id, Duplicate.media_type == "movie").delete()
        released = _release_vod_row(db, tmdb_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    if released is not None:
        _after_vod_row_released(db, tmdb_id, title, released, arr_folder)
    if deleted:
        emit_library_event("movie_removed", {"tmdb_id": tmdb_id, "title": title, "media_type": "movie"})
        log_activity(db, "radarr_remove", f"Removed '{title}' from Radarr library")
    if deleted or (clean_up_if_gone and arr_folder):
        from routers.library import _cleanup_playlists_all_users
        threading.Thread(target=_cleanup_playlists_all_users, args=(tmdb_id, "movie"),
                         kwargs={"arr_folder": arr_folder}, daemon=True).start()
    return deleted


# File deletes Radarr makes because it can't see the file any more
# (deleteReason "missingFromDisk", #381) go through the scan's storage-outage
# guard (#106). Radarr sends one per film as its refresh walks the library, so
# when its storage goes away (a share mounted below the root folder, a pool
# with a disk gone) a burst arrives -- sometimes with pauses (a slow metadata
# call) and for longer than any fixed wait on a large library. So reports are
# collected until none has come for MISSING_SETTLE_SECONDS, and then judged
# like a scan's lost files together with every report of the last
# MISSING_WINDOW_SECONDS (films removed or kept for it before): a loss that
# looks like an outage is refused (rows, requests and tombstones kept), any
# other is removed as before. A burst that a pause split is so judged as a
# whole once its total looks like an outage, instead of slice by slice. In
# memory only: after a restart the next scan judges them.
MISSING_SETTLE_SECONDS = 600
MISSING_WINDOW_SECONDS = 6 * 3600
_missing_lock = threading.Lock()
_missing_pending: dict = {}          # tmdb_id -> (title, arr_folder)
_missing_recent: dict = {}           # tmdb_id -> (monotonic time judged, was a downloaded row)
_missing_gen = 0
_missing_timer = None


def _queue_missing_from_disk(tmdb_id: int, title: str, arr_folder: Optional[str]) -> None:
    global _missing_gen, _missing_timer
    with _missing_lock:
        _missing_pending[tmdb_id] = (title, arr_folder)
        if _missing_timer is not None:
            _missing_timer.cancel()
        _missing_gen += 1
        _missing_timer = threading.Timer(MISSING_SETTLE_SECONDS, _flush_missing_from_disk, args=(_missing_gen,))
        _missing_timer.daemon = True
        _missing_timer.start()


def _forget_missing_from_disk(tmdb_id) -> None:
    """The film was imported again (or removed from Radarr): its queued
    missingFromDisk delete no longer applies, nor does its earlier report."""
    with _missing_lock:
        _missing_pending.pop(tmdb_id, None)
        _missing_recent.pop(tmdb_id, None)


def _flush_missing_from_disk(gen: Optional[int] = None, db: Optional[Session] = None) -> dict:
    """Judge the collected missingFromDisk deletes, with the recent ones (see above)."""
    import time
    with _missing_lock:
        if gen is not None and gen != _missing_gen:
            return {"status": "superseded"}   # a newer event re-armed the timer
        batch = dict(_missing_pending)
        _missing_pending.clear()
        now = time.monotonic()
        for k in [k for k, (t, _) in _missing_recent.items() if now - t > MISSING_WINDOW_SECONDS]:
            del _missing_recent[k]
        recent = {k: v for k, v in _missing_recent.items() if k not in batch}
    if not batch:
        return {"status": "empty"}
    own_db = db is None
    if own_db:
        from models.database import SessionLocal
        db = SessionLocal()
    try:
        rows = {t for (t,) in db.query(Movie.tmdb_id).filter(Movie.source == "radarr")}
        # Downloads already removed for an earlier report still count, in the
        # loss and in the library it is measured against.
        gone = sum(1 for t, (_, was_row) in recent.items() if was_row and t not in rows)
        lost_now = rows & set(batch)
        lost = len(lost_now) + len(rows & set(recent)) + gone
        total = len(rows) + gone
        if file_loss_looks_like_an_outage(lost, total):
            with _missing_lock:
                for t in batch:
                    _missing_recent[t] = (now, t in rows)
            logger.error(
                f"[Radarr webhook] REFUSING to remove {len(lost_now)} downloaded movies Radarr reported missing "
                f"from disk: with the reports of the last {MISSING_WINDOW_SECONDS // 3600} h that is {lost} of "
                f"{total}. That many looks like Radarr's media storage being unavailable, not a clean-up. Rows "
                f"kept; if the files really are gone, remove the movies from Radarr.")
            return {"status": "refused", "kept": len(lost_now)}
        removed = 0
        for tmdb_id, (title, arr_folder) in batch.items():
            try:
                removed += _remove_downloaded_movie(db, tmdb_id, title, arr_folder, clean_up_if_gone=True)
                logger.info(f"[Radarr webhook] MovieFileDelete (missing from disk) for '{title}' (tmdb:{tmdb_id}) — removed from DB")
            except Exception as e:
                logger.error(f"[Radarr webhook] MovieFileDelete DB cleanup failed for tmdb:{tmdb_id}: {e}")
            with _missing_lock:
                _missing_recent[tmdb_id] = (now, tmdb_id in rows)
        return {"status": "removed", "removed": removed}
    finally:
        if own_db:
            db.close()


@webhook_router.post("/webhook")
def radarr_webhook(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Radarr webhook — triggered on Download, MovieAdded, MovieDelete, MovieFileDelete events."""
    _check_webhook_auth(request, db)
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
    if not isinstance(movie_data, dict):
        return {"status": "skipped", "reason": "no movie"}
    tmdb_id = movie_data.get("tmdbId")
    title = movie_data.get("title", "Unknown")
    # The deleted download's folder: its playlist clean-up must find that
    # copy, not "the first item with this TMDB id" (maybe the VOD one, #296).
    movie_file = payload.get("movieFile") if isinstance(payload.get("movieFile"), dict) else {}
    file_path = str(movie_file.get("path") or "").replace("\\", "/")
    arr_folder = file_path.rsplit("/", 1)[0] if "/" in file_path else movie_data.get("folderPath")
    logger.info(f"[Radarr webhook] {event_type} for '{title}' (tmdb:{tmdb_id})")

    if not tmdb_id:
        return {"status": "skipped", "reason": "no tmdbId"}

    # MovieFileDelete with upgrade reason — file is being replaced, ignore
    if event_type == "MovieFileDelete":
        delete_reason = payload.get("deleteReason", "")
        if delete_reason == "upgrade":
            logger.info(f"[Radarr webhook] MovieFileDelete upgrade for '{title}' — ignoring")
            return {"status": "ignored", "reason": "upgrade"}
        if str(delete_reason).lower() == "missingfromdisk":
            # Radarr can't see the file: judged with the others of its burst (#381).
            _queue_missing_from_disk(tmdb_id, title, arr_folder)
            logger.info(f"[Radarr webhook] MovieFileDelete missing from disk for '{title}' — queued for the storage-outage check")
            return {"status": "queued", "tmdb_id": tmdb_id}
        # Non-upgrade file deletion — remove from DB
        _forget_missing_from_disk(tmdb_id)
        try:
            deleted = _remove_downloaded_movie(db, tmdb_id, title, arr_folder)
        except Exception as e:
            logger.error(f"[Radarr webhook] MovieFileDelete DB cleanup failed for tmdb:{tmdb_id}: {e}")
            raise HTTPException(500, "Failed to process delete event")
        logger.info(f"[Radarr webhook] MovieFileDelete for '{title}' (tmdb:{tmdb_id}) — removed {deleted} from DB")
        return {"status": "deleted", "tmdb_id": tmdb_id}

    # MovieDelete — remove from DB
    if event_type == "MovieDelete":
        _forget_missing_from_disk(tmdb_id)
        try:
            deleted = db.query(Movie).filter(Movie.tmdb_id == tmdb_id, Movie.source == "radarr").delete()
            db.query(DownloadRequest).filter(DownloadRequest.tmdb_id == tmdb_id, DownloadRequest.media_type == "movie").delete()
            # Clear duplicate tombstones — deleting the downloaded copy is a
            # clean slate; the title may legitimately re-import from VOD later
            db.query(Duplicate).filter(Duplicate.tmdb_id == tmdb_id, Duplicate.media_type == "movie").delete()
            released = _release_vod_row(db, tmdb_id)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"[Radarr webhook] MovieDelete DB cleanup failed for tmdb:{tmdb_id}: {e}")
            raise HTTPException(500, "Failed to process delete event")
        if released is not None:
            _after_vod_row_released(db, tmdb_id, title, released, arr_folder)
        if deleted:
            emit_library_event("movie_removed", {"tmdb_id": tmdb_id, "title": title, "media_type": "movie"})
            log_activity(db, "radarr_remove", f"Removed '{title}' from Radarr library")
            from routers.library import _cleanup_playlists_all_users
            threading.Thread(target=_cleanup_playlists_all_users, args=(tmdb_id, "movie"),
                             kwargs={"arr_folder": arr_folder}, daemon=True).start()
        logger.info(f"[Radarr webhook] MovieDelete for '{title}' (tmdb:{tmdb_id}) — removed {deleted} from DB")
        return {"status": "deleted", "tmdb_id": tmdb_id}

    # Download / MovieAdded — scan and tag
    _forget_missing_from_disk(tmdb_id)

    def _webhook_background(tmdb_id, title, event_type):
        import time
        from datetime import datetime
        from models.database import SessionLocal, get_setting
        from pathlib import Path
        from services.jellyfin import JellyfinService

        # Per-movie lock: if another webhook event for the same movie is already
        # being processed (e.g. Download + MovieAdded close together), skip.
        with _webhook_locks_guard:
            if tmdb_id in _webhook_locks and _webhook_locks[tmdb_id].locked():
                logger.info(f"[Radarr webhook] Skipping duplicate processing for tmdb:{tmdb_id}")
                return
            # Bound the dict: prune unlocked (idle) locks if it grows large.
            if len(_webhook_locks) > 512:
                for k in [k for k, l in _webhook_locks.items() if not l.locked()]:
                    _webhook_locks.pop(k, None)
            if tmdb_id not in _webhook_locks:
                _webhook_locks[tmdb_id] = threading.Lock()
            lock = _webhook_locks[tmdb_id]

        if not lock.acquire(blocking=False):
            logger.info(f"[Radarr webhook] Skipping duplicate processing for tmdb:{tmdb_id}")
            return

        db = SessionLocal()
        try:
            scan_radarr_library(db)

            db_movie = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
            if not db_movie:
                logger.warning(f"[Radarr webhook] Movie tmdb:{tmdb_id} not found after scan")
                return

            # A Download just landed: stamp its download time now, so it is at the
            # front of "recently added" rows immediately, without waiting for the
            # scan's movieFile.dateAdded (which is normally the same moment, but
            # this is the authoritative "it arrived now"). date_added (first seen
            # in the library) is only set if the row had none.
            if event_type == "Download":
                db_movie.downloaded_at = datetime.utcnow()
                if not db_movie.date_added:
                    db_movie.date_added = datetime.utcnow()
                db.commit()

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
                        update_nfo_tags(Path(db_movie.nfo_path), tags, tentacle_owned_tags(db))
                    db.commit()
                    logger.info(f"[Radarr webhook] Tagged '{title}' with {tagged_from}")
                else:
                    logger.info(f"[Radarr webhook] '{title}' already has all list tags")

            # Push tags to Jellyfin via API.
            # scan_radarr_library() already triggered a library scan and pushed tags
            # for movies it found in Jellyfin. But for newly downloaded movies, Jellyfin
            # may not have indexed them yet. Wait for Jellyfin to scan, then retry.
            # jf_item stays None when Jellyfin is not configured or the movie
            # has no tags; the playlist block below reads it (#284).
            jf_item = None
            jf_url = get_setting(db, "jellyfin_url")
            jf_key = get_setting(db, "jellyfin_api_key")
            jf_uid = get_setting(db, "jellyfin_user_id", "")
            try:
                if jf_url and jf_key and db_movie.tags:
                    jf = JellyfinService(jf_url, jf_key, jf_uid)
                    movie_title = db_movie.title or title
                    movie_year = str(db_movie.year or "")

                    # Retry loop: wait for Jellyfin to index the new movie: the item
                    # of the file Radarr has now. After a quality upgrade the listing
                    # can still hold the replaced file's item, about to be removed.
                    file_name = re.split(r"[\\/]", db_movie.radarr_path or "")[-1] or None
                    jf_item = None
                    max_attempts = 5
                    for attempt in range(max_attempts):
                        jf_item = jf.search_by_tmdb_id(
                            tmdb_id, "Movie", title=movie_title, year=movie_year, file_name=file_name
                        )
                        if jf_item:
                            break
                        if file_name and attempt == max_attempts - 1:
                            # Never listed under that file name (another layout):
                            # the TMDB match, as before.
                            jf_item = jf.search_by_tmdb_id(
                                tmdb_id, "Movie", title=movie_title, year=movie_year
                            )
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
                        # Cache Jellyfin item ID for click-to-play
                        if jf_item.get("Id") and db_movie.jellyfin_item_id != jf_item["Id"]:
                            db_movie.jellyfin_item_id = jf_item["Id"]
                            db.commit()

                        # Fetch full item DTO (includes Genres, CommunityRating, ProductionYear)
                        # for native playlist expression matching
                        full_item = jf.get_item_by_id(jf_item["Id"])
                        if full_item:
                            jf_item = full_item

                        # Merge with existing Jellyfin tags rather than replacing
                        existing_jf_tags = set(jf_item.get("Tags", []))
                        desired_tags = set(db_movie.tags)
                        merged = list(existing_jf_tags | desired_tags)
                        if jf.set_item_tags(jf_item["Id"], merged):
                            logger.info(f"[Radarr webhook] Pushed tags to Jellyfin for '{title}': {merged}")
                        else:
                            logger.warning(f"[Radarr webhook] Failed to set tags on '{title}' in Jellyfin")

                        # Refresh metadata so Jellyfin fetches posters/info from TMDB,
                        # then wait for images before notifying clients (avoids empty posters).
                        if jf.refresh_item_metadata(jf_item["Id"]):
                            logger.info(f"[Radarr webhook] Triggered metadata refresh for '{title}'")
                            if jf.wait_for_images(jf_item["Id"], max_wait=30, poll_interval=3):
                                logger.info(f"[Radarr webhook] Images ready for '{title}'")
                                # Re-fetch full DTO now that images are available
                                refreshed = jf.get_item_by_id(jf_item["Id"])
                                if refreshed:
                                    jf_item = refreshed
                            else:
                                logger.info(f"[Radarr webhook] Images not ready for '{title}' after 30s, continuing anyway")
                    else:
                        logger.warning(
                            f"[Radarr webhook] '{title}' (tmdb:{tmdb_id}) not found in Jellyfin "
                            f"after {max_attempts} attempts — tags will be pushed on next scheduled scan"
                        )
            except requests.HTTPError as e:
                # Jellyfin refused a read or write of the item (e.g. it was just
                # removed). The tags and playlists catch up on the next scan;
                # the notice and the rest of this pass must still happen.
                logger.warning(f"[Radarr webhook] Jellyfin update for '{title}' stopped: {e}")
                jf_item = None

            # Add item directly to matching playlists — no need to wait for
            # Jellyfin tag indexing since we match by known tags from the DB.
            try:
                if jf_item:
                    from services.smartlists import add_item_to_matching_playlists
                    result = add_item_to_matching_playlists(
                        db, jf_item["Id"], list(db_movie.tags or []), "movie", jf_item=jf_item
                    )
                    logger.info(f"[Radarr webhook] Added '{title}' to {result.get('added_to', 0)} playlist(s)")
                else:
                    # Item not in Jellyfin yet — skip playlist update.
                    # Nightly sync will pick it up once Jellyfin has indexed it.
                    # Never do a full playlist rebuild from a webhook — clearing
                    # and re-querying all playlists can drop items whose tags
                    # haven't been indexed yet, causing content to disappear.
                    logger.info(f"[Radarr webhook] Skipping playlist update for '{title}' (not in Jellyfin yet)")

                # Notify plugin to clear caches + broadcast WebSocket to all clients
                from services.smartlists import _notify_jellyfin_plugin
                _notify_jellyfin_plugin(db)
            except Exception as e:
                logger.warning(f"[Radarr webhook] Playlist update failed: {e}")

            # Per-user download notification
            if event_type == "Download":
                try:
                    from models.database import DownloadRequest, create_notification
                    dr = db.query(DownloadRequest).filter(
                        DownloadRequest.tmdb_id == tmdb_id,
                        DownloadRequest.media_type == "movie"
                    ).first()
                    from services.bad_copy import is_replacing, clear_replacing
                    replaced = is_replacing(db, "movie", tmdb_id)
                    if replaced:
                        clear_replacing(db, "movie", tmdb_id)
                    if dr:
                        create_notification(
                            db, user_id=dr.user_id, tmdb_id=tmdb_id, media_type="movie",
                            title=db_movie.title,
                            message=(f"A new copy of {db_movie.title} is ready to watch" if replaced
                                     else f"{db_movie.title} has completed and is ready to watch"),
                            poster_path=db_movie.poster_path,
                            jellyfin_item_id=db_movie.jellyfin_item_id,
                        )
                except Exception as e:
                    logger.warning(f"[Radarr webhook] Notification creation failed: {e}")

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
            lock.release()
            db.close()

    thread = threading.Thread(target=_webhook_background, args=(tmdb_id, title, event_type), daemon=True)
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
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "A provider migration preview")

    return preview_migration(from_provider, to_provider, db)


@router.post("/migration/run")
def run_migration(body: MigrateRequest, db: Session = Depends(get_db)):
    """Migrate content from one provider to another"""
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "A provider migration")
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

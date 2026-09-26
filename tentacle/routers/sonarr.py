"""
Tentacle - Sonarr Router
Sonarr library scanning, webhooks, and NFO management
"""

import threading
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from models.database import get_db, Series, ListItem, ListSubscription, DownloadRequest, Duplicate, get_setting, log_activity
from services.sonarr import scan_sonarr_library, SonarrService
from services.nfo import update_nfo_tags, write_series_nfo
from services.logstream import emit_library_event

from routers.auth import require_admin, _has_internal_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sonarr", tags=["sonarr"], dependencies=[Depends(require_admin)])


def _check_webhook_auth(request: Request, db: Session) -> None:
    """Opt-in webhook authentication (see routers/radarr.py for rationale).

    If `webhook_secret` is configured, require a matching `?secret=` (or
    X-Tentacle-Secret header); otherwise allow but log a one-line warning.
    """
    import hmac
    secret = get_setting(db, "webhook_secret", "")
    if not secret:
        logger.warning(
            "[Sonarr webhook] Received unauthenticated webhook — set a 'webhook_secret' "
            "in settings and add ?secret=... to the webhook URL to reject forged events."
        )
        return
    provided = request.headers.get("X-Tentacle-Secret") or request.query_params.get("secret") or ""
    if not (provided and hmac.compare_digest(provided, secret)):
        logger.warning("[Sonarr webhook] Rejected webhook with missing/invalid secret")
        raise HTTPException(401, "Invalid or missing webhook secret")

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
def sonarr_webhook(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Sonarr webhook — triggered on Download, SeriesAdd, SeriesDelete, EpisodeFileDelete events."""
    _check_webhook_auth(request, db)
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
    episodes = payload.get("episodes", [])
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
            try:
                # Clear following state on any matching series (hybrid VOD+Sonarr)
                hybrid = db.query(Series).filter(Series.tmdb_id == tmdb_id, Series.source != "sonarr").first()
                if hybrid:
                    hybrid.sonarr_monitored = False
                    hybrid.sonarr_path = None
                deleted = db.query(Series).filter(Series.tmdb_id == tmdb_id, Series.source == "sonarr").delete()
                db.query(DownloadRequest).filter(DownloadRequest.tmdb_id == tmdb_id, DownloadRequest.media_type == "series").delete()
                # Clear duplicate tombstones — deleting the downloaded copy is a
                # clean slate; the title may legitimately re-import from VOD later
                if deleted:
                    db.query(Duplicate).filter(Duplicate.tmdb_id == tmdb_id, Duplicate.media_type == "series").delete()
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error(f"[Sonarr webhook] SeriesDelete DB cleanup failed for tmdb:{tmdb_id}: {e}")
                raise HTTPException(500, "Failed to process delete event")
            if deleted:
                emit_library_event("series_removed", {"tmdb_id": tmdb_id, "title": title, "media_type": "series"})
                log_activity(db, "sonarr_remove", f"Removed '{title}' from Sonarr library")
                from routers.library import _cleanup_playlists_all_users
                threading.Thread(target=_cleanup_playlists_all_users, args=(tmdb_id, "series"), daemon=True).start()
            logger.info(f"[Sonarr webhook] SeriesDelete for '{title}' (tmdb:{tmdb_id}) — removed {deleted} from DB")
        return {"status": "deleted", "tmdb_id": tmdb_id}

    # Download / SeriesAdd / EpisodeFileDelete — scan and tag, coalesced (#182)
    _queue_webhook_event(tmdb_id, title, event_type, episodes)
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


def _after_scan(db, tmdb_id, title, event_type, first_episode=None, episode_count=1):
    """What one series' webhook event does once the Sonarr scan has run:
    download bookkeeping, list tags, the Jellyfin tag push, playlists and the
    ready-to-watch notification."""
    import time
    from models.database import get_setting
    from services.jellyfin import JellyfinService
    try:
        if not tmdb_id:
            logger.warning(f"[Sonarr webhook] No TMDB ID for '{title}' — scan completed but cannot tag")
            return

        db_series = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
        if not db_series:
            logger.warning(f"[Sonarr webhook] Series tmdb:{tmdb_id} not found after scan")
            return

        if event_type == "Download":
            # An episode just landed: that is the series' newest download,
            # which is what a "recently downloaded" row sorts by. (The
            # episode label used to be recorded only the first time, when
            # date_added was still missing.)
            db_series.downloaded_at = datetime.utcnow()
            if not db_series.date_added:
                db_series.date_added = datetime.utcnow()
            if first_episode:
                s = first_episode.get("seasonNumber", 0)
                e = first_episode.get("episodeNumber", 0)
                ep_title = first_episode.get("title", "")
                label = f"S{s:02d}E{e:02d}"
                if ep_title:
                    label += f" \u00b7 {ep_title}"
                db_series.last_downloaded_episode = label
            db.commit()

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

        # Push tags to Jellyfin via API. (jf_item was unset when Jellyfin was not
        # configured or the series had no tags, and the NameError that followed
        # skipped the plugin notify below.)
        jf_item = None
        jf_url = get_setting(db, "jellyfin_url")
        jf_key = get_setting(db, "jellyfin_api_key")
        jf_uid = get_setting(db, "jellyfin_user_id", "")
        if jf_url and jf_key and db_series.tags:
            jf = JellyfinService(jf_url, jf_key, jf_uid)
            series_title = db_series.title or title
            series_year = str(db_series.year or "")

            # Retry loop: wait for Jellyfin to index the new series/episode
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
                # Cache Jellyfin item ID for click-to-play
                if jf_item.get("Id") and db_series.jellyfin_item_id != jf_item["Id"]:
                    db_series.jellyfin_item_id = jf_item["Id"]
                    db.commit()

                # Fetch full item DTO (includes Genres, CommunityRating, ProductionYear)
                # for native playlist expression matching
                full_item = jf.get_item_by_id(jf_item["Id"])
                if full_item:
                    jf_item = full_item

                # Tentacle's own tags replaced, everything else kept (#180).
                from services.tagger import merge_owned_tags, tentacle_owned_tags
                merged = merge_owned_tags(jf_item.get("Tags"), db_series.tags or [], tentacle_owned_tags(db))
                if jf.set_item_tags(jf_item["Id"], merged):
                    logger.info(f"[Sonarr webhook] Pushed tags to Jellyfin for '{title}': {merged}")
                else:
                    logger.warning(f"[Sonarr webhook] Failed to set tags on '{title}' in Jellyfin")

                if jf.refresh_item_metadata(jf_item["Id"]):
                    logger.info(f"[Sonarr webhook] Triggered metadata refresh for '{title}'")
                    if jf.wait_for_images(jf_item["Id"], max_wait=30, poll_interval=3):
                        logger.info(f"[Sonarr webhook] Images ready for '{title}'")
                        refreshed = jf.get_item_by_id(jf_item["Id"])
                        if refreshed:
                            jf_item = refreshed
                    else:
                        logger.info(f"[Sonarr webhook] Images not ready for '{title}' after 30s, continuing anyway")
            else:
                logger.warning(
                    f"[Sonarr webhook] '{title}' (tmdb:{tmdb_id}) not found in Jellyfin "
                    f"after {max_attempts} attempts — tags will be pushed on next scheduled scan"
                )

        # Add item directly to matching playlists — no Jellyfin tag query needed
        try:
            if jf_item:
                from services.smartlists import add_item_to_matching_playlists
                result = add_item_to_matching_playlists(
                    db, jf_item["Id"], list(db_series.tags or []), "series", jf_item=jf_item
                )
                logger.info(f"[Sonarr webhook] Added '{title}' to {result.get('added_to', 0)} playlist(s)")
            else:
                logger.info(f"[Sonarr webhook] Skipping playlist update for '{title}' (not in Jellyfin yet)")

            # Notify plugin to clear caches + broadcast WebSocket to all clients
            from services.smartlists import _notify_jellyfin_plugin
            _notify_jellyfin_plugin(db)
        except Exception as e:
            logger.warning(f"[Sonarr webhook] Playlist update failed: {e}")

        # Per-user download notification
        if event_type == "Download":
            try:
                from models.database import DownloadRequest, create_notification
                # Build episode label for the message
                ep_label = ""
                if episode_count > 1:
                    ep_label = f" - {episode_count} new episodes"
                elif first_episode:
                    s = first_episode.get("seasonNumber", 0)
                    e = first_episode.get("episodeNumber", 0)
                    ep_label = f" - S{s:02d}E{e:02d}"

                verb = "have" if episode_count > 1 else "has"
                notif_msg = f"{db_series.title}{ep_label} {verb} completed and {'are' if episode_count > 1 else 'is'} ready to watch"
                from services.bad_copy import is_replacing, clear_replacing
                if is_replacing(db, "series", tmdb_id):
                    clear_replacing(db, "series", tmdb_id)
                    notif_msg = f"A new copy of {db_series.title}{ep_label} is ready to watch"

                # Notify the requester
                notified_user_ids = set()
                dr = db.query(DownloadRequest).filter(
                    DownloadRequest.tmdb_id == tmdb_id,
                    DownloadRequest.media_type == "series"
                ).first()
                if dr:
                    create_notification(
                        db, user_id=dr.user_id, tmdb_id=tmdb_id, media_type="series",
                        title=db_series.title, message=notif_msg,
                        poster_path=db_series.poster_path,
                        jellyfin_item_id=db_series.jellyfin_item_id,
                    )
                    notified_user_ids.add(dr.user_id)

                # Also notify users who follow this series (auto-monitored downloads)
                if db_series.sonarr_monitored:
                    from routers.library import _get_followers_for_series
                    follower_ids = _get_followers_for_series(db, tmdb_id)
                    for uid in follower_ids:
                        if uid not in notified_user_ids:
                            create_notification(
                                db, user_id=uid, tmdb_id=tmdb_id, media_type="series",
                                title=db_series.title, message=notif_msg,
                                poster_path=db_series.poster_path,
                                jellyfin_item_id=db_series.jellyfin_item_id,
                            )
                            notified_user_ids.add(uid)
            except Exception as e:
                logger.warning(f"[Sonarr webhook] Notification creation failed: {e}")

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
        # The batch's other series share this session: a failed flush must not
        # fail every one after it.
        db.rollback()


# ── Webhook coalescing (#182) ─────────────────────────────────────────────
# Every event used to run its own full Sonarr scan and Jellyfin listing in its
# own thread: five EpisodeFileDelete events for one series in 2 s were five
# concurrent scans of every series, with Jellyfin timing out for minutes
# after. Events are queued per series; one worker waits for a quiet spell
# (WEBHOOK_QUIET_SECONDS, at most WEBHOOK_MAX_WAIT_SECONDS after the first
# event), runs ONE scan, then does each series' own follow-up. Events that
# arrive during a scan form the next batch, with their own wait.
WEBHOOK_QUIET_SECONDS = 10
WEBHOOK_MAX_WAIT_SECONDS = 30
_webhook_lock = threading.Lock()
_webhook_pending: dict = {}      # series key -> {"tmdb_id", "title", "event_type", "episodes"}
_webhook_state: dict = {"worker": None, "first": 0.0, "last": 0.0}


def _queue_webhook_event(tmdb_id, title, event_type, episodes) -> None:
    import time
    key = tmdb_id or f"title:{title}"
    with _webhook_lock:
        now = time.monotonic()
        if not _webhook_pending:
            _webhook_state["first"] = now
        entry = _webhook_pending.get(key)
        if entry is None:
            _webhook_pending[key] = {"tmdb_id": tmdb_id, "title": title, "event_type": event_type,
                                     "episodes": list(episodes or []) if event_type == "Download" else []}
        else:
            # A Download outranks the others: it carries the episodes and the
            # ready-to-watch notification. Only a Download's episodes count: a
            # deleted episode is not a new one.
            if event_type == "Download" and entry["event_type"] != "Download":
                entry["episodes"] = []
            if event_type == "Download" or entry["event_type"] != "Download":
                entry["event_type"] = event_type
            if event_type == "Download":
                entry["episodes"].extend(episodes or [])
        _webhook_state["last"] = now
        if _webhook_state["worker"] is None:
            worker = threading.Thread(target=_drain_webhook_events, daemon=True, name="sonarr-webhooks")
            _webhook_state["worker"] = worker
            worker.start()


def _drain_webhook_events() -> None:
    try:
        _drain_webhook_batches()
    finally:
        # Normally cleared under the lock when the queue is empty; this covers
        # an unexpected exit, which would otherwise leave every later event
        # queued behind a worker that no longer exists.
        with _webhook_lock:
            if _webhook_state["worker"] is threading.current_thread():
                _webhook_state["worker"] = None


def _drain_webhook_batches() -> None:
    import time
    from models.database import SessionLocal
    while True:
        with _webhook_lock:
            if not _webhook_pending:
                _webhook_state["worker"] = None
                return
            now = time.monotonic()
            due = min(_webhook_state["last"] + WEBHOOK_QUIET_SECONDS,
                      _webhook_state["first"] + WEBHOOK_MAX_WAIT_SECONDS)
            if now < due:
                batch, wait = None, due - now
            else:
                batch, wait = list(_webhook_pending.values()), 0
                _webhook_pending.clear()
        if batch is None:
            time.sleep(wait)
            continue
        db = None
        try:
            db = SessionLocal()
            logger.info(f"[Sonarr webhook] One scan for {len(batch)} series")
            scan_sonarr_library(db)
            for entry in batch:
                # A file deleted or a series added downloaded nothing: the scan
                # is all it needs. Its tail used to push tags, refresh the item
                # (waiting 30 s for images), add it to playlists and log
                # "Sonarr downloaded" for a deletion (#182).
                if entry["event_type"] != "Download":
                    logger.info(f"[Sonarr webhook] {entry['event_type']} for '{entry['title']}': library rescanned")
                    continue
                eps = entry["episodes"]
                _after_scan(db, entry["tmdb_id"], entry["title"], entry["event_type"],
                            eps[0] if eps else None, episode_count=max(1, len(eps)))
        except Exception as e:
            logger.error(f"[Sonarr webhook] Processing failed: {e}", exc_info=True)
        finally:
            if db is not None:
                db.close()

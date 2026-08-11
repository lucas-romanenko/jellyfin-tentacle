"""
Tentacle - Download Health
Stall detection and self-healing for the Radarr/Sonarr download queues.

Classification (shared with routers/activity.py) adds two phases on top of the
existing downloading/importing/queued/warning statuses:
  stuck          — hard fault (arr reports error/failed/warning-with-message) OR
                   no sizeleft progress for STALL_MINUTES (QUEUED_STALL_MINUTES
                   for "queued" items: healthy queue waits are bounded, dead
                   torrents at size=0 are not — only elapsed time tells them apart)
  import_blocked — finished download the arr has declined to import (e.g. "not
                   an upgrade"); it will never import on its own

Stall state is tracked by the 5-minute background job (run_download_health_check)
in the `downloads_stall_state` Setting as {download_id: {sizeleft, since}} —
request-path reads never write it.

Auto-fix (both toggles default OFF):
  downloads_auto_fix_enabled    — cancel + blocklist stuck items, re-grab the best
                                  alternative release preferring the OPPOSITE
                                  protocol from the one that failed
  downloads_auto_import_enabled — for import_blocked items, force a ManualImport
                                  when the arr's own lookup finds exactly one
                                  unambiguous file; otherwise dequeue but keep the
                                  file on disk for a human to sort out
"""

import json
import logging
import threading
import time
from datetime import datetime

import requests

from models.database import SessionLocal, get_setting, set_setting, log_activity, log_deletion

logger = logging.getLogger(__name__)

STALL_MINUTES = 30
QUEUED_STALL_MINUTES = 240
IMPORT_STAGE_STATES = {"importPending", "importBlocked", "imported", "importing"}
WAITING_STATUSES = {"queued", "paused", "delay"}
NOT_TRYING_STATUSES = {"paused", "delay"}  # never expected to resolve on their own — excluded from stall tracking

STALL_STATE_KEY = "downloads_stall_state"

# Serializes every resolve (manual Fix button + auto-fix sweep) — two racing
# callers could otherwise both cancel + re-grab, leaving duplicate downloads.
_resolve_lock = threading.Lock()


# ─── Arr HTTP helpers ─────────────────────────────────────────────────────────

def _arr_conn(db, app: str) -> tuple:
    url = get_setting(db, f"{app}_url", "")
    key = get_setting(db, f"{app}_api_key", "")
    return url.rstrip("/"), key


def _arr_get(url: str, key: str, path: str, **params):
    r = requests.get(f"{url}/api/v3/{path}", headers={"X-Api-Key": key}, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _arr_post(url: str, key: str, path: str, body):
    r = requests.post(f"{url}/api/v3/{path}", headers={"X-Api-Key": key}, json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else {}


def _arr_delete(url: str, key: str, path: str, **params):
    r = requests.delete(f"{url}/api/v3/{path}", headers={"X-Api-Key": key}, params=params, timeout=30)
    r.raise_for_status()


def fetch_queue_records(url: str, key: str, app: str) -> list:
    """Full queue with all pages (the activity router caps at 100; fixes must
    see everything)."""
    extra = {"includeUnknownMovieItems": "false", "includeMovie": "true"} if app == "radarr" \
        else {"includeUnknownSeriesItems": "false", "includeSeries": "true", "includeEpisode": "true"}
    records, page = [], 1
    while True:
        data = _arr_get(url, key, "queue", page=page, pageSize=200, **extra)
        batch = data.get("records", [])
        records.extend(batch)
        if page * 200 >= data.get("totalRecords", 0) or not batch:
            return records
        page += 1


# ─── Classification ───────────────────────────────────────────────────────────

def get_stall_state(db) -> dict:
    try:
        return json.loads(get_setting(db, STALL_STATE_KEY, "") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def classify_queue_item(item: dict, stall_state: dict) -> dict:
    """Returns {"status", "reason", "stalled_minutes"}. Status is one of:
    downloading | importing | import_blocked | queued | warning | stuck."""
    size = item.get("size") or 0
    sizeleft = item.get("sizeleft") or 0
    status = item.get("status") or ""
    tracked_status = item.get("trackedDownloadStatus") or ""
    dl_state = item.get("trackedDownloadState") or ""
    download_id = item.get("downloadId") or str(item.get("id"))
    msgs = [m for sm in (item.get("statusMessages") or []) for m in (sm.get("messages") or [])]
    reason = item.get("errorMessage") or (msgs[0] if msgs else None)

    # Import stage: a warning/error here is an IMPORT decision, not a stuck
    # download — cancelling + re-grabbing a finished download would be harmful.
    if dl_state in IMPORT_STAGE_STATES:
        if dl_state == "importBlocked" or reason:
            return {"status": "import_blocked", "reason": reason, "stalled_minutes": 0.0}
        return {"status": "importing", "reason": None, "stalled_minutes": 0.0}

    eligible = status not in NOT_TRYING_STATUSES
    stalled_minutes = 0.0
    if eligible:
        since = (stall_state.get(download_id) or {}).get("since")
        if since:
            try:
                stalled_minutes = (datetime.utcnow() - datetime.fromisoformat(since)).total_seconds() / 60
            except (ValueError, TypeError):
                pass

    # Hard faults fire immediately — the arr already diagnosed the problem
    # (e.g. "The download is stalled with no connections" arrives as
    # status=warning, not trackedDownloadStatus=error).
    hard_fault = eligible and (
        tracked_status == "error" or status == "failed" or (status == "warning" and bool(reason))
    )
    threshold = QUEUED_STALL_MINUTES if status == "queued" else STALL_MINUTES
    if hard_fault or (eligible and stalled_minutes >= threshold):
        return {
            "status": "stuck",
            "reason": reason or f"No download progress for over {threshold} minutes",
            "stalled_minutes": round(stalled_minutes, 1),
        }

    if tracked_status == "warning":
        return {"status": "warning", "reason": reason, "stalled_minutes": round(stalled_minutes, 1)}
    progress_zero = size > 0 and (size - sizeleft) <= 0
    if status in WAITING_STATUSES or (dl_state == "downloading" and (progress_zero or size == 0)):
        return {"status": "queued", "reason": None, "stalled_minutes": round(stalled_minutes, 1)}
    return {"status": "downloading", "reason": None, "stalled_minutes": round(stalled_minutes, 1)}


# ─── Stall tracking (background job only writes) ──────────────────────────────

def update_stall_tracking(db) -> int:
    """Advance the stall clock for active queue items whose sizeleft hasn't
    moved, reset on progress, prune departed downloads."""
    stall_state = get_stall_state(db)
    now_iso = datetime.utcnow().isoformat()
    seen = set()
    for app in ("radarr", "sonarr"):
        url, key = _arr_conn(db, app)
        if not url or not key:
            continue
        try:
            records = fetch_queue_records(url, key, app)
        except Exception as e:
            logger.warning(f"[Download health] {app} queue fetch failed: {e}")
            continue
        for r in records:
            if r.get("trackedDownloadState") in IMPORT_STAGE_STATES or r.get("status") in NOT_TRYING_STATUSES:
                continue
            download_id = r.get("downloadId") or str(r.get("id"))
            seen.add(download_id)
            sizeleft = r.get("sizeleft") or 0
            prev = stall_state.get(download_id)
            if not (prev and prev.get("sizeleft") == sizeleft):
                stall_state[download_id] = {"sizeleft": sizeleft, "since": now_iso}
    pruned = {k: v for k, v in stall_state.items() if k in seen}
    set_setting(db, STALL_STATE_KEY, json.dumps(pruned))
    return len(pruned)


def _clear_stall_entry(db, download_id: str):
    stall_state = get_stall_state(db)
    if stall_state.pop(download_id, None) is not None:
        set_setting(db, STALL_STATE_KEY, json.dumps(stall_state))


# ─── Fix actions ──────────────────────────────────────────────────────────────

def _grab_release(url: str, key: str, guid: str, indexer_id: int):
    _arr_post(url, key, "release", {"guid": guid, "indexerId": indexer_id})


def resolve_stuck_download(db, app: str, queue_id: int, reason: str = "manual") -> dict:
    """Cancel + blocklist a stuck queue item, then grab the best alternative
    release, preferring the opposite protocol from the one that failed."""
    url, key = _arr_conn(db, app)
    if not url or not key:
        return {"ok": False, "error": f"{app} not configured"}

    with _resolve_lock:
        # Re-fetch fresh — the queue can change between UI render and click
        try:
            records = fetch_queue_records(url, key, app)
        except Exception as e:
            return {"ok": False, "error": f"queue fetch failed: {e}"}
        r = next((x for x in records if x.get("id") == queue_id), None)
        if not r:
            return {"ok": False, "error": "not found in queue (already resolved?)"}

        title = r.get("title") or str(queue_id)
        protocol = r.get("protocol")
        movie_id = r.get("movieId")
        episode_id = r.get("episodeId")
        download_id = r.get("downloadId") or str(queue_id)

        try:
            _arr_delete(url, key, f"queue/{queue_id}", removeFromClient="true", blocklist="true")
        except Exception as e:
            return {"ok": False, "title": title, "error": f"cancel failed: {e}"}

        picked = None
        try:
            if app == "radarr" and movie_id:
                releases = _arr_get(url, key, "release", movieId=movie_id)
            elif app == "sonarr" and episode_id:
                releases = _arr_get(url, key, "release", episodeId=episode_id)
            else:
                releases = []
            candidates = [x for x in releases if not x.get("rejected")]
            prefer = "usenet" if protocol == "torrent" else "torrent"
            picked = next((x for x in candidates if x.get("protocol") == prefer), None) \
                or (candidates[0] if candidates else None)
            if picked:
                _grab_release(url, key, picked["guid"], picked["indexerId"])
        except Exception as e:
            logger.warning(f"[Download health] replacement search failed for '{title}': {e}")

        detail = (f"Cancelled stuck {protocol} download; replaced with {picked.get('protocol')} release: {picked.get('title')}"
                  if picked else f"Cancelled stuck {protocol} download; no alternative release found")
        log_deletion(db, kind="download-fix", name=title, size_bytes=r.get("size") or 0,
                     reason=reason, detail=detail)
        log_activity(db, "download_fix", f"Stuck download fixed: {title}" if picked
                     else f"Stuck download cancelled (no replacement found): {title}")
        _clear_stall_entry(db, download_id)
        return {"ok": True, "title": title, "replaced": bool(picked),
                "picked_title": picked.get("title") if picked else None,
                "picked_protocol": picked.get("protocol") if picked else None}


def remove_download(db, app: str, queue_id: int, delete_file: bool = True, reason: str = "manual") -> dict:
    """Dequeue WITHOUT blocklisting or re-grabbing — for finished downloads the
    arr will never import, or a user changing their mind."""
    url, key = _arr_conn(db, app)
    if not url or not key:
        return {"ok": False, "error": f"{app} not configured"}
    try:
        records = fetch_queue_records(url, key, app)
    except Exception as e:
        return {"ok": False, "error": f"queue fetch failed: {e}"}
    r = next((x for x in records if x.get("id") == queue_id), None)
    if not r:
        return {"ok": False, "error": "not found in queue (already removed?)"}
    title = r.get("title") or str(queue_id)
    try:
        _arr_delete(url, key, f"queue/{queue_id}",
                    removeFromClient="true" if delete_file else "false", blocklist="false")
    except Exception as e:
        return {"ok": False, "title": title, "error": str(e)}
    log_deletion(db, kind="download-fix", name=title, size_bytes=r.get("size") or 0, reason=reason,
                 detail="Removed from download queue" + (" and deleted the file" if delete_file else " (file left in place)"))
    _clear_stall_entry(db, r.get("downloadId") or str(queue_id))
    return {"ok": True, "title": title}


def attempt_manual_import(db, app: str, download_id: str,
                          movie_id: int = None, episode_id: int = None) -> dict:
    """Force-import a completed download the arr's automated pipeline declined —
    but only when its own Manual Import lookup finds exactly one candidate file
    matching the expected movie/episode. Anything ambiguous is left for a human."""
    url, key = _arr_conn(db, app)
    if not url or not key:
        return {"ok": False, "reason": f"{app} not configured"}

    candidates = _arr_get(url, key, "manualimport", downloadId=download_id)
    if len(candidates) != 1:
        return {"ok": False, "reason": f"{len(candidates)} candidate files found — needs manual review"}
    item = candidates[0]

    file = {
        "path": item["path"],
        "quality": item.get("quality"),
        "languages": item.get("languages") or [],
        "releaseGroup": item.get("releaseGroup"),
        "downloadId": download_id,
    }
    if app == "sonarr":
        series = item.get("series")
        episodes = item.get("episodes") or []
        if not series or len(episodes) != 1:
            return {"ok": False, "reason": "series/episode not confidently matched — needs manual review"}
        if episode_id is not None and episodes[0]["id"] != episode_id:
            return {"ok": False, "reason": "matched episode doesn't match what was originally grabbed"}
        if item.get("releaseType") not in (None, "singleEpisode"):
            return {"ok": False, "reason": f"{item.get('releaseType')} release — needs manual review"}
        file["seriesId"] = series["id"]
        file["episodeIds"] = [episodes[0]["id"]]
    else:
        movie = item.get("movie")
        if not movie:
            return {"ok": False, "reason": "movie not confidently matched — needs manual review"}
        if movie_id is not None and movie["id"] != movie_id:
            return {"ok": False, "reason": "matched movie doesn't match what was originally grabbed"}
        file["movieId"] = movie["id"]

    if not file.get("quality"):
        return {"ok": False, "reason": "quality could not be determined — needs manual review"}

    result = _arr_post(url, key, "command", {"name": "ManualImport", "importMode": "auto", "files": [file]})
    command_id = result.get("id")
    for _ in range(20):
        time.sleep(0.5)
        status = _arr_get(url, key, f"command/{command_id}")
        if status.get("status") == "completed":
            return {"ok": True, "title": item.get("name")}
        if status.get("status") in ("failed", "aborted", "cancelled"):
            return {"ok": False, "reason": status.get("exception") or f"import {status['status']}"}
    return {"ok": False, "reason": "still running after 10s — check Radarr/Sonarr directly"}


# ─── Background job (every 5 minutes) ─────────────────────────────────────────

def run_download_health_check():
    """Always advances stall tracking so the UI's phase stays accurate; only
    acts on stuck / import_blocked items when the corresponding toggle is on."""
    db = SessionLocal()
    try:
        update_stall_tracking(db)

        auto_fix = get_setting(db, "downloads_auto_fix_enabled", "false") == "true"
        auto_import = get_setting(db, "downloads_auto_import_enabled", "false") == "true"
        if not auto_fix and not auto_import:
            return

        stall_state = get_stall_state(db)
        for app in ("radarr", "sonarr"):
            url, key = _arr_conn(db, app)
            if not url or not key:
                continue
            try:
                records = fetch_queue_records(url, key, app)
            except Exception:
                continue
            for r in records:
                cls = classify_queue_item(r, stall_state)
                try:
                    if auto_fix and cls["status"] == "stuck":
                        result = resolve_stuck_download(db, app, r["id"], reason="auto")
                        if result.get("ok"):
                            logger.info(f"[Download health] auto-fixed: {result.get('title')}")
                    elif auto_import and cls["status"] == "import_blocked":
                        download_id = r.get("downloadId") or str(r["id"])
                        imported = attempt_manual_import(db, app, download_id,
                                                         movie_id=r.get("movieId"),
                                                         episode_id=r.get("episodeId"))
                        if imported.get("ok"):
                            log_activity(db, "download_fix",
                                         f"Blocked import force-imported: {r.get('title')}")
                            logger.info(f"[Download health] auto-imported: {r.get('title')}")
                        else:
                            result = remove_download(db, app, r["id"], delete_file=False, reason="auto")
                            if result.get("ok"):
                                logger.info(f"[Download health] blocked import dequeued (file kept): {result.get('title')}")
                except Exception:
                    logger.exception(f"[Download health] action failed for queue item {r.get('id')} ({app})")
    except Exception:
        logger.exception("[Download health] check failed")
    finally:
        db.close()

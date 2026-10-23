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
                                  protocol from the one that failed (or let
                                  the arr search: resolve_stuck_download)
  downloads_auto_import_enabled — for import_blocked items, force a ManualImport
                                  when the arr's own lookup finds exactly one
                                  unambiguous file the arr didn't refuse;
                                  otherwise dequeue but keep the file on disk
                                  for a human to sort out

A ManualImport skips the arr's import decisions and replaces the existing file
(#766), so a candidate with a permanent rejection ("Not an upgrade…", "Sample",
wrong series) is never imported by the sweep, and each download is tried once:
the tries are kept in the `downloads_auto_import_tried` Setting as
{download_id: {at, told}} for a week.
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

# Auto-fix does something Tentacle cannot undo: it cancels the queue item and
# BLOCKLISTS the release, so that release is never grabbed again. A hard fault
# fires with no elapsed-time threshold, and the messages a download client
# publishes when it goes away ("Unable to communicate with ...") land on EVERY
# item in the queue at once — so an unguarded sweep blocklists the whole queue
# on one client restart. Two guards, in the spirit of the provider-prune caps:
#   * never act on more than AUTO_FIX_MAX_PER_RUN items in one sweep
#   * when most of an eligible queue is stuck simultaneously, that is an
#     outage of the client or the arr, not of the releases — log and wait
AUTO_FIX_MAX_PER_RUN = 5
AUTO_FIX_OUTAGE_MIN_QUEUE = 4
AUTO_FIX_OUTAGE_FRACTION = 0.5
IMPORT_STAGE_STATES = {"importPending", "importBlocked", "imported", "importing"}
WAITING_STATUSES = {"queued", "paused", "delay"}
NOT_TRYING_STATUSES = {"paused", "delay"}  # never expected to resolve on their own — excluded from stall tracking

STALL_STATE_KEY = "downloads_stall_state"
IMPORT_TRIED_KEY = "downloads_auto_import_tried"
IMPORT_TRIED_DAYS = 7
IMPORT_VERIFY_TRIES = 3   # queue reads after the command, 1 s apart

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


def _grab_may_have_gone_through(e: Exception) -> bool:
    """A grab the arr answered with a 4xx or its own 500 (e.g. no download
    client for that protocol) was refused. No answer at all (timeout, reset)
    or a gateway's 502/503/504 may still have reached the download client."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return e.response.status_code in (502, 503, 504)
    return isinstance(e, requests.RequestException)


def _arr_search(url: str, key: str, app: str, movie_id, episode_id) -> bool:
    """Ask the arr to run its own automatic search for the title (what its
    "Redownload Failed" would have done). True when the command was accepted."""
    body = {"name": "MoviesSearch", "movieIds": [movie_id]} if app == "radarr" \
        else {"name": "EpisodeSearch", "episodeIds": [episode_id]}
    try:
        _arr_post(url, key, "command", body)
        return True
    except Exception as e:
        logger.warning(f"[Download health] {app} search command failed: {e}")
        return False


def resolve_stuck_download(db, app: str, queue_id: int, reason: str = "manual") -> dict:
    """Cancel + blocklist a stuck queue item, then grab the best alternative
    release, preferring the opposite protocol from the one that failed.

    Exactly one side looks for the replacement (#444). Removing with
    blocklist=true makes Radarr/Sonarr search again by themselves ("Redownload
    Failed", on by default), and that grab only shows in their queue seconds
    later, so Tentacle grabbing too downloads the title twice. When Tentacle
    picks the replacement it removes with skipRedownload=true; when it grabs
    nothing it asks the arr for its own search instead. A download of several
    episodes (a season pack: several queue records, one downloadId) is left to
    Sonarr's own re-search, which knows to look for the whole season."""
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
        arr_name = app.capitalize()

        several_episodes = app == "sonarr" and bool(r.get("downloadId")) and any(
            x.get("downloadId") == r.get("downloadId") and x.get("episodeId") != episode_id
            for x in records)
        tentacle_picks = not several_episodes and bool(movie_id if app == "radarr" else episode_id)

        delete_params = {"removeFromClient": "true", "blocklist": "true"}
        if tentacle_picks:
            delete_params["skipRedownload"] = "true"
        try:
            _arr_delete(url, key, f"queue/{queue_id}", **delete_params)
        except Exception as e:
            return {"ok": False, "title": title, "error": f"cancel failed: {e}"}

        picked = None
        searching = not tentacle_picks  # the arr re-searches by itself
        if tentacle_picks:
            try:
                if app == "radarr":
                    releases = _arr_get(url, key, "release", movieId=movie_id)
                else:
                    releases = _arr_get(url, key, "release", episodeId=episode_id)
                candidates = [x for x in releases if not x.get("rejected")]
                prefer = "usenet" if protocol == "torrent" else "torrent"
                picked = next((x for x in candidates if x.get("protocol") == prefer), None) \
                    or (candidates[0] if candidates else None)
            except Exception as e:
                logger.warning(f"[Download health] replacement search failed for '{title}': {e}")
            if picked:
                try:
                    _grab_release(url, key, picked["guid"], picked["indexerId"])
                except Exception as e:
                    logger.warning(f"[Download health] replacement grab for '{title}' failed: {e}")
                    if not _grab_may_have_gone_through(e):
                        picked = None
            if not picked:
                searching = _arr_search(url, key, app, movie_id, episode_id)

        if picked:
            detail = (f"Cancelled stuck {protocol} download; replaced with "
                      f"{picked.get('protocol')} release: {picked.get('title')}")
            activity = f"Stuck download fixed: {title}"
        elif searching:
            detail = f"Cancelled stuck {protocol} download; {arr_name} is searching for a replacement"
            activity = f"Stuck download cancelled ({arr_name} is searching for a replacement): {title}"
        else:
            detail = f"Cancelled stuck {protocol} download; no alternative release found"
            activity = f"Stuck download cancelled (no replacement found): {title}"
        log_deletion(db, kind="download-fix", name=title, size_bytes=r.get("size") or 0,
                     reason=reason, detail=detail)
        log_activity(db, "download_fix", activity)
        _clear_stall_entry(db, download_id)
        return {"ok": True, "title": title, "replaced": bool(picked),
                "searching": bool(searching and not picked),
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


def _rejections(item: dict) -> tuple:
    """(permanent, temporary) refusal reasons of a Manual Import candidate.
    Older arrs list plain strings; those count as permanent."""
    permanent, temporary = [], []
    for r in item.get("rejections") or []:
        if isinstance(r, dict):
            reason = r.get("reason") or "refused"
            (temporary if (r.get("type") or "").lower() == "temporary" else permanent).append(reason)
        elif r:
            permanent.append(str(r))
    return permanent, temporary


def _still_blocked(url: str, key: str, app: str, download_id: str):
    """After a completed ManualImport: the arr's own message when the download
    is still import-blocked in its queue, else None (imported, or importing)."""
    for n in range(IMPORT_VERIFY_TRIES):
        if n:
            time.sleep(1)
        records = fetch_queue_records(url, key, app)
        r = next((x for x in records if (x.get("downloadId") or str(x.get("id"))) == download_id), None)
        if not r or r.get("trackedDownloadState") != "importBlocked":
            return None
    msgs = [m for sm in (r.get("statusMessages") or []) for m in (sm.get("messages") or [])]
    return r.get("errorMessage") or (msgs[0] if msgs else "still blocked")


def attempt_manual_import(db, app: str, download_id: str,
                          movie_id: int = None, episode_id: int = None,
                          allow_rejected: bool = False) -> dict:
    """Force-import a completed download the arr's automated pipeline declined —
    but only when its own Manual Import lookup finds exactly one candidate file
    matching the expected movie/episode. Anything ambiguous is left for a human.

    A candidate the arr refused for good (e.g. "Not an upgrade for existing
    episode file(s)") answers {"ok": False, "refused": [reasons]} unless
    allow_rejected (the admin confirmed on the Health page): a manual import
    replaces the file already in the library. Temporary refusals answer
    "wait": the arr retries those itself. "sent" is set once the ManualImport
    command went out; ok is True only when the download then left the blocked
    state."""
    url, key = _arr_conn(db, app)
    if not url or not key:
        return {"ok": False, "reason": f"{app} not configured"}

    candidates = _arr_get(url, key, "manualimport", downloadId=download_id)
    if len(candidates) != 1:
        return {"ok": False, "reason": f"{len(candidates)} candidate files found — needs manual review"}
    item = candidates[0]
    arr_name = app.capitalize()
    permanent, temporary = _rejections(item)
    if permanent and not allow_rejected:
        return {"ok": False, "refused": permanent,
                "reason": f"{arr_name} refused it: {'; '.join(permanent)}"}
    if temporary and not permanent and not allow_rejected:
        return {"ok": False, "wait": True,
                "reason": f"{arr_name} will retry it itself: {'; '.join(temporary)}"}

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
    title = item.get("name")
    try:
        command_id = result.get("id")
        for _ in range(20):
            time.sleep(0.5)
            status = _arr_get(url, key, f"command/{command_id}")
            if status.get("status") == "completed":
                # "Completed" isn't "imported": the arr catches a file's import
                # failure inside the command, and the download stays blocked.
                blocked = _still_blocked(url, key, app, download_id)
                if blocked:
                    return {"ok": False, "sent": True, "title": title,
                            "reason": f"{arr_name} ran the import but the download is still blocked: {blocked}"}
                return {"ok": True, "sent": True, "title": title}
            if status.get("status") in ("failed", "aborted", "cancelled"):
                return {"ok": False, "sent": True, "title": title,
                        "reason": status.get("exception") or f"import {status['status']}"}
    except Exception as e:
        return {"ok": False, "sent": True, "title": title, "reason": f"couldn't check the import: {e}"}
    return {"ok": False, "sent": True, "title": title,
            "reason": f"still running after 10s — check {arr_name} directly"}


# ─── Background job (every 5 minutes) ─────────────────────────────────────────

def get_import_tried(db) -> dict:
    """{download_id: {"at", "told"}} of the sweep's force-imports, kept a week."""
    try:
        tried = json.loads(get_setting(db, IMPORT_TRIED_KEY, "") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    now = datetime.utcnow()
    kept = {}
    for download_id, entry in tried.items():
        try:
            if (now - datetime.fromisoformat(entry["at"])).days < IMPORT_TRIED_DAYS:
                kept[download_id] = entry
        except (KeyError, TypeError, ValueError):
            pass
    return kept


def _save_import_tried(db, tried: dict):
    set_setting(db, IMPORT_TRIED_KEY, json.dumps(tried))


def _auto_import_blocked(db, app: str, r: dict):
    """The sweep's one go at a blocked download: import it once, or leave it
    for the admin with the reason in Activity."""
    download_id = r.get("downloadId") or str(r["id"])
    title = r.get("title")
    tried = get_import_tried(db)
    entry = tried.get(download_id)
    if entry:
        if not entry.get("told"):
            entry["told"] = True
            _save_import_tried(db, tried)
            log_activity(db, "download_fix",
                         f"Blocked import left for you (auto-import already tried it once): {title}")
        return

    imported = attempt_manual_import(db, app, download_id,
                                     movie_id=r.get("movieId"), episode_id=r.get("episodeId"))
    if imported.get("sent"):
        ok = bool(imported.get("ok"))
        tried[download_id] = {"at": datetime.utcnow().isoformat(), "told": not ok}
        _save_import_tried(db, tried)
        if ok:
            log_activity(db, "download_fix", f"Blocked import force-imported: {title}")
            logger.info(f"[Download health] auto-imported: {title}")
        else:
            log_activity(db, "download_fix",
                         f"Blocked import not imported, left for you: {title} — {imported.get('reason')}")
            logger.warning(f"[Download health] auto-import of '{title}' didn't take: {imported.get('reason')}")
        return
    if imported.get("wait"):
        logger.info(f"[Download health] blocked import left to {app}: {title} — {imported.get('reason')}")
        return
    result = remove_download(db, app, r["id"], delete_file=False, reason="auto")
    if result.get("ok"):
        log_activity(db, "download_fix",
                     f"Blocked import left for review (file kept): {title} — {imported.get('reason')}")
        logger.info(f"[Download health] blocked import dequeued (file kept): {title} — {imported.get('reason')}")


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
            classified = [(r, classify_queue_item(r, stall_state)) for r in records]
            stuck_total = sum(1 for _, c in classified if c["status"] == "stuck")

            app_auto_fix = auto_fix
            if (auto_fix and len(records) >= AUTO_FIX_OUTAGE_MIN_QUEUE
                    and stuck_total >= len(records) * AUTO_FIX_OUTAGE_FRACTION):
                msg = (f"{stuck_total}/{len(records)} {app} queue items are stuck at "
                       f"once — treating this as a download client / {app} outage and "
                       f"skipping auto-fix this sweep")
                logger.error(f"[Download health] {msg}")
                log_activity(db, "download_fix_skipped", msg.capitalize())
                app_auto_fix = False

            fixed_this_run = 0
            for r, cls in classified:
                try:
                    if app_auto_fix and cls["status"] == "stuck":
                        if fixed_this_run >= AUTO_FIX_MAX_PER_RUN:
                            logger.warning(
                                f"[Download health] auto-fix cap of {AUTO_FIX_MAX_PER_RUN} "
                                f"reached for {app} this sweep — {stuck_total - fixed_this_run} "
                                f"stuck item(s) left for the next run")
                            break
                        result = resolve_stuck_download(db, app, r["id"], reason="auto")
                        fixed_this_run += 1
                        if result.get("ok"):
                            logger.info(f"[Download health] auto-fixed: {result.get('title')}")
                    elif auto_import and cls["status"] == "import_blocked":
                        _auto_import_blocked(db, app, r)
                except Exception:
                    logger.exception(f"[Download health] action failed for queue item {r.get('id')} ({app})")
    except Exception:
        logger.exception("[Download health] check failed")
    finally:
        db.close()

"""What Radarr/Sonarr know that nobody sees unless they open them.

- Release checks: why a title in Activity → Searching has not downloaded.
  Radarr/Sonarr don't keep the reasons from their automatic searches, so a
  check runs their interactive search (every indexer, live) and sums up what
  came back: nothing at all, usable releases, or everything rejected and why.
  The same list lets someone pick a release by hand. Checks are cached (an
  interactive search hits every indexer) and run in the background for titles
  that have been searching for a while.
- Problems: Radarr/Sonarr's own health list (indexers failing, download client
  unreachable, root folder missing) plus low disk space — the usual causes of
  "it just never downloads".
- Coming up: the week's episodes of monitored shows, from Sonarr's calendar.
"""
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import get_setting

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT = 180          # an interactive search waits on every indexer
CHECK_TTL = 12 * 3600         # a check is good for this long in Activity
GRAB_FRESH = 25 * 60          # Radarr/Sonarr keep search results ~30 min for grabs
AUTO_CHECK_AFTER = 45 * 60    # give the automatic search a chance first
AUTO_CHECKS_PER_RUN = 2
MAX_RELEASES = 40

_checks: dict = {}            # title key -> {"at": ts, "data": {...}}
_checks_lock = threading.Lock()
_running: set = set()


class InsightError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def title_key(media_type: str, tmdb_id: int = 0, tvdb_id: int = 0) -> str:
    kind = "series" if media_type == "series" else "movie"
    return f"{kind}:tmdb:{tmdb_id}" if tmdb_id else f"{kind}:tvdb:{tvdb_id}"


def _conn(db: Session, app: str) -> tuple:
    return (get_setting(db, f"{app}_url") or "").rstrip("/"), get_setting(db, f"{app}_api_key") or ""


def _get(url: str, key: str, path: str, timeout: float = 15, **params):
    r = requests.get(f"{url}/api/v3/{path}", headers={"X-Api-Key": key}, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── Release checks ─────────────────────────────────────────────────────────

# Radarr/Sonarr word rejections precisely but tersely ("720p is not wanted in
# profile"); group them into a few reasons a person can act on.
_REASONS = [
    ("delay", r"delay", "waiting out your delay profile"),
    ("blocklist", r"blocklist", "blocklisted"),
    ("existing", r"existing file|not an upgrade|already meets cutoff|is not a .*upgrade", "not better than what you have"),
    # Language before quality: both say "... is not wanted in profile".
    ("language", r"language", "wrong language"),
    ("quality", r"not wanted in profile|quality", "quality not in your profile"),
    ("size", r"size|too large|too small", "wrong size"),
    ("seeders", r"seeder|peers", "not enough seeders"),
    ("format", r"custom format", "custom format score too low"),
    ("age", r"retention|older than|minimum age|age", "too old or too new"),
    ("match", r"unknown|wasn't requested|not requested|does not match|doesn't match|unable to|parse|wrong",
     "doesn't match this title"),
]


def reason_of(text: str) -> tuple:
    t = (text or "").lower()
    for key, pattern, label in _REASONS:
        if re.search(pattern, t):
            return key, label
    return "other", "other reasons"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _release_entry(r: dict) -> dict:
    quality = (r.get("quality") or {}).get("quality") or {}
    rejections = [x if isinstance(x, str) else (x.get("reason") or x.get("message") or "")
                  for x in (r.get("rejections") or [])]
    reasons = []
    for text in rejections:
        label = reason_of(text)[1]
        if label not in reasons:
            reasons.append(label)
    return {
        "title": r.get("title") or "",
        "quality": quality.get("name") or "",
        "resolution": quality.get("resolution") or 0,
        "size_bytes": r.get("size") or 0,
        "seeders": r.get("seeders"),
        "leechers": r.get("leechers"),
        "protocol": r.get("protocol") or "",
        "indexer": r.get("indexer") or "",
        "languages": ", ".join(l.get("name", "") for l in (r.get("languages") or []) if l.get("name")),
        "age_days": r.get("age"),
        "rejected": bool(r.get("rejected")),
        "temporarily_rejected": bool(r.get("temporarilyRejected")),
        "reasons": reasons,
        "raw_reasons": [x for x in rejections if x],
        "guid": r.get("guid"),
        "indexer_id": r.get("indexerId"),
    }


def summarize(releases: list, arr: str) -> dict:
    """What a list of releases means for someone waiting on the title."""
    entries = [_release_entry(r) for r in releases]
    usable = [e for e in entries if not e["rejected"]]
    delayed = [e for e in entries if e["rejected"] and e["temporarily_rejected"]]
    counts: dict = {}
    for e in entries:
        if e["rejected"]:
            for label in e["reasons"] or ["other reasons"]:
                counts[label] = counts.get(label, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    # "Best was ..." is about quality, so only among releases turned down for it.
    best = max((e for e in entries if e["resolution"] and "quality not in your profile" in e["reasons"]),
               key=lambda e: e["resolution"], default=None)

    if not entries:
        state, short = "none", "No releases found"
        summary = ("No releases found. Nobody seems to have uploaded it yet, "
                   "or your indexers didn't return it.")
    elif usable:
        state, short = "usable", f"{_plural(len(usable), 'usable release')} found"
        summary = f"{_plural(len(usable), 'usable release')} found. {arr} should grab one shortly."
    elif delayed and len(delayed) == len(entries):
        state, short = "delayed", "Waiting out your delay profile"
        summary = (f"Found {_plural(len(entries), 'release')}, held back by your delay profile. "
                   f"{arr} will grab one when the delay ends.")
    else:
        state = "rejected"
        first = top[0][0] if top else "other reasons"
        short = f"None usable: {first}"
        parts = []
        for label, n in top[:3]:
            part = f"{n} {label}"
            if label == "quality not in your profile" and best:
                part += f" (best was {best['quality']})"
            parts.append(part)
        summary = f"Found {_plural(len(entries), 'release')}, none usable: " + ", ".join(parts) + "."

    # Usable first (in Radarr/Sonarr's own preference order), then the
    # rejected ones best quality first.
    ordered = usable + sorted((e for e in entries if e["rejected"]),
                              key=lambda e: (-e["resolution"], -(e["seeders"] or 0)))
    return {
        "state": state, "short": short, "summary": summary,
        "total": len(entries), "usable": len(usable),
        "reasons": [{"label": label, "count": n} for label, n in top],
        "best_quality": best["quality"] if best else None,
        "releases": ordered[:MAX_RELEASES],
    }


def _target(db: Session, media_type: str, tmdb_id: int, tvdb_id: int) -> tuple:
    """(app, url, key, release params, scope label, arr record)."""
    from routers.activity import ArrTitle, _find_arr_record, _missing_aired, _ep_label
    from fastapi import HTTPException
    try:
        svc, rec = _find_arr_record(db, ArrTitle(media_type=media_type, tmdb_id=tmdb_id, tvdb_id=tvdb_id))
    except HTTPException as e:
        raise InsightError(e.status_code, e.detail)
    if media_type == "movie":
        url, key = _conn(db, "radarr")
        return "Radarr", url, key, {"movieId": rec["id"]}, rec.get("title", ""), rec
    url, key = _conn(db, "sonarr")
    missing = _missing_aired(svc.get_episodes(rec["id"]))
    if not missing:
        raise InsightError(409, "Sonarr isn't looking for any episodes of this show")
    # The newest missing episode: the one someone is most likely waiting for.
    ep = max(missing, key=lambda e: e.get("airDateUtc") or "")
    return "Sonarr", url, key, {"episodeId": ep["id"]}, _ep_label(ep), rec


def check(db: Session, media_type: str, tmdb_id: int = 0, tvdb_id: int = 0,
          max_age: Optional[float] = None) -> dict:
    """Search now (or reuse a check younger than max_age) and sum it up."""
    k = title_key(media_type, tmdb_id, tvdb_id)
    ttl = CHECK_TTL if max_age is None else max_age
    with _checks_lock:
        hit = _checks.get(k)
        if hit and time.time() - hit["at"] < ttl:
            return hit["data"]
    app, url, key, params, scope, rec = _target(db, media_type, tmdb_id, tvdb_id)
    started = time.time()
    try:
        releases = _get(url, key, "release", timeout=SEARCH_TIMEOUT, **params)
    except requests.Timeout:
        raise InsightError(504, f"{app}'s indexers took too long to answer. Try again in a minute.")
    except Exception as e:
        logger.warning(f"[Insight] Release search for '{rec.get('title')}' failed: {e}")
        raise InsightError(502, f"{app} couldn't search right now")
    data = summarize(releases if isinstance(releases, list) else [], app)
    data.update(scope=scope if media_type == "series" else "",
                checked_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                took_seconds=round(time.time() - started, 1))
    with _checks_lock:
        _checks[k] = {"at": time.time(), "data": data}
    logger.info(f"[Insight] Checked '{rec.get('title')}' {scope}: {data['summary']}")
    return data


def cached_line(media_type: str, tmdb_id: int = 0, tvdb_id: int = 0) -> Optional[dict]:
    """The short result of the last check, for the Searching card."""
    with _checks_lock:
        hit = _checks.get(title_key(media_type, tmdb_id, tvdb_id))
    if not hit or time.time() - hit["at"] >= CHECK_TTL:
        return None
    d = hit["data"]
    return {"state": d["state"], "short": d["short"], "summary": d["summary"], "checked_at": d["checked_at"]}


def forget(media_type: str, tmdb_id: int = 0, tvdb_id: int = 0) -> None:
    with _checks_lock:
        _checks.pop(title_key(media_type, tmdb_id, tvdb_id), None)


def grab(db: Session, media_type: str, tmdb_id: int, tvdb_id: int, guid: str, indexer_id: int) -> dict:
    """Download one release from a check, even one the profile rejected."""
    app = "radarr" if media_type == "movie" else "sonarr"
    url, key = _conn(db, app)
    if not (url and key):
        raise InsightError(503, f"{app.capitalize()} is not configured")
    try:
        r = requests.post(f"{url}/api/v3/release", headers={"X-Api-Key": key},
                          json={"guid": guid, "indexerId": indexer_id}, timeout=60)
    except Exception as e:
        raise InsightError(502, f"Couldn't reach {app.capitalize()}: {e}")
    if r.status_code == 404:
        forget(media_type, tmdb_id, tvdb_id)
        raise InsightError(409, "That list is too old to download from. Check again for a fresh one.")
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            detail = body.get("message") if isinstance(body, dict) else (body[0].get("errorMessage") if body else "")
        except Exception:
            pass
        raise InsightError(502, f"{app.capitalize()} refused it" + (f": {detail}" if detail else ""))
    forget(media_type, tmdb_id, tvdb_id)
    return {"ok": True, "message": f"Sent to your download client. It will show under Downloading shortly."}


def run_auto_checks() -> None:
    """Check titles that have been searching a while, a couple per run."""
    from models.database import SessionLocal
    from routers.activity import _get_wanted
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        done = 0
        for item in list(_get_wanted(db).get("searching") or []):
            if done >= AUTO_CHECKS_PER_RUN:
                break
            mt, tmdb, tvdb = item.get("media_type"), item.get("tmdb_id") or 0, item.get("tvdb_id") or 0
            if cached_line(mt, tmdb, tvdb) is not None:
                continue
            try:
                since = datetime.strptime(item.get("waiting_since") or "", "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            if now - since < timedelta(seconds=AUTO_CHECK_AFTER):
                continue
            k = title_key(mt, tmdb, tvdb)
            if k in _running:
                continue
            _running.add(k)
            try:
                check(db, mt, tmdb, tvdb)
            except InsightError as e:
                logger.info(f"[Insight] Auto-check of '{item.get('title')}' skipped: {e}")
            except Exception as e:
                logger.warning(f"[Insight] Auto-check of '{item.get('title')}' failed: {e}")
            finally:
                _running.discard(k)
            done += 1
    finally:
        db.close()


# ── Problems ──────────────────────────────────────────────────────────────

PROBLEMS_TTL = 60
LOW_DISK_BYTES = 20 * 1024 ** 3
LOW_DISK_FRACTION = 0.05
_problems_cache: dict = {"at": 0, "data": None}

# Health sources that stop downloads from happening.
_KINDS = [("indexer", "indexer"), ("downloadclient", "download_client"), ("rootfolder", "root_folder"),
          ("remotepath", "download_client"), ("importmechanism", "download_client")]


def _kind_of(source: str) -> str:
    s = (source or "").lower()
    for needle, kind in _KINDS:
        if needle in s:
            return kind
    return "other"


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.0f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def _disk_problems(url: str, key: str, app: str) -> list:
    out = []
    roots = [f.get("path") or "" for f in _get(url, key, "rootfolder")]
    disks = _get(url, key, "diskspace")
    for root in roots:
        disk = max((d for d in disks if root.startswith((d.get("path") or "\0").rstrip("/") + "/")
                    or root == d.get("path")), key=lambda d: len(d.get("path") or ""), default=None)
        if not disk or not disk.get("totalSpace"):
            continue
        free, total = disk.get("freeSpace") or 0, disk["totalSpace"]
        if free < LOW_DISK_BYTES or free / total < LOW_DISK_FRACTION:
            out.append({"app": app, "kind": "disk", "level": "error" if free < LOW_DISK_BYTES / 4 else "warning",
                        "message": f"Only {_gb(free)} free on the disk for {root}. Downloads will fail when it fills up.",
                        "disk": disk.get("path")})
    return out


def problems(db: Session) -> list:
    """Warnings and errors from Radarr/Sonarr's health list, plus low disk space."""
    now = time.time()
    if _problems_cache["data"] is not None and now - _problems_cache["at"] < PROBLEMS_TTL:
        return _problems_cache["data"]
    out, seen_disks = [], set()
    for app_key, app in (("radarr", "Radarr"), ("sonarr", "Sonarr")):
        url, key = _conn(db, app_key)
        if not (url and key):
            continue
        try:
            health = _get(url, key, "health", timeout=10)
        except Exception as e:
            out.append({"app": app, "kind": "unreachable", "level": "error",
                        "message": f"Tentacle can't reach {app}, so nothing new will download.",
                        "detail": str(e)[:200]})
            continue
        for h in health or []:
            level = (h.get("type") or "").lower()
            if level not in ("warning", "error"):
                continue
            if "update" in (h.get("source") or "").lower():
                continue
            out.append({"app": app, "kind": _kind_of(h.get("source")), "level": level,
                        "message": h.get("message") or "", "wiki": h.get("wikiUrl") or ""})
        try:
            for p in _disk_problems(url, key, app):
                if p["disk"] not in seen_disks:
                    seen_disks.add(p["disk"])
                    out.append(p)
        except Exception as e:
            logger.debug(f"[Insight] {app} disk space check failed: {e}")
    _problems_cache.update(at=now, data=out)
    return out


def searching_problems(db: Session) -> list:
    """The problems that explain titles stuck in Searching."""
    return [p for p in problems(db) if p["kind"] in ("indexer", "download_client", "root_folder", "disk", "unreachable")]


# ── Coming up ─────────────────────────────────────────────────────────────

CALENDAR_TTL = 600
CALENDAR_DAYS = 7
_calendar_cache: dict = {"at": 0, "data": None}


def coming_up(db: Session) -> list:
    """Episodes of monitored shows airing in the next week, soonest first."""
    now = time.time()
    if _calendar_cache["data"] is not None and now - _calendar_cache["at"] < CALENDAR_TTL:
        return _calendar_cache["data"]
    url, key = _conn(db, "sonarr")
    if not (url and key):
        return []
    from routers.activity import _extract_poster
    start = datetime.utcnow()
    try:
        eps = _get(url, key, "calendar", timeout=15, includeSeries="true", unmonitored="false",
                   start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   end=(start + timedelta(days=CALENDAR_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    except Exception as e:
        logger.debug(f"[Insight] Sonarr calendar failed: {e}")
        return _calendar_cache["data"] or []
    out = []
    for ep in eps or []:
        series = ep.get("series") or {}
        if not ep.get("monitored", True) or series.get("monitored") is False or ep.get("hasFile"):
            continue
        out.append({
            "tmdb_id": series.get("tmdbId") or 0,
            "tvdb_id": series.get("tvdbId") or 0,
            "title": series.get("title", ""),
            "media_type": "series",
            "episode": f"S{ep.get('seasonNumber') or 0:02d}E{ep.get('episodeNumber') or 0:02d}",
            "episode_title": ep.get("title") or "",
            "air_date_utc": ep.get("airDateUtc"),
            "sonarr_poster": _extract_poster(series),
        })
    out.sort(key=lambda x: x.get("air_date_utc") or "")
    _calendar_cache.update(at=now, data=out)
    return out

"""
Tentacle - Stream Health
Detects dead VOD .strm files — streams the IPTV provider no longer serves.

Checks are two-tier:
  Movies   — authoritative: ask the provider's own catalog via Xtream
             get_vod_info (the stream id embedded in the .strm URL is the
             vod_id the API expects). Catalog says gone = dead.
  Episodes — the id in an episode URL is the EPISODE's stream id, not the
             series_id get_series_info wants, so there's no cheap catalog
             lookup; fall back to a streamed range-GET probe (read one chunk,
             never buffer a whole movie) with the provider's user agent.

Inconclusive results (network error, malformed URL, provider down) are NEVER
treated as dead — only a definitive negative marks an entry bad. Known-bad
entries are rechecked after KNOWN_BAD_RECHECK_DAYS and auto-cleared if the
stream recovered.

The daily sweep probes a rotating batch of healthy items (cursor + batch size
persisted in settings) so a large catalog gets full coverage over time without
hammering the provider.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

from models.database import (
    SessionLocal, get_setting, set_setting,
    Movie, Series, Provider, StreamHealth,
    log_activity, log_deletion,
)

logger = logging.getLogger(__name__)

KNOWN_BAD_RECHECK_DAYS = 7
DEFAULT_BATCH_SIZE = 100

# Direct provider URLs as written by the VOD sync:
#   http://server/movie/{user}/{pass}/{stream_id}.mp4
#   http://server/series/{user}/{pass}/{episode_id}.mp4
_STREAM_ID_RE = re.compile(r"/(movie|series)/[^/]+/[^/]+/(\d+)\.")


def _read_strm(strm_path: str):
    try:
        return Path(strm_path).read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def _parse_stream_id(url: str):
    m = _STREAM_ID_RE.search(url or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


def _probe_url(url: str, user_agent: str) -> bool | None:
    """Streamed range-GET reachability check. True = alive, False = definitively
    dead (HTTP error status / empty body), None = inconclusive."""
    try:
        with requests.get(url, headers={"Range": "bytes=0-65535", "User-Agent": user_agent},
                          timeout=(10, 15), stream=True, allow_redirects=True) as r:
            if r.status_code in (200, 206):
                for chunk in r.iter_content(65536):
                    return len(chunk) > 0
                return False
            if 400 <= r.status_code < 500:
                return False
            return None  # 5xx — provider hiccup, not proof the content is gone
    except requests.RequestException:
        return None


def check_stream(db, media_type: str, kind: str, stream_id: int, url: str, provider: Provider) -> bool | None:
    """True = alive, False = dead, None = couldn't determine."""
    user_agent = (provider.user_agent if provider else None) or "TiviMate/4.7.0 (Linux; Android 12)"
    # Movies: catalog check is authoritative and cheap
    if kind == "movie" and provider and provider.provider_type == "xtream":
        try:
            from services.xtream_client import XtreamClient
            client = XtreamClient(provider.server_url, provider.username, provider.password,
                                  user_agent=user_agent, timeout=20)
            try:
                data = client.get_vod_info(stream_id)
            finally:
                client.close()
            if isinstance(data, dict):
                info = data.get("info")
                # Providers signal "unknown vod_id" with an empty/missing info block
                return bool(info)
        except Exception as e:
            logger.debug(f"[Stream health] catalog check inconclusive for vod {stream_id}: {e}")
    return _probe_url(url, user_agent)


def _mark_bad(db, media_type: str, tmdb_id: int, title: str, episode: str,
              strm_path: str, stream_url: str):
    entry = db.query(StreamHealth).filter(StreamHealth.strm_path == strm_path).first()
    now = datetime.utcnow()
    if entry:
        entry.fail_count = (entry.fail_count or 1) + 1
        entry.last_checked_at = now
    else:
        db.add(StreamHealth(
            media_type=media_type, tmdb_id=tmdb_id, title=title, episode=episode,
            strm_path=strm_path, stream_url=stream_url,
            fail_count=1, first_failed_at=now, last_checked_at=now,
        ))
    db.commit()


def _provider_map(db) -> dict:
    return {p.id: p for p in db.query(Provider).all()}


def _series_sample_strm(show_dir: str):
    """First .strm under a show folder — a cheap liveness sample. If the
    provider dropped the show, every episode URL dies together."""
    try:
        root = Path(show_dir)
        if not root.is_dir():
            return None
        for f in sorted(root.rglob("*.strm")):
            return str(f)
    except OSError:
        pass
    return None


def _check_item(db, item, media_type: str, providers: dict) -> bool | None:
    """Check one Movie/Series row. Returns alive/dead/inconclusive; records
    dead results in the registry."""
    if media_type == "movie":
        strm_path = item.strm_path
        episode = None
    else:
        strm_path = _series_sample_strm(item.strm_path) if item.strm_path else None
        episode = None
        if strm_path:
            m = re.search(r"S(\d{2})E(\d{2})", Path(strm_path).stem, re.IGNORECASE)
            if m:
                episode = f"S{m.group(1)}E{m.group(2)}"
    if not strm_path or not Path(strm_path).exists():
        return None
    url = _read_strm(strm_path)
    if not url:
        return None
    kind, stream_id = _parse_stream_id(url)
    provider = providers.get(item.provider_id)
    alive = check_stream(db, media_type, kind, stream_id, url, provider)
    if alive is False:
        _mark_bad(db, media_type, item.tmdb_id, item.title, episode, strm_path, url)
    return alive


def check_title(db, media_type: str, tmdb_id: int) -> dict:
    """On-demand check for one library title."""
    model = Movie if media_type == "movie" else Series
    item = db.query(model).filter(model.tmdb_id == tmdb_id).first()
    if not item or not item.strm_path:
        return {"ok": False, "error": "No VOD record with a .strm path for this title"}
    alive = _check_item(db, item, media_type, _provider_map(db))
    return {"ok": True, "title": item.title,
            "result": "alive" if alive else ("dead" if alive is False else "inconclusive")}


def recheck_known_bad(db) -> dict:
    """Re-test known-bad entries; clear the ones that recovered or whose files
    are gone (nothing left to track)."""
    providers = _provider_map(db)
    cleared, rechecked = [], 0
    for entry in db.query(StreamHealth).all():
        if not Path(entry.strm_path).exists():
            cleared.append(entry.title)
            db.delete(entry)
            continue
        url = entry.stream_url or _read_strm(entry.strm_path)
        if not url:
            continue
        kind, stream_id = _parse_stream_id(url)
        # Look up provider through the library record (creds may have rotated)
        model = Movie if entry.media_type == "movie" else Series
        item = db.query(model).filter(model.tmdb_id == entry.tmdb_id).first()
        provider = providers.get(item.provider_id) if item else None
        rechecked += 1
        alive = check_stream(db, entry.media_type, kind, stream_id, url, provider)
        entry.last_checked_at = datetime.utcnow()
        if alive is True:
            cleared.append(entry.title)
            db.delete(entry)
        elif alive is False:
            entry.fail_count = (entry.fail_count or 1) + 1
    db.commit()
    return {"rechecked": rechecked, "cleared": cleared}


def run_stream_health_sweep():
    """Daily job: recheck stale known-bad entries, then probe the next rotating
    batch of healthy VOD titles."""
    db = SessionLocal()
    try:
        stats = {"rechecked": 0, "cleared": 0, "probed": 0, "new_bad": 0, "inconclusive": 0}

        # 1. Recheck known-bad entries older than the recheck window
        cutoff = datetime.utcnow() - timedelta(days=KNOWN_BAD_RECHECK_DAYS)
        stale = db.query(StreamHealth).filter(StreamHealth.last_checked_at < cutoff).count()
        if stale:
            result = recheck_known_bad(db)
            stats["rechecked"] = result["rechecked"]
            stats["cleared"] = len(result["cleared"])

        # 2. Probe the next batch of healthy items (movies + series interleaved,
        #    cursor per type so both make progress every night)
        try:
            batch_size = int(get_setting(db, "stream_health_batch_size", str(DEFAULT_BATCH_SIZE)))
        except ValueError:
            batch_size = DEFAULT_BATCH_SIZE
        providers = _provider_map(db)
        bad_paths = {e.strm_path for e in db.query(StreamHealth.strm_path).all()}

        for media_type, model in (("movie", Movie), ("series", Series)):
            per_type = max(1, batch_size // 2)
            cursor_key = f"stream_health_cursor_{media_type}"
            try:
                cursor = int(get_setting(db, cursor_key, "0"))
            except ValueError:
                cursor = 0
            items = (db.query(model)
                     .filter(model.source.like("provider_%"), model.strm_path.isnot(None))
                     .order_by(model.id)
                     .all())
            if not items:
                continue
            if cursor >= len(items):
                cursor = 0
            batch = items[cursor:cursor + per_type]
            for item in batch:
                if item.strm_path in bad_paths:
                    continue
                alive = _check_item(db, item, media_type, providers)
                stats["probed"] += 1
                if alive is False:
                    stats["new_bad"] += 1
                elif alive is None:
                    stats["inconclusive"] += 1
            set_setting(db, cursor_key, str(cursor + len(batch)))

        set_setting(db, "stream_health_last_run", json.dumps({
            "at": datetime.utcnow().isoformat(), **stats,
        }))
        if stats["new_bad"]:
            log_activity(db, "stream_health",
                         f"Stream health sweep: {stats['new_bad']} dead stream(s) found "
                         f"({stats['probed']} probed)")
        logger.info(f"[Stream health] sweep complete: {stats}")
    except Exception:
        logger.exception("[Stream health] sweep failed")
    finally:
        db.close()


def remove_dead_stream(db, entry_id: int, user_name: str = None) -> dict:
    """Delete the dead .strm (+ .nfo / DB record for movies) and drop the
    registry entry."""
    entry = db.query(StreamHealth).filter(StreamHealth.id == entry_id).first()
    if not entry:
        return {"ok": False, "error": "Entry not found"}

    strm = Path(entry.strm_path)
    try:
        if strm.exists():
            strm.unlink()
            nfo = strm.with_suffix(".nfo")
            if nfo.exists():
                nfo.unlink()
            if strm.parent.exists() and not any(strm.parent.iterdir()):
                strm.parent.rmdir()
    except OSError as e:
        return {"ok": False, "error": f"File delete failed: {e}"}

    detail = "Dead stream removed — .strm/.nfo deleted"
    if entry.media_type == "movie":
        movie = db.query(Movie).filter(Movie.tmdb_id == entry.tmdb_id).first()
        if movie and movie.strm_path == entry.strm_path:
            db.delete(movie)
            detail += " along with the library record"
    # Series entries only remove the sampled episode file — the show record
    # stays; remaining episodes get their own entries if they're dead too.

    label = entry.title + (f" {entry.episode}" if entry.episode else "")
    log_deletion(db, kind="stream-health", name=label, media_type=entry.media_type,
                 reason="manual", user_name=user_name, detail=detail)
    db.delete(entry)
    db.commit()
    return {"ok": True, "title": label}

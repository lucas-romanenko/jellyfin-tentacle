"""GET /api/music/status: cheap enough to poll.

Everything comes from the database and memory. Integration health is cached
and refreshed in the background at most every ten minutes, so a monitor
polling every minute never makes Tentacle call Lidarr, MusicBrainz or the
players.
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import func

from models.database import MusicAlbum, get_setting

logger = logging.getLogger(__name__)

HEALTH_TTL = 600
_health = {"at": 0.0, "checked_at": None, "data": {}, "refreshing": False}
_health_lock = threading.Lock()


def _check_integrations() -> dict:
    from models.database import SessionLocal
    from services import service_checks
    from services.lidarr import LidarrClient, LidarrError
    from services.music.players import enabled_players
    db = SessionLocal()
    out = {}
    try:
        url, key = get_setting(db, "lidarr_url"), get_setting(db, "lidarr_api_key")
        if url and key:
            try:
                status = LidarrClient(url, key).system_status()
                out["lidarr"] = {"ok": True, "detail": f"Lidarr {status.get('version') or ''}".strip()}
            except LidarrError as e:
                out["lidarr"] = {"ok": False, "detail": e.message}
        else:
            out["lidarr"] = {"ok": False, "detail": "not configured"}
        mb = service_checks.check_musicbrainz(db, None)
        out["musicbrainz"] = {"ok": mb["success"], "detail": mb["message"]}
        for player in enabled_players(db):
            try:
                out[player.id] = player.health()
            except Exception as e:
                out[player.id] = {"ok": False, "detail": str(e)}
        if (get_setting(db, "deezer_enabled", "true") or "").lower() == "true":
            dz = service_checks.check_deezer()
            out["deezer"] = {"ok": dz["success"], "detail": dz["message"]}
    finally:
        db.close()
    return out


def _refresh():
    try:
        data = _check_integrations()
        with _health_lock:
            _health.update(at=time.monotonic(), data=data,
                           checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception as e:
        logger.warning(f"[Music] health check failed: {e}")
    finally:
        _health["refreshing"] = False


def integrations() -> dict:
    """Cached health; a stale cache is refreshed in the background, never inline."""
    with _health_lock:
        stale = time.monotonic() - _health["at"] > HEALTH_TTL
        if stale and not _health["refreshing"]:
            _health["refreshing"] = True
            threading.Thread(target=_refresh, name="music-health", daemon=True).start()
        return {"checked_at": _health["checked_at"], **_health["data"]}


def _json_setting(db, key):
    try:
        return json.loads(get_setting(db, key) or "null")
    except ValueError:
        return None


def status(db) -> dict:
    from services.music import jobs, settings as music_settings, worker
    from services.music.original import CATEGORIES
    rows = db.query(MusicAlbum.category, func.count(MusicAlbum.id)).filter(
        MusicAlbum.monitored.is_(True)).group_by(MusicAlbum.category).all()
    counts = {c: 0 for c in CATEGORIES}
    unchecked = 0
    for category, n in rows:
        if category in counts:
            counts[category] = n
        else:
            unchecked += n
    counts["unchecked"] = unchecked
    return {
        "enabled": music_settings.is_enabled(db),
        "last_reconcile": _json_setting(db, "music_last_reconcile"),
        "reconcile_running": dict(jobs.progress) if jobs.progress["running"] else None,
        "counts": counts,
        "needs_review": counts.get("review", 0),
        "last_error": worker.state["last_error"] or _json_setting(db, "music_last_error"),
        "jobs_waiting": worker.pending(),
        "integrations": integrations(),
    }

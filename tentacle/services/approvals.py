"""Requests that wait for an admin (family features, Roadmap 1c).

A family member whose requests need approval (TentacleUser.request_mode
"ask", the default for everyone who isn't an admin) doesn't reach
Radarr/Sonarr/Lidarr: the request is kept as a MediaRequest and the admins
are told. An admin approves it (the original request then runs as that
person, so their Downloads playlist and "ready to watch" message work as
before) or declines it, with an optional reason. Either way the person is
told. Admins' own requests, and people set to "direct", go straight through.

Callers send the same body they always did; a held request answers
{"added": 0, "already_exists": 0, "failed": 0, "pending": 1, "detail": ...}.
Older clients that don't know "pending" show the detail line.
"""
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models.database import (
    MediaRequest, Movie, Series, TentacleUser, create_notification, get_setting,
)

logger = logging.getLogger(__name__)

KINDS = ("movie", "series", "album")
ASK, DIRECT = "ask", "direct"
# Decided requests stay visible to their owner this long (Activity, Discover).
SHOW_DECIDED_DAYS = 14
MAX_PENDING_PER_USER = 50

_decide_lock = threading.Lock()


def _live(owner_id: int, activity: bool = False) -> None:
    """Open screens: the admins' Requests and badge, the person's own
    requests and Discover labels (and Activity once Radarr/Sonarr have it)."""
    from services import live_updates
    live_updates.changed("requests", user_ids=[owner_id], admins=True, clients=True)
    if activity:
        live_updates.changed("activity", user_ids=[owner_id], admins=True, clients=True)


def needs_approval(user: Optional[TentacleUser]) -> bool:
    """Whether this person's requests wait for an admin."""
    if user is None or user.is_admin:
        return False
    return (user.request_mode or ASK) != DIRECT


def approver_name(db: Session) -> str:
    """Who approves, in words a family member knows: the owner's name, else
    "your admin"."""
    owner = (get_setting(db, "jellyfin_user_id", "") or "").replace("-", "")
    q = db.query(TentacleUser).filter(TentacleUser.is_admin == True)  # noqa: E712
    admins = q.order_by(TentacleUser.id).all()
    for a in admins:
        if a.jellyfin_user_id == owner:
            return a.display_name
    return admins[0].display_name if len(admins) == 1 else "your admin"


def request_mode(db: Session, user: Optional[TentacleUser]) -> dict:
    """What the request buttons say for this person (plugin, TV app, dashboard)."""
    return {"mode": ASK if needs_approval(user) else DIRECT, "approver": approver_name(db)}


def _details(db: Session, kind: str, tmdb_id: Optional[int]) -> dict:
    """Title, year and poster for the request list, from TMDB's cache; {} when
    TMDB can't say (the list then shows the id)."""
    if not tmdb_id or kind not in ("movie", "series"):
        return {}
    try:
        from routers.discover import _get_tmdb
        tmdb = _get_tmdb(db)
        if tmdb is None:
            return {}
        d = tmdb.get_movie_details(tmdb_id) if kind == "movie" else tmdb.get_series_details(tmdb_id)
        if not d:
            return {}
        return {"title": d.get("title") or d.get("name") or "", "year": d.get("year") or "",
                "poster_path": d.get("poster_path") or ""}
    except Exception as e:  # TMDB down or not set up: the request is kept anyway
        logger.debug(f"[Requests] No TMDB details for {kind} {tmdb_id}: {e}")
        return {}


def _held_response(db: Session, count: int) -> dict:
    who = approver_name(db)
    return {
        "added": 0, "already_exists": 0, "failed": 0, "pending": count,
        "detail": f"Sent to {who} to approve. You'll see it in Activity.",
    }


def hold(db: Session, user: TentacleUser, kind: str, payload: dict, *,
         tmdb_ids: list = (), tvdb_ids: list = (), title: str = "", year: str = "",
         poster_path: str = "") -> dict:
    """Keep a request for an admin. One MediaRequest per title; asking again
    for a title already waiting changes nothing. Tells the admins."""
    assert kind in KINDS
    waiting = db.query(MediaRequest).filter(MediaRequest.user_id == user.id,
                                            MediaRequest.status == "pending").count()
    ids = [("tmdb", int(t)) for t in (tmdb_ids or []) if _int(t)]
    ids += [("tvdb", int(t)) for t in (tvdb_ids or []) if _int(t)]
    if kind == "album":
        ids = [("mbid", payload.get("mbid"))]
    new = []
    for id_kind, value in ids:
        q = db.query(MediaRequest).filter(MediaRequest.user_id == user.id, MediaRequest.kind == kind,
                                          MediaRequest.status == "pending")
        if id_kind == "tmdb":
            q = q.filter(MediaRequest.tmdb_id == value)
        elif id_kind == "tvdb":
            q = q.filter(MediaRequest.tvdb_id == value)
        else:
            q = q.filter(MediaRequest.payload.like(f'%"mbid": {json.dumps(value)}%'))
        if q.first():
            continue
        if waiting + len(new) >= MAX_PENDING_PER_USER:
            break
        one = dict(payload)
        if id_kind == "tmdb":
            one["tmdb_ids"], one["tvdb_ids"] = [value], []
        elif id_kind == "tvdb":
            one["tmdb_ids"], one["tvdb_ids"] = [], [value]
        info = _details(db, kind, value if id_kind == "tmdb" else None)
        row = MediaRequest(
            user_id=user.id, kind=kind,
            tmdb_id=value if id_kind == "tmdb" else None,
            tvdb_id=value if id_kind == "tvdb" else None,
            title=(title or info.get("title") or "")[:300] or _fallback_title(kind, id_kind, value),
            year=str(year or info.get("year") or "")[:8],
            poster_path=(poster_path or info.get("poster_path") or "")[:300] or None,
            payload=json.dumps(one), status="pending",
        )
        db.add(row)
        new.append(row)
    db.commit()
    for row in new:
        logger.info(f"[Requests] {user.display_name} asked for {row.kind} '{row.title}' (waiting for an admin)")
        _tell_admins_new(db, user, row)
    if new:
        _live(user.id)
    return _held_response(db, max(1, len(ids)))


def _int(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _fallback_title(kind: str, id_kind: str, value) -> str:
    noun = {"movie": "Movie", "series": "Show", "album": "Album"}[kind]
    return f"{noun} {value}" if id_kind != "mbid" else noun


def _link(db: Session) -> str:
    from models.database import tentacle_address
    base = (get_setting(db, "external_url", "") or "").strip().rstrip("/") or tentacle_address(db)
    return f"{base}/#requests" if base else ""


def _tell_admins_new(db: Session, user: TentacleUser, row: MediaRequest) -> None:
    from services import pushover
    what = f"{row.title} ({row.year})" if row.year else row.title
    for admin in db.query(TentacleUser).filter(TentacleUser.is_admin == True).all():  # noqa: E712
        try:
            create_notification(db, admin.id, row.tmdb_id or 0, _media_type(row.kind), row.title,
                                f"{user.display_name} asked for this. Approve or decline it in Tentacle → Requests.",
                                poster_path=row.poster_path, kind="request", force=True)
        except Exception as e:
            db.rollback()
            logger.warning(f"[Requests] Could not tell {admin.display_name}: {e}")
    pushover.send(db, f"{user.display_name} asked for {what}",
                  "Approve or decline it in Tentacle → Requests.", _link(db))


def _media_type(kind: str) -> str:
    return "series" if kind == "series" else "movie"


# ── Admin side ──────────────────────────────────────────────────────────────

def as_dict(row: MediaRequest, names: dict) -> dict:
    return {
        "id": row.id, "kind": row.kind, "tmdb_id": row.tmdb_id, "tvdb_id": row.tvdb_id,
        "title": row.title, "year": row.year or "", "poster_path": row.poster_path or "",
        "status": row.status, "note": row.note or "", "decided_by": row.decided_by or "",
        "requested_by": names.get(row.user_id, ""),
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        "decided_at": row.decided_at.isoformat() + "Z" if row.decided_at else None,
        "episodes": len(json.loads(row.payload or "{}").get("selected_episodes") or []),
    }


def _names(db: Session) -> dict:
    return {u.id: u.display_name for u in db.query(TentacleUser).all()}


def listing(db: Session, *, user: Optional[TentacleUser] = None, days: int = 30) -> dict:
    """Waiting requests first, then the ones decided in the last `days`. With
    `user`, only theirs."""
    since = datetime.utcnow() - timedelta(days=days)
    q = db.query(MediaRequest)
    if user is not None:
        q = q.filter(MediaRequest.user_id == user.id)
    pending = q.filter(MediaRequest.status == "pending").order_by(MediaRequest.created_at).all()
    decided = (q.filter(MediaRequest.status != "pending", MediaRequest.decided_at >= since)
               .order_by(MediaRequest.decided_at.desc()).limit(100).all())
    names = _names(db)
    return {"pending": [as_dict(r, names) for r in pending],
            "decided": [as_dict(r, names) for r in decided]}


def pending_count(db: Session) -> int:
    return db.query(MediaRequest).filter(MediaRequest.status == "pending").count()


def _replay(db: Session, row: MediaRequest, admin_name: str):
    """Run the original request as its owner. Returns the outcome dict."""
    from services import media_requests
    body = json.loads(row.payload or "{}")
    via = f"approved by {admin_name}"
    if row.kind == "movie":
        def downloaded(tmdb_id):
            existing = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
            return bool(existing and existing.source == "radarr")
        out = media_requests.request_movies(
            db, body.get("tmdb_ids") or [], user_id=row.user_id, via=via,
            quality_profile_override=body.get("quality_profile_override"),
            legacy_profile_id=body.get("quality_profile_id"),
            already_owned=downloaded, want_release_date=True)
        return out.as_response()
    if row.kind == "series":
        def downloaded(tmdb_id):
            existing = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
            return bool(existing and existing.source == "sonarr")
        out = media_requests.request_series(
            db, tmdb_ids=body.get("tmdb_ids") or [], tvdb_ids=body.get("tvdb_ids") or [],
            user_id=row.user_id, via=via,
            quality_profile_override=body.get("quality_profile_override"),
            legacy_profile_id=body.get("quality_profile_id"),
            monitor=body.get("monitor") or "all",
            season_folder=body.get("season_folder") if body.get("season_folder") is not None else True,
            selected_episodes=body.get("selected_episodes"), monitor_new=body.get("monitor_new") or False,
            already_owned=downloaded)
        return out.as_response()
    return media_requests.request_album(db, body.get("mbid") or "", user_id=row.user_id, via=via,
                                        choice=body.get("choice"))


class NotPending(Exception):
    pass


def approve(db: Session, request_id: int, admin: TentacleUser) -> dict:
    """Send the request on. A failure is kept on the request (status failed,
    the reason in note) so the admin can try again; the person isn't told
    about a failure, only about the yes."""
    from services.media_requests import RequestRefused
    with _decide_lock:
        row = db.query(MediaRequest).filter(MediaRequest.id == request_id).first()
        if row is None or row.status not in ("pending", "failed"):
            raise NotPending()
        try:
            outcome = _replay(db, row, admin.display_name)
        except RequestRefused as e:
            row.status, row.note = "failed", e.message[:300]
            row.decided_by, row.decided_at = admin.display_name, datetime.utcnow()
            db.commit()
            _live(row.user_id)
            return {"ok": False, "status": row.status, "detail": e.message}
        if row.kind == "album":
            ok = outcome.get("status") == "requested"
        else:
            ok = bool(outcome.get("added") or outcome.get("already_exists"))
        row.decided_by, row.decided_at = admin.display_name, datetime.utcnow()
        if ok:
            row.status, row.note = "approved", None
        else:
            row.status = "failed"
            row.note = (outcome.get("detail") or outcome.get("error") or "Radarr/Sonarr didn't take it")[:300]
        db.commit()
    if ok:
        # What was just added shows as searching at once, not after the cache's minutes.
        try:
            from routers.activity import invalidate_wanted_cache
            invalidate_wanted_cache()
        except Exception:
            pass
    _live(row.user_id, activity=ok)
    if ok:
        logger.info(f"[Requests] {admin.display_name} approved '{row.title}' for user {row.user_id}")
        _tell_owner(db, row, f"{admin.display_name} said yes. It's downloading now; "
                             f"you'll get a message when it's ready to watch.")
    else:
        logger.warning(f"[Requests] Approved '{row.title}' but it wasn't added: {row.note}")
    return {"ok": ok, "status": row.status, "detail": row.note or "", "outcome": outcome}


def decline(db: Session, request_id: int, admin: TentacleUser, note: str = "") -> dict:
    with _decide_lock:
        row = db.query(MediaRequest).filter(MediaRequest.id == request_id).first()
        if row is None or row.status not in ("pending", "failed"):
            raise NotPending()
        row.status = "declined"
        row.note = (note or "").strip()[:300] or None
        row.decided_by, row.decided_at = admin.display_name, datetime.utcnow()
        db.commit()
    _live(row.user_id)
    logger.info(f"[Requests] {admin.display_name} declined '{row.title}' for user {row.user_id}")
    msg = f"{admin.display_name} said no this time."
    if row.note:
        msg += f' "{row.note}"'
    _tell_owner(db, row, msg)
    return {"ok": True, "status": row.status}


def cancel(db: Session, request_id: int, user: TentacleUser) -> bool:
    """The person takes back a request that is still waiting."""
    row = db.query(MediaRequest).filter(MediaRequest.id == request_id, MediaRequest.user_id == user.id,
                                        MediaRequest.status == "pending").first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    _live(user.id)
    return True


def _tell_owner(db: Session, row: MediaRequest, message: str) -> None:
    try:
        create_notification(db, row.user_id, row.tmdb_id or 0, _media_type(row.kind), row.title, message,
                            poster_path=row.poster_path, kind="request")
    except Exception as e:
        db.rollback()
        logger.warning(f"[Requests] Could not tell user {row.user_id} about '{row.title}': {e}")


def status_for(db: Session, user: Optional[TentacleUser], kind: str, tmdb_id: int) -> Optional[dict]:
    """This person's latest request for a title, for Discover's detail
    ("Waiting for approval", "Declined"), or None."""
    if user is None or not tmdb_id:
        return None
    row = (db.query(MediaRequest)
           .filter(MediaRequest.user_id == user.id, MediaRequest.kind == kind, MediaRequest.tmdb_id == tmdb_id)
           .order_by(MediaRequest.id.desc()).first())
    if row is None:
        return None
    if row.status != "pending" and row.decided_at and row.decided_at < datetime.utcnow() - timedelta(days=SHOW_DECIDED_DAYS):
        return None
    return {"id": row.id, "status": row.status, "note": row.note or "", "decided_by": row.decided_by or ""}


def pending_keys(db: Session, user: Optional[TentacleUser]) -> set:
    """(media_type, tmdb_id) of this person's waiting requests (Discover cards)."""
    if user is None:
        return set()
    rows = db.query(MediaRequest.kind, MediaRequest.tmdb_id).filter(
        MediaRequest.user_id == user.id, MediaRequest.status == "pending",
        MediaRequest.tmdb_id.isnot(None)).all()
    return {(_media_type(k), t) for k, t in rows}

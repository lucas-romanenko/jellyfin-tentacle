"""Everything each person has downloaded, for the admins (Lucas, 2026-11-09:
"even though I clicked to allow Roman to download without having to put a
request through me, I feel like there should still be a section somewhere in
Tentacle for admins to see everything that each person has downloaded").

A row is written whenever a download goes to Radarr/Sonarr for a person
(`media_requests.record_download_request`): asked straight away, approved by an
admin, or added by one of their lists. Rows are never deleted when the title
leaves (only with the person, services/gone_people.py), so the list keeps what
was removed. The status is read from the library each time, never stored:

- ready        the download is in the library
- downloading  Radarr/Sonarr still has the request and no file yet
- removed      neither: deleted, or taken out of Radarr/Sonarr

Requests still waiting for an admin, declined or failed come from
`media_requests` (services/approvals.py) and are listed with them; approved
ones are here already (the approval sends the download).
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from models.database import (DeletionLog, DownloadHistory, DownloadRequest, MediaRequest, Movie, Series,
                             TentacleUser, get_setting, set_setting)

logger = logging.getLogger(__name__)

BACKFILLED_KEY = "download_history_backfilled"
TMDB_LOOKUPS_PER_READ = 20   # titles not in the library and not known yet: looked up a few at a time


def record(db: Session, user_id: int, tmdb_id: int, media_type: str, via: str = "",
           asked_at: Optional[datetime] = None) -> None:
    """One download sent for `user_id`. Does not commit."""
    db.add(DownloadHistory(user_id=user_id, tmdb_id=tmdb_id, media_type=media_type,
                           via=_via(via), asked_at=asked_at or datetime.utcnow()))


def _via(via: str) -> str:
    """Keep how it was asked for: an approval or a list; anything else (the
    dashboard, the plugin, the TV app) was asked straight away."""
    via = via or ""
    return via if via.startswith("approved by ") or via.startswith("list ") else ""


def backfill_once(db: Session) -> int:
    """Once per install: the downloads recorded before this list existed (the
    requests still standing, and the approved ones). Returns rows added."""
    if get_setting(db, BACKFILLED_KEY, "") == "1":
        return 0
    have = {(h.user_id, h.tmdb_id, h.media_type) for h in db.query(DownloadHistory).all()}
    added = 0
    for r in db.query(MediaRequest).filter(MediaRequest.status == "approved",
                                           MediaRequest.kind.in_(("movie", "series")),
                                           MediaRequest.tmdb_id.isnot(None)).all():
        key = (r.user_id, r.tmdb_id, r.kind)
        if key in have:
            continue
        have.add(key)
        db.add(DownloadHistory(user_id=r.user_id, tmdb_id=r.tmdb_id, media_type=r.kind,
                               via=f"approved by {r.decided_by}" if r.decided_by else "",
                               title=r.title or None, year=r.year, poster_path=r.poster_path,
                               asked_at=r.created_at, created_at=r.decided_at or r.created_at))
        added += 1
    for r in db.query(DownloadRequest).all():
        key = (r.user_id, r.tmdb_id, r.media_type)
        if key in have:
            continue
        have.add(key)
        db.add(DownloadHistory(user_id=r.user_id, tmdb_id=r.tmdb_id, media_type=r.media_type, via="",
                               asked_at=r.created_at, created_at=r.created_at))
        added += 1
    set_setting(db, BACKFILLED_KEY, "1")
    db.commit()
    if added:
        logger.info(f"[Downloads] Download history: {added} earlier downloads added")
    return added


def _library_rows(db: Session, media_type: str, tmdb_ids: set) -> dict:
    """tmdb_id → the library row holding a download of it."""
    if not tmdb_ids:
        return {}
    if media_type == "movie":
        from services.radarr import downloaded_movie_rows
        q = downloaded_movie_rows(db).filter(Movie.tmdb_id.in_(tmdb_ids))
    else:
        from services.sonarr import downloaded_series_rows
        q = downloaded_series_rows(db).filter(Series.tmdb_id.in_(tmdb_ids))
    return {r.tmdb_id: r for r in q.all()}


def _any_rows(db: Session, model, tmdb_ids: set) -> dict:
    if not tmdb_ids:
        return {}
    return {r.tmdb_id: r for r in db.query(model).filter(model.tmdb_id.in_(tmdb_ids)).all()}


def _removed_at(db: Session, h: DownloadHistory) -> Optional[datetime]:
    """When it was deleted, from the deletion log (by title), if it says."""
    if not h.title:
        return None
    row = (db.query(DeletionLog.created_at)
           .filter(DeletionLog.name == h.title, DeletionLog.created_at >= h.created_at)
           .order_by(DeletionLog.created_at).first())
    return row[0] if row else None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


def listing(db: Session, user_id: Optional[int] = None) -> dict:
    """Every download (and every request not sent), newest asked first, with
    each one's status. With `user_id`, only that person's. Titles not known
    yet are filled in from the library or TMDB and kept."""
    q = db.query(DownloadHistory)
    if user_id is not None:
        q = q.filter(DownloadHistory.user_id == user_id)
    history = q.order_by(DownloadHistory.asked_at.desc(), DownloadHistory.id.desc()).all()

    ids = {"movie": set(), "series": set()}
    for h in history:
        ids.setdefault(h.media_type, set()).add(h.tmdb_id)
    ready = {"movie": _library_rows(db, "movie", ids["movie"]),
             "series": _library_rows(db, "series", ids["series"])}
    known = {"movie": _any_rows(db, Movie, ids["movie"]), "series": _any_rows(db, Series, ids["series"])}
    standing = {(r.media_type, r.tmdb_id) for r in db.query(DownloadRequest.media_type, DownloadRequest.tmdb_id)}

    lookups, filled = 0, False
    items = []
    for h in history:
        lib = known.get(h.media_type, {}).get(h.tmdb_id)
        if lib is not None and (not h.title or not h.poster_path):
            h.title, h.year = h.title or lib.title, h.year or lib.year
            h.poster_path = h.poster_path or lib.poster_path
            filled = True
        elif not h.title and lookups < TMDB_LOOKUPS_PER_READ:
            from services.approvals import _details
            lookups += 1
            d = _details(db, h.media_type, h.tmdb_id)
            if d.get("title"):
                h.title, h.year, h.poster_path = d["title"], d.get("year") or None, d.get("poster_path") or None
                filled = True
        if h.tmdb_id in ready.get(h.media_type, {}):
            status, since = "ready", None
        elif (h.media_type, h.tmdb_id) in standing:
            status, since = "downloading", None
        else:
            status, since = "removed", _removed_at(db, h)
        items.append({
            "kind": h.media_type, "tmdb_id": h.tmdb_id, "user_id": h.user_id,
            "title": h.title or f"TMDB {h.tmdb_id}", "year": h.year or "", "poster_path": h.poster_path or "",
            "how": "approved" if h.via.startswith("approved by ") else "list" if h.via.startswith("list ") else "direct",
            "via": h.via, "status": status, "asked_at": _iso(h.asked_at), "status_at": _iso(since),
        })
    if filled:
        db.commit()

    q = db.query(MediaRequest).filter(MediaRequest.status.in_(("pending", "declined", "failed")),
                                      MediaRequest.kind.in_(("movie", "series")))
    if user_id is not None:
        q = q.filter(MediaRequest.user_id == user_id)
    for r in q.all():
        items.append({
            "kind": r.kind, "tmdb_id": r.tmdb_id, "user_id": r.user_id,
            "title": r.title or f"TMDB {r.tmdb_id}", "year": r.year or "", "poster_path": r.poster_path or "",
            "how": "ask", "via": f"{'declined' if r.status == 'declined' else 'tried'} by {r.decided_by}"
            if r.decided_by else "",
            "status": "waiting" if r.status == "pending" else r.status, "note": r.note or "",
            "asked_at": _iso(r.created_at), "status_at": _iso(r.decided_at),
        })
    items.sort(key=lambda i: i["asked_at"] or "", reverse=True)

    names = {u.id: u.display_name for u in db.query(TentacleUser).all()}
    for i in items:
        i["person"] = names.get(i["user_id"], "")
    return {"items": items}


def counts_by_person(db: Session) -> dict:
    """user_id → how many downloads they've sent (for the People page)."""
    from sqlalchemy import func
    return dict(db.query(DownloadHistory.user_id, func.count(DownloadHistory.id))
                .group_by(DownloadHistory.user_id).all())

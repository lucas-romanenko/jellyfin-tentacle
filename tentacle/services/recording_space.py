"""A space limit for Live TV recordings (#868): keep them under N GB, oldest
first, and optionally none older than N days.

Off until an admin sets a limit (settings `recordings_limit_gb`,
`recordings_limit_days`; empty = off): an upgrade deletes nothing. Every
PASS_MINUTES the pass reads Jellyfin's recordings either way and keeps the
list (`recording_sizes`) for the Recordings page's total and its "what would
go" preview.

- Recordings = Jellyfin's video items whose path is under its recordings
  folders (`/System/Configuration/livetv`), in the libraries that hold them;
  size = the item's MediaSources size, recorded = its DateCreated. Jellyfin's
  items, so library and disk stay in step: deleting goes through Jellyfin
  (DELETE /Items/{id}), which removes the file and the item.
- Never deleted (`kept`): anyone's favourite (the recording or its show's
  folder), one someone is part-way through, one playing now, one recording
  now (its timer, its booking, or begun less than SETTLE ago), and what a
  recording rule keeps (Keep forever, or its newest "keep the last N").
- Over the limit, the oldest of the rest go until the total (kept ones
  count) is under it; with days set, the rest older than that go too.
- Anything that can't be read (Jellyfin's folders, items, a user's
  favourites or progress, the timers, the sessions, any item's size, date or
  path) skips the whole pass: nothing is deleted on a guess. A delete that
  fails ends the pass.
- Each deletion goes in the Deletion Log (kind `recordings-limit`).

Jellyfin's own per-series "keep up to" still applies; the limit doesn't
change it or count on it.
"""
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models.database import (RecordingRule, RecordingSize, SessionLocal, get_setting, log_deletion,
                             set_setting)
from services.recording_art import job as J

logger = logging.getLogger(__name__)

PASS_MINUTES = 15
SETTLE = timedelta(minutes=10)      # begun less than this ago: still recording as far as anyone can tell
LIMIT_SETTING = "recordings_limit_gb"
DAYS_SETTING = "recordings_limit_days"
GB = 1000 ** 3                      # as disks and Jellyfin's clients count
_ID = re.compile(r"\A[0-9a-fA-F]{32}\Z")
_lock = threading.Lock()
_timer: Optional[threading.Timer] = None
_timer_lock = threading.Lock()
_last: dict = {"at": None, "error": None, "deleted": 0, "seen": None}


class Unreadable(Exception):
    """Something the pass needs couldn't be read: no pass this time."""


@dataclass
class Rec:
    item_id: str
    name: str
    recorded_at: datetime
    size: int
    rule_id: Optional[int] = None
    kept: Optional[str] = None      # a KEPT key, or None: the limit may delete it
    detail: str = ""                # whose favourite, which rule


# ── Settings ────────────────────────────────────────────────────────────────

def _number(db: Session, key: str) -> Optional[float]:
    raw = (get_setting(db, key, "") or "").strip()
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def limits(db: Session) -> tuple:
    """(limit in GB or None, days or None). Both None: off."""
    days = _number(db, DAYS_SETTING)
    return _number(db, LIMIT_SETTING), int(days) if days else None


def set_limits(db: Session, limit_gb: Optional[float], days: Optional[int]) -> None:
    set_setting(db, LIMIT_SETTING, f"{limit_gb:g}" if limit_gb else "")
    set_setting(db, DAYS_SETTING, str(int(days)) if days else "")


# ── Reading Jellyfin ────────────────────────────────────────────────────────

def _recording_paths(jf) -> list:
    cfg = jf.get("/System/Configuration/livetv") or {}
    paths = [cfg.get("RecordingPath"), cfg.get("MovieRecordingPath"), cfg.get("SeriesRecordingPath")]
    paths += list(cfg.get("MediaLocationsCreated") or [])
    return sorted({p.rstrip("/\\") for p in paths if p})


def _under(path: str, roots: list) -> bool:
    p = (path or "").replace("\\", "/")
    return any(p.startswith(r.replace("\\", "/") + "/") for r in roots)


def _users_marks(jf, ids: set) -> tuple:
    """({item id: [names whose favourite]}, {item id: [names part-way through]})
    over every Jellyfin user, for the recordings and their folders."""
    fav: dict = {}
    resume: dict = {}
    for u in jf.get("/Users") or []:
        uid, name = u.get("Id"), u.get("Name") or "someone"
        if not uid:
            raise Unreadable("a Jellyfin user without an id")
        for filt, into in (("IsFavorite", fav), ("IsResumable", resume)):
            got = jf.get("/Items", userId=uid, Recursive="true", Filters=filt, EnableImages="false",
                         EnableUserData="true")
            if not isinstance(got, dict) or not isinstance(got.get("Items"), list):
                raise Unreadable(f"{name}'s {'favourites' if into is fav else 'progress'}")
            for it in got["Items"]:
                iid = J._id(it.get("Id"))
                if iid in ids:
                    into.setdefault(iid, []).append(name)
    return fav, resume


def _playing(jf) -> set:
    sessions = jf.get("/Sessions")
    if not isinstance(sessions, list):
        raise Unreadable("Jellyfin's sessions")
    return {J._id((s.get("NowPlayingItem") or {}).get("Id")) for s in sessions if s.get("NowPlayingItem")}


def read(db: Session, jf, now: datetime) -> list:
    """Every recording, with why it's kept. Raises Unreadable."""
    try:
        roots = _recording_paths(jf)
        libs, _ = J.recordings_libraries(jf, any_library=True)
        videos, folders = {}, {}
        for lib in libs:
            videos.update((J._id(i.get("Id")), i) for i in J._children(jf, lib["id"], folders=False))
            folders.update((J._id(i.get("Id")), i) for i in J._children(jf, lib["id"], folders=True))
        busy, now_on = J._busy(jf, now)
        playing = _playing(jf)
    except Unreadable:
        raise
    except Exception as e:
        raise Unreadable(f"Jellyfin's recordings ({type(e).__name__}: {e})"[:200])
    if not roots:
        return []
    items = []
    for iid, it in videos.items():
        path = it.get("Path")
        if not path:
            raise Unreadable(f"“{it.get('Name') or iid}” has no path")
        if not _under(path, roots):
            continue        # another library's video next to the recordings: not one
        if not _ID.match(iid):
            raise Unreadable(f"“{it.get('Name') or iid}” has an odd id")
        size = int(((it.get("MediaSources") or [{}])[0] or {}).get("Size") or 0)
        created = J._parse(it.get("DateCreated"))
        if size <= 0:
            raise Unreadable(f"the size of “{it.get('Name') or iid}”")
        if created is None:
            raise Unreadable(f"when “{it.get('Name') or iid}” was recorded")
        items.append((iid, it, size, created))
    try:
        fav, resume = _users_marks(jf, {i for i, *_ in items} | set(folders))
    except Unreadable:
        raise
    except Exception as e:
        raise Unreadable(f"Jellyfin's favourites ({type(e).__name__}: {e})"[:200])

    rules = {r.id: r for r in db.query(RecordingRule).all()}
    recs = []
    for iid, it, size, created in items:
        b = J._booking_of(db, it.get("Name") or "", created)
        rec = Rec(iid, it.get("Name") or "Recording", created, size, rule_id=b.rule_id if b is not None else None)
        name = J.M.fold(J.M.strip_prefix(it.get("Name")))
        # its show's folders up to the library: a favourite show keeps its recordings
        chain, p, seen = [iid], J._id(it.get("ParentId")), set()
        while p and p in folders and p not in seen:
            seen.add(p)
            chain.append(p)
            p = J._id(folders[p].get("ParentId"))
        fans = sorted({n for c in chain for n in fav.get(c, [])})
        if fans:
            rec.kept, rec.detail = "favourite", ", ".join(fans)
        elif iid in playing:
            rec.kept = "playing"
        elif ((b is not None and b.state == "recording") or created + SETTLE > now
              or any(n == name and created >= s - timedelta(minutes=2) for n, s in now_on)):
            rec.kept = "recording"
        elif resume.get(iid):
            rec.kept, rec.detail = "watching", ", ".join(sorted(set(resume[iid])))
        recs.append(rec)
    # what rules keep: everything (Keep forever), or their newest N
    by_rule: dict = {}
    for rec in sorted(recs, key=lambda r: r.recorded_at, reverse=True):
        rule = rules.get(rec.rule_id)
        if rule is None:
            continue
        by_rule[rule.id] = by_rule.get(rule.id, 0) + 1
        if rec.kept is None and (rule.keep_forever or (rule.keep_last and by_rule[rule.id] <= rule.keep_last)):
            rec.kept, rec.detail = "keep", rule.name
    return recs


# ── What goes (pure) ────────────────────────────────────────────────────────

def plan(recs: list, limit_gb: Optional[float], days: Optional[int], now: datetime) -> list:
    """[(Rec, why)] to delete, oldest first: what is older than `days`, then
    the oldest of the rest until the total is under `limit_gb`. Kept ones
    never go, and count towards the total."""
    free = sorted((r for r in recs if r.kept is None), key=lambda r: (r.recorded_at, r.item_id))
    out = []
    if days:
        cut = now - timedelta(days=days)
        out = [(r, f"older than {days} day{'' if days == 1 else 's'}") for r in free if r.recorded_at < cut]
    if limit_gb:
        limit = limit_gb * GB
        gone = {r.item_id for r, _ in out}
        total = sum(r.size for r in recs) - sum(r.size for r, _ in out)
        for r in free:
            if total <= limit:
                break
            if r.item_id in gone:
                continue
            out.append((r, f"over the recordings limit ({limit_gb:g} GB)"))
            total -= r.size
    return sorted(out, key=lambda x: (x[0].recorded_at, x[0].item_id))


# ── The pass ────────────────────────────────────────────────────────────────

def _store(db: Session, recs: list, now: datetime) -> None:
    db.query(RecordingSize).delete()
    for r in recs:
        db.add(RecordingSize(item_id=r.item_id, name=r.name[:200], recorded_at=r.recorded_at, size_bytes=r.size,
                             rule_id=r.rule_id, kept=r.kept, read_at=now))
    db.commit()


def _gb(n: int) -> str:
    return f"{n / GB:.1f} GB"


def run_pass(db: Session, jf=None, now: Optional[datetime] = None) -> dict:
    """One pass: read, keep the list, delete what the limit says. Returns
    what it did."""
    now = now or datetime.utcnow()
    jf = jf if jf is not None else J.jellyfin_for(db)
    out = {"recordings": 0, "total_bytes": 0, "deleted": 0, "freed_bytes": 0, "skipped": None}
    if jf is None:
        out["skipped"] = "Jellyfin isn't connected"
        _last.update(at=now, error=out["skipped"])
        return out
    with _lock:
        try:
            recs = read(db, jf, now)
        except Unreadable as e:
            db.rollback()
            out["skipped"] = f"Couldn't read {e}: nothing deleted this time"
            _last.update(at=now, error=out["skipped"])
            logger.warning(f"[Recordings limit] {out['skipped']}")
            return out
        out["recordings"], out["total_bytes"] = len(recs), sum(r.size for r in recs)
        limit_gb, days = limits(db)
        doomed = plan(recs, limit_gb, days, now)
        gone = set()
        error = None
        for r, why in doomed:
            # once more, just before: still there, still a recording
            try:
                now_item = J._item(jf, r.item_id)
                if now_item is None:
                    gone.add(r.item_id)
                    continue
                if not _under(now_item.get("Path"), _recording_paths(jf)):
                    error = f"“{r.name}” is no longer in the recordings folder: stopped"
                    break
                jf.delete_item(r.item_id)
            except Exception as e:
                error = f"Jellyfin didn't delete “{r.name}” ({type(e).__name__}: {e})"[:200]
                logger.warning(f"[Recordings limit] {error}")
                break
            gone.add(r.item_id)
            out["deleted"] += 1
            out["freed_bytes"] += r.size
            logger.info(f"[Recordings limit] deleted “{r.name}” ({_gb(r.size)}, recorded "
                        f"{r.recorded_at:%Y-%m-%d %H:%M} UTC): {why}")
            log_deletion(db, kind="recordings-limit", name=r.name, size_bytes=r.size, reason="auto",
                         detail=f"{why[0].upper()}{why[1:]}; recorded {r.recorded_at:%Y-%m-%d %H:%M} UTC")
        recs = [r for r in recs if r.item_id not in gone]
        _store(db, recs, now)
        out["recordings"], out["total_bytes"] = len(recs), sum(r.size for r in recs)
        _last.update(at=now, error=error, deleted=out["deleted"])
    return out


def run_scheduled() -> None:
    db = SessionLocal()
    try:
        before = _last.get("seen")
        summary = run_pass(db)
        seen = (summary["recordings"], summary["total_bytes"])
        if summary["skipped"] is None:
            _last["seen"] = seen
        if summary["deleted"] or (summary["skipped"] is None and seen != before):
            limit_gb, days = limits(db)
            logger.info(f"[Recordings limit] {summary['recordings']} recordings, {_gb(summary['total_bytes'])}; "
                        f"limit {f'{limit_gb:g} GB' if limit_gb else 'off'}"
                        f"{f', {days} days' if days else ''}; deleted {summary['deleted']}"
                        f" ({_gb(summary['freed_bytes'])})")
            from services.recording_rules import book
            book.notify()       # the Recordings page's total
    except Exception as e:
        db.rollback()
        _last.update(at=datetime.utcnow(), error=str(e)[:200])
        logger.warning(f"[Recordings limit] pass failed: {e}")
    finally:
        db.close()


def request_pass(delay: float = 5.0) -> None:
    """A pass in a moment (the limit changed)."""
    global _timer
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(delay, run_scheduled)
        _timer.daemon = True
        _timer.start()


# ── For the Recordings page ─────────────────────────────────────────────────

def _rec_of(row: RecordingSize) -> Rec:
    return Rec(row.item_id, row.name or "Recording", row.recorded_at, row.size_bytes, row.rule_id, row.kept)


def status(db: Session, now: Optional[datetime] = None, limit_gb: Optional[float] = None,
           days: Optional[int] = None, preview: bool = False) -> dict:
    """The total and the limit as of the last read; what would go now at the
    saved limit, or at `limit_gb`/`days` when `preview` (nothing deleted)."""
    now = now or datetime.utcnow()
    rows = db.query(RecordingSize).all()
    recs = [_rec_of(r) for r in rows]
    saved_gb, saved_days = limits(db)
    if not preview:
        limit_gb, days = saved_gb, saved_days
    kept: dict = {}
    for r in recs:
        if r.kept:
            kept[r.kept] = kept.get(r.kept, 0) + 1
    doomed = plan(recs, limit_gb, days, now) if (limit_gb or days) else []
    total = sum(r.size for r in recs)
    kept_bytes = sum(r.size for r in recs if r.kept)
    read_at = max((r.read_at for r in rows if r.read_at), default=None)
    oldest = min((r.recorded_at for r in recs), default=None)
    return {
        "limit_gb": saved_gb, "days": saved_days, "on": bool(saved_gb or saved_days),
        "preview": {"limit_gb": limit_gb, "days": days} if preview else None,
        "count": len(recs), "total_bytes": total, "kept": kept, "kept_bytes": kept_bytes,
        "oldest": oldest.isoformat() + "Z" if oldest else None,
        "would_go": [{"item_id": r.item_id, "name": r.name, "size_bytes": r.size,
                      "recorded_at": r.recorded_at.isoformat() + "Z", "why": why} for r, why in doomed],
        "would_free_bytes": sum(r.size for r, _ in doomed),
        "kept_over": bool(limit_gb and kept_bytes > limit_gb * GB),
        "read_at": read_at.isoformat() + "Z" if read_at else None,
        "last_at": _last["at"].isoformat() + "Z" if _last.get("at") else None,
        "error": _last.get("error"), "minutes": PASS_MINUTES,
    }

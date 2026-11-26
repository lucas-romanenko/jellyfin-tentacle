"""Save offline, the server's part first (26 Nov 2026, Lucas: "No way to have
it keep going in background either? takes a while to save and i need to
keep that page open").

On Save offline the device asks the server (POST /api/offline/saves). A
title the device can take as it is (a file on the server's disk that its
browser plays, small enough for a phone) is ready at once. Anything else,
every provider title included, is first made into a finished copy on the
server (services/offline_convert.py): pulled from the provider on the
lowest-ranked connection (anyone who wants to watch takes it), converted
where it must be, with no page open. When it's ready the person is told
("<title> is ready to save": Tentacle's message in Jellyfin, and a push
message on the device that asked, services/web_push.py), and the device's
part is only copying a finished file at the server's full speed, resumable
from any byte (Background Fetch in Chrome, with the page closed).

A phone or a tablet (the device says so: offline.js) gets the smaller
phone copy (at most 1080p, offline_convert `phone`), a computer the full one.

One OfflineSave row per person, title and kind of device, until the device
has it (DELETE). A copy someone still wants is never stopped or deleted
(offline_convert.wanted); a ready one nobody fetched is let go after
READY_KEEP_S. After a restart the copies still being made start again.
"""
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models.database import Notification, OfflineSave, SessionLocal, TentacleUser, get_setting
from services import offline
from services import offline_convert as C
from services.family import FamilyError

logger = logging.getLogger(__name__)

READY_KEEP_S = 3 * 24 * 3600     # a ready copy nobody came for
FAILED_KEEP_S = 24 * 3600
NOTE_KIND = "offline"

_wanted_cache = (0.0, set())
_wanted_lock = threading.Lock()


# ── What a title needs ───────────────────────────────────────────────────────

def is_remote(item: dict) -> bool:
    """A provider's title (a .strm): its bytes come from the provider (pure)."""
    sources = item.get("MediaSources") or []
    path = ((sources[0].get("Path") if sources else None) or item.get("Path") or "").lower()
    container = ((sources[0].get("Container") if sources else None) or "").lower()
    return path.startswith(("http://", "https://")) or path.endswith(".strm") or container == "strm"


def phone_fits(item: dict) -> bool:
    """Small enough for a phone as it is, by Jellyfin's own look at it (pure)."""
    sources = item.get("MediaSources") or []
    streams = (sources[0].get("MediaStreams") if sources else None) or []
    video = next((s for s in streams if s.get("Type") == "Video"), None)
    if not video:
        return False
    w, h = int(video.get("Width") or 0), int(video.get("Height") or 0)
    bps = int(video.get("BitRate") or 0)
    return (0 < max(w, h) <= C.PHONE_LONG and 0 < min(w, h) <= C.PHONE_SHORT
            and bps <= C.PHONE_COPY_MAX_BPS)


def as_is(item: dict, phone: bool, caps: frozenset) -> bool:
    """The device takes the title as it is, straight from Jellyfin (pure):
    a file on the server's own disk its browser plays (and, for a phone,
    small enough). A provider's title never: the server gets it first."""
    if is_remote(item):
        return False
    sources = item.get("MediaSources") or []
    video = next((m for m in (sources[0].get("MediaStreams") if sources else None) or [] if m.get("Type") == "Video"), {})
    if video.get("DvProfile") == 5 or video.get("DvBlSignalCompatibilityId") == 0:
        return False        # Dolby Vision only: its colours need converting (offline_convert)
    info = offline.summary(item)
    if C.fits(info["source_container"], C.streams_from_item(item), caps) is not True:
        return False
    return not phone or phone_fits(item)


def label(item: dict) -> str:
    """How the message names the title: "Bluey S1E2", "Madden (2026)" (pure)."""
    if item.get("Type") == "Episode":
        se = ""
        if item.get("ParentIndexNumber") is not None and item.get("IndexNumber") is not None:
            se = f" S{item['ParentIndexNumber']}E{item['IndexNumber']}"
        return f"{item.get('SeriesName') or item.get('Name') or ''}{se}".strip()
    year = item.get("ProductionYear")
    return f"{item.get('Name') or ''}{f' ({year})' if year else ''}"


def _jf(db) -> tuple:
    url = (get_setting(db, "jellyfin_url", "") or "").strip().rstrip("/")
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    if not url or not key:
        raise FamilyError("Jellyfin isn't connected yet.", 409)
    return url, key


def estimate(item: dict) -> int:
    """About how big the source is: Jellyfin's size of it, else ~1 MB a second."""
    info = offline.summary(item)
    size = info.get("size") or 0
    if size > (50 << 20):
        return int(size)
    return int((info.get("runtime_ticks") or 0) / 1e7 * (1 << 20))


def start_job(db, item: dict, phone: bool, caps: frozenset, who: str) -> C.Job:
    url, key = _jf(db)
    iid = offline.norm_item_id(item.get("Id") or "")
    return C.get_or_start(iid, caps, f"{url}/Items/{iid}/Download", key, estimate(item), who, phone)


# ── The person's saves ───────────────────────────────────────────────────────

def _answer(iid: str, row: Optional[OfflineSave], job: Optional[C.Job], direct: bool = False) -> dict:
    """What the Offline page shows for a title (pure apart from the job)."""
    if direct:
        return {"id": iid, "state": "ready", "direct": True}
    if job is not None and job.done:
        return {"id": iid, "state": "ready", "size": job.size}
    if job is not None and job.error:
        return {"id": iid, "state": "failed", "error": job.error}
    if row is not None and row.state == "failed":
        return {"id": iid, "state": "failed", "error": row.error or C.BROKE}
    out = {"id": iid, "state": "preparing", "fraction": round(job.fraction, 3) if job else 0.0}
    if job is not None:
        out["estimate"] = job.guess_total()
    return out


def _row(db, user, iid: str, phone: bool, caps: frozenset) -> Optional[OfflineSave]:
    return db.query(OfflineSave).filter(OfflineSave.user_id == user.id, OfflineSave.jellyfin_item_id == iid,
                                        OfflineSave.device == ("phone" if phone else "pc"),
                                        OfflineSave.caps == C.caps_key(caps)).first()


def request(db: Session, user: TentacleUser, item_id: str, phone: bool, caps: frozenset,
            push_endpoint: str = "") -> dict:
    """Save offline tapped on a device: ready at once, or the server starts
    making its copy (FamilyError when this person may not save it)."""
    item = offline.check(db, user, item_id, fresh=True)
    iid = offline.norm_item_id(item_id)
    if as_is(item, phone, caps):
        return _answer(iid, None, None, direct=True)
    job = start_job(db, item, phone, caps, user.display_name)
    row = _row(db, user, iid, phone, caps)
    if row is None:
        row = OfflineSave(user_id=user.id, jellyfin_item_id=iid, device="phone" if phone else "pc",
                          caps=C.caps_key(caps), copy_key=job.key)
        db.add(row)
    row.name = label(item)
    row.copy_key = job.key
    row.push_endpoint = (push_endpoint or "")[:2000] or row.push_endpoint
    if job.done:
        # Made before (another device, another person): nothing to wait for or tell
        row.state, row.size, row.ready_at = "ready", job.size, row.ready_at or datetime.utcnow()
        row.notified_at = row.notified_at or datetime.utcnow()
    elif job.error:
        row.state, row.error = "failed", job.error
    else:
        row.state, row.error, row.notified_at = "preparing", None, None
    db.commit()
    _forget_wanted()
    logger.info(f"[Offline] {user.display_name}: Save offline {row.name} on a {row.device} "
                f"({'ready' if job.done else 'the server makes its copy first'})")
    return _answer(iid, row, job)


def status(db: Session, user: TentacleUser, ids: list, phone: bool, caps: frozenset) -> list:
    """How far each title has got on the server (only this person's)."""
    out = []
    for raw in ids[:300]:
        iid = offline.norm_item_id(raw)
        if not iid:
            continue
        row = _row(db, user, iid, phone, caps)
        job = C.find(iid, caps, phone)
        if row is None and job is None:
            out.append({"id": iid, "state": "unknown"})
            continue
        if row is not None and row.state != "failed" and job is None:
            # Not in memory (Tentacle restarted): finished on disk, else made again
            try:
                item = offline.check(db, user, iid)
                job = start_job(db, item, phone, caps, user.display_name)
                if not job.done and not job.error and row.state != "preparing":
                    row.state, row.notified_at = "preparing", None      # its file went: made again
                db.commit()
            except FamilyError as e:
                row.state, row.error = "failed", e.message
                db.commit()
        out.append(_answer(iid, row, job))
    return out


def forget(db: Session, user: TentacleUser, item_id: str, phone: bool, caps: frozenset) -> bool:
    """The device has it now, or it was removed there: the server's copy
    may go (offline_convert sweeps a copy nobody wants once read)."""
    iid = offline.norm_item_id(item_id)
    row = _row(db, user, iid, phone, caps)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    _forget_wanted()
    still = db.query(OfflineSave).filter(OfflineSave.copy_key == row.copy_key).count()
    job = C._jobs.get(row.copy_key)
    if not still and job is not None and not job.done and not job.readers:
        # Removed before the server finished: stop making it
        logger.info(f"[Offline] {user.display_name} removed {row.name} before it was ready: stopped")
        C.stop(job)
    return True


# ── Ready: tell them ─────────────────────────────────────────────────────────

def wanted_keys() -> set:
    """The copies someone still waits for or hasn't fetched (offline_convert.wanted)."""
    global _wanted_cache
    import time
    with _wanted_lock:
        at, keys = _wanted_cache
        if time.monotonic() - at < 30:
            return keys
    db = SessionLocal()
    try:
        keys = {k for (k,) in db.query(OfflineSave.copy_key).filter(OfflineSave.state != "failed").all()}
    finally:
        db.close()
    with _wanted_lock:
        _wanted_cache = (time.monotonic(), keys)
    return keys


def _forget_wanted():
    global _wanted_cache
    with _wanted_lock:
        _wanted_cache = (0.0, set())


def _finished(job: C.Job):
    """offline_convert's word that a copy is finished (or failed): the rows
    waiting for it are updated and their people told (in a thread: the push
    services are on the internet)."""
    threading.Thread(target=_finished_rows, args=(job.key, job.done, job.size, job.error),
                     name="offline-ready", daemon=True).start()


def _finished_rows(key: str, done: bool, size: int, error: str):
    db = SessionLocal()
    try:
        rows = db.query(OfflineSave).filter(OfflineSave.copy_key == key, OfflineSave.state == "preparing").all()
        for row in rows:
            if done:
                row.state, row.size, row.ready_at, row.error = "ready", size, datetime.utcnow(), None
            else:
                row.state, row.error = "failed", error or C.BROKE
        db.commit()
        if done:
            for row in rows:
                if row.notified_at is None:
                    tell(db, row)
    except Exception as e:
        db.rollback()
        logger.warning(f"[Offline] After making {key}: {type(e).__name__}: {e}")
    finally:
        db.close()
        _forget_wanted()


def _group(row: OfflineSave) -> str:
    """A show's episodes share one message ("3 episodes of Bluey")."""
    name = row.name or ""
    head, sep, tail = name.rpartition(" S")
    return head if sep and tail[:1].isdigit() and "E" in tail else name


def tell(db, row: OfflineSave):
    """"<title> is ready to save": Tentacle's message (Jellyfin shows it) and
    a push message on the device that asked (any of theirs if it didn't say)."""
    group = _group(row)
    ready = db.query(OfflineSave).filter(OfflineSave.user_id == row.user_id, OfflineSave.state == "ready").all()
    same = [r for r in ready if _group(r) == group]
    message = (f"{row.name} is ready to save" if len(same) <= 1 else
               f"{len(same)} episodes of {group} are ready to save")
    note = db.query(Notification).filter(Notification.user_id == row.user_id, Notification.kind == NOTE_KIND,
                                         Notification.title == group, Notification.dismissed_at.is_(None)).first()
    if note is None:
        note = Notification(user_id=row.user_id, tmdb_id=0, media_type="series" if group != row.name else "movie",
                            title=group, kind=NOTE_KIND)
        db.add(note)
    note.message = message
    note.jellyfin_item_id = row.jellyfin_item_id
    note.created_at = datetime.utcnow()
    row.notified_at = datetime.utcnow()
    db.commit()
    try:
        from services import live_updates
        live_updates.changed("notifications", user_ids=[row.user_id], clients=True)
    except Exception:
        pass
    from services import web_push
    sent = web_push.send(db, row.user_id, {
        "title": message, "body": "Tap to save it on this device.",
        "url": "/#offline", "tag": f"offline-{group}"[:120],
        "save": {"id": row.jellyfin_item_id, "size": row.size or 0, "device": row.device, "caps": row.caps},
    }, endpoint=row.push_endpoint or None)
    logger.info(f"[Offline] {row.name} is ready to save: told the person"
                f"{f' (push to {sent} device(s))' if sent else ''}")


# ── Housekeeping ─────────────────────────────────────────────────────────────

def sweep() -> int:
    """Lets go of ready copies nobody came for and old failures (scheduler).
    Returns how many rows went."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        gone = db.query(OfflineSave).filter(
            ((OfflineSave.state == "ready") & (OfflineSave.ready_at < now - timedelta(seconds=READY_KEEP_S)))
            | ((OfflineSave.state == "failed") & (OfflineSave.created_at < now - timedelta(seconds=FAILED_KEEP_S)))
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    if gone:
        _forget_wanted()
        logger.info(f"[Offline] {gone} Save offline request(s) nobody came back for: let go")
    return gone


async def resume_all():
    """After a restart: the copies still being made start again (a copy
    finished before is found on disk and told about)."""
    await asyncio.sleep(20)
    db = SessionLocal()
    try:
        rows = db.query(OfflineSave).filter(OfflineSave.state.in_(("preparing", "ready"))).all()
        picked = 0
        for row in rows:
            if row.copy_key in C._jobs:
                continue
            picked += 1
            user = db.get(TentacleUser, row.user_id)
            if user is None:
                db.delete(row)
                continue
            try:
                item = offline.check(db, user, row.jellyfin_item_id)
                job = start_job(db, item, row.device == "phone", C.caps_of(row.caps.replace("-", ",")),
                                user.display_name)
            except FamilyError as e:
                row.state, row.error = "failed", e.message
                continue
            except Exception as e:
                logger.warning(f"[Offline] {row.name} not started again: {type(e).__name__}: {e}")
                continue
            if job.done and row.state == "preparing":
                _finished(job)
        db.commit()
        if picked:
            logger.info(f"[Offline] {picked} Save offline request(s) picked up after the restart")
    finally:
        db.close()


_resume_task = None


def start():
    """main.py's lifespan: hooks into offline_convert and picks up what a restart cut."""
    global _resume_task
    C.set_loop(asyncio.get_running_loop())
    C.wanted = wanted_keys
    if _finished not in C.on_finished:
        C.on_finished.append(_finished)
    if _resume_task is None or _resume_task.done():
        _resume_task = asyncio.get_running_loop().create_task(resume_all())

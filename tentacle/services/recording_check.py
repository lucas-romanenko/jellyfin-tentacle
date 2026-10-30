"""After a Live TV recording (#869): every PASS_MINUTES on the scheduler,
each finished recording is checked once, through the plugin (the files are
Jellyfin's, in its recordings folder; Tentacle never sees them).

- Its clock: when the provider closes a channel mid-recording and the
  re-dial isn't joined (routers/livetv.py `_ReplaySplicer`: "sent as it
  came"), the new stream carries the provider's new clock, hours away. The
  file loses nothing, but ffprobe and so Jellyfin take its length from the
  first and last timestamps: a 3 h 45 min game shows as 21.7 h and seeking
  past the jump misbehaves. The plugin (POST /Tentacle/Recordings/{id}/Retime)
  compares the probed length with the file's wall-clock window; far over it,
  it copies the file with continuous timestamps (`ffmpeg -fflags
  +genpts+discardcorrupt -c copy`) beside it, puts the copy in its place
  only when its length fits the window and nothing was lost, and refreshes
  the item. Live bytes are never rewritten.
- Its PremiereDate: Jellyfin leaves it empty on recordings; it becomes the
  programme's real start (Tentacle's booking or the guide, else when the
  recording began), so clients date and sort it like anything else.

Never while Jellyfin records it (its timer, its booking, the plugin's own
check of the file); no copy while Jellyfin records anything or a recording
starts within 20 min (a copy is a whole file read and written).
"""
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import RecordingCheck, SessionLocal, log_activity
from services.recording_art import job as J

logger = logging.getLogger(__name__)

PASS_MINUTES = 15
SETTLE = timedelta(minutes=10)      # after it began, before the plugin is asked (it refuses a file still written)
CHECKS_PER_PASS = 6
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(hours=6)
COPY_TIMEOUT = 3600                 # one recording's copy (13.7 GB at disk speed is a few minutes)
DONE = ("ok", "retimed", "not_fixed", "no_window", "not_a_recording")
DATED = ("ok", "retimed", "not_fixed", "no_window")      # given their PremiereDate
_lock = threading.Lock()


class PluginTooOld(Exception):
    """The plugin has no after-recording check yet."""


def _dashed(item_id: str) -> str:
    i = J._id(item_id)
    return f"{i[:8]}-{i[8:12]}-{i[12:16]}-{i[16:20]}-{i[20:]}" if len(i) == 32 else i


def retime(jf, item_id: str) -> tuple:
    """The plugin's clock check of one recording: (HTTP status, body)."""
    status, body = jf.post(f"/Tentacle/Recordings/{_dashed(item_id)}/Retime", COPY_TIMEOUT)
    if status == 404 and not body.get("state"):
        raise PluginTooOld("the Tentacle plugin has no recording check (update it)")
    return status, body


def set_premiere(jf, item_id: str, start: datetime) -> Optional[bool]:
    """PremiereDate = `start` (UTC) when Jellyfin has none. True set, False
    Jellyfin had one, None not done."""
    status, body = jf.post(f"/Tentacle/Recordings/{_dashed(item_id)}/Premiere", 30,
                         date=start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if status == 404 and not body.get("state"):
        raise PluginTooOld("the Tentacle plugin has no recording check (update it)")
    return bool(body.get("set")) if status == 200 else None


def _hours(seconds) -> str:
    s = int(seconds or 0)
    return f"{s // 3600} h {s % 3600 // 60:02d} min" if s >= 3600 else f"{s // 60} min"


def _finished(item: dict, ctx: dict, now: datetime, now_on: list) -> bool:
    """Not being recorded as far as Tentacle knows (the plugin checks the file
    itself too). Unlike the pictures' wait, not by the probed length, which is
    the very thing that may be wrong."""
    if ctx["booking_state"] == "recording":
        return False
    name = J.M.fold(J.M.strip_prefix(item.get("Name")))
    if any(n == name and ctx["created"] >= s - timedelta(minutes=2) for n, s in now_on):
        return False
    return ctx["created"] + SETTLE <= now


def _due(row: Optional[RecordingCheck], now: datetime) -> bool:
    if row is None:
        return True
    if row.state in DONE:
        return False
    return (row.attempts or 0) < MAX_ATTEMPTS and not (row.updated_at and row.updated_at + RETRY_AFTER > now)


def check(db: Session, jf, item: dict, row: Optional[RecordingCheck], now: datetime) -> Optional[RecordingCheck]:
    """One recording's clock check. Returns its row, or None when the plugin
    says it still records (asked again next pass)."""
    status, body = retime(jf, item.get("Id"))
    state = body.get("state") or f"http {status}"
    if status == 409:
        return row
    if row is None:
        row = RecordingCheck(item_id=J._id(item.get("Id")), state=state, attempts=0, created_at=now)
        db.add(row)
    name = item.get("Name") or "Recording"
    row.name, row.state, row.updated_at = name, state, now
    row.duration, row.wall, row.after = body.get("duration"), body.get("wall"), body.get("after")
    row.detail = (body.get("error") or "")[:250] or None
    if state not in DONE:
        row.state = "failed" if state != "no_space" else state
        row.attempts = (row.attempts or 0) + 1
        row.detail = row.detail or state
        logger.warning(f"[Recording check] “{name}”: {state} {row.detail or ''}".rstrip())
    elif state == "retimed":
        msg = (f"Recording “{name}”: its clock jumped ({_hours(row.duration)} for {_hours(row.wall)} recorded); "
               f"copied with continuous timestamps, now {_hours(row.after)}")
        logger.info(f"[Recording check] {msg}")
        log_activity(db, "livetv_recording_retimed", msg, {"item_id": row.item_id, "duration": row.duration,
                                                            "wall": row.wall, "after": row.after})
    elif state == "not_fixed":
        msg = (f"Recording “{name}” shows as {_hours(row.duration)} for {_hours(row.wall)} recorded, and a copy "
               f"with continuous timestamps didn't fix it: left as it was")
        logger.warning(f"[Recording check] {msg}")
        log_activity(db, "livetv_recording_not_fixed", msg, {"item_id": row.item_id})
    return row


def run_pass(db: Session, jf=None, now: Optional[datetime] = None, limit: int = CHECKS_PER_PASS) -> dict:
    """One pass, newest recordings first. Returns what it did."""
    now = now or datetime.utcnow()
    jf = jf if jf is not None else J.jellyfin_for(db)
    out = {"ok": 0, "retimed": 0, "not_fixed": 0, "failed": 0, "waiting": 0, "premiere": 0}
    if jf is None:
        return out
    with _lock:
        libs, _ = J.recordings_libraries(jf, any_library=True)
        videos = {}
        for lib in libs:
            videos.update((J._id(i.get("Id")), i) for i in J._children(jf, lib["id"], folders=False))
        if not videos:
            return out
        rows = {r.item_id: r for r in db.query(RecordingCheck).all()}
        busy, now_on = J._busy(jf, now)
        checked = 0
        for item in sorted(videos.values(), key=lambda v: v.get("DateCreated") or "", reverse=True):
            iid = J._id(item.get("Id"))
            row = rows.get(iid)
            if row is not None and not _due(row, now) and (row.premiere_set is not None or row.state not in DATED):
                continue
            ctx = J.context(db, item)
            if not _finished(item, ctx, now, now_on):
                out["waiting"] += 1
                continue
            try:
                if _due(row, now):
                    if busy or checked >= limit:
                        out["waiting"] += 1
                        continue
                    checked += 1
                    row = check(db, jf, item, row, now)
                    if row is None or row.updated_at != now:     # the plugin: it still records
                        out["waiting"] += 1
                        continue
                    rows[iid] = row
                    out[row.state if row.state in out else "ok" if row.state in DONE else "failed"] += 1
                if row is not None and row.premiere_set is None and row.state in DATED:
                    done = set_premiere(jf, item.get("Id"), ctx["start"] or ctx["created"])
                    if done is not None:
                        row.premiere_set = 1 if done else 0
                        out["premiere"] += int(done)
            except PluginTooOld as e:
                logger.info(f"[Recording check] {e}")
                db.commit()
                break
            except requests.RequestException as e:
                logger.warning(f"[Recording check] “{item.get('Name')}”: Jellyfin didn't answer: {e}")
                out["failed"] += 1
            db.commit()
    return out


def run_scheduled() -> None:
    db = SessionLocal()
    try:
        summary = run_pass(db)
        if any(v for k, v in summary.items() if k != "waiting"):
            logger.info(f"[Recording check] pass: {summary}")
    except Exception as e:
        db.rollback()
        logger.warning(f"[Recording check] pass failed: {e}")
    finally:
        db.close()

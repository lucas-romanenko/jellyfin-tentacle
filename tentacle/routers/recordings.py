"""Recordings: whether Jellyfin is recording (or about to), for anything that
restarts Tentacle or Jellyfin.

A recording isn't a playback session: Jellyfin's /Sessions shows nothing
while it records, and nothing in the minutes of pre-padding before. A deploy
or an updater that waits for "nothing playing" would restart the server in
the middle of a game. GET /api/live/recordings/busy says when it is safe.
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from models.database import get_db, get_setting
from routers.auth import require_internal_or_admin

logger = logging.getLogger(__name__)
router = APIRouter()

# A timer whose recording (start minus pre-padding) begins within this many
# minutes counts as busy: a restart takes a minute or two, and a timer that
# fires while Jellyfin is down is lost.
BUSY_SOON_MINUTES = 20


def busy_from_timers(items: list, now: datetime, soon_minutes: int = BUSY_SOON_MINUTES) -> dict:
    """What Jellyfin's timers say: recording now (InProgress, or a New timer
    already inside its padded window: Jellyfin retrying an open), starting
    within `soon_minutes`, and the next recording's start after that."""
    from routers.livetv import _parse_timer_date, _timer_seconds
    recording, soon, next_start = [], [], None
    for t in items or []:
        status = t.get("Status")
        if status not in ("InProgress", "New"):
            continue
        start = _parse_timer_date(t.get("StartDate"))
        end = _parse_timer_date(t.get("EndDate"))
        if start is None:
            continue
        rec_start = start - timedelta(seconds=_timer_seconds(t.get("PrePaddingSeconds")))
        rec_end = (end or start) + timedelta(seconds=_timer_seconds(t.get("PostPaddingSeconds")))
        if rec_end <= now:
            continue   # a timer Jellyfin never started stays New for ever
        what = {"name": t.get("Name") or "", "channel": t.get("ChannelName") or "",
                "starts": rec_start.isoformat() + "Z", "ends": rec_end.isoformat() + "Z"}
        if status == "InProgress" or rec_start <= now:
            recording.append(what)
        elif rec_start <= now + timedelta(minutes=soon_minutes):
            soon.append(what)
        elif next_start is None or rec_start < next_start:
            next_start = rec_start
    return {"recording": recording, "starting_soon": soon,
            "next_start": next_start.isoformat() + "Z" if next_start else None}


@router.get("/api/live/recordings/busy", dependencies=[Depends(require_internal_or_admin)])
def recordings_busy(soon_minutes: int = Query(BUSY_SOON_MINUTES, ge=0, le=24 * 60),
                    db: Session = Depends(get_db)):
    """busy: Jellyfin is recording, or a recording starts (with its
    pre-padding) within `soon_minutes`; a restart of Tentacle or Jellyfin
    now would cut it. `known` is false when Jellyfin couldn't be asked: a
    caller should treat that as busy (it can't tell)."""
    from routers.livetv import _jellyfin_timers, _stream_slots
    url = (get_setting(db, "jellyfin_url", "") or "").strip()
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    # Tentacle's own broker knows a recording that is pulling a stream now,
    # even when Jellyfin can't be asked.
    pulling = _stream_slots.recording_active()
    out = {"busy": pulling, "known": False, "recording": [], "starting_soon": [], "next_start": None,
           "tentacle_recording_streams": pulling}
    if not url or not key:
        out["reason"] = "Jellyfin isn't connected"
        return out
    try:
        data = _jellyfin_timers(url, key)
    except Exception as e:
        logger.info(f"[Recordings] busy: Jellyfin's timers not read: {e}")
        out["reason"] = "Jellyfin's recordings couldn't be read"
        return out
    out.update(busy_from_timers((data or {}).get("Items") or [], datetime.utcnow(), soon_minutes))
    out["known"] = True
    out["busy"] = bool(pulling or out["recording"] or out["starting_soon"])
    return out

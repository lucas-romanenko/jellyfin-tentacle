"""Keep Tentacle's own background work off the provider while it is busy.

An Xtream account tolerates very few connections, and going over the
limit does not refuse the newcomer: the provider answers the ALREADY OPEN
stream's next request with 509, and that stream is usually a recording.
Measured on one household (2026-09-24): three 509 storms on one NHL
recording, and the nightly VOD sync (25 minutes of player_api and
get_series_info calls) was running through two of them alongside a movie
someone had started. The health sweep already stands aside for live TV;
nothing else did.

`wait_until_quiet` is a one-off wait for a short job: block (polling) while
a live stream or recording is being proxied, up to a configurable limit,
then go ahead anyway -- a household that watches live TV every evening must
still get its catalogue synced eventually. `JobPause` is the same wait for
a LONG job that pauses repeatedly (the sync, at every category boundary),
with ONE budget shared by all of its pauses, so a recording that starts
mid-sync is honoured too without the job being held for the limit at each
pause. Both are safe to call from the scheduler's threads: the live-stream
check only reads module state.
"""
import logging
import threading
import time
from typing import Callable, Optional

from models.database import get_setting

logger = logging.getLogger(__name__)

# How long a background job waits for live TV / a recording to finish before
# running anyway. 0 = never wait.
DEFAULT_DEFER_SECONDS = 4 * 3600
POLL_SECONDS = 30.0


def live_streams_active() -> bool:
    """True while Tentacle is proxying at least one live stream (a viewer or a
    recording -- from here they look the same, and both matter)."""
    from services.stream_health import _live_streams_active
    return _live_streams_active()


def recording_protected(db) -> bool:
    """Recording protection is on (livetv_protect_recordings) and a recording
    is being pulled right now. Then background provider work waits for as
    long as the recording runs -- whatever provider_jobs_defer_while_live_seconds
    says, and without spending that budget: a recording ends by itself, and
    on the accounts this setting is for, one provider request next to it is
    what cuts it."""
    try:
        from routers import livetv
        if not livetv._protect_recordings(db):
            return False
        return livetv._stream_slots.recording_active()
    except Exception:
        return False


def refuse_while_recording(db, what: str) -> None:
    """For a button that would contact the provider now: with recording
    protection on and a recording running, answer 503 instead."""
    if recording_protected(db):
        from fastapi import HTTPException
        logger.warning(f"[Provider] {what} refused: a recording is running and recording protection is on")
        raise HTTPException(503, f"{what} would contact the provider while a recording is running, and "
                                 f"recording protection is on (livetv_protect_recordings). Try again once "
                                 f"the recording has finished.")


# Wall time spent in wait_for_recordings, per sync run (key None = not
# tied to a run: the nightly EPG wait, discovery), so the sync status route
# does not take a run that waits for a protected recording for a stuck one
# (routers.sync). WALL time: while any waiter of a run is active the clock
# runs once, however many wait at the same moment. key -> {"active": waiters
# in progress, "since": monotonic start of the current stretch, "seconds":
# finished stretches}.
_protected_wait: "dict" = {}
_protected_wait_lock = threading.Lock()


def _wait_enter(key) -> None:
    with _protected_wait_lock:
        e = _protected_wait.setdefault(key, {"active": 0, "since": None, "seconds": 0.0})
        if e["active"] == 0:
            e["since"] = time.monotonic()
        e["active"] += 1


def _wait_leave(key) -> None:
    with _protected_wait_lock:
        e = _protected_wait.get(key)
        if e is None or e["active"] <= 0:
            return
        e["active"] -= 1
        if e["active"] == 0 and e["since"] is not None:
            e["seconds"] += max(0.0, time.monotonic() - e["since"])
            e["since"] = None


def protected_wait_state(run_id=None) -> tuple:
    """(this run is waiting for a protected recording right now, wall
    seconds it has waited so far, the current stretch included)."""
    with _protected_wait_lock:
        e = _protected_wait.get(run_id)
        if e is None:
            return False, 0.0
        ongoing = (time.monotonic() - e["since"]) if e["active"] and e["since"] is not None else 0.0
        return e["active"] > 0, e["seconds"] + max(0.0, ongoing)


def forget_protected_waits(keep=()) -> None:
    """Drop the tallies of runs that are no longer running (and not waiting)."""
    keep = set(keep)
    with _protected_wait_lock:
        for key in [k for k, e in _protected_wait.items() if k not in keep and not e["active"]]:
            del _protected_wait[key]


def reset_protected_wait_seconds() -> None:
    with _protected_wait_lock:
        _protected_wait.clear()


def wait_for_recordings(db, what: str, cancel_check: Optional[Callable[[], bool]] = None,
                        poll_seconds: float = POLL_SECONDS, max_seconds: Optional[float] = None,
                        run_id=None) -> bool:
    """Block while recording_protected(). True when free to go (at once if
    protection is off or nothing is recording), False if cancelled or --
    with `max_seconds` -- if the recording is still running after that.
    `run_id`: the sync run the wait is booked to (protected_wait_state)."""
    if not recording_protected(db):
        return True
    started = time.monotonic()
    logger.info(f"[Provider] {what} is waiting: a recording is running and recording protection is on")
    _wait_enter(run_id)
    try:
        waited = 0.0
        while recording_protected(db):
            if cancel_check and cancel_check():
                return False
            if max_seconds is not None and waited >= max_seconds:
                logger.warning(f"[Provider] {what} waited {waited / 60:.0f} min for a protected recording "
                               f"and is skipped this time")
                return False
            step = poll_seconds if max_seconds is None else min(poll_seconds, max(0.0, max_seconds - waited))
            time.sleep(step)
            waited += step
    finally:
        _wait_leave(run_id)
    logger.info(f"[Provider] {what} resuming after {time.monotonic() - started:.0f}s: the recording has finished")
    return True


# The nightly EPG download waits at most this long for a protected
# recording, then is skipped until the next night: everything after it in
# the nightly job (sweeps, playlists, home rows) must not be held for a
# whole evening of recordings, and yesterday's guide still covers days.
EPG_WAIT_FOR_RECORDING_SECONDS = 3600.0


def defer_seconds(db) -> float:
    """The configured maximum wait; garbage keeps the default."""
    raw = get_setting(db, "provider_jobs_defer_while_live_seconds", "")
    try:
        return max(0.0, float(raw)) if raw.strip() else float(DEFAULT_DEFER_SECONDS)
    except (ValueError, AttributeError):
        return float(DEFAULT_DEFER_SECONDS)


def wait_until_quiet(db, what: str, cancel_check: Optional[Callable[[], bool]] = None,
                     max_seconds: Optional[float] = None, poll_seconds: float = POLL_SECONDS) -> bool:
    """Block while a live stream is active. Returns True when the provider is
    quiet (immediately if it already was), False when the wait ran out or was
    cancelled and the caller should decide for itself."""
    limit = defer_seconds(db) if max_seconds is None else max_seconds
    if not recording_protected(db) and (limit <= 0 or not live_streams_active()):
        return True
    started = time.monotonic()
    logger.info(f"[Provider] {what} is waiting: a live stream or recording is running "
                f"(will wait up to {limit / 60:.0f} min)")
    waited = 0.0        # time that counts against the limit (not a protected recording's)
    while True:
        if cancel_check and cancel_check():
            return False
        protected = recording_protected(db)
        if waited >= limit and not protected:
            logger.warning(f"[Provider] {what} waited {waited / 60:.0f} min for live TV to finish "
                           f"and is going ahead anyway")
            return False
        step = poll_seconds if protected else min(poll_seconds, max(0.0, limit - waited))
        time.sleep(step)
        if not protected:
            waited += step
        if not live_streams_active() and not recording_protected(db):
            logger.info(f"[Provider] {what} resuming after {time.monotonic() - started:.0f}s: "
                        f"no live stream is running")
            return True


class JobPause:
    """One wait budget for a whole job.

    A long job (the sync) pauses at several points; the budget is spent
    across ALL of them, so "wait up to 4 hours" means four hours for the
    job, not four hours per pause -- otherwise a channel left playing all
    evening could keep the nightly sync from ever finishing. Once the
    budget is spent the job goes ahead without waiting again."""

    def __init__(self, db, what: str, cancel_check: Optional[Callable[[], bool]] = None,
                 poll_seconds: float = POLL_SECONDS):
        self.db = db
        self.what = what
        self.run_id = None      # the sync run this job's waits are booked to (set by sync_provider)
        self.cancel_check = cancel_check
        self.limit = defer_seconds(db)
        self.poll_seconds = poll_seconds
        self.spent = 0.0
        self._exhausted_logged = False

    def _protected(self) -> bool:
        return recording_protected(self.db)

    def would_wait(self) -> bool:
        """True when a call now would block: live TV is on and budget is left,
        or a recording runs under recording protection."""
        return self._protected() or (self.limit > 0 and self.spent < self.limit and live_streams_active())

    def __call__(self) -> bool:
        """Block while a live stream is active, within what is left of the
        budget. True when the provider is quiet, False when the job should go
        ahead regardless (budget spent, or cancelled). While a recording runs
        under recording protection it waits regardless, spending no budget."""
        if self._protected():
            if not wait_for_recordings(self.db, self.what, self.cancel_check, self.poll_seconds,
                                       run_id=self.run_id):
                return False            # cancelled
        if self.limit <= 0 or not live_streams_active():
            return True
        if self.spent >= self.limit:
            if not self._exhausted_logged:
                self._exhausted_logged = True
                logger.warning(f"[Provider] {self.what} has waited its full {self.limit / 60:.0f} min "
                               f"for live TV to finish and is going ahead anyway")
            return False
        started = time.monotonic()
        logger.info(f"[Provider] {self.what} is waiting: a live stream or recording is running "
                    f"({(self.limit - self.spent) / 60:.0f} min of waiting left for this run)")
        while True:
            if self.cancel_check and self.cancel_check():
                return False
            remaining = self.limit - self.spent
            if remaining <= 0:
                return self()          # logs "going ahead" once
            if self._protected():
                # A recording started while this job waited for a viewer:
                # wait it out without spending the budget, then carry on.
                if not wait_for_recordings(self.db, self.what, self.cancel_check, self.poll_seconds,
                                       run_id=self.run_id):
                    return False
                if not live_streams_active():
                    return True
                continue
            step = min(self.poll_seconds, remaining)
            time.sleep(step)
            self.spent += step
            if not live_streams_active():
                logger.info(f"[Provider] {self.what} resuming after {time.monotonic() - started:.0f}s: "
                            f"no live stream is running")
                return True


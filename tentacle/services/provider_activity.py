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


def wait_for_recordings(db, what: str, cancel_check: Optional[Callable[[], bool]] = None,
                        poll_seconds: float = POLL_SECONDS) -> bool:
    """Block while recording_protected(). True when free to go (at once if
    protection is off or nothing is recording), False if cancelled."""
    if not recording_protected(db):
        return True
    started = time.monotonic()
    logger.info(f"[Provider] {what} is waiting: a recording is running and recording protection is on")
    while recording_protected(db):
        if cancel_check and cancel_check():
            return False
        time.sleep(poll_seconds)
    logger.info(f"[Provider] {what} resuming after {time.monotonic() - started:.0f}s: the recording has finished")
    return True


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
            if not wait_for_recordings(self.db, self.what, self.cancel_check, self.poll_seconds):
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
                if not wait_for_recordings(self.db, self.what, self.cancel_check, self.poll_seconds):
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


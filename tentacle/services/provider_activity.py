"""Keep Tentacle's own background work off the provider while it is busy.

An Xtream account tolerates very few connections, and going over the
limit does not refuse the newcomer: the provider answers the ALREADY OPEN
stream's next request with 509, and that stream is usually a recording.
Measured on one household (2026-09-24): three 509 storms on one NHL
recording, and the nightly VOD sync (25 minutes of player_api and
get_series_info calls) was running through two of them alongside a movie
someone had started. The health sweep already stands aside for live TV;
nothing else did.

`wait_until_quiet` is for the scheduler's jobs: block (polling) while a
live stream or recording is being proxied, up to a configurable limit, then
go ahead anyway -- a household that watches live TV every evening must
still get its catalogue synced eventually. `pause_while_live` is the same
wait for use INSIDE a long job, between provider calls, so a recording that
starts mid-sync is honoured too. Both are safe to call from the scheduler's
threads: the live-stream check only reads module state.
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
    if limit <= 0 or not live_streams_active():
        return True
    started = time.monotonic()
    logger.info(f"[Provider] {what} is waiting: a live stream or recording is running "
                f"(will wait up to {limit / 60:.0f} min)")
    while True:
        if cancel_check and cancel_check():
            return False
        waited = time.monotonic() - started
        if waited >= limit:
            logger.warning(f"[Provider] {what} waited {waited / 60:.0f} min for live TV to finish "
                           f"and is going ahead anyway")
            return False
        time.sleep(min(poll_seconds, max(0.0, limit - waited)))
        if not live_streams_active():
            logger.info(f"[Provider] {what} resuming after {time.monotonic() - started:.0f}s: "
                        f"no live stream is running")
            return True


def pause_while_live(db, what: str, cancel_check: Optional[Callable[[], bool]] = None) -> None:
    """For use between provider calls inside a long job."""
    wait_until_quiet(db, what, cancel_check)

"""The music module's one background worker.

Everything that talks to Lidarr or MusicBrainz in the background runs here,
one job at a time: pinning a request, a webhook event, the daily reconcile.
User requests go first; the reconcile is low priority and, between artists,
steps aside for anything more urgent that came in. One job at a time is also
what keeps Tentacle from asking Lidarr for too much.
"""
import itertools
import logging
import queue
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

URGENT, NORMAL, BACKGROUND = 0, 1, 2

_queue: "queue.PriorityQueue" = queue.PriorityQueue()
_seq = itertools.count()
_thread = None
_thread_lock = threading.Lock()
_local = threading.local()
state = {"running": None, "last_error": None}


def submit(fn, priority: int = NORMAL, label: str = "") -> None:
    """Run fn(db) on the worker thread with its own database session."""
    _queue.put((priority, next(_seq), label or getattr(fn, "__name__", "job"), fn))
    _ensure_thread()


def _ensure_thread():
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="music-worker", daemon=True)
            _thread.start()


def _run(label: str, fn):
    from models.database import SessionLocal
    db = SessionLocal()
    previous = state["running"]
    state["running"] = label
    try:
        fn(db)
    except Exception as e:
        message = getattr(e, "message", None) or str(e)
        logger.error(f"[Music] {label} failed: {message}", exc_info=not hasattr(e, "message"))
        record_error(f"{label}: {message}")
    finally:
        state["running"] = previous
        db.close()


def _loop():
    while True:
        priority, _, label, fn = _queue.get()
        _local.priority = priority
        try:
            _run(label, fn)
        finally:
            _queue.task_done()


def run_urgent_jobs() -> None:
    """Called by a long background job between steps: run anything more urgent first."""
    current = getattr(_local, "priority", BACKGROUND)
    while True:
        with _queue.mutex:
            if not _queue.queue or _queue.queue[0][0] >= current:
                return
        try:
            priority, _, label, fn = _queue.get_nowait()
        except queue.Empty:
            return
        if priority >= current:  # lost a race with another getter: put it back
            _queue.put((priority, next(_seq), label, fn))
            _queue.task_done()
            return
        try:
            _run(label, fn)
        finally:
            _queue.task_done()


def record_error(message: str) -> None:
    """Remember the last failure (shown by /api/music/status), across restarts too."""
    import json
    from models.database import SessionLocal, set_setting
    state["last_error"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "message": message}
    db = SessionLocal()
    try:
        set_setting(db, "music_last_error", json.dumps(state["last_error"]))
    except Exception:
        pass
    finally:
        db.close()


def pending() -> int:
    return _queue.qsize()

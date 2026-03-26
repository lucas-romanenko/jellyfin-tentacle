"""
Tentacle - Log Streamer
In-memory ring buffer for sync logs + SSE endpoint.
Lets the frontend watch sync progress in real time.
"""

import asyncio
import logging
import queue
import threading
from datetime import datetime
from collections import deque
from typing import AsyncGenerator

# Global ring buffer - last 500 log lines
_log_buffer: deque = deque(maxlen=500)
_subscribers: list = []
_lock = threading.Lock()


class SSELogHandler(logging.Handler):
    """Custom logging handler that writes to the ring buffer and notifies subscribers"""

    LEVEL_COLORS = {
        "DEBUG": "gray",
        "INFO": "info",
        "WARNING": "warn",
        "ERROR": "error",
        "CRITICAL": "error",
    }

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            entry = {
                "ts": datetime.utcnow().isoformat(),
                "level": record.levelname,
                "color": self.LEVEL_COLORS.get(record.levelname, "info"),
                "msg": msg,
                "logger": record.name.split(".")[-1],
            }
            with _lock:
                _log_buffer.append(entry)
                # Notify all active SSE subscribers
                dead = []
                for q in _subscribers:
                    try:
                        q.put_nowait(entry)
                    except:
                        dead.append(q)
                for q in dead:
                    _subscribers.remove(q)
        except Exception:
            pass


def setup_sse_logging():
    """Attach SSE handler to tentacle loggers"""
    handler = SSELogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(logging.DEBUG)

    # Attach to relevant loggers only — NOT root, to avoid double-logging
    for name in ["tentacle", "services.sync", "services.tmdb", "services.radarr",
                 "services.jellyfin", "routers.sync", "__main__", "main"]:
        logging.getLogger(name).addHandler(handler)


def get_recent_logs(limit: int = 200) -> list:
    with _lock:
        return list(_log_buffer)[-limit:]


async def log_event_generator(last_id: int = 0) -> AsyncGenerator[str, None]:
    """SSE generator — sends buffered logs then streams new ones"""
    q: queue.Queue = queue.Queue(maxsize=200)

    # First send buffered logs
    with _lock:
        buffered = list(_log_buffer)
        _subscribers.append(q)

    # Replay buffer
    for i, entry in enumerate(buffered):
        if i >= last_id:
            yield _format_sse(entry, i)

    # Stream new logs
    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                # Non-blocking check every 0.5s
                entry = await loop.run_in_executor(None, lambda: q.get(timeout=0.5))
                yield _format_sse(entry, -1)
            except queue.Empty:
                # Send keepalive
                yield ": keepalive\n\n"
            except asyncio.CancelledError:
                break
    finally:
        with _lock:
            if q in _subscribers:
                _subscribers.remove(q)


def _format_sse(entry: dict, idx: int) -> str:
    import json
    data = json.dumps(entry)
    if idx >= 0:
        return f"id: {idx}\ndata: {data}\n\n"
    return f"data: {data}\n\n"


# ── Library Event Channel ────────────────────────────────────────────────

_lib_subscribers: list = []
_lib_lock = threading.Lock()


def emit_library_event(event_type: str, data: dict):
    """Emit a library change event to all SSE subscribers.
    event_type: 'movie_added' | 'movie_removed'
    data: {tmdb_id, title, year, poster_path, source, source_tag, tags, in_library, media_type}
    """
    entry = {"event": event_type, **data}
    with _lib_lock:
        dead = []
        for q in _lib_subscribers:
            try:
                q.put_nowait(entry)
            except:
                dead.append(q)
        for q in dead:
            _lib_subscribers.remove(q)


async def library_event_generator() -> AsyncGenerator[str, None]:
    """SSE generator for library change events"""
    import json
    q: queue.Queue = queue.Queue(maxsize=100)

    with _lib_lock:
        _lib_subscribers.append(q)

    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                entry = await loop.run_in_executor(None, lambda: q.get(timeout=1.0))
                event_type = entry.pop("event", "update")
                yield f"event: {event_type}\ndata: {json.dumps(entry)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"
            except asyncio.CancelledError:
                break
    finally:
        with _lib_lock:
            if q in _lib_subscribers:
                _lib_subscribers.remove(q)

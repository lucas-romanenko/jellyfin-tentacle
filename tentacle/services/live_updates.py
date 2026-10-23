"""
Live updates: what changed reaches every open screen within a second or two.

One channel per signed-in dashboard tab (GET /api/updates, server-sent
events) carries short "this changed" notes by topic; the page re-reads what
it shows for that topic. Nothing else is in a note (no titles, no names), so
a note can go to more people than strictly need it.

Topics:
  requests       a request was made, approved, declined, failed or taken back
  activity       a download came, went, changed state or moved on (progress
                 notes carry {"progress": true}: only an open Activity cares)
  notifications  a message for this person ("ready to watch", "said yes")
  people         someone was added, changed, locked or removed (admins)
  me             an admin changed this person's own settings (kids, requests)
  health         a problem appeared or went away (admins)
  home           this person's home screen or the default home changed
  livetv         the Live TV lineup, pool or folded networks changed
  mychannels     this person's favourite channels changed
  groups         new provider groups or categories to look at (admins)
  youtube        who follows which YouTube channel changed (My YouTube,
                 Sources → YouTube)
  library        a title came into or left the library ({"event": ...})
  recordings     a recording rule changed, or what it will record (everyone:
                 recordings are the household's)

Jellyfin Web and the Tentacle TV app hear about requests, activity and
notifications through the plugin (POST /Tentacle/Notify, one Jellyfin
socket message per person: tell_clients).

Tentacle runs one uvicorn worker, so the hub is in memory. publish() is
called from request threads, the scheduler and the event loop alike.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# How long a client may wait for a keep-alive before it assumes the line is dead.
KEEPALIVE_SECONDS = 20
QUEUE_SIZE = 200

_lock = threading.Lock()
_subs: "set[_Sub]" = set()
_seq = 0


class _Sub:
    __slots__ = ("user_id", "is_admin", "loop", "queue", "overflowed")

    def __init__(self, user_id: int, is_admin: bool, loop: asyncio.AbstractEventLoop):
        self.user_id = user_id
        self.is_admin = is_admin
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.overflowed = False

    def offer(self, note: dict) -> None:
        """On the event loop. A full queue means a stuck reader: it gets one
        "resync" instead (re-read everything), never a growing backlog."""
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(note)
        except asyncio.QueueFull:
            self.overflowed = True
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait({"topic": "resync"})

    def bye(self) -> None:
        """On the event loop: end this tab's channel now."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait({"topic": "bye"})


def active() -> bool:
    """Anyone to tell: the running app (the plugin) or an open tab."""
    return _clients_on or subscribers() > 0


def subscribers() -> int:
    with _lock:
        return len(_subs)


def connected_user_ids() -> set:
    with _lock:
        return {s.user_id for s in _subs}


def publish(topic: str, *, user_ids: Iterable[int] = (), admins: bool = False,
            everyone: bool = False, data: Optional[dict] = None) -> int:
    """Tell the open dashboards of `user_ids` (and every admin's with
    `admins`, everybody's with `everyone`) that `topic` changed. Never
    raises. Returns how many tabs were told."""
    global _seq
    try:
        users = {int(u) for u in user_ids if u is not None}
    except (TypeError, ValueError):
        users = set()
    with _lock:
        _seq += 1
        note = {"topic": topic, "seq": _seq, **({"data": data} if data else {})}
        targets = [s for s in _subs if everyone or s.user_id in users or (admins and s.is_admin)]
    for s in targets:
        try:
            s.loop.call_soon_threadsafe(s.offer, note)
        except RuntimeError:
            pass  # its loop is closed: the tab is gone
    return len(targets)


def _format(note: dict) -> str:
    return f"event: change\ndata: {json.dumps(note, separators=(',', ':'))}\n\n"


async def stream(user_id: int, is_admin: bool, is_disconnected=None):
    """The SSE body for one tab. First a "hello" (the tab re-reads everything
    it shows when it (re)connects: what changed while it was away is not
    replayed), then notes as they come, a comment line as keep-alive."""
    sub = _Sub(user_id, bool(is_admin), asyncio.get_running_loop())
    with _lock:
        _subs.add(sub)
    activity_watch.wake()
    try:
        yield f"retry: 3000\nevent: hello\ndata: {json.dumps({'seq': _seq, 'keepalive': KEEPALIVE_SECONDS})}\n\n"
        while True:
            try:
                note = await asyncio.wait_for(sub.queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                if is_disconnected is not None and await is_disconnected():
                    break
                yield ": keepalive\n\n"
                continue
            if note.get("topic") == "bye":
                break
            if note.get("topic") == "resync":
                sub.overflowed = False
            yield _format(note)
    finally:
        with _lock:
            _subs.discard(sub)


# ── Jellyfin Web and the TV app, through the plugin ───────────────────────────

CLIENT_TOPICS = ("requests", "activity", "notifications", "recordings")
CLIENT_DELAY = 0.3   # gathers a burst (approve = requests + notifications + activity)
_client_lock = threading.Lock()
_client_pending: "list[tuple]" = []        # (topic, Tentacle user ids, admins too)
_client_timer: Optional[threading.Timer] = None
_clients_on = False                        # start(): only the running app talks to the plugin


def tell_clients(topic: str, *, user_ids: Iterable[int] = (), admins: bool = False) -> None:
    """Have the plugin tell these people's Jellyfin clients (Jellyfin Web's
    Tentacle scripts, the TV app) that `topic` changed. Gathered for a moment
    and sent in the background; never raises, never blocks."""
    global _client_timer
    if topic not in CLIENT_TOPICS or not _clients_on:
        return
    try:
        ids = frozenset(int(u) for u in user_ids if u is not None)
    except (TypeError, ValueError):
        return
    if not ids and not admins:
        return
    with _client_lock:
        _client_pending.append((topic, ids, admins))
        if _client_timer is None:
            _client_timer = threading.Timer(CLIENT_DELAY, _send_to_clients)
            _client_timer.daemon = True
            _client_timer.start()


def _send_to_clients() -> None:
    global _client_timer
    with _client_lock:
        pending = list(_client_pending)
        _client_pending.clear()
        _client_timer = None
    if not pending:
        return
    try:
        from models.database import SessionLocal, TentacleUser, get_setting
        db = SessionLocal()
        try:
            url = (get_setting(db, "jellyfin_url", "") or "").strip().rstrip("/")
            key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
            people = db.query(TentacleUser.id, TentacleUser.jellyfin_user_id, TentacleUser.is_admin).all()
        finally:
            db.close()
        if not url or not key:
            return
        topics_of: "dict[str, set]" = {}   # Jellyfin user id (no dashes) -> topics
        for pk, jf_id, is_admin in people:
            for topic, ids, admins in pending:
                if jf_id and (pk in ids or (admins and is_admin)):
                    topics_of.setdefault(jf_id.replace("-", "").lower(), set()).add(topic)
        post_notify(url, key, topics_of)
    except Exception as e:
        logger.info(f"[Live] couldn't tell the Jellyfin clients: {e}")


def post_notify(url: str, key: str, topics_of: dict) -> None:
    """POST /Tentacle/Notify, one call per distinct set of topics (usually one)."""
    import requests
    by_topics: "dict[tuple, list]" = {}
    for jf_id, topics in topics_of.items():
        by_topics.setdefault(tuple(sorted(topics)), []).append(jf_id)
    for topics, users in by_topics.items():
        r = requests.post(f"{url}/Tentacle/Notify", headers={"X-Emby-Token": key},
                          params={"userIds": ",".join(sorted(users)), "topics": ",".join(topics)},
                          timeout=5)
        if r.status_code == 404:
            logger.debug("[Live] the plugin has no /Tentacle/Notify yet (older plugin)")
            return


def start() -> None:
    """The app is up: talk to the plugin, watch Radarr/Sonarr's queues, and
    end the open channels when the server is asked to stop (uvicorn waits
    for every open response before it stops; a channel never ends by itself,
    so a restart waited for Docker to kill the process)."""
    global _clients_on
    _clients_on = True
    activity_watch.start()
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            before = signal.getsignal(sig)
        except (ValueError, OSError):
            continue
        if not callable(before) or getattr(before, "_live_updates", False):
            continue

        def on_stop(signum, frame, before=before):
            close_all()
            before(signum, frame)
        on_stop._live_updates = True
        try:
            signal.signal(sig, on_stop)
        except ValueError:
            pass   # not the main thread (tests)


def close_all() -> None:
    """End every open channel (the tabs reconnect to the next server)."""
    with _lock:
        targets = list(_subs)
    for s in targets:
        try:
            s.loop.call_soon_threadsafe(s.bye)
        except Exception:
            pass


def stop() -> None:
    global _clients_on
    _clients_on = False
    activity_watch.stop()


def after_commit(db, topic: str, **kw) -> None:
    """publish() once `db`'s transaction is committed (a tab told sooner
    would read the old state)."""
    from sqlalchemy import event
    event.listen(db, "after_commit", lambda _s: publish(topic, **kw), once=True)


def changed(topic: str, *, user_ids: Iterable[int] = (), admins: bool = False,
            everyone: bool = False, data: Optional[dict] = None, clients: bool = False) -> None:
    """publish() to the dashboards and, with `clients`, tell_clients() too."""
    user_ids = list(user_ids or ())
    publish(topic, user_ids=user_ids, admins=admins, everyone=everyone, data=data)
    if clients:
        tell_clients(topic, user_ids=user_ids, admins=admins)


# ── Downloads: Radarr/Sonarr have no "grabbed"/progress webhook we use ────────

class _ActivityWatch:
    """Reads Radarr's and Sonarr's queues (only the queues: two small local
    calls) every FAST seconds while a dashboard is open, else every SLOW, and
    says "activity" when something came, went or changed state (dashboards
    and Jellyfin clients of its requester and the admins), or only moved on
    (dashboards, {"progress": true})."""

    FAST = 3.0
    SLOW = 15.0

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last: Optional[dict] = None
        self._problems_at = 0.0
        self._problems_sig: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-activity", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                logger.debug(f"[Live] activity watch: {e}")
            self._wake.clear()
            self._wake.wait(self.FAST if subscribers() else self.SLOW)

    PROBLEMS_EVERY = 60.0

    def _problems(self) -> None:
        """Radarr/Sonarr's own warnings (indexers failing, disk filling): the
        admins' banner and Health count, once a minute while a tab is open."""
        now = time.monotonic()
        if not subscribers() or now - self._problems_at < self.PROBLEMS_EVERY:
            return
        self._problems_at = now
        from models.database import SessionLocal
        from services import arr_insight
        db = SessionLocal()
        try:
            sig = json.dumps(sorted((p.get("app", ""), p.get("message", "")) for p in arr_insight.problems(db)))
        finally:
            db.close()
        if self._problems_sig is not None and sig != self._problems_sig:
            publish("health", admins=True)
        self._problems_sig = sig

    def tick(self) -> None:
        try:
            self._problems()
        except Exception as e:
            logger.debug(f"[Live] problems watch: {e}")
        snap = snapshot()
        if snap is None:
            return
        last, self._last = self._last, snap
        if last is None:
            return
        notify_state = {}
        notify_progress = {}
        for key in set(last) | set(snap):
            a, b = last.get(key), snap.get(key)
            if a is None or b is None or a["state"] != b["state"]:
                notify_state[key] = a or b
            elif a["progress"] != b["progress"]:
                notify_progress[key] = b
        if notify_state:
            owners = owners_of(notify_state.values())
            changed("activity", user_ids=owners, admins=True, clients=True)
        elif notify_progress:
            publish("activity", user_ids=owners_of(notify_progress.values()), admins=True,
                    data={"progress": True})


activity_watch = _ActivityWatch()


def snapshot() -> Optional[dict]:
    """What Radarr's and Sonarr's queues hold now: key -> {media_type, tmdb_id,
    state, progress (whole percent)}. None when neither is set up."""
    from models.database import SessionLocal, get_setting
    db = SessionLocal()
    try:
        apps = [(name, get_setting(db, f"{name}_url"), get_setting(db, f"{name}_api_key"))
                for name in ("radarr", "sonarr")]
    finally:
        db.close()
    apps = [a for a in apps if a[1] and a[2]]
    if not apps:
        return None
    out = {}
    for name, url, key in apps:
        records = _queue(name, url, key)
        if records is None:
            return None   # not answering: no news, rather than "everything left"
        for item in records:
            media = item.get("movie" if name == "radarr" else "series") or {}
            total = item.get("size") or 0
            left = item.get("sizeleft") or 0
            out[(name, item.get("id"))] = {
                "media_type": "movie" if name == "radarr" else "series",
                "tmdb_id": media.get("tmdbId") or 0,
                "state": (item.get("status"), item.get("trackedDownloadState"), item.get("trackedDownloadStatus")),
                "progress": int((total - left) * 100 / total) if total else 0,
            }
    try:
        from services import download_readiness
        for e in download_readiness.in_flight():
            out[("ready", e["media_type"], e["tmdb_id"], e["episode"])] = {
                "media_type": e["media_type"], "tmdb_id": e["tmdb_id"], "state": ("importing",), "progress": 100}
    except Exception:
        pass
    return out


def _queue(name: str, url: str, key: str) -> Optional[list]:
    """One app's queue, quietly (a down Radarr would log every few seconds;
    Activity itself says so)."""
    import requests
    try:
        r = requests.get(f"{url.rstrip('/')}/api/v3/queue", headers={"X-Api-Key": key},
                         params={"pageSize": 100, ("includeMovie" if name == "radarr" else "includeSeries"): True},
                         timeout=5)
        r.raise_for_status()
        return r.json().get("records", [])
    except Exception as e:
        logger.debug(f"[Live] {name} queue: {e}")
        return None


def owners_of(items: Iterable[dict]) -> set:
    """Who asked for these titles (Tentacle user ids)."""
    keys = {(i.get("media_type"), i.get("tmdb_id")) for i in items if i.get("tmdb_id")}
    if not keys:
        return set()
    from models.database import SessionLocal, DownloadRequest
    db = SessionLocal()
    try:
        rows = db.query(DownloadRequest.media_type, DownloadRequest.tmdb_id, DownloadRequest.user_id).filter(
            DownloadRequest.tmdb_id.in_({k[1] for k in keys})).all()
    finally:
        db.close()
    return {uid for mt, tid, uid in rows if (mt, tid) in keys}


def reset() -> None:
    """Tests: forget every tab and pending client message."""
    global _client_timer, _clients_on
    _clients_on = False
    with _lock:
        _subs.clear()
    with _client_lock:
        _client_pending.clear()
        if _client_timer is not None:
            _client_timer.cancel()
        _client_timer = None
    activity_watch._last = None

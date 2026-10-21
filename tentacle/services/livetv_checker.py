"""Live TV: does each channel actually play? A background stream checker.

Opens one channel at a time, reads its first bytes (real MPEG-TS, or an HLS
playlist whose first segment is real media), closes it, and stores the
result on the channel (`LiveChannel.check_*`). The pickers show it as
"Works (checked X ago)" / "Not working" / "Not checked yet".

The rule above all: a viewer or a recording always comes first. Many IPTV
accounts carry ONE stream at a time, so a test stream next to a viewer cuts
the viewer (or the provider refuses the viewer). So the checker:

  - opens nothing while anything is streaming through Tentacle (a viewer, a
    recording, a film through /api/vod), while Jellyfin plays anything (a
    VOD .strm plays straight from the provider), while another provider
    check (hourly health, stream-health sweep) runs, or within QUIET_AFTER
    seconds of the last stream open (someone zapping channels);
  - is pre-empted: every stream open (`routers.livetv`: `_StreamSlots.acquire_lease`
    and `_open_shared_upstream`) awaits `preempt()` first, which cancels the
    check in flight and waits for its connection to close, so the provider
    slot is free before the viewer's own open goes out;
  - tests one channel at a time over all providers (so never more than one
    connection per provider), a few seconds each;
  - backs off a provider that answers "busy / too many connections /
    refused" (429, 509, 458, 401, 403, 407) or can't be reached: no verdict,
    and nothing on that provider for a doubling while (5 min .. 6 h);
  - calls a channel broken only after FAILS_FOR_BROKEN failures in a row.

Order: the pool first (what people can pick: single channels and each
folded network's chosen variant), then every other enabled channel (the
other variants of folded networks), unchecked first, then oldest result.
Paced so the pool cycles in about a day: one check every
max(MIN_GAP, CYCLE / pool size) seconds.

No setting: it is always on (started from main.py's lifespan, never in
tests). `status` is what GET /api/live/checker/status shows.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin

import httpx

from models.database import SessionLocal, LiveChannel, Provider

logger = logging.getLogger(__name__)

# ── Pace ──────────────────────────────────────────────────────────────────
CYCLE_SECONDS = 24 * 3600            # a pool channel is checked again after this
VARIANT_CYCLE_SECONDS = 3 * 24 * 3600  # other variants, less often
RETRY_FAILED_SECONDS = 1800          # a first failure is checked again this soon
MIN_GAP_SECONDS = 20.0               # never two checks closer than this
MAX_IDLE_SECONDS = 600.0             # nothing due: look again at the latest after this
BUSY_POLL_SECONDS = 30.0             # someone is watching: look again after this
QUIET_AFTER_SECONDS = 120.0          # after the last stream open, wait this long
START_DELAY_SECONDS = 300.0          # after start-up (Jellyfin and the guide settle first)
FAILS_FOR_BROKEN = 2

# ── One check ─────────────────────────────────────────────────────────────
# The whole check -- connect and first bytes; HLS: playlists and the first
# segment -- within this budget, so a viewer who opens past Tentacle (a film
# Jellyfin plays straight from the provider) meets the checker's connection
# for a few seconds at most. Nothing in time = a failure (retried; broken
# only after FAILS_FOR_BROKEN in a row).
CHECK_SECONDS = 5.0
TIMEOUT = httpx.Timeout(connect=CHECK_SECONDS, read=CHECK_SECONDS, write=CHECK_SECONDS, pool=CHECK_SECONDS)
TS_BYTES = 564                       # three packets decide an MPEG-TS start
MAX_READ = 256 * 1024
# "Not now" answers, about the account rather than the channel: no verdict,
# back off the provider. 458/551 are panels' "max connections reached".
BUSY_CODES = {401, 403, 407, 429, 458, 509, 551}
# A ts open refused like this has its HLS form tried (as routers.livetv does).
FALLBACK_CODES = {400, 404, 405, 406, 415}
BACKOFF_START = 300.0
BACKOFF_MAX = 6 * 3600.0

DEFAULT_UA = "TiviMate/4.7.0 (Linux; Android 12)"

WORKS, BROKEN, UNCHECKED = "works", "broken", "unchecked"

status = {
    "running": False,
    "state": "stopped",          # stopped | starting | checking | paused | waiting | idle | backoff
    "paused_reason": None,
    "current": None,             # {"channel_id", "name", "since"}
    "last": None,                # {"channel_id", "name", "result", "detail", "at"}
    "checks": 0,
    "preempted": 0,
    "backoff": {},               # provider id -> {"until": iso, "seconds": float, "code": ...}
    "next_at": None,
}

_inflight: "Optional[asyncio.Task]" = None
_inflight_channel: Optional[int] = None
_last_open = -1e9                     # monotonic time of the last stream open (preempt)
_backoff: "dict[int, dict]" = {}      # provider id -> {"until": monotonic, "seconds", "code"}
_loop_task: "Optional[asyncio.Task]" = None


def _now() -> datetime:
    return datetime.utcnow()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


# ── Results ───────────────────────────────────────────────────────────────

def channel_result(ch) -> dict:
    """{"state": works|broken|unchecked, "at": iso|None} for one LiveChannel."""
    state = ch.check_state if ch.check_state in (WORKS, BROKEN) else UNCHECKED
    return {"state": state, "at": _iso(ch.check_at) if state != UNCHECKED else None}


def results(db, networks: "Optional[dict]" = None) -> dict:
    """Every checked channel: "c<id>" -> {"state", "at"}. A channel missing
    from the answer is not checked yet ({"state": "unchecked", "at": None}).
    A folded network's state is its chosen variant's: `networks` maps
    network id -> chosen LiveChannel id, else livetv_catalog.chosen_variants(db)
    if the catalog has it; each gives "n<nid>" too. Never calls pool()."""
    out = {}
    rows = (db.query(LiveChannel.id, LiveChannel.check_state, LiveChannel.check_at)
            .filter(LiveChannel.check_state.isnot(None)).all())
    for cid, state, at in rows:
        if state in (WORKS, BROKEN):
            out[f"c{cid}"] = {"state": state, "at": _iso(at)}
    if networks is None:
        networks = _chosen_variants(db)
    for nid, cid in (networks or {}).items():
        out[f"n{nid}"] = out.get(f"c{cid}", {"state": UNCHECKED, "at": None})
    return out


def state_of(res: dict, key: str) -> dict:
    return res.get(key) or {"state": UNCHECKED, "at": None}


def _chosen_variants(db) -> dict:
    try:
        from services import livetv_catalog
    except ImportError:
        return {}
    fn = getattr(livetv_catalog, "chosen_variants", None)
    if fn is None:
        return {}
    try:
        return dict(fn(db) or {})
    except Exception as e:
        logger.debug(f"[LiveTV check] chosen variants unknown: {e}")
        return {}


def record(db, channel_id: int, works: bool, detail: str = "", now: Optional[datetime] = None) -> None:
    """Store one verdict. Broken only after FAILS_FOR_BROKEN failures in a
    row: one bad answer keeps the earlier state (or "not checked yet")."""
    ch = db.get(LiveChannel, channel_id)
    if ch is None:
        return
    now = now or _now()
    ch.check_at = now
    ch.check_detail = (detail or "")[:200] or None
    if works:
        ch.check_state, ch.check_fails = WORKS, 0
    else:
        ch.check_fails = (ch.check_fails or 0) + 1
        if ch.check_fails >= FAILS_FOR_BROKEN:
            ch.check_state = BROKEN
    db.commit()


# ── What to check next ────────────────────────────────────────────────────

POOL_CACHE_SECONDS = 600.0
_pool_cache = {"at": -1e9, "ids": []}
_new_cache = {"at": -1e9, "ids": []}


def _pool_channel_ids(db) -> list:
    """LiveChannel ids behind the pool, in pool order: "c<id>" entries and
    each folded network's chosen variant. Cached for a few minutes (the pool
    is thousands of channels; the checker asks every round)."""
    if time.monotonic() - _pool_cache["at"] < POOL_CACHE_SECONDS:
        return list(_pool_cache["ids"])
    ids = _read_pool_ids(db)
    _pool_cache.update(at=time.monotonic(), ids=ids)
    return list(ids)


def _read_pool_ids(db) -> list:
    from services.livetv_lineups import pool
    chosen = None
    ids = []
    for e in pool(db):
        key = str(e.get("key") or "")
        cid = e.get("channel_id")
        if cid is None and key.startswith("c") and key[1:].isdigit():
            cid = int(key[1:])
        if cid is None and key.startswith("n") and key[1:].isdigit():
            if chosen is None:
                chosen = _chosen_variants(db)
            cid = chosen.get(int(key[1:])) or chosen.get(key[1:])
        if cid is None:
            m = re.match(r"^/api/live/stream/(\d+)$", str(e.get("stream_path") or ""))
            cid = int(m.group(1)) if m else None
        if cid is not None:
            ids.append(int(cid))
    return list(dict.fromkeys(ids))


def _due(ch, cycle: float, now: datetime) -> bool:
    if ch.check_at is None:
        return True
    age = (now - ch.check_at).total_seconds()
    if (ch.check_fails or 0) > 0 and ch.check_state != BROKEN:
        return age >= RETRY_FAILED_SECONDS
    return age >= cycle


def _eligible_providers(db) -> dict:
    """Providers whose channels may be checked now: Live TV on, not backed
    off, not known to be failing (an outage must not mark every channel
    broken)."""
    from services import provider_health
    try:
        health = provider_health.load_state(db)
    except Exception:
        health = {}
    mono = time.monotonic()
    out = {}
    for p in db.query(Provider).all():
        if not p.live_tv_enabled:
            continue
        if (health.get(str(p.id)) or {}).get("state") == "failing":
            continue
        b = _backoff.get(p.id)
        if b and b["until"] > mono:
            continue
        out[p.id] = p
    return out


def candidates(db, now: Optional[datetime] = None) -> list:
    """Channel ids due for a check, best first: pool channels (unchecked,
    then oldest result), then the other enabled channels (other variants of
    folded networks) the same way."""
    now = now or _now()
    providers = _eligible_providers(db)
    if not providers:
        return []
    pool_ids = _pool_channel_ids(db)
    rank = {cid: i for i, cid in enumerate(pool_ids)}
    rows = {c.id: c for c in db.query(LiveChannel).filter(
        (LiveChannel.enabled == True) | (LiveChannel.id.in_(pool_ids))).all()}  # noqa: E712

    def age_key(ch):
        return (ch.check_at is not None, ch.check_at or datetime.min, rank.get(ch.id, 0), ch.id)

    first = sorted((rows[c] for c in pool_ids if c in rows), key=age_key)
    rest = sorted((c for cid, c in rows.items() if cid not in rank), key=age_key)
    due = [c for c in first if c.provider_id in providers and _due(c, CYCLE_SECONDS, now)]
    out = [c.id for c in due if c.check_at is None]
    # Channels of the provider's new groups, never checked, the groups the
    # admin is looking at first: the review shows which of them work, so
    # they go before the pool's re-checks (each is checked once).
    new_ids = [cid for cid in _new_group_ids(db) if cid not in rank]
    new_rows = {}
    for i in range(0, len(new_ids), 500):
        new_rows.update((c.id, c) for c in db.query(LiveChannel).filter(LiveChannel.id.in_(new_ids[i:i + 500])))
    out += [cid for cid in new_ids if cid in new_rows and new_rows[cid].provider_id in providers
            and new_rows[cid].check_state is None and _due(new_rows[cid], CYCLE_SECONDS, now)]
    out += [c.id for c in due if c.check_at is not None]
    out += [c.id for c in rest if c.provider_id in providers and _due(c, VARIANT_CYCLE_SECONDS, now)]
    return list(dict.fromkeys(out))


def _new_group_ids(db) -> list:
    """Channel ids of new groups without a verdict yet, the groups the admin
    is looking at first (services/provider_new.py), cached like the pool."""
    if time.monotonic() - _new_cache["at"] < POOL_CACHE_SECONDS:
        return list(_new_cache["ids"])
    try:
        from services import provider_new
        ids = provider_new.unchecked_new_channel_ids(db)
    except Exception as e:
        logger.debug(f"[LiveTV check] New groups' channels not read: {e}")
        ids = []
    _new_cache.update(at=time.monotonic(), ids=ids)
    return list(ids)


def forget_new_groups() -> None:
    """The admin ticked groups to look at: their channels go first now."""
    _new_cache["at"] = -1e9


def gap_seconds(pool_size: int) -> float:
    return max(MIN_GAP_SECONDS, CYCLE_SECONDS / max(1, pool_size))


# ── Is anybody using the provider? ────────────────────────────────────────

def busy_reason(db) -> Optional[str]:
    """Why no check may start now, or None. Cheap checks first; Jellyfin's
    sessions last (a blocking HTTP call: run this off the event loop)."""
    try:
        from routers import livetv
        slots = livetv._stream_slots
        if slots.active > 0 or livetv._shared_streams or livetv._pending_opens:
            return "someone is watching or recording"
        if slots.recording_active():
            return "a recording is running"
    except Exception:
        return "stream state unknown"
    if time.monotonic() - _last_open < QUIET_AFTER_SECONDS:
        return "a stream opened moments ago"
    try:
        from services import provider_health, stream_health
        if provider_health.check_running():
            return "the provider health check is running"
        if stream_health._sweep_lock.locked():
            return "the stream health sweep is running"
    except Exception:
        pass
    from services.provider_activity import jellyfin_playing
    if jellyfin_playing(db):
        return "Jellyfin is playing something"
    return None


async def preempt(reason: str = "a stream is opening") -> bool:
    """Called before every stream open: note the activity, and stop a check
    in flight at once, returning only once its connection is closed (at most
    2 s), so the provider slot is the viewer's. True when one was stopped."""
    global _last_open
    _last_open = time.monotonic()
    task = _inflight
    if task is None or task.done():
        return False
    task.cancel()
    status["preempted"] += 1
    status["state"], status["paused_reason"] = "paused", reason
    logger.info(f"[LiveTV check] Stopped the check of channel {_inflight_channel}: {reason}")
    try:
        await asyncio.wait({task}, timeout=2.0)
    except Exception:
        pass
    return True


# ── One check ─────────────────────────────────────────────────────────────

def _ts_start(first: bytes) -> bool:
    from routers.livetv import _ts_sync_offset
    return _ts_sync_offset(first) is not None


def _first_uri(playlist: str) -> Optional[str]:
    for line in playlist.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


async def _read_head(resp, need: int) -> bytes:
    head = b""
    async for piece in resp.aiter_bytes():
        head += piece
        if len(head) >= need or len(head) >= MAX_READ:
            break
    return head


async def probe(client: httpx.AsyncClient, url: str, ua: str, hops: int = 3) -> tuple:
    """(verdict, code, detail): verdict "works" | "fail" | "busy". Follows an
    HLS playlist to its first segment (at most `hops` playlists)."""
    try:
        async with client.stream("GET", url, headers={"User-Agent": ua}) as r:
            code = r.status_code
            if code in BUSY_CODES:
                return "busy", code, f"refused ({code})"
            if code >= 400:
                return "fail", code, f"HTTP {code}"
            ct = (r.headers.get("content-type") or "").lower()
            head = await _read_head(r, TS_BYTES)
            final_url = str(r.url)
    except httpx.ConnectError as e:
        return "busy", None, f"can't connect ({type(e).__name__})"
    except httpx.ConnectTimeout:
        return "busy", None, "no answer"
    except httpx.HTTPError as e:
        return "fail", None, f"no data ({type(e).__name__})"
    if not head:
        return "fail", code, "no data"
    if _ts_start(head):
        return "works", code, "MPEG-TS"
    text = head[:4096].decode("utf-8", "replace")
    if text.lstrip().startswith("#EXTM3U"):
        if hops <= 0:
            return "fail", code, "playlist loop"
        nxt = _first_uri(text)
        if not nxt:
            return "fail", code, "empty playlist"
        return await probe(client, urljoin(final_url, nxt), ua, hops - 1)
    from routers.livetv import _looks_like_error_page
    if _looks_like_error_page(ct, head):
        return "fail", code, "error page, not video"
    if ct.startswith(("video/", "audio/", "application/octet-stream")) or "mp4" in ct:
        return "works", code, ct or "media"
    return "fail", code, f"not video ({ct or 'unknown'})"


async def check_channel(channel_id: int) -> tuple:
    """Open one channel the way a viewer's open would (same URL choice and
    user agent) and judge its first bytes. (verdict, code, detail, provider_id)."""
    from routers.livetv import _live_open_urls

    def urls():
        db = SessionLocal()
        try:
            ch = db.get(LiveChannel, channel_id)
            if ch is None:
                return None
            p = db.get(Provider, ch.provider_id)
            url, fallback = _live_open_urls(db, p, ch)
            return url, fallback, (p.user_agent if p else None) or DEFAULT_UA, ch.provider_id
        finally:
            db.close()

    got = await asyncio.to_thread(urls)
    if got is None:
        return "gone", None, "channel removed", None
    url, fallback, ua, pid = got
    if not url.startswith(("http://", "https://")):
        return "fail", None, "no stream address", pid
    try:
        async with asyncio.timeout(CHECK_SECONDS):
            async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT) as client:
                verdict, code, detail = await probe(client, url, ua)
                if verdict == "fail" and fallback and code in FALLBACK_CODES:
                    verdict, code, detail = await probe(client, fallback, ua)
    except TimeoutError:
        return "fail", None, f"no video within {CHECK_SECONDS:.0f} s", pid
    return verdict, code, detail, pid


def _back_off(pid: int, code, detail: str) -> float:
    prev = _backoff.get(pid)
    seconds = min(BACKOFF_MAX, prev["seconds"] * 2) if prev else BACKOFF_START
    _backoff[pid] = {"until": time.monotonic() + seconds, "seconds": seconds, "code": code}
    status["backoff"][str(pid)] = {"until": _iso(_now() + timedelta(seconds=seconds)),
                                   "seconds": seconds, "code": code, "detail": detail}
    logger.info(f"[LiveTV check] Provider {pid} said {detail}: no checks on it for {seconds / 60:.0f} min")
    return seconds


def _clear_backoff(pid) -> None:
    _backoff.pop(pid, None)
    status["backoff"].pop(str(pid), None)


# ── The loop ──────────────────────────────────────────────────────────────

def _pick(db) -> tuple:
    """(channel id or None, its name, pool size)."""
    due = candidates(db)
    pool_size = len(_pool_channel_ids(db))
    if not due:
        return None, None, pool_size
    ch = db.get(LiveChannel, due[0])
    return due[0], (ch.guide_name if ch else str(due[0])), pool_size


async def step(check=None) -> float:
    """One round: maybe check one channel. Returns how long to sleep next.
    `check`: the coroutine function doing the check (tests pass a fake)."""
    global _inflight, _inflight_channel
    check = check or check_channel

    def look():
        db = SessionLocal()
        try:
            why = busy_reason(db)
            if why:
                return why, None, None, 0
            cid, name, size = _pick(db)
            return None, cid, name, size
        finally:
            db.close()

    why, cid, name, pool_size = await asyncio.to_thread(look)
    if why:
        status["state"], status["paused_reason"] = "paused", why
        return BUSY_POLL_SECONDS
    status["paused_reason"] = None
    if cid is None:
        status["state"] = "backoff" if _backoff else "idle"
        return MAX_IDLE_SECONDS
    # Between the look (in a thread) and here a stream may have opened.
    if time.monotonic() - _last_open < QUIET_AFTER_SECONDS:
        status["state"], status["paused_reason"] = "paused", "a stream opened moments ago"
        return BUSY_POLL_SECONDS
    try:
        from routers import livetv
        if livetv._stream_slots.active > 0 or livetv._shared_streams or livetv._pending_opens:
            status["state"], status["paused_reason"] = "paused", "someone is watching or recording"
            return BUSY_POLL_SECONDS
    except Exception:
        pass

    status["state"] = "checking"
    status["current"] = {"channel_id": cid, "name": name, "since": _iso(_now())}
    _inflight_channel = cid
    _inflight = asyncio.get_running_loop().create_task(asyncio.wait_for(check(cid), CHECK_SECONDS + 3))
    try:
        verdict, code, detail, pid = await _inflight
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise                               # shutting down
        return BUSY_POLL_SECONDS                # pre-empted: no verdict
    except asyncio.TimeoutError:
        verdict, code, detail, pid = "fail", None, f"no video within {CHECK_SECONDS:.0f} s", None
    except Exception as e:
        logger.warning(f"[LiveTV check] Channel {cid} check failed: {type(e).__name__}: {e}")
        verdict, code, detail, pid = "error", None, type(e).__name__, None
    finally:
        _inflight, _inflight_channel = None, None
        status["current"] = None

    status["checks"] += 1
    status["last"] = {"channel_id": cid, "name": name, "result": verdict, "detail": detail,
                      "at": _iso(_now())}
    if verdict == "busy" and pid is not None:
        _back_off(pid, code, detail)
        return MIN_GAP_SECONDS
    if verdict in (WORKS, "fail"):
        if pid is not None and verdict == WORKS:
            _clear_backoff(pid)

        def store():
            db = SessionLocal()
            try:
                record(db, cid, verdict == WORKS, detail)
            finally:
                db.close()
        await asyncio.to_thread(store)
    return gap_seconds(pool_size)


async def run_forever(start_delay: float = START_DELAY_SECONDS) -> None:
    status.update(running=True, state="starting")
    try:
        await asyncio.sleep(start_delay)
        while True:
            try:
                wait = await step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[LiveTV check] {type(e).__name__}: {e}")
                wait = MAX_IDLE_SECONDS
            status["next_at"] = _iso(_now() + timedelta(seconds=wait))
            await asyncio.sleep(wait)
    finally:
        status.update(running=False, state="stopped", current=None)


def start() -> None:
    """From main.py's lifespan, on the event loop."""
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.get_running_loop().create_task(run_forever())
        logger.info("Live TV stream checker started: one channel at a time, only while nobody watches")


def stop() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


# ── Status for the admin ──────────────────────────────────────────────────

def summary(db) -> dict:
    """What GET /api/live/checker/status shows: the loop's state, progress
    over the pool and over everything enabled, the last check."""
    now = _now()
    pool_ids = set(_pool_channel_ids(db))
    rows = db.query(LiveChannel.id, LiveChannel.check_state, LiveChannel.check_at).filter(
        (LiveChannel.enabled == True) | (LiveChannel.id.in_(pool_ids))).all()  # noqa: E712

    def tally(sel):
        t = {"total": 0, WORKS: 0, BROKEN: 0, UNCHECKED: 0, "fresh": 0}
        for cid, state, at in sel:
            t["total"] += 1
            t[state if state in (WORKS, BROKEN) else UNCHECKED] += 1
            if at is not None and (now - at).total_seconds() < CYCLE_SECONDS:
                t["fresh"] += 1
        return t

    last_at = max((at for _, _, at in rows if at is not None), default=None)
    return {
        **{k: v for k, v in status.items()},
        "pool": tally([r for r in rows if r[0] in pool_ids]),
        "enabled": tally(rows),
        "last_checked_at": _iso(last_at),
        "gap_seconds": gap_seconds(len(pool_ids)),
        "cycle_hours": CYCLE_SECONDS / 3600,
    }

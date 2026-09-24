"""Provider VOD (movies and series episodes) served through Tentacle.

Why: a `.strm` that points straight at the provider opens a connection
Tentacle never sees. On an account that tolerates one or two connections,
a movie started on the TV makes the provider answer the recording that is
already running with HTTP 509 -- the most common way a recording was cut
into pieces on the system this was written for. Played through here, a
title takes a "vod" lease from the same broker as live TV (routers.livetv:
a recording outranks it, a viewer outranks it), the account's credentials
never leave the server, and a dropped upstream is resumed from the byte it
stopped at, which is what a separate resume proxy used to do.

How a client plays a file: ffmpeg opens it with `Range: bytes=0-`, then
seeks are new requests (`Range: bytes=N-`) -- dozens per film, seconds
apart. So the lease belongs to the PLAYBACK, keyed by the token, and is
kept for as long as requests keep coming (`vod_lease_idle_seconds` after
the last one); a seek reuses it instead of fighting itself for a slot.

The URL is `/api/vod/{movie|series}/{provider}.{stream_id}.{sig}.{ext}`
(services.vod_tokens): public, like the tuner routes, because ffmpeg
presents no session; the signature is what makes it serve only what the
sync wrote. HLS is not involved: Xtream VOD is one file, and that is what
is streamed.
"""
import asyncio
import logging
import random
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from models.database import Provider, get_db, get_setting
from services import vod_tokens
from services.ssrf import is_safe_url, lan_origin_guard
from routers import livetv

logger = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_IDLE_SECONDS = 45.0     # a seek arrives within seconds; a stopped player never does
SWEEP_SECONDS = 10.0
# A request that ends without being superseded by a newer one and without
# reaching the end of its range is a player that went away; its slot is
# released after this grace, not after the idle window.
DISCONNECT_GRACE_SECONDS = 3.0
RECONNECT_ATTEMPTS = 6          # per playback, reset by a connection that lasts
_UA_DEFAULT = "TiviMate/4.7.0 (Linux; Android 12)"
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


def vod_enabled(db) -> bool:
    return (get_setting(db, "vod_via_tentacle_enabled", "") or "").strip().lower() in ("1", "true", "yes", "on")


def idle_seconds(db) -> float:
    raw = get_setting(db, "vod_lease_idle_seconds", "") or ""
    try:
        return max(5.0, float(raw)) if raw.strip() else DEFAULT_IDLE_SECONDS
    except ValueError:
        return DEFAULT_IDLE_SECONDS


class _VodResponse(StreamingResponse):
    """A streaming response whose cleanup is tied to the RESPONSE, not to
    the generator's `finally` or a background task: when the client hangs
    up, Starlette cancels the send task, and if that lands before the body
    generator was entered neither of those runs -- but this coroutine's
    `finally` always does, cancelled or not."""

    def __init__(self, *args, finish, **kwargs):
        super().__init__(*args, **kwargs)
        self._finish = finish

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._finish()


class _Playback:
    """One title being played: its lease and when it was last asked for."""
    __slots__ = ("token", "owner", "client", "lease", "last_used", "started", "idle", "stopped",
                 "active_bodies", "opening", "generation")

    def __init__(self, token: str, owner: str, lease, idle: float, client: str = ""):
        self.token = token
        self.owner = owner
        self.client = client
        self.lease = lease
        self.idle = idle
        self.started = self.last_used = asyncio.get_running_loop().time()
        self.stopped = asyncio.Event()
        self.active_bodies = 0
        # Requests between arriving and getting their upstream open. A slot
        # is never released while one is pending: a seek that arrives during
        # the disconnect grace and waits out a 509 on open would otherwise
        # end up streaming on a playback whose lease was given away.
        self.opening = 0
        # One playback, one provider connection: a new request (a seek) ends
        # the body still streaming the previous range, so two never run at once
        # under one counted slot.
        self.generation = 0

    def touch(self):
        self.last_used = asyncio.get_running_loop().time()

    def stop(self):
        """Called by the broker when a recording or viewer takes the slot."""
        if not self.stopped.is_set():
            logger.warning(f"[VOD] {self.owner}: playback stopped — a recording or live stream needed "
                           f"its connection slot")
            self.stopped.set()


_playbacks: "dict[str, _Playback]" = {}
_sweeper: "asyncio.Task | None" = None
_pending_releases: "set[asyncio.Task]" = set()


def _release(pb: _Playback):
    if _playbacks.get(pb.token) is pb:
        del _playbacks[pb.token]
    livetv._stream_slots.release_lease(pb.lease)
    logger.info(f"[VOD] {pb.owner}: playback ended, slot released")


def _close_later(resp, client) -> None:
    """Close an upstream response and its client from a task of their own,
    so cleanup that runs during a cancellation still completes."""
    async def close():
        try:
            await resp.aclose()
        finally:
            await client.aclose()
    task = asyncio.get_running_loop().create_task(close())
    _pending_releases.add(task)
    task.add_done_callback(_pending_releases.discard)


def _release_soon(pb: _Playback, generation: int) -> None:
    """The player left mid-request: give the slot back after a short grace,
    unless a newer request (a seek that arrived late) has claimed it."""
    async def later():
        await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
        if _playbacks.get(pb.token) is pb and pb.generation == generation \
                and pb.active_bodies == 0 and pb.opening == 0:
            logger.info(f"[VOD] {pb.owner}: the player went away")
            _release(pb)
    task = asyncio.get_running_loop().create_task(later())
    _pending_releases.add(task)
    task.add_done_callback(_pending_releases.discard)


def _sweep_once(now: float) -> int:
    """Release playbacks that were stopped, or that nobody asked for lately."""
    n = 0
    for pb in list(_playbacks.values()):
        if pb.active_bodies or pb.opening:
            continue
        if pb.stopped.is_set() or now - pb.last_used > pb.idle:
            _release(pb)
            n += 1
    return n


async def _sweep():
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            _sweep_once(asyncio.get_running_loop().time())
        except Exception as e:   # the sweeper must outlive any one bad entry
            logger.error(f"[VOD] sweep failed: {e}")


def _ensure_sweeper():
    global _sweeper
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.get_running_loop().create_task(_sweep())


async def _playback_for(db, token: str, owner: str, client: str = "") -> _Playback:
    pb = _playbacks.get(token)
    if pb is not None and not pb.stopped.is_set():
        pb.touch()
        return pb
    limit = livetv._max_concurrent_streams(db)
    lease = await livetv._stream_slots.acquire_lease(limit, livetv._SLOT_WAIT_SECONDS, "vod", owner)
    if lease is None:
        livetv._stream_slots.refused += 1
        logger.warning(f"[VOD] {owner}: refused — at capacity ({limit}) and every slot is held by "
                       f"a recording or live stream")
        raise HTTPException(503, "The provider is busy: a recording or live stream has the connection. "
                                 "Try again in a minute.")
    existing = _playbacks.get(token)
    if existing is not None and not existing.stopped.is_set():
        # A concurrent first request for the same title got there while we
        # waited for the slot: one playback, one lease -- give this one back.
        livetv._stream_slots.release_lease(lease)
        existing.touch()
        return existing
    pb = _Playback(token, owner, lease, idle_seconds(db), client)
    lease.on_preempt = pb.stop
    _playbacks[token] = pb
    _ensure_sweeper()
    return pb


_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(?:\d+|\*)$")


def _content_range(header: Optional[str]) -> tuple:
    """(first, last) byte of a 206's Content-Range; (None, None) otherwise."""
    m = _CONTENT_RANGE_RE.match((header or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _range_bounds(range_header: Optional[str]) -> tuple:
    """(start, end) of a `bytes=start-[end]` header; (0, None) otherwise."""
    m = _RANGE_RE.match((range_header or "").strip())
    if not m:
        return 0, None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


def _range_start(range_header: Optional[str]) -> int:
    return _range_bounds(range_header)[0]


async def _open(client: httpx.AsyncClient, method: str, url: str, headers: dict, guard) -> httpx.Response:
    """One request, redirects followed by hand and re-validated (services.ssrf)."""
    current = url
    for _ in range(livetv._MAX_STREAM_REDIRECTS):
        req = client.build_request(method, current, headers=headers)
        resp = await client.send(req, stream=True)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        location = resp.headers.get("location")
        await resp.aclose()
        if not location:
            raise HTTPException(502, "Redirect without Location header")
        from urllib.parse import urljoin
        current = urljoin(current, location)
        if not guard(current):
            logger.warning(f"[VOD] Blocked redirect to non-public host: {current}")
            raise HTTPException(502, "Stream redirect points to a non-public host")
    raise HTTPException(502, "Too many redirects")


async def _open_with_retry(client, method, url, headers, guard, owner: str, player_left=None) -> httpx.Response:
    """Open, waiting out a provider that is momentarily refusing (429/509,
    5xx, timeouts) on the live path's terms: a growing delay, a bounded
    budget. Anything else is final. `player_left` (async, -> bool) stops the
    waiting when the request's client has gone: Starlette does not cancel a
    handler on disconnect, and a slot must not be held for a player that is
    no longer there."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    backoff = 1.0
    while True:
        try:
            resp = await _open(client, method, url, headers, guard)
            if resp.status_code in (200, 206):
                return resp
            status = resp.status_code
            await resp.aclose()
            if status not in livetv._OPEN_RETRYABLE_STATUS:
                raise HTTPException(502 if status >= 500 else 404, f"The provider answered {status}")
            reason = f"HTTP {status}"
        except httpx.TransportError as e:
            reason = str(e) or type(e).__name__
        waited = loop.time() - started
        if waited + backoff > livetv._OPEN_RETRY_BUDGET:
            logger.error(f"[VOD] {owner}: could not open after {waited:.0f}s: {reason}")
            raise HTTPException(503, "The provider is not answering; try again shortly")
        if player_left is not None and await player_left():
            logger.info(f"[VOD] {owner}: the player left while the provider was refusing ({reason})")
            raise HTTPException(503, "The player went away")
        logger.warning(f"[VOD] {owner}: open refused (retry in {backoff:.0f}s, {waited:.0f}s so far): {reason}")
        await asyncio.sleep(backoff * (0.8 + random.random() * 0.4))
        backoff = min(backoff * 2, 5.0)


def _resolve(db, kind: str, token_file: str):
    parsed = vod_tokens.parse(kind, token_file)
    if not parsed or not vod_tokens.verify(vod_tokens.token_secret(db), kind, parsed):
        raise HTTPException(404, "Unknown stream")
    provider = db.query(Provider).filter(Provider.id == parsed["provider_id"]).first()
    if not provider or (provider.provider_type or "xtream") != "xtream" or not provider.server_url:
        raise HTTPException(404, "Unknown stream")
    path = "movie" if kind == "movie" else "series"
    url = f"{provider.server_url.rstrip('/')}/{path}/{provider.username}/{provider.password}/{parsed['stream_id']}.{parsed['container']}"
    guard = lan_origin_guard(provider.server_url)
    if not guard(url):
        raise HTTPException(502, "Stream URL points to a non-public host")
    owner = f"vod:{kind}:{provider.id}:{parsed['stream_id']}"
    return url, guard, (provider.user_agent or _UA_DEFAULT), owner


@router.head("/api/vod/{kind}/{token_file}")
async def vod_head(kind: str, token_file: str, request: Request, db: Session = Depends(get_db)):
    """Headers only; no lease -- a probe, not a play."""
    url, guard, ua, owner = _resolve(db, kind, token_file)
    client = httpx.AsyncClient(follow_redirects=False,
                               timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0))
    try:
        resp = await _open(client, "HEAD", url, {"User-Agent": ua}, guard)
        try:
            passthrough = {k: v for k, v in resp.headers.items()
                           if k.lower() in ("content-length", "content-range", "accept-ranges", "content-type")}
            return Response(status_code=resp.status_code, headers=passthrough)
        finally:
            await resp.aclose()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"The provider did not answer: {e}")
    finally:
        await client.aclose()


@router.get("/api/vod/{kind}/{token_file}")
async def vod_stream(kind: str, token_file: str, request: Request, db: Session = Depends(get_db)):
    """Stream one provider file through Tentacle: byte ranges passed through
    (so seeking works), resumed from where it stopped if the upstream drops,
    counted and ranked against live TV and recordings."""
    url, guard, ua, owner = _resolve(db, kind, token_file)
    # One playback per title PER CLIENT: two TVs on the same film are two
    # playbacks (two provider connections, two slots), not one that they
    # keep taking from each other.
    who = request.client.host if request.client else "unknown"
    pb = await _playback_for(db, f"{kind}/{token_file}@{who}", owner, who)
    range_header = request.headers.get("range")
    start, end = _range_bounds(range_header)
    headers = {"User-Agent": ua}
    if range_header:
        headers["Range"] = range_header
    client = httpx.AsyncClient(follow_redirects=False,
                               timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0))
    async def player_left() -> bool:
        try:
            return await request.is_disconnected()
        except RuntimeError:            # no receive channel (a test, or an unusual server)
            return False

    pb.opening += 1
    try:
        resp = await _open_with_retry(client, "GET", url, headers, guard, owner, player_left=player_left)
    except BaseException:
        pb.opening -= 1
        await client.aclose()
        # The player has gone (or the provider refused for good): nothing is
        # streaming for this playback, so give the slot back now instead of
        # holding it for the idle window.
        if pb.active_bodies == 0 and pb.opening == 0:
            _release(pb)
        raise
    pb.opening -= 1
    passthrough = {k: v for k, v in resp.headers.items()
                   if k.lower() in ("content-length", "content-range", "accept-ranges")}
    content_type = resp.headers.get("content-type") or "video/mp4"
    status = resp.status_code
    # Where the bytes we forward actually start and end is what the PROVIDER
    # says (a 206's Content-Range), not what the client asked for: a suffix
    # range (`bytes=-500`) has no start until the provider names it, and a
    # provider that answers a Range request with 200 is sending byte 0 and
    # cannot be asked to resume from anywhere.
    if status == 206:
        cr_start, cr_end = _content_range(resp.headers.get("content-range"))
        start = cr_start if cr_start is not None else start
        end = cr_end if cr_end is not None else end
        resumable = True
    else:
        start, end = 0, None
        resumable = not range_header and resp.headers.get("accept-ranges", "").lower() == "bytes"
    pb.active_bodies += 1
    pb.generation += 1
    my_generation = pb.generation
    current = {"resp": resp}
    state = {"finished": False, "complete": False}

    def finish():
        """Runs exactly once per request, however it ends (the generator's
        `finally`, or the response's own `finally` when the generator was
        never entered). Synchronous on purpose: it may run while the task
        is being cancelled, when every `await` would be cancelled again --
        so the bookkeeping is immediate and the closes go to a task of
        their own."""
        if state["finished"]:
            return
        state["finished"] = True
        pb.active_bodies -= 1
        pb.touch()
        _close_later(current["resp"], client)
        # Ended by a seek (a newer request took over) or by reaching the end
        # of the range: the player is still there and will ask again -- keep
        # the slot for the idle window. Otherwise the player went away.
        if not state["complete"] and pb.generation == my_generation and pb.active_bodies == 0 \
                and not pb.stopped.is_set():
            _release_soon(pb, my_generation)

    async def body():
        sent = 0
        attempts = 0
        loop = asyncio.get_running_loop()
        try:
            while True:
                resp = current["resp"]
                opened_at = loop.time()
                reason = "the provider closed the connection early"
                try:
                    # No chunk_size: aiter_bytes(chunk_size=) keeps its partial
                    # batch to itself when the connection breaks, and the
                    # bytes received just before every drop would be lost --
                    # then re-fetched from the wrong offset.
                    async for chunk in resp.aiter_bytes():
                        if pb.stopped.is_set():
                            return
                        if pb.generation != my_generation:
                            logger.debug(f"[VOD] {owner}: superseded by a newer request (seek); ending this range")
                            return
                        sent += len(chunk)
                        pb.touch()
                        yield chunk
                    state["complete"] = True
                    return                      # the provider finished the file/range
                except httpx.HTTPError as e:
                    reason = str(e) or type(e).__name__
                await resp.aclose()
                if loop.time() - opened_at >= 30.0:
                    attempts = 0                # a connection that lasted resets the budget
                if pb.stopped.is_set() or not resumable or attempts >= RECONNECT_ATTEMPTS:
                    logger.error(f"[VOD] {owner}: stream lost after {sent} bytes, giving up: {reason}")
                    return
                if end is not None and start + sent > end:
                    state["complete"] = True
                    return                  # the requested range was delivered in full
                attempts += 1
                delay = min(5.0, 2 ** (attempts - 1)) * (0.8 + random.random() * 0.4)
                logger.warning(f"[VOD] {owner}: upstream dropped after {sent} bytes; resuming from byte "
                               f"{start + sent} in {delay:.0f}s: {reason}")
                await asyncio.sleep(delay)
                resume = f"bytes={start + sent}-" + (str(end) if end is not None else "")
                try:
                    # No player_left here: while the body streams, Starlette
                    # owns the disconnect listener and cancels this generator
                    # itself; a second reader of the ASGI receive is not ours.
                    current["resp"] = await _open_with_retry(client, "GET", url, dict(headers, Range=resume),
                                                             guard, owner)
                except HTTPException as e:
                    logger.error(f"[VOD] {owner}: could not resume: {e.detail}")
                    return
                if current["resp"].status_code != 206:
                    logger.error(f"[VOD] {owner}: the provider would not resume from byte {start + sent} "
                                 f"(answered {current['resp'].status_code})")
                    return
        finally:
            finish()

    return _VodResponse(body(), status_code=status, media_type=content_type, headers=passthrough,
                        finish=finish)

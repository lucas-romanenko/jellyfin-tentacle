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
from services.xtream_client import quote_cred
from routers import livetv

logger = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_IDLE_SECONDS = 45.0     # a seek arrives within seconds; a stopped player never does
SWEEP_SECONDS = 10.0
# A request that ends without being superseded by a newer one and without
# reaching the end of its range is a player that went away; its slot is
# released after this grace, not after the idle window.
DISCONNECT_GRACE_SECONDS = 3.0
RECONNECT_ATTEMPTS = 6          # per request, reset by a connection that lasts or delivers
# A connection that delivered this much (or lasted 30 s) resets the reconnect
# budget: the provider cuts every 5-20 s after 60-160 MB, so a film is dozens
# of drops, each one a fresh start.
PROGRESS_RESET_BYTES = 8 << 20
# While another request of the same playback waits, the holder keeps the one
# provider connection this long, then hands it over (time-sharing). Well under
# what ffmpeg and Jellyfin wait for data.
QUANTUM_SECONDS = 8.0
# A waiting request takes the connection from a holder that kept it this
# much past its quantum (a paused player blocks the holder's body).
PREEMPT_GRACE_SECONDS = 2.0
CLOSE_SETTLE_SECONDS = 2.0
_UA_DEFAULT = "TiviMate/4.7.0 (Linux; Android 12)"
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


def vod_enabled(db) -> bool:
    """On unless the admin opted out (`vod_via_tentacle_enabled` "false"):
    an install that never set it, new or upgraded, streams VOD through
    Tentacle. A direct .strm ends the episode when the provider drops the
    connection (Jellyfin's ffmpeg reads the short body as the end of file)."""
    raw = (get_setting(db, "vod_via_tentacle_enabled", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def idle_seconds(db) -> float:
    raw = get_setting(db, "vod_lease_idle_seconds", "") or ""
    try:
        return max(5.0, float(raw)) if raw.strip() else DEFAULT_IDLE_SECONDS
    except ValueError:
        return DEFAULT_IDLE_SECONDS


def _leaf_exceptions(exc):
    subs = getattr(exc, "exceptions", None)
    if not subs:
        return [exc]
    return [leaf for sub in subs for leaf in _leaf_exceptions(sub)]


def is_early_range_end(exc) -> bool:
    """Whether `exc` is only the server refusing to finish a body that ended
    below its Content-Length: a VOD range ended on purpose (a seek superseded
    it, the playback was pre-empted, a resume was given up). The provider's
    Content-Length is passed on, so the server raises, and closing the
    connection is exactly what tells that player its range is over (#285).
    h11 says "Too little data for declared Content-Length"; httptools
    "Response content shorter than Content-Length"."""
    if exc is None:
        return False
    leaves = _leaf_exceptions(exc)
    return all("Too little data for declared Content-Length" in str(e)
               or "Response content shorter than Content-Length" in str(e) for e in leaves)


class EarlyRangeEndFilter(logging.Filter):
    """Drops uvicorn's "Exception in ASGI application" traceback for an early
    range end; the connection still closes (the exception still reaches it)."""

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        return not is_early_range_end(exc)


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


class _Reader:
    """One request of a playback: Jellyfin's ffmpeg for the playback, a
    subtitle extraction next to it, a second transcode, the range a seek
    left. Only the playback's `holder` has a provider connection (`resp`);
    the others wait, parked, for their turn."""
    __slots__ = ("turn", "resp", "since")

    def __init__(self):
        self.turn = asyncio.Event()
        self.resp = None
        self.since = 0.0


class _Playback:
    """One title being played: its lease and when it was last asked for."""
    __slots__ = ("token", "owner", "client", "lease", "last_used", "started", "idle", "stopped",
                 "active_bodies", "opening", "generation", "upstream", "holder", "waiters",
                 "closing", "open_lock", "size", "content_type")

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
        # The provider response the holder is reading. Closed from outside
        # when another request takes the connection or the slot is taken: a
        # body whose player is paused is blocked sending to the player and
        # would otherwise keep the provider connection open indefinitely.
        self.upstream = None
        # Counts requests; the newest one's number says whether a request
        # that ends was replaced (a seek) or its player went away.
        self.generation = 0
        # One playback, one provider connection, however many requests
        # Jellyfin makes for the title (a subtitle extraction next to the
        # playback, a restart 0.1 s after a stop, two transcodes at once,
        # ffmpeg's seeks): the provider cuts an account that has two open,
        # and counts one that has just closed for a few seconds. The newest
        # request takes the connection; the one it parks waits and goes on
        # from its own byte when its turn comes back (the newer one ends, or
        # has had it for QUANTUM_SECONDS while others wait). Never two
        # opens at once (`open_lock`), and the old connection is closed
        # before the new one opens (`closing`, settle()).
        self.holder = None
        self.waiters = []
        self.closing = set()
        self.open_lock = asyncio.Lock()
        # The file's size and type, from the first open: a HEAD during the
        # playback is answered from them instead of a provider connection.
        self.size = None
        self.content_type = None

    def touch(self):
        self.last_used = asyncio.get_running_loop().time()

    def close_upstream(self, resp) -> None:
        task = asyncio.get_running_loop().create_task(resp.aclose())
        _pending_releases.add(task)
        task.add_done_callback(_pending_releases.discard)
        self.closing.add(task)
        task.add_done_callback(self.closing.discard)

    async def settle(self) -> None:
        """Let the connections this playback just closed finish closing
        before a new one opens (briefly: a close never waits on the player)."""
        if self.closing:
            await asyncio.wait(set(self.closing), timeout=CLOSE_SETTLE_SECONDS)

    def take(self, reader: "_Reader") -> None:
        """`reader` gets the connection; the holder is parked, its connection closed."""
        cur = self.holder
        if cur is reader:
            return
        if cur is not None:
            if cur.resp is not None:
                self.close_upstream(cur.resp)
                cur.resp = None
            if cur not in self.waiters:
                self.waiters.append(cur)
        if reader in self.waiters:
            self.waiters.remove(reader)
        reader.since = asyncio.get_running_loop().time()
        self.holder = reader
        self.upstream = None
        reader.turn.set()

    def hand_on(self, reader: "_Reader") -> None:
        """`reader` is done (finished, or its player left): the request
        that has waited longest gets the connection."""
        if reader in self.waiters:
            self.waiters.remove(reader)
        if self.holder is reader:
            self.holder = None
            self.upstream = None
            if self.waiters and not self.stopped.is_set():
                self.take(self.waiters[0])

    def stop(self):
        """Called by the broker when a recording or viewer takes the slot."""
        if not self.stopped.is_set():
            logger.warning(f"[VOD] {self.owner}: playback stopped — a recording or live stream needed "
                           f"its connection slot")
            self.stopped.set()
            if self.upstream is not None:
                self.close_upstream(self.upstream)
                self.upstream = None
            if self.holder is not None and self.holder.resp is not None:
                self.close_upstream(self.holder.resp)
                self.holder.resp = None
            for r in list(self.waiters):
                r.turn.set()           # parked requests end instead of waiting for a turn
            # Its slot is gone (the broker moved it to the newcomer), so it is
            # not a playback any more: off the list now, even if its request
            # is still stuck writing to a paused player. The player's next
            # request starts a new playback, which asks for a slot again.
            if _playbacks.get(self.token) is self:
                del _playbacks[self.token]


_playbacks: "dict[str, _Playback]" = {}
_sweeper: "asyncio.Task | None" = None
_pending_releases: "set[asyncio.Task]" = set()


def _release(pb: _Playback):
    if _playbacks.get(pb.token) is pb:
        del _playbacks[pb.token]
    livetv._stream_slots.release_lease(pb.lease)
    logger.info(f"[VOD] {pb.owner}: playback ended, slot released")


def _close_later_client(client) -> None:
    task = asyncio.get_running_loop().create_task(client.aclose())
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
    limit = livetv.overall_stream_limit(db)
    # owner is vod:<kind>:<provider id>:<stream id> (_resolve)
    try:
        provider_id = int(owner.split(":")[2])
    except (IndexError, ValueError):
        provider_id = None
    provider = db.get(livetv.Provider, provider_id) if provider_id is not None else None
    provider_limit = livetv.provider_stream_limit(db, provider) if provider is not None else 0
    livetv._stream_slots.set_protect(livetv._protect_recordings(db))
    try:
        lease = await livetv._stream_slots.acquire_lease(limit, livetv._SLOT_WAIT_SECONDS, "vod", owner,
                                                         provider_id=provider.id if provider else None,
                                                         provider_limit=provider_limit)
    except livetv.RecordingProtected:
        raise HTTPException(503, livetv.PROTECTED_REFUSAL_DETAIL)
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
        # The guard resolves DNS with a blocking getaddrinfo: off the event
        # loop, so a resolver that hangs doesn't freeze every stream (#464).
        if not await asyncio.to_thread(guard, current):
            logger.warning(f"[VOD] Blocked redirect to non-public host: {current}")
            raise HTTPException(502, "Stream redirect points to a non-public host")
    raise HTTPException(502, "Too many redirects")


async def _open_with_retry(client, method, url, headers, guard, owner: str, player_left=None,
                           stopped=None) -> httpx.Response:
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
        if stopped is not None and stopped():
            raise HTTPException(503, "The connection slot was needed by a recording or live stream")
        try:
            resp = await _open(client, method, url, headers, guard)
            if stopped is not None and stopped():
                await resp.aclose()     # lost the slot while this attempt was in flight
                raise HTTPException(503, "The connection slot was needed by a recording or live stream")
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
        if stopped is not None and stopped():
            raise HTTPException(503, "The connection slot was needed by a recording or live stream")
        if player_left is not None and await player_left():
            logger.info(f"[VOD] {owner}: the player left while the provider was refusing ({reason})")
            raise HTTPException(503, "The player went away")
        logger.warning(f"[VOD] {owner}: open refused (retry in {backoff:.0f}s, {waited:.0f}s so far): {reason}")
        await asyncio.sleep(backoff * (0.8 + random.random() * 0.4))
        backoff = min(backoff * 2, 5.0)


def _resolve(db, kind: str, token_file: str):
    """The stream's URL, guard, user agent and owner. Resolves DNS (twice)
    with a blocking getaddrinfo: callers run it in a worker thread (#464)."""
    parsed = vod_tokens.parse(kind, token_file)
    # Read, never create: the secret is made by the sync that writes the
    # links. An install that never turned VOD-through-Tentacle on has none,
    # and an anonymous request must not make one.
    secret = (get_setting(db, vod_tokens.SECRET_SETTING, "") or "").strip()
    if not parsed or not secret or not vod_tokens.verify(secret, kind, parsed):
        raise HTTPException(404, "Unknown stream")
    provider = db.query(Provider).filter(Provider.id == parsed["provider_id"]).first()
    if not provider or (provider.provider_type or "xtream") != "xtream" or not provider.server_url:
        raise HTTPException(404, "Unknown stream")
    path = "movie" if kind == "movie" else "series"
    login = f"{quote_cred(provider.username)}/{quote_cred(provider.password)}"
    url = f"{provider.server_url.rstrip('/')}/{path}/{login}/{parsed['stream_id']}.{parsed['container']}"
    guard = lan_origin_guard(provider.server_url)
    if not guard(url):
        raise HTTPException(502, "Stream URL points to a non-public host")
    owner = f"vod:{kind}:{provider.id}:{parsed['stream_id']}"
    return url, guard, (provider.user_agent or _UA_DEFAULT), owner


@router.head("/api/vod/{kind}/{token_file}")
async def vod_head(kind: str, token_file: str, request: Request, db: Session = Depends(get_db)):
    """Headers only; no lease -- a probe, not a play. Still a provider
    connection, so with recording protection on it is refused while a
    recording runs, like a play would be. During a playback of the title
    from the same client it is answered from what the playback already
    knows: a second connection is what makes the provider cut the first."""
    url, guard, ua, owner = await asyncio.to_thread(_resolve, db, kind, token_file)
    who = request.client.host if request is not None and request.client else "unknown"
    pb = _playbacks.get(f"{kind}/{token_file}@{who}")
    if pb is not None and not pb.stopped.is_set() and pb.size is not None:
        pb.touch()
        return Response(status_code=200, headers={"content-length": str(pb.size), "accept-ranges": "bytes",
                                                  "content-type": pb.content_type or "video/mp4"})
    slots = livetv._stream_slots
    slots.set_protect(livetv._protect_recordings(db))
    if slots.protect and slots.recording_active():
        try:
            slots._refuse_protected("vod", f"{owner} (HEAD)")
        except livetv.RecordingProtected:
            raise HTTPException(503, livetv.PROTECTED_REFUSAL_DETAIL)
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
        # The error type only: an httpx error's text can carry the request
        # URL, whose path holds the provider account.
        raise HTTPException(502, f"The provider did not answer: {type(e).__name__}")
    finally:
        await client.aclose()


_SIZE_RE = re.compile(r"/(\d+)\s*$")


async def _wait_turn(pb: _Playback, reader: _Reader) -> bool:
    """Wait, parked, until `reader` holds the playback's connection again.
    A holder that keeps it past its quantum (a paused player blocks its body
    where it cannot notice) is parked from here. False once the playback was
    stopped."""
    while pb.holder is not reader:
        if pb.stopped.is_set():
            return False
        reader.turn.clear()
        try:
            await asyncio.wait_for(reader.turn.wait(), QUANTUM_SECONDS + PREEMPT_GRACE_SECONDS)
        except asyncio.TimeoutError:
            async with pb.open_lock:
                holder = pb.holder
                if holder is not reader and not pb.stopped.is_set() and \
                        (holder is None or asyncio.get_running_loop().time() - holder.since >= QUANTUM_SECONDS):
                    pb.take(reader)
    return not pb.stopped.is_set()


@router.get("/api/vod/{kind}/{token_file}")
async def vod_stream(kind: str, token_file: str, request: Request, db: Session = Depends(get_db)):
    """Stream one provider file through Tentacle: byte ranges passed through
    (so seeking works), resumed from where it stopped if the upstream drops
    or ends short, counted and ranked against live TV and recordings, and
    one provider connection per playback however many requests share it."""
    url, guard, ua, owner = await asyncio.to_thread(_resolve, db, kind, token_file)
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

    reader = _Reader()
    pb.opening += 1
    try:
        async with pb.open_lock:
            # The request that holds the connection (the range a seek left, a
            # subtitle extraction, a stopped transcode) is parked and its
            # connection closed BEFORE this one opens.
            pb.take(reader)
            await pb.settle()
            resp = await _open_with_retry(client, "GET", url, headers, guard, owner, player_left=player_left,
                                          stopped=pb.stopped.is_set)
            reader.resp = resp
            reader.since = asyncio.get_running_loop().time()
            pb.upstream = resp
    except BaseException:
        pb.opening -= 1
        pb.hand_on(reader)
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
    length = resp.headers.get("content-length", "")
    if status == 206:
        cr_start, cr_end = _content_range(resp.headers.get("content-range"))
        start = cr_start if cr_start is not None else start
        end = cr_end if cr_end is not None else end
        resumable = True
        m = _SIZE_RE.search(resp.headers.get("content-range") or "")
        if m:
            pb.size = int(m.group(1))
    else:
        start, end = 0, None
        resumable = not range_header and resp.headers.get("accept-ranges", "").lower() == "bytes"
        if length.isdigit():
            pb.size = int(length)
    pb.content_type = content_type
    # The last byte the provider said this body carries: a body that ends
    # cleanly before it is a drop, not the end of the file.
    last = end
    if last is None and length.isdigit():
        last = start + int(length) - 1
    pb.active_bodies += 1
    pb.generation += 1
    my_generation = pb.generation
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
        if pb.upstream is not None and pb.upstream is reader.resp:
            pb.upstream = None
        if reader.resp is not None:
            pb.close_upstream(reader.resp)
            reader.resp = None
        _close_later_client(client)
        pb.hand_on(reader)
        # Replaced by a newer request (a seek), or the end of the range: the
        # player is still there and will ask again -- keep the slot for the
        # idle window. Otherwise the player went away.
        if not state["complete"] and pb.generation == my_generation and pb.active_bodies == 0 \
                and not pb.stopped.is_set():
            _release_soon(pb, my_generation)

    async def body():
        sent = 0
        attempts = 0
        loop = asyncio.get_running_loop()
        opened_at = loop.time()
        since_open = 0
        try:
            while True:
                if pb.stopped.is_set():
                    return
                if reader.resp is None:
                    # Parked (another request had the connection), or after a drop.
                    if not await _wait_turn(pb, reader):
                        return
                    if end is not None and start + sent > end:
                        state["complete"] = True
                        return
                    resume = f"bytes={start + sent}-" + (str(end) if end is not None else "")
                    async with pb.open_lock:
                        if pb.holder is not reader:
                            continue            # taken again while waiting for the lock
                        await pb.settle()
                        try:
                            # No player_left here: while the body streams, Starlette
                            # owns the disconnect listener and cancels this generator
                            # itself; a second reader of the ASGI receive is not ours.
                            again = await _open_with_retry(client, "GET", url, dict(headers, Range=resume),
                                                           guard, owner, stopped=pb.stopped.is_set)
                        except HTTPException as e:
                            logger.error(f"[VOD] {owner}: could not resume: {e.detail}")
                            return
                        if again.status_code != 206:
                            pb.close_upstream(again)
                            logger.error(f"[VOD] {owner}: the provider would not resume from byte {start + sent} "
                                         f"(answered {again.status_code})")
                            return
                        reader.resp = again
                        reader.since = loop.time()
                        pb.upstream = again
                        opened_at, since_open = loop.time(), 0
                resp = reader.resp
                reason = "the provider closed the connection early"
                try:
                    # No chunk_size: aiter_bytes(chunk_size=) keeps its partial
                    # batch to itself when the connection breaks, and the
                    # bytes received just before every drop would be lost --
                    # then re-fetched from the wrong offset.
                    async for chunk in resp.aiter_bytes():
                        if pb.stopped.is_set():
                            return
                        if reader.resp is not resp:
                            break               # handed to another request (closed from outside)
                        sent += len(chunk)
                        since_open += len(chunk)
                        pb.touch()
                        yield chunk
                        if pb.waiters and pb.holder is reader and loop.time() - reader.since >= QUANTUM_SECONDS:
                            # Time-share: another request of this playback is
                            # waiting; it gets the connection for a while.
                            pb.take(pb.waiters[0])
                            break
                    else:
                        if last is None or start + sent > last:
                            state["complete"] = True
                            return              # the provider finished the file/range
                        # A body that ends cleanly short of its Content-Length
                        # or range is a drop ("Stream ends prematurely at X,
                        # should be Y" in Jellyfin's ffmpeg), not the end.
                        reason = f"the body ended at byte {start + sent}, before byte {last}"
                except (httpx.HTTPError, httpx.StreamError) as e:
                    reason = str(e) or type(e).__name__
                if reader.resp is not resp or pb.holder is not reader:
                    continue                    # handed to another request: wait for the turn back
                if pb.stopped.is_set():
                    return                      # pre-empted
                pb.close_upstream(resp)
                reader.resp = None
                if pb.upstream is resp:
                    pb.upstream = None
                if since_open >= PROGRESS_RESET_BYTES or loop.time() - opened_at >= 30.0:
                    attempts = 0                # a connection that lasted or delivered resets the budget
                if not resumable or attempts >= RECONNECT_ATTEMPTS:
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
        finally:
            finish()

    return _VodResponse(body(), status_code=status, media_type=content_type, headers=passthrough,
                        finish=finish)

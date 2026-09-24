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


class _Playback:
    """One title being played: its lease and when it was last asked for."""
    __slots__ = ("token", "owner", "lease", "last_used", "idle", "stopped", "active_bodies")

    def __init__(self, token: str, owner: str, lease, idle: float):
        self.token = token
        self.owner = owner
        self.lease = lease
        self.idle = idle
        self.last_used = asyncio.get_running_loop().time()
        self.stopped = asyncio.Event()
        self.active_bodies = 0

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


def _release(pb: _Playback):
    if _playbacks.get(pb.token) is pb:
        del _playbacks[pb.token]
    livetv._stream_slots.release_lease(pb.lease)
    logger.info(f"[VOD] {pb.owner}: playback ended, slot released")


def _sweep_once(now: float) -> int:
    """Release playbacks that were stopped, or that nobody asked for lately."""
    n = 0
    for pb in list(_playbacks.values()):
        if pb.active_bodies:
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


async def _playback_for(db, token: str, owner: str) -> _Playback:
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
    pb = _Playback(token, owner, lease, idle_seconds(db))
    lease.on_preempt = pb.stop
    _playbacks[token] = pb
    _ensure_sweeper()
    return pb


def _range_start(range_header: Optional[str]) -> int:
    m = _RANGE_RE.match((range_header or "").strip())
    return int(m.group(1)) if m else 0


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


async def _open_with_retry(client, method, url, headers, guard, owner: str) -> httpx.Response:
    """Open, waiting out a provider that is momentarily refusing (429/509,
    5xx, timeouts) on the live path's terms: a growing delay, a bounded
    budget. Anything else is final."""
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
    pb = await _playback_for(db, f"{kind}/{token_file}", owner)
    range_header = request.headers.get("range")
    start = _range_start(range_header)
    headers = {"User-Agent": ua}
    if range_header:
        headers["Range"] = range_header
    client = httpx.AsyncClient(follow_redirects=False,
                               timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0))
    try:
        resp = await _open_with_retry(client, "GET", url, headers, guard, owner)
    except BaseException:
        await client.aclose()
        raise
    passthrough = {k: v for k, v in resp.headers.items()
                   if k.lower() in ("content-length", "content-range", "accept-ranges")}
    content_type = resp.headers.get("content-type") or "video/mp4"
    status = resp.status_code
    resumable = status == 206 or resp.headers.get("accept-ranges", "").lower() == "bytes"
    pb.active_bodies += 1

    async def body():
        nonlocal resp
        sent = 0
        attempts = 0
        loop = asyncio.get_running_loop()
        try:
            while True:
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
                        sent += len(chunk)
                        pb.touch()
                        yield chunk
                    return                      # the provider finished the file/range
                except httpx.HTTPError as e:
                    reason = str(e) or type(e).__name__
                await resp.aclose()
                if loop.time() - opened_at >= 30.0:
                    attempts = 0                # a connection that lasted resets the budget
                if pb.stopped.is_set() or not resumable or attempts >= RECONNECT_ATTEMPTS:
                    logger.error(f"[VOD] {owner}: stream lost after {sent} bytes, giving up: {reason}")
                    return
                attempts += 1
                delay = min(5.0, 2 ** (attempts - 1)) * (0.8 + random.random() * 0.4)
                logger.warning(f"[VOD] {owner}: upstream dropped after {sent} bytes; resuming from byte "
                               f"{start + sent} in {delay:.0f}s: {reason}")
                await asyncio.sleep(delay)
                try:
                    resp = await _open_with_retry(client, "GET", url, dict(headers, Range=f"bytes={start + sent}-"),
                                                  guard, owner)
                except HTTPException as e:
                    logger.error(f"[VOD] {owner}: could not resume: {e.detail}")
                    return
                if resp.status_code != 206:
                    await resp.aclose()
                    logger.error(f"[VOD] {owner}: the provider would not resume from byte {start + sent} "
                                 f"(answered {resp.status_code})")
                    return
        finally:
            pb.active_bodies -= 1
            pb.touch()
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=status, media_type=content_type, headers=passthrough)

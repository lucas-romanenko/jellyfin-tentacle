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

One person watching is one provider connection. Jellyfin pulls a title for
more than the picture: a browser direct-plays the file from its own address
while Jellyfin's ffmpeg extracts its subtitles from Jellyfin's, and that
extraction reads the whole file, for minutes after the player stopped. So a
title's requests share one playback (one connection, time-shared) unless
Jellyfin says more people are watching it than there are playbacks, and a
playback is closed a few seconds after the last Jellyfin session playing it
stopped (`_watch_viewers`). Within a playback the viewer's own requests
come first: a client Jellyfin lists as the session's address (a browser or
TV playing the file itself) takes the connection back whenever it wants
data, and Jellyfin's work for the title reads while the player does not.
"""
import asyncio
import logging
import random
import re
import time
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
# The first resume after a drop waits this long (then doubling, at most 5 s):
# the provider closes a film's connection every few minutes when several
# streams are open, and a player's buffer covers a second, not ten.
RESUME_FIRST_DELAY = 0.5
# While another request of the same playback waits, the holder keeps the one
# provider connection this long, then hands it over (time-sharing). Well under
# what ffmpeg and Jellyfin wait for data.
QUANTUM_SECONDS = 8.0
# A waiting request takes the connection from a holder that kept it this
# much past its quantum (a paused player blocks the holder's body).
PREEMPT_GRACE_SECONDS = 2.0
CLOSE_SETTLE_SECONDS = 2.0
# While a playback runs, Jellyfin's sessions are asked this often who is
# watching which title. A playback whose viewer has gone (stopped, or moved
# on to the next episode) for VIEWER_GONE_SECONDS is closed: what still pulls
# it then is Jellyfin's own work for the title, not a person.
VIEWER_CHECK_SECONDS = 2.0
VIEWER_GONE_SECONDS = 4.0
# An older answer says nothing about who is watching now.
VIEWERS_FRESH_SECONDS = 15.0
# After a title was closed for having no viewer, a request that carries on
# reading it (a Range past byte 0, nothing of the title running) is refused
# for this long unless Jellyfin shows a viewer again: ffmpeg answers a body
# that ended early by asking for the rest at once (measured, ffmpeg 7.1:
# "Stream ends prematurely", then `Range: bytes=<where it was>-`). A player
# starting the title reads its start first (byte 0), which lifts it.
GONE_REFUSE_SECONDS = 60.0
# How long a Jellyfin item's VOD address is remembered (a relink rewrites it).
ITEM_TITLES_SECONDS = 600.0
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
    __slots__ = ("turn", "resp", "since", "client", "last_chunk", "waiting")

    def __init__(self, client: str = ""):
        self.turn = asyncio.Event()
        self.resp = None
        self.since = 0.0
        self.client = client
        # When it last passed bytes on (loop time): a holder whose player
        # stopped reading (paused, buffer full) passes nothing.
        self.last_chunk = 0.0
        # Asking for the connection right now (opening, or parked and its
        # player wants bytes) -- as opposed to parked while its player is
        # not reading, which wants nothing.
        self.waiting = True


class _Playback:
    """One title being played: its lease and when it was last asked for."""
    __slots__ = ("token", "title", "owner", "client", "lease", "last_used", "started", "idle", "stopped",
                 "active_bodies", "opening", "generation", "upstream", "holder", "waiters",
                 "closing", "open_lock", "size", "content_type", "watched_at")

    def __init__(self, token: str, owner: str, lease, idle: float, client: str = "", title: str = ""):
        self.token = token
        # `movie/<token file>`: the same for every client of the title (token is title@client)
        self.title = title or token.rsplit("@", 1)[0]
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
        # When Jellyfin last listed a session playing this title (loop time);
        # None until it has.
        self.watched_at = None

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
        that has waited longest gets the connection, one that is asking for
        it before one whose player is not reading."""
        if reader in self.waiters:
            self.waiters.remove(reader)
        if self.holder is reader:
            self.holder = None
            self.upstream = None
            if self.waiters and not self.stopped.is_set():
                self.take(next((r for r in self.waiters if r.waiting), self.waiters[0]))

    def stop(self, why: str = "a recording or live stream needed its connection slot"):
        """Called by the broker when a recording or viewer takes the slot,
        and when nobody is watching the title any more."""
        if not self.stopped.is_set():
            logger.warning(f"[VOD] {self.owner}: playback stopped — {why}")
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
_viewer_watch: "asyncio.Task | None" = None
# What Jellyfin said last: {"titles": {title: [the address of each session
# playing it]} or None (unknown: no Jellyfin connection, an error), "at":
# loop time}.
_viewers: dict = {"titles": None, "at": 0.0}
# The Jellyfin server to ask, from the settings when a playback starts.
_jellyfin: dict = {"url": "", "key": ""}
# Jellyfin item id -> (monotonic time, the VOD titles its media sources play)
_item_titles: "dict[str, tuple]" = {}
# title -> loop time it was closed for having no viewer (GONE_REFUSE_SECONDS)
_gone: "dict[str, float]" = {}
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
    global _sweeper, _viewer_watch
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.get_running_loop().create_task(_sweep())
    if _viewer_watch is None or _viewer_watch.done():
        _viewer_watch = asyncio.get_running_loop().create_task(_watch_viewers())


def _address(endpoint) -> str:
    """A session's RemoteEndPoint as a request's client address."""
    text = str(endpoint or "").strip()
    return text[7:] if text.lower().startswith("::ffff:") else text


def _jellyfin_watched_titles(url: str, key: str) -> Optional[dict]:
    """{title: [the address of each Jellyfin session playing it]} for the
    Tentacle VOD titles being played now; None when Jellyfin can't say (not
    set up, not answering). Blocking: run in a worker thread."""
    if not url or not key:
        return None
    import requests
    headers = {"X-Emby-Token": key}
    try:
        r = requests.get(f"{url}/Sessions", params={"activeWithinSeconds": 960}, headers=headers, timeout=5)
        r.raise_for_status()
        playing = [(str(s["NowPlayingItem"]["Id"]), _address(s.get("RemoteEndPoint"))) for s in r.json()
                   if isinstance(s, dict) and (s.get("NowPlayingItem") or {}).get("Id")]
        ids = [i for i, _ in playing]
        now = time.monotonic()
        missing = sorted({i for i in ids if i not in _item_titles
                          or now - _item_titles[i][0] > ITEM_TITLES_SECONDS})
        if missing:
            # A session's item carries no media sources; the item's do (its .strm address).
            r = requests.get(f"{url}/Items", params={"Ids": ",".join(missing), "Fields": "MediaSources"},
                             headers=headers, timeout=10)
            r.raise_for_status()
            found = {str(it.get("Id")): frozenset(t for ms in (it.get("MediaSources") or [])
                                                  if (t := vod_tokens.title_in_url(ms.get("Path") or "")))
                     for it in r.json().get("Items") or []}
            if len(_item_titles) > 5000:
                _item_titles.clear()
            for i in missing:
                _item_titles[i] = (now, found.get(i, frozenset()))
        watched: dict = {}
        for i, address in playing:
            for title in _item_titles.get(i, (0, frozenset()))[1]:
                watched.setdefault(title, []).append(address)
        return watched
    except Exception as e:
        # The error type only: a requests error's text can carry the address.
        logger.info(f"[VOD] Jellyfin sessions unknown ({type(e).__name__}): playbacks are left running")
        return None


def _fresh_viewers() -> Optional[dict]:
    titles, at = _viewers["titles"], _viewers["at"]
    if titles is None or asyncio.get_running_loop().time() - at > VIEWERS_FRESH_SECONDS:
        return None
    return titles


def _viewer_count(title: str) -> Optional[int]:
    """How many Jellyfin sessions are playing `title`, per the last fresh
    answer; None when unknown."""
    titles = _fresh_viewers()
    return None if titles is None else len(titles.get(title) or ())


def _is_viewer(title: str, client: str) -> bool:
    """Whether requests from `client` are the viewer's own: Jellyfin lists a
    session playing `title` from that address (a browser or TV playing the
    file itself). Jellyfin's ffmpeg comes from Jellyfin's address."""
    titles = _fresh_viewers()
    return bool(titles and client and client in (titles.get(title) or ()))


def _next_turn(pb: "_Playback", holder: "_Reader", now: float) -> "Optional[_Reader]":
    """Who the holder hands the connection to now, if anyone: a viewer that
    wants data at once when the holder is Jellyfin's own work; otherwise the
    longest waiting after a quantum -- but a viewer that keeps reading is
    never timed out for Jellyfin's work (that reads while the player doesn't)."""
    if not pb.waiters:
        return None
    holder_is_viewer = _is_viewer(pb.title, holder.client)
    viewers = [r for r in pb.waiters if r.waiting and _is_viewer(pb.title, r.client)]
    if viewers and not holder_is_viewer:
        return viewers[0]
    if now - holder.since < QUANTUM_SECONDS:
        return None
    if holder_is_viewer:
        return viewers[0] if viewers else None
    return pb.waiters[0]


def _enforce_viewers(titles: Optional[dict], now: float) -> int:
    """Close the playbacks whose Jellyfin viewer has gone. A playback no
    session has played yet is left alone (the player reports playing only
    after Jellyfin probed and started the title, and a client that never
    reports is still a person), and so is everything while Jellyfin can't say."""
    if titles is None:
        return 0
    n = 0
    for pb in list(_playbacks.values()):
        if pb.stopped.is_set():
            continue
        if titles.get(pb.title):
            pb.watched_at = now
        elif pb.watched_at is not None and now - pb.watched_at >= VIEWER_GONE_SECONDS:
            pb.stop("nobody is watching it in Jellyfin any more (what still read it was Jellyfin's own "
                    "work for the title, such as a subtitle extraction)")
            _release(pb)
            _gone[pb.title] = now
            n += 1
    return n


async def _watch_viewers():
    """While any playback runs: ask Jellyfin who is watching, close what
    nobody watches any more."""
    loop = asyncio.get_running_loop()
    while _playbacks:
        try:
            titles = None
            if _jellyfin["url"] and _jellyfin["key"]:
                titles = await asyncio.to_thread(_jellyfin_watched_titles, _jellyfin["url"], _jellyfin["key"])
            now = loop.time()
            _viewers["titles"], _viewers["at"] = titles, now
            _enforce_viewers(titles, now)
        except Exception as e:   # the watch must outlive any one bad answer
            logger.error(f"[VOD] viewer check failed: {e}")
        await asyncio.sleep(VIEWER_CHECK_SECONDS)


def _refuse_continuation(title: str, start: int) -> bool:
    """Whether this request is Jellyfin's own work coming back for a title
    that was closed for having no viewer: it carries on from past byte 0,
    nothing of the title runs, and Jellyfin shows nobody watching it."""
    gone = _gone.get(title)
    if gone is None:
        return False
    loop = asyncio.get_running_loop()
    if loop.time() - gone > GONE_REFUSE_SECONDS or start == 0:
        del _gone[title]                # a player (re)starting the title reads its start first
        return False
    if any(pb.title == title and not pb.stopped.is_set() for pb in _playbacks.values()):
        return False
    viewers = _viewer_count(title)
    if viewers:
        del _gone[title]
    return viewers == 0


def _shared_playback(title: str, token: str) -> "Optional[_Playback]":
    """The running playback a request for `title` from this client joins:
    its own, else another client's of the same title while Jellyfin shows
    no more people watching the title than there are playbacks of it (the
    browser that direct-plays a film and Jellyfin's subtitle extraction of
    it are one person, one connection). Unknown viewers: per client, as before."""
    own = _playbacks.get(token)
    if own is not None and not own.stopped.is_set():
        return own
    others = [pb for pb in _playbacks.values() if pb.title == title and not pb.stopped.is_set()]
    if not others:
        return None
    viewers = _viewer_count(title)
    if viewers is None or viewers > len(others):
        return None
    return max(others, key=lambda pb: pb.last_used)


async def _playback_for(db, title: str, owner: str, client: str = "") -> _Playback:
    token = f"{title}@{client}"
    pb = _shared_playback(title, token)
    if pb is not None:
        pb.touch()
        return pb
    _jellyfin["url"] = (get_setting(db, "jellyfin_url", "") or "").rstrip("/")
    _jellyfin["key"] = get_setting(db, "jellyfin_api_key", "") or ""
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
    existing = _shared_playback(title, token)
    if existing is not None:
        # A concurrent first request for the same title got there while we
        # waited for the slot: one playback, one lease -- give this one back.
        livetv._stream_slots.release_lease(lease)
        existing.touch()
        return existing
    pb = _Playback(token, owner, lease, idle_seconds(db), client, title)
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
    pb = _shared_playback(f"{kind}/{token_file}", f"{kind}/{token_file}@{who}")
    if pb is not None and pb.size is not None:
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
    loop = asyncio.get_running_loop()
    while pb.holder is not reader:
        if pb.stopped.is_set():
            return False
        reader.turn.clear()
        holder = pb.holder
        me_viewer = _is_viewer(pb.title, reader.client)
        holder_viewer = holder is not None and _is_viewer(pb.title, holder.client)
        # Between a viewer and Jellyfin's work, the one waiting looks again
        # soon: a viewer to take over from a holder that cannot hand over (its
        # consumer stopped reading), Jellyfin's work to read while the viewer's
        # player is not reading.
        wait = PREEMPT_GRACE_SECONDS if holder is not None and me_viewer != holder_viewer \
            else QUANTUM_SECONDS + PREEMPT_GRACE_SECONDS
        try:
            await asyncio.wait_for(reader.turn.wait(), wait)
        except asyncio.TimeoutError:
            async with pb.open_lock:
                holder = pb.holder
                if holder is reader or pb.stopped.is_set():
                    continue
                now = loop.time()
                if holder is None:
                    pb.take(reader)
                elif _is_viewer(pb.title, holder.client) and not _is_viewer(pb.title, reader.client):
                    # The viewer's player has stopped reading for a while (or
                    # was handed the connection and has not asked for it):
                    # read meanwhile. Not while it is (re)opening its connection.
                    idle = now - max(holder.last_chunk, holder.since) >= PREEMPT_GRACE_SECONDS
                    if idle and (holder.resp is not None or not holder.waiting):
                        pb.take(reader)
                elif _is_viewer(pb.title, reader.client) and not _is_viewer(pb.title, holder.client):
                    pb.take(reader)         # Jellyfin's work did not hand over: its consumer is stuck
                elif now - holder.since >= QUANTUM_SECONDS:
                    pb.take(reader)
    return not pb.stopped.is_set()


@router.get("/api/vod/{kind}/{token_file}")
async def vod_stream(kind: str, token_file: str, request: Request, db: Session = Depends(get_db)):
    """Stream one provider file through Tentacle: byte ranges passed through
    (so seeking works), resumed from where it stopped if the upstream drops
    or ends short, counted and ranked against live TV and recordings, and
    one provider connection per playback however many requests share it."""
    url, guard, ua, owner = await asyncio.to_thread(_resolve, db, kind, token_file)
    # One playback per title per PERSON: two TVs on the same film are two
    # playbacks (two provider connections, two slots), not one that they
    # keep taking from each other; a second client of a title only one
    # person watches (Jellyfin's subtitle extraction next to a browser that
    # plays the file itself) joins the playback (_shared_playback).
    who = request.client.host if request.client else "unknown"
    range_header = request.headers.get("range")
    start, end = _range_bounds(range_header)
    if _refuse_continuation(f"{kind}/{token_file}", start):
        logger.info(f"[VOD] {owner}: refused a pull from byte {start} of a title nobody watches any more "
                    f"(Jellyfin's own work coming back for the rest, such as a subtitle extraction)")
        raise HTTPException(503, "Nobody is watching this title any more")
    pb = await _playback_for(db, f"{kind}/{token_file}", owner, who)
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

    reader = _Reader(who)
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
            reader.waiting = False
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
    # Either way the byte a drop stopped at is known, so a drop is always
    # resumed with a Range request; only a 206 that starts at exactly that
    # byte is spliced in (a provider that ignores ranges ends the body
    # instead of replaying bytes the player already has). The provider
    # behind this answers a request from byte 0 with a 200 that names no
    # Accept-Ranges, then serves `bytes=N-` with a 206: such a film used to
    # "give up" at its first drop, every few minutes with several streams open.
    length = resp.headers.get("content-length", "")
    if status == 206:
        cr_start, cr_end = _content_range(resp.headers.get("content-range"))
        start = cr_start if cr_start is not None else start
        end = cr_end if cr_end is not None else end
        m = _SIZE_RE.search(resp.headers.get("content-range") or "")
        if m:
            pb.size = int(m.group(1))
    else:
        start, end = 0, None
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
        resumed_from = None
        try:
            while True:
                if pb.stopped.is_set():
                    return
                if reader.resp is None:
                    # Parked (another request had the connection), or after a drop.
                    reader.waiting = True
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
                        got = _content_range(again.headers.get("content-range"))[0]
                        if again.status_code != 206 or got not in (None, start + sent):
                            pb.close_upstream(again)
                            answer = again.status_code if again.status_code != 206 else f"206 from byte {got}"
                            logger.error(f"[VOD] {owner}: the provider would not resume from byte {start + sent} "
                                         f"(answered {answer})")
                            return
                        if resumed_from is not None:
                            logger.info(f"[VOD] {owner}: resumed from byte {resumed_from} after the provider "
                                        f"dropped the connection")
                            resumed_from = None
                        reader.resp = again
                        reader.since = reader.last_chunk = loop.time()
                        reader.waiting = False
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
                        reader.last_chunk = loop.time()
                        if pb.waiters and pb.holder is reader:
                            # Time-share: another request of this playback is
                            # waiting; it gets the connection for a while (a
                            # viewer at once, _next_turn).
                            nxt = _next_turn(pb, reader, reader.last_chunk)
                            if nxt is not None:
                                pb.take(nxt)
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
                if attempts >= RECONNECT_ATTEMPTS:
                    logger.error(f"[VOD] {owner}: stream lost after {sent} bytes, giving up: {reason}")
                    return
                if end is not None and start + sent > end:
                    state["complete"] = True
                    return                  # the requested range was delivered in full
                attempts += 1
                delay = min(5.0, RESUME_FIRST_DELAY * 2 ** (attempts - 1)) * (0.8 + random.random() * 0.4)
                resumed_from = start + sent
                logger.warning(f"[VOD] {owner}: upstream dropped after {sent} bytes; resuming from byte "
                               f"{start + sent} in {delay:.1f}s: {reason}")
                await asyncio.sleep(delay)
        finally:
            finish()

    return _VodResponse(body(), status_code=status, media_type=content_type, headers=passthrough,
                        finish=finish)

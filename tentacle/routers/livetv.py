"""
Tentacle - Live TV Router

Handles live channel sync, channel management, HDHomeRun emulation,
and XMLTV serving for Jellyfin integration.

HDHomeRun endpoints (Jellyfin connects to these):
  GET /hdhr/discover.json       → Device discovery
  GET /hdhr/lineup.json         → Channel lineup
  GET /hdhr/lineup_status.json  → Scan status
  GET /hdhr/xmltv.xml           → EPG guide data

Channel management:
  GET    /api/live/channels          → List channels
  PUT    /api/live/channels/{id}     → Update channel
  POST   /api/live/channels/bulk     → Bulk enable/disable
  GET    /api/live/groups            → List groups
  PUT    /api/live/groups/{id}       → Enable/disable group
  POST   /api/live/sync/{provider_id}  → Sync channels from provider
  POST   /api/live/sync-epg/{provider_id} → Sync EPG data
  GET    /api/live/sync-status       → Sync progress
  GET    /api/live/status            → Live TV status overview
"""

import asyncio
import hashlib
import logging
import re
import threading
from collections import Counter, deque

import httpx
from datetime import datetime, timedelta
from typing import Optional, List

from services.epg_categories import infer_category
from services.youtube import livetv as youtube_livetv

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models.database import (
    EPGProgram,
    LiveChannel,
    LiveChannelGroup,
    Provider,
    SessionLocal,
    get_db,
    get_setting,
    set_setting,
    log_activity,
)
from routers.auth import require_admin, require_internal_or_admin
from services.ssrf import explain_url, is_safe_url, lan_origin_guard
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

router = APIRouter()

# Reusable dependency list for Live TV *management* routes (provider config,
# sync, channel/group mutations, guide refresh). The HDHomeRun tuner-emulation,
# stream-proxy, playlist and XMLTV routes are intentionally left public because
# Jellyfin's tuner integration cannot present credentials.
_admin = [Depends(require_admin)]

# Cap simultaneous upstream pulls so a burst of clients can't exhaust our own
# socket/CPU budget (or an account that really is connection-limited). The cap
# is the admin's call -- setting `livetv_max_concurrent_streams`, 0 = no limit
# -- because the provider's advertised max_connections cannot be trusted either
# way: panels report 1 for accounts that are not capped at all (#87).
_DEFAULT_MAX_CONCURRENT_STREAMS = 6
_MAX_CONCURRENT_STREAMS = _DEFAULT_MAX_CONCURRENT_STREAMS  # kept for importers
# A slot is usually about to free up (a recording ending as the next begins, a
# viewer changing channel), so wait this long for one before refusing. Short:
# a tuner client is blocked on the answer.
_SLOT_WAIT_SECONDS = 5.0

# Max redirect hops we will follow on a stream/chunk fetch, matching httpx's own
# default ceiling.
_MAX_STREAM_REDIRECTS = 10


async def _send_checked(client, url: str, headers: dict, guard=None):
    """GET `url` through `client`, following redirects *and re-validating each hop*.

    `client` must be built with follow_redirects=False. Letting httpx follow
    redirects itself defeats the `is_safe_url` pre-flight: the URL that was
    checked is not the URL that finally gets fetched, so an upstream (an IPTV
    provider — untrusted third-party content) can answer with
    `302 -> http://10.0.0.5:8096/...` and the stream proxy, which is a public
    unauthenticated route, would fetch it and stream the body back to the caller.

    Returns the open streaming response (caller closes it) for the first hop
    that is not a redirect.

    `guard` validates every hop; callers pass one scoped to the channel's
    provider (#76) so a deliberately-configured LAN re-streamer works while
    everything else is still held to is_safe_url().
    """
    guard = guard or is_safe_url
    current = url
    for _ in range(_MAX_STREAM_REDIRECTS):
        req = client.build_request("GET", current, headers=headers)
        resp = await client.send(req, stream=True)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        location = resp.headers.get("location")
        await resp.aclose()
        if not location:
            raise HTTPException(502, "Redirect without Location header")
        current = urljoin(current, location)
        # The guard resolves DNS with a blocking getaddrinfo: off the event
        # loop, so a resolver that hangs doesn't freeze every stream (#464).
        if not await asyncio.to_thread(guard, current):
            logger.warning(f"[LiveTV] Blocked redirect to non-public host: {current}")
            raise HTTPException(502, "Stream redirect points to a non-public host")
    raise HTTPException(502, "Too many redirects")


# A running HLS stream's playlist reads: headers in, body within this long.
_HLS_PLAYLIST_READ = 10.0
# Its segment reads: this many times the segment's own duration (at least
# _HLS_SEGMENT_READ_MIN s). A segment slower than that cannot keep a live
# stream going anyway: the playlist window moves on while it arrives.
_HLS_SEGMENT_READ_FACTOR = 3.0
_HLS_SEGMENT_READ_MIN = 20.0
# After a read is cut at its bound, the next one of that kind gets twice the
# time, up to this many times the bound; a body that arrives resets it.
_HLS_READ_STRETCH_MAX = 4.0


def _segment_read_limit(target_duration) -> float:
    try:
        seconds = float(target_duration) * _HLS_SEGMENT_READ_FACTOR
    except (TypeError, ValueError):
        seconds = 0.0
    return max(_HLS_SEGMENT_READ_MIN, seconds)


async def _aread_within(resp, seconds: float, stretch=None) -> bytes:
    """resp.aread() with a bound on the whole body. httpx's read timeout is
    per read: a body that trickles a byte at a time never reaches it, and a
    running recording would wait on it for as long as the provider likes.
    Past the bound it is a ReadTimeout, retried like any other.

    stretch ({"x": 1.0}, one per stream and kind of read): a read cut at its
    bound gives the next one twice the time, up to _HLS_READ_STRETCH_MAX; a
    body that arrives within the plain bound puts it back (one that needed
    the extra time keeps it, or every other read of a slow provider would be
    cut again). A provider that has turned slow but still delivers is waited
    for; a trickle is still cut.

    asyncio.timeout(), not wait_for(): on Python 3.11 wait_for() can swallow
    a cancellation that arrives as the read completes, and the stream would
    go on pulling from the provider after its client left."""
    factor = stretch["x"] if stretch else 1.0
    t0 = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(seconds * factor):
            body = await resp.aread()
    except TimeoutError:        # asyncio.timeout's own: httpx raises only its own exceptions
        if stretch is not None:
            stretch["x"] = min(factor * 2, _HLS_READ_STRETCH_MAX)
        raise httpx.ReadTimeout(f"the body did not arrive within {seconds * factor:.0f}s",
                                request=resp.request) from None
    if stretch is not None and asyncio.get_running_loop().time() - t0 <= seconds:
        stretch["x"] = 1.0
    return body

# Opening a stream: statuses worth waiting out, and for how long. Kept well under
# a tuner client's patience; the running worker has its own, longer budget.
_OPEN_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 509}


def _raw_retryable(status) -> bool:
    """A raw MPEG-TS open or re-dial also waits out 407 -- what this provider
    family answers for an ended session, even from the token URL a fresh 302
    has just handed out -- and any 5xx, including the non-standard 513 and
    Cloudflare's 520-524. Both cleared within minutes in production, while
    ending on them cut running recordings for good (#298). 401/403/404 still
    stop at once: at the channel URL they mean the login or the channel."""
    return status is not None and (status in _OPEN_RETRYABLE_STATUS or status == 407
                                   or 500 <= status < 600)


# #299: some panels answer a stream request with 200 OK and an error body
# ({"error":"There is an Database Error",...}, an HTML page). A raw connection
# whose first bytes are not an MPEG-TS sync byte and that is labelled as text,
# or starts like JSON/HTML, is such a page: never proxied, waited out like a
# refusal. Real TS always starts with 0x47, whatever its label says -- or,
# when a connection starts mid-packet (its first byte can be anything, "{" and
# "<" included), shows the sync byte every 188 bytes from within the first
# packet.
_NOT_MEDIA_TYPES = ("text/html", "application/json", "text/plain", "application/xml", "text/xml")


def _ts_sync_offset(first: bytes):
    """Where the first whole MPEG-TS packet starts in a connection's first
    bytes: 0 when they start with the sync byte, k when 0x47 comes at k,
    k+188 and k+376 (a start mid-packet), else None."""
    if first[:1] == b"G":
        return 0
    return next((k for k in range(min(188, len(first) - 376))
                 if first[k] == first[k + 188] == first[k + 376] == 0x47), None)


def _looks_like_error_page(content_type: str, first: bytes) -> bool:
    if _ts_sync_offset(first) is not None:
        return False
    ct = (content_type or "").lower()
    return any(t in ct for t in _NOT_MEDIA_TYPES) or first.lstrip()[:1] in (b"{", b"<")


async def _decidable_start(pieces, need: int = 564):
    """The connection's pieces, the first ones merged until the start can be
    judged: it begins with the sync byte, or holds three packets' worth (a
    stream that starts mid-packet shows 0x47 every 188 bytes), or the
    connection ended (an error page is short)."""
    head = b""
    try:
        async for piece in pieces:
            if head is not None:
                head += piece
                if head[:1] != b"G" and len(head) < need:
                    continue
                piece, head = head, None
            yield piece
    except httpx.HTTPError:
        if head:
            yield head      # what came before the drop still counts
        raise
    if head:
        yield head


# #368: many panels open every raw TS connection with their buffer, ~20 s
# behind live: packets an earlier connection already delivered, byte for byte.
# Forwarded again, they repeated in the recording at every re-dial (on a
# "newest connection wins" account every ~13 s: files 1.7-2.6x their content,
# time jumping back at each reconnect). _ReplaySplicer joins a re-dialled
# connection right after the last byte sent, but only when that is proven;
# anything else goes out as it comes (duplicates at worst, never a loss).
_SPLICE_HOLD_BYTES = 32 * 1024 * 1024   # the most of a replay held: half a client's byte slack
_SPLICE_HOLD_SECONDS = 30.0             # the longest the output pauses for one
_SPLICE_DECIDE_PACKETS = 2048           # the first frame start must come within this
_SPLICE_INDEX_MAX = 16384               # frame starts remembered (minutes of a channel)
_SPLICE_TAIL_MAX = 4 * 1024 * 1024      # bytes since the last one, kept to compare


def _ts_frame_start(pkt) -> bool:
    """A TS packet that starts a video or audio PES carrying a timestamp: the
    first packet of a frame. Its bytes occur once in a stream (the PTS moves
    on), unlike tables, null packets or a silent frame's payload."""
    if pkt[0] != 0x47 or not pkt[1] & 0x40 or not pkt[3] & 0x10:
        return False
    i = 4 + (1 + pkt[4] if pkt[3] & 0x20 else 0)
    return (i + 13 <= 188 and pkt[i:i + 3] == b"\x00\x00\x01"
            and (0xC0 <= pkt[i + 3] <= 0xEF or pkt[i + 3] == 0xBD)
            and pkt[i + 6] & 0xC0 == 0x80 and bool(pkt[i + 7] & 0x80))


class _ReplaySplicer:
    """What a raw TS stream sent (its frame starts and where they went), and
    the judging of a re-dialled connection against it.

    After a re-dial the new connection is held until its first frame start.
    If that packet was sent before, at output offset o, the connection is a
    replay that should reach the end of what was sent at sent_total - o bytes
    past it. It is held up to that point, every frame start on the way must
    sit exactly where it was sent (a gap in between shifts them), and the
    bytes sent since the last frame start must come right before it. Then the
    held replay is dropped and the stream goes on from there. Anything else
    (a first frame never sent: a real gap or no replay; a replay that differs,
    is too large or too slow; a break before the join) releases what was held."""

    def __init__(self, channel_id: int, health: dict):
        self.channel_id, self.health = channel_id, health
        self.sent_total = 0
        self._index: dict = {}          # frame-start packet -> where it was sent (the newest)
        self._order = deque()           # (offset, packet), oldest first
        self._tail: list = []           # bytes sent since the last frame start
        self._tail_len = 0
        self._tail_ok = False           # none yet, or the tail outgrew _SPLICE_TAIL_MAX
        self._breaks = deque([0])       # where a connection not joined began in the output
        self._hold = None               # the re-dialled connection's bytes while it is judged

    def sent(self, data: bytes) -> None:
        """Whole packets that went out, in order."""
        base, last = self.sent_total, None
        for i in range(0, len(data) - 187, 188):
            if data[i + 1] & 0x40 and _ts_frame_start(data[i:i + 188]):
                key = data[i:i + 188]
                self._index[key] = base + i
                self._order.append((base + i, key))
                last = i
        self.sent_total += len(data)
        if last is not None:
            self._tail, self._tail_len, self._tail_ok = [data[last:]], len(data) - last, True
        elif self._tail_ok and data:
            self._tail.append(data)
            self._tail_len += len(data)
            if self._tail_len > _SPLICE_TAIL_MAX:
                self._tail, self._tail_len, self._tail_ok = [], 0, False
        # Older than a hold could reach back to: never a join point again.
        floor = self.sent_total - _SPLICE_HOLD_BYTES
        while self._order and (len(self._order) > _SPLICE_INDEX_MAX or self._order[0][0] < floor):
            off, key = self._order.popleft()
            if self._index.get(key) == off:
                del self._index[key]
        while len(self._breaks) > 1 and self._breaks[0] < floor - _SPLICE_DECIDE_PACKETS * 188:
            self._breaks.popleft()

    def begin(self, now: float, outage: float) -> None:
        """A re-dialled connection starts delivering (its partial first packet trimmed)."""
        self._hold, self._scan, self._first, self._join = bytearray(), 0, None, None
        self._since, self._outage = now, outage

    def feed(self, piece: bytes, now: float) -> bytes:
        """The connection's next bytes; returns what goes out now."""
        if self._hold is None:
            return piece
        self._hold += piece
        hold = self._hold
        if hold[:1] != b"G":
            return self._release("not MPEG-TS")
        while self._scan + 188 <= len(hold) and (self._join is None or self._scan < self._join):
            n = self._scan
            self._scan += 188
            if not (hold[n + 1] & 0x40 and _ts_frame_start(hold[n:n + 188])):
                if self._first is None and self._scan >= _SPLICE_DECIDE_PACKETS * 188:
                    return self._release("no frame start to join on")
                continue
            sent_at = self._index.get(bytes(hold[n:n + 188]))
            if self._first is None:
                if sent_at is None:
                    return self._release("it starts with a frame not sent before: a gap, or no replay")
                if not self._tail_ok:
                    return self._release("too long since the last frame start sent")
                # What came before that frame (tables, the end of the previous
                # frame) is dropped too: it must have gone out right before it,
                # not be cut off by an earlier connection's start.
                if any(sent_at - n < b <= sent_at for b in self._breaks):
                    return self._release("what precedes its first frame was not sent")
                self._first = (n, sent_at)
                self._join = n + self.sent_total - sent_at
                if self._join > _SPLICE_HOLD_BYTES:
                    return self._release(f"a replay of {self._join / 1e6:.0f} MB is too large to hold")
            elif sent_at != self._first[1] + n - self._first[0]:
                return self._release("its frames are not where they were sent")
        if self._join is not None and len(hold) >= self._join:
            tail = b"".join(self._tail)
            if hold[self._join - len(tail):self._join] != tail:
                return self._release("the bytes before the join point differ")
            out = bytes(hold[self._join:])
            self.health["replays_joined"] += 1
            self.health["replay_bytes_skipped"] += self._join
            self.health["_covered_seconds"] = self.health.get("_covered_seconds", 0.0) + self._outage
            logger.info(f"[LiveTV] Raw stream for channel {self.channel_id}: rejoined where it "
                        f"stopped, {self._join / 1e6:.1f} MB the provider sent again skipped")
            self._hold = None
            return out
        if now - self._since > _SPLICE_HOLD_SECONDS:
            return self._release(f"no join point within {_SPLICE_HOLD_SECONDS:.0f}s")
        return b""

    def abort(self) -> bytes:
        """The connection ended while it was being judged: what it held goes out."""
        return self._release("the connection ended before the join point") if self._hold is not None else b""

    def _release(self, why: str) -> bytes:
        out, self._hold = bytes(self._hold), None
        self._breaks.append(self.sent_total)
        logger.info(f"[LiveTV] Raw stream for channel {self.channel_id}: re-dial not joined ({why}); "
                    f"sent as it came")
        return out


def _raw_media_type(content_type: str) -> str:
    """What the tuner is told a raw stream is: the provider's type, unless it
    is missing or names text (an error page's label on a stream that turned
    out to be real TS, or one that will be re-dialled until it is)."""
    ct = (content_type or "").lower()
    if not ct or any(t in ct for t in _NOT_MEDIA_TYPES):
        return "video/mp2t"
    return content_type
# What a provider answers once a tokenized stream URL has expired (seen in
# production: 407 about an hour into an HLS stream). Recovered by resolving
# the CHANNEL url again, which hands out a fresh token -- at most
# _MAX_RERESOLVE times in a row without a segment arriving in between;
# after that the account really is refusing and the stream ends as before.
# Except a 407 from the channel URL itself on a RECORDING: this provider
# family answers 407 for an ended session for a few seconds while the token
# is renewed, so it is waited out like a refusal, as the raw path does
# (_raw_retryable). 401/403 there still end it: the login is refused.
_TOKEN_EXPIRED_STATUS = {401, 403, 404, 407, 410}
_MAX_RERESOLVE = 3
# #184: a running HLS stream refused (429/509) for _REVIVE_AFTER s in a row
# may have lost its session for good -- on a "newest connection wins" account
# the older session is ended, and asking its tokenized URL again answers 509
# until the timer closes (2 h of a game lost in production). It resolves the
# channel URL again, in the same response, but ONLY when no rival is
# delivering: another stream on the same provider account, of equal or
# higher priority, that delivered within _RIVAL_FRESH s. Re-resolving against
# a delivering rival would kick it (and it would kick back: ping-pong, both
# recordings damaged). Between attempts the cooldown doubles from
# _REVIVE_COOLDOWN to _REVIVE_COOLDOWN_CAP, and resets once segments flow.
# Raw TS is not affected: its re-dial already starts from the channel URL.
_REVIVE_AFTER = 45.0
_REVIVE_COOLDOWN = 60.0
_REVIVE_COOLDOWN_CAP = 300.0
_RIVAL_FRESH = 20.0
# Jellyfin marks a timer InProgress only after the tuner stream is open, and
# a lookup can fail: a live pull counts as a possible recording until
# Jellyfin has ANSWERED a lookup made this long after the pull started.
_CLASSIFY_GRACE = 10.0
# A rival whose state is still "streaming" (it has not started failing) is
# delivering for this long after its last success: a segment or read can
# take a while, and a raw TS stream marks itself only every few seconds.
_RIVAL_STREAMING_FRESH = 60.0
# The cooldown returns to _REVIVE_COOLDOWN only after this long of unbroken
# delivery: a player outside Tentacle that re-opens the account every time it
# is kicked would otherwise be fought every 60 s (#184 review, Q3).
_REVIVE_RESET_AFTER = 600.0
# How often a raw TS stream re-marks itself as delivering (Q1).
_RAW_MARK_EVERY = 5.0
# For a recording, a 404/410 counts toward _MAX_RERESOLVE only after this long
# of unbroken 404/410 (a panel blip is seconds); viewers keep the 3-try rule.
_GONE_GRACE = 180.0
# An HLS recording whose channel URL keeps answering with a continuous stream
# instead of a playlist ends after this long, so Jellyfin re-opens on it.
_STREAM_ANSWER_LIMIT = 120.0


class _NotAPlaylist(httpx.TransportError):
    """The channel URL answered with a stream where a playlist was expected."""
_OPEN_RETRY_BUDGET = 20.0   # seconds
# How long the open waits for the first playlist's body once its headers are in
# (the variant read after it has the same bound).
_OPEN_PLAYLIST_READ = 10.0

# Backoff while a RUNNING stream re-dials. A transport error is retried on a
# short cap (the tuner reader is waiting). A refusal -- 429/509, the account
# is over its connection limit right now -- is waited out on a longer cap:
# every attempt is itself a new connection at the provider, and re-dialling
# every few seconds while it is saturated keeps it saturated.
_BACKOFF_CAP = 5.0
_REFUSAL_STATUS = {429, 509}
_REFUSAL_BACKOFF_CAP = 15.0

# Stand-ins a provider (or a re-streamer in front of it, e.g. tuliprox) serves
# when it has nothing for a channel: 200 OK and a few seconds of valid MPEG-TS,
# usually black. Proxied as the channel, a scheduled recording "succeeds" with
# ten minutes of black that Jellyfin never retries, or dies on its own buffer
# with an error that says nothing about the cause (#140). Recognised by file
# name, never fetched, and refused with a 503 while the channel is opening, so
# Jellyfin fails the timer honestly and tries again a minute later.
_PLACEHOLDER_STEMS = {
    "black",
    "channel_unavailable",
    "user_connections_exhausted",
    "provider_connections_exhausted",
    "user_account_expired",
}
# Placeholder refusals since start, per channel: {"count", "segment", "at"}.
_placeholders: "dict[int, dict]" = {}
# When each channel last got an Activity line for one (at most once an hour).
_placeholder_activity_at: "dict[int, float]" = {}
_PLACEHOLDER_ACTIVITY_EVERY = 3600.0


def _placeholder_name(url: str) -> "str | None":
    """The file name, when `url` is a known placeholder segment."""
    from urllib.parse import urlparse
    try:
        base = urlparse(url).path.rsplit("/", 1)[-1].lower()
    except ValueError:
        return None
    stem, dot, ext = base.rpartition(".")
    return base if dot and ext == "ts" and stem in _PLACEHOLDER_STEMS else None


class _ProviderPlaceholder(Exception):
    """The provider answered with a placeholder instead of the channel."""

    def __init__(self, segment: str):
        super().__init__(f"the provider served a placeholder ({segment}) instead of the channel")
        self.segment = segment


def _record_activity_for_placeholder(channel_id: int, segment: str) -> None:
    db = SessionLocal()
    try:
        ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
        name = ch.guide_name if ch else f"channel {channel_id}"
        log_activity(db, "livetv_placeholder",
                     f"{name}: the provider served a placeholder ({segment}) instead of the "
                     f"channel, so it is unavailable right now. Recordings are refused and "
                     f"retried rather than saved as black.")
    except Exception as e:
        logger.debug(f"[LiveTV] Could not log the placeholder for channel {channel_id}: {e}")
    finally:
        db.close()


async def _note_placeholder(channel_id: int, segment: str) -> None:
    """Count, log and (at most hourly) put a placeholder in Activity."""
    loop = asyncio.get_running_loop()
    entry = _placeholders.setdefault(channel_id, {"count": 0})
    entry.update(count=entry["count"] + 1, segment=segment,
                 at=datetime.utcnow().isoformat() + "Z")
    logger.warning(f"[LiveTV] Channel {channel_id}: the provider served a placeholder ({segment}) "
                   f"— channel unavailable")
    last = _placeholder_activity_at.get(channel_id)
    if last is None or loop.time() - last >= _PLACEHOLDER_ACTIVITY_EVERY:
        _placeholder_activity_at[channel_id] = loop.time()
        await asyncio.to_thread(_record_activity_for_placeholder, channel_id, segment)


# Jellyfin retries a failed recording open 60 s later (at most 10 times, and
# only until the timer ends).
TUNER_RETRY_AFTER_SECONDS = 60


class _TunerRefusal(HTTPException):
    """A refused tuner open: no free slot, a recording needed the slot,
    recording protection, or a placeholder. Answered as a 503 with NO body by
    tuner_refusal_handler (registered on the app in main.py).

    Jellyfin's tuner (SharedHttpStream.Open) never looks at the status: it
    copies whatever body arrives. FastAPI's JSON error body "opened" as a
    stream, was probed for 3 s, and left a recording .nfo and show folder
    behind on every retry. An empty body fails at once ("Zero bytes copied")
    and Jellyfin retries (#140). The reason goes in X-Tentacle-Reason."""

    def __init__(self, detail: str):
        super().__init__(503, detail)


async def tuner_refusal_handler(request, exc: "_TunerRefusal") -> Response:
    reason = str(exc.detail or "unavailable").encode("ascii", "replace").decode("ascii")[:300]
    return Response(status_code=503, content=b"", headers={
        "X-Tentacle-Reason": reason,
        "Retry-After": str(TUNER_RETRY_AFTER_SECONDS),
        "Cache-Control": "no-cache, no-store",
        "Connection": "close",
    })


class _PlaceholderRefusal(_TunerRefusal):
    """The 503 a channel open answers when the provider served a placeholder."""


async def _refuse_placeholder(channel_id: int, segment: str):
    await _note_placeholder(channel_id, segment)
    raise _PlaceholderRefusal(f"Channel unavailable: the provider served a placeholder ({segment})")


def _media_segments(playlist_text: str, base_url: str) -> list:
    """Segment URLs of an HLS media playlist, resolved against its URL."""
    return [urljoin(base_url, line.strip()) for line in playlist_text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


# What an upstream pull is for. Lower number = more important. A recording
# outranks a viewer (tvheadend: 300 vs 100): a viewer who is cut off changes
# channel; a recording that is cut off is gone for good.
_LEASE_PRIORITY = {"recording": 0, "live": 10, "vod": 20}

# Recording protection (setting `livetv_protect_recordings`, default off).
#
# Some accounts carry ONE heavy connection well and punish a second one on
# the stream that is already open, whatever max_connections says. Measured
# on one household's Xtream account (2026-09-23/24): one continuous-TS
# channel alone dropped once in 30 min; two TS recordings at once were
# closed by the provider every ~8 s, in turn, for 1 h 45 min; a TV episode
# played next to a TS recording closed the recording every ~20 s for as
# long as it played; and every new connection (a film, a card preview, the
# nightly sync) put a running HLS recording into a 509 storm of 30 s to
# 3 min. A connection limit cannot express that -- "2" still lets a film
# open next to a recording. With protection on, while a recording is being
# pulled:
#   * a NEW upstream for a viewer ("live") or a film ("vod") is refused
#     with 503 -- except a viewer of a channel that is already being pulled,
#     who attaches to that pull (_SharedUpstream) and costs the provider
#     nothing;
#   * a recording always opens, and when it does, every running viewer or
#     film upstream is stopped first (cleanly: the viewer's stream ends, the
#     film's connection closes) -- on that account a film left running cuts
#     the recording every ~20 s until it ends;
#   * Tentacle's own background provider work waits (services.provider_activity).
# It errs towards the recording: when Jellyfin cannot say right now what is
# being recorded, a live open is let through rather than refused (it may
# BE the next recording) and only films are stopped for a recording.
def _protect_recordings(db) -> bool:
    return (get_setting(db, "livetv_protect_recordings", "") or "").strip().lower() in ("1", "true", "yes", "on")


class RecordingProtected(Exception):
    """acquire_lease refused a viewer or film because a recording runs and
    recording protection is on."""

    def __init__(self, kind: str, owner: "str | None"):
        super().__init__(f"{kind} '{owner}' refused: a recording is running and recording protection is on")
        self.kind = kind
        self.owner = owner


# Refusals are logged at most this often (a player retrying every few
# seconds must not flood the log); the rest are counted.
_PROTECT_LOG_EVERY = 60.0
PROTECTED_REFUSAL_DETAIL = ("A recording is running and recording protection is on "
                            "(livetv_protect_recordings): no other provider connection is opened "
                            "until it ends. Channels already being recorded can still be watched.")


class _Lease:
    """One upstream pull's claim on a connection slot."""
    __slots__ = ("id", "kind", "priority", "owner", "stream_key", "started", "preempted", "on_preempt",
                 "provider_id", "channel_id", "account")

    def __init__(self, lease_id: int, kind: str, owner: "str | None", stream_key: "str | None" = None):
        self.id = lease_id
        self.kind = kind
        self.priority = _LEASE_PRIORITY.get(kind, _LEASE_PRIORITY["live"])
        self.owner = owner
        self.stream_key = stream_key   # the channel's GuideNumber, for sync_recordings
        self.started = asyncio.get_running_loop().time()
        self.preempted = False
        # Set by the owner once its pump exists: called (may be async) when
        # a more important pull takes this slot.
        self.on_preempt = None
        # Which account and channel this pull is, once known (#184's rival test).
        self.provider_id = None
        self.channel_id = None
        self.account = None     # (server host, username): two provider rows, one account


class _StreamSlots:
    """Counts upstream pulls against a limit that can change while running,
    and knows what each one is for.

    An asyncio.Semaphore bakes its size in at creation, which is why the old
    ceiling could not be a setting. All access is from the event loop.

    At capacity, a pull that outranks one already running takes its slot
    (`_LEASE_PRIORITY`): a recording pre-empts a viewer, never the other way
    round, and never an equal. Everything else waits briefly for a slot and
    is then refused, as before."""

    def __init__(self):
        self.leases: "dict[int, _Lease]" = {}
        self.refused = 0
        self.preempted_since_start = 0
        self.last_refused: "dict | None" = None
        self._next_id = 1
        # Replaced (not cleared) each time it fires, so a waiter that took a
        # reference before checking can never miss a wake-up.
        self._freed: "asyncio.Event | None" = None
        # Pulls waiting for a slot: id -> (priority, arrival). When a slot
        # frees, the most important, earliest waiter gets it -- a recording
        # that is waiting is never beaten to a freed slot by a viewer that
        # happened to start waiting first.
        self._waiting: "dict[int, tuple]" = {}
        self._next_waiter = 1
        # Recording protection (see _protect_recordings). The setting is
        # read by whoever opens a pull and by the recording refresher, and
        # kept here for the synchronous paths (sync_recordings).
        self.protect = False
        self.protected_refusals = 0
        self.protected_preemptions = 0
        self.last_protected_refusal: "dict | None" = None
        self._protect_logged_at = -1e9
        self._protect_unlogged = 0
        self._enforcing: "set[asyncio.Task]" = set()

    @property
    def active(self) -> int:
        return len(self.leases)

    def recording_active(self) -> bool:
        """A recording is being pulled right now. Safe from worker threads."""
        return any(l.kind == "recording" and not l.preempted for l in list(self.leases.values()))

    def set_protect(self, on: bool) -> None:
        """Apply the current setting. Turned on while a recording runs, it
        clears the way for it at once, not at the next open."""
        on = bool(on)
        was, self.protect = self.protect, on
        if on and not was:
            self._enforce_soon()

    def _refuse_protected(self, kind: str, owner: "str | None"):
        self.protected_refusals += 1
        self.last_protected_refusal = {"kind": kind, "owner": owner,
                                       "at": datetime.utcnow().isoformat() + "Z"}
        now = asyncio.get_running_loop().time()
        if now - self._protect_logged_at >= _PROTECT_LOG_EVERY:
            more = f" ({self._protect_unlogged} more refused since the last message)" if self._protect_unlogged else ""
            logger.warning(f"[LiveTV] Recording protection: refused {kind} '{owner}' — a recording is "
                           f"running, so no other provider connection is opened until it ends{more}")
            self._protect_logged_at, self._protect_unlogged = now, 0
        else:
            self._protect_unlogged += 1
        raise RecordingProtected(kind, owner)

    def _protect_victims(self, films_only: bool, live_before: "float | None" = None) -> "list[_Lease]":
        """Take every running viewer and film pull off the books (recording
        protection). `films_only` when it is not certain which live pulls are
        recordings: a film never is. `live_before`: only live pulls that were
        already running then -- a lookup answer issued at that time cannot
        know about a pull (maybe a recording) that opened after it."""
        victims = [l for l in self.leases.values()
                   if l.kind != "recording" and not l.preempted
                   and (l.kind == "vod" or (not films_only and (live_before is None or l.started < live_before)))]
        for victim in victims:
            victim.preempted = True
            self.leases.pop(victim.id, None)
        self.protected_preemptions += len(victims)
        self.preempted_since_start += len(victims)
        return victims

    async def _clear_and_grant(self, kind: str, owner: "str | None", stream_key: "str | None",
                               films_only: bool) -> "_Lease | None":
        """A recording under protection: stop every viewer and film pull and
        take a slot in the same step, before anything is awaited -- so a
        waiter woken while the victims stop finds a recording running (and
        is refused), never a free slot. None when there was nothing to stop
        (the ordinary path decides then). Every victim freed a slot, so the
        count never goes over the limit."""
        victims = self._protect_victims(films_only, _recording_cache.get("answer_issued_at"))
        if not victims:
            return None
        lease = self._grant(kind, owner, stream_key)
        try:
            await self._stop_victims(victims, owner)
        except asyncio.CancelledError:
            self.leases.pop(lease.id, None)
            self._wake()
            raise
        self._wake()
        return lease

    async def _clear_for_recording(self, owner: "str | None", films_only: bool = False,
                                   live_before: "float | None" = None) -> int:
        """Stop every running viewer and film pull because a recording is
        running (it was recognised after it opened, a current answer came in,
        or protection was just turned on)."""
        victims = self._protect_victims(films_only, live_before)
        await self._stop_victims(victims, owner)
        return len(victims)

    async def _stop_victims(self, victims, owner: "str | None") -> None:
        for victim in victims:
            logger.warning(f"[LiveTV] Recording protection: {victim.kind} '{victim.owner}' is stopped — "
                           f"recording '{owner}' has the provider connection to itself")
            if victim.on_preempt is not None:
                try:
                    result = victim.on_preempt()
                    if asyncio.iscoroutine(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[LiveTV] Stopping {victim.kind} '{victim.owner}' failed: {e}")

    def _enforce_soon(self, live_before: "float | None" = None) -> None:
        """From a synchronous path (a recording was just recognised, or the
        setting was just turned on): clear the way in a task of its own."""
        if not (self.protect and self.recording_active()):
            return
        if not any(l.kind != "recording" and not l.preempted for l in self.leases.values()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        rec = next((l.owner for l in self.leases.values() if l.kind == "recording" and not l.preempted), None)
        # Which live pulls are recordings is only as good as the last answer
        # from Jellyfin: if that is not current, stop films only.
        if live_before is None:
            live_before = _recording_cache.get("answer_issued_at")
        task = loop.create_task(self._clear_for_recording(rec, films_only=not _recording_answer_is_current(),
                                                          live_before=live_before))
        self._enforcing.add(task)
        task.add_done_callback(self._enforcing.discard)

    def _grant(self, kind: str, owner: "str | None", stream_key: "str | None" = None) -> _Lease:
        lease = _Lease(self._next_id, kind, owner, stream_key)
        self._next_id += 1
        self.leases[lease.id] = lease
        return lease

    def sync_recordings(self, stream_keys, issued_at: "float | None" = None) -> int:
        """Bring running pulls into line with which channels are recorded NOW.

        Promote: Jellyfin marks a timer InProgress only AFTER its tuner
        stream is open (RecordingsManager.RecordStream: OpenLiveStreamInternal,
        then timer.Status = InProgress), so the pull that starts a recording
        is classified "live" at the moment it opens; the next lookup corrects
        it here, before anything could take its slot.

        Demote: a recording that has ended while a viewer keeps watching the
        same channel must go back to viewer priority, or it would hold a
        recording's rank for ever and the next scheduled recording on another
        channel could be refused for it. Returns how many changed."""
        keys = set(stream_keys or ())
        n = 0
        for lease in self.leases.values():
            if lease.stream_key is None:
                continue
            recorded = lease.stream_key in keys
            if lease.kind == "live" and recorded:
                lease.kind, lease.priority = "recording", _LEASE_PRIORITY["recording"]
                n += 1
                logger.info(f"[LiveTV] '{lease.owner}' is being recorded — its slot now outranks viewers")
            elif lease.kind == "recording" and not recorded:
                lease.kind, lease.priority = "live", _LEASE_PRIORITY["live"]
                n += 1
                logger.info(f"[LiveTV] '{lease.owner}' is no longer being recorded — back to viewer priority")
        if n:
            self._wake()        # a demoted pull may now be a waiting recording's to take
        if self.protect:
            # Every current answer, not only one that changed a rank: a
            # viewer let through while Jellyfin could not answer, or left
            # running because protection was switched on from a stale answer,
            # is stopped as soon as Jellyfin says it is not a recording.
            self._enforce_soon(live_before=issued_at)
        return n

    def _wake(self):
        if self._freed is not None:
            self._freed.set()
            self._freed = asyncio.Event()

    def _victim_for(self, kind: str) -> "_Lease | None":
        """The least important running pull this kind may take a slot from:
        strictly lower priority only; among equals, the most recent."""
        prio = _LEASE_PRIORITY.get(kind, _LEASE_PRIORITY["live"])
        candidates = [l for l in self.leases.values() if l.priority > prio and not l.preempted]
        if not candidates:
            return None
        return max(candidates, key=lambda l: (l.priority, l.started))

    async def _take_from(self, victim: _Lease, kind: str, owner: "str | None",
                         stream_key: "str | None") -> _Lease:
        """Move the victim's slot to the newcomer in one step -- the slot is
        never free in between, so nobody woken meanwhile can take it and put
        the count over the limit -- then stop the victim."""
        victim.preempted = True
        self.leases.pop(victim.id, None)
        lease = self._grant(kind, owner, stream_key)
        self.preempted_since_start += 1
        logger.warning(f"[LiveTV] At capacity: {kind} '{owner}' takes the slot of {victim.kind} "
                       f"'{victim.owner}', which is stopped")
        if victim.on_preempt is not None:
            try:
                result = victim.on_preempt()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                # The newcomer went away while the victim was being stopped:
                # nobody owns this lease, give the slot back.
                self.leases.pop(lease.id, None)
                self._wake()
                raise
            except Exception as e:
                logger.error(f"[LiveTV] Stopping pre-empted {victim.kind} '{victim.owner}' failed: {e}")
        return lease

    def _first_in_line(self, waiter_id: int) -> bool:
        best = min(self._waiting.items(), key=lambda kv: kv[1])[0] if self._waiting else None
        return best == waiter_id

    async def acquire_lease(self, limit: int, wait: float, kind: str = "live",
                            owner: "str | None" = None, stream_key: "str | None" = None,
                            certain: bool = True) -> "_Lease | None":
        """A slot for one upstream pull, or None when refused at capacity.

        With recording protection on (self.protect): a recording first stops
        every viewer and film pull; a viewer or film is refused with
        RecordingProtected while a recording runs. `certain` is False when
        the caller could not learn from Jellyfin just now what is being
        recorded -- then a live pull is not refused (it may be the next
        recording) and a recording stops films only."""
        prio = _LEASE_PRIORITY.get(kind, _LEASE_PRIORITY["live"])
        if self.protect:
            if kind == "recording":
                lease = await self._clear_and_grant(kind, owner, stream_key, films_only=not certain)
                if lease is not None:
                    return lease
            elif self.recording_active() and (certain or kind == "vod"):
                self._refuse_protected(kind, owner)
        if limit <= 0:
            return self._grant(kind, owner, stream_key)
        # A free slot goes to the newcomer only if nobody as important is
        # already waiting for one.
        if len(self.leases) < limit and not any(p <= prio for p, _ in self._waiting.values()):
            return self._grant(kind, owner, stream_key)
        victim = self._victim_for(kind)
        if victim is not None:
            return await self._take_from(victim, kind, owner, stream_key)
        if self._freed is None:
            self._freed = asyncio.Event()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        me = self._next_waiter
        self._next_waiter += 1
        self._waiting[me] = (prio, me)
        try:
            while True:
                freed = self._freed          # before checking: a set after this is not missed
                if self.protect and kind != "recording" and self.recording_active() \
                        and (certain or kind == "vod"):
                    self._refuse_protected(kind, owner)     # a recording started while we waited
                if len(self.leases) < limit and self._first_in_line(me):
                    return self._grant(kind, owner, stream_key)
                # Something may have become takeable while we waited (a
                # recording demoted back to a viewer when its timer ended).
                victim = self._victim_for(kind)
                if victim is not None and self._first_in_line(me):
                    return await self._take_from(victim, kind, owner, stream_key)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(freed.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
        finally:
            self._waiting.pop(me, None)
            # The queue changed: whoever is first in line now re-checks.
            if self._waiting:
                self._wake()

    async def acquire(self, limit: int, wait: float) -> bool:
        """The original shape: an anonymous viewer-priority slot."""
        return await self.acquire_lease(limit, wait, "live", None) is not None

    def release_lease(self, lease: "_Lease | None"):
        if lease is not None:
            self.leases.pop(lease.id, None)
        self._wake()

    def release(self):
        """The original shape: frees the most recent anonymous slot."""
        anon = [l for l in self.leases.values() if l.owner is None]
        self.release_lease(max(anon, key=lambda l: l.started) if anon else None)

    def lease_for(self, owner: str) -> "_Lease | None":
        # Called from worker threads too (the sync /api/live/streams route);
        # iterate a copy so the loop's own inserts/pops cannot trip it.
        for lease in list(self.leases.values()):
            if lease.owner == owner:
                return lease
        return None


_stream_slots = _StreamSlots()


def _max_concurrent_streams(db) -> int:
    """The configured ceiling; 0 means unlimited. A value that does not parse
    falls back to the default rather than silently removing the cap."""
    raw = get_setting(db, "livetv_max_concurrent_streams", "")
    try:
        return max(0, int(raw)) if raw.strip() else _DEFAULT_MAX_CONCURRENT_STREAMS
    except (ValueError, AttributeError):
        return _DEFAULT_MAX_CONCURRENT_STREAMS


# How long a running stream keeps retrying a failing upstream before it ends.
# 120 s was a fixed constant (#136); a provider that refuses for a little
# longer -- seen live: 509s for 160 s while another household stream was open,
# accepting again 18 s after the pump had given up -- turned one recording
# into four files. Ending the response is the one thing Jellyfin cannot undo:
# it never appends to a recording once its tuner stream closes. 0 = keep
# retrying for as long as the client stays connected (the recorder stays
# until its timer ends; a viewer who gives up closes the socket, which ends
# the retries).
_DEFAULT_RECONNECT_BUDGET = 120.0


def _reconnect_budget(db) -> float:
    """Seconds of unbroken upstream failure a running stream will wait out;
    0 means until the client leaves. Garbage keeps the default."""
    raw = get_setting(db, "livetv_reconnect_budget_seconds", "")
    try:
        return max(0.0, float(raw)) if raw.strip() else _DEFAULT_RECONNECT_BUDGET
    except (ValueError, AttributeError):
        return _DEFAULT_RECONNECT_BUDGET


# Which stream an Xtream channel URL asks the provider for.
#
# "m3u8" (the default, unchanged behaviour) is HLS: the pump reloads the
# playlist every few seconds and fetches every segment -- about 3,600 short
# requests over a three-hour game -- and on a connection-limited account
# every one of them is a chance for the provider to answer 509 because
# something else opened a connection meanwhile. "ts" is the continuous
# MPEG-TS stream most panels also serve: ONE connection for the whole
# programme, taken through the raw-TS path (#103) with its re-dial. "auto"
# picks ts when the account advertises it (user_info.allowed_output_formats,
# remembered at sync time), else m3u8. Changing the setting takes effect at
# the next channel sync, which rewrites every channel's URL in place.
_LIVE_STREAM_FORMATS = ("auto", "m3u8", "ts")
_DEFAULT_LIVE_STREAM_FORMAT = "m3u8"


def _live_stream_format(db) -> str:
    raw = (get_setting(db, "livetv_stream_format", "") or "").strip().lower()
    return raw if raw in _LIVE_STREAM_FORMATS else _DEFAULT_LIVE_STREAM_FORMAT


def _remembered_output_formats(db, provider_id: int) -> list:
    import json
    raw = get_setting(db, f"livetv_output_formats_{provider_id}", "") or ""
    try:
        val = json.loads(raw) if raw.strip() else []
    except ValueError:
        return []
    return [str(v).lower() for v in val] if isinstance(val, list) else []


def _remember_output_formats(db, provider_id: int, info: dict) -> None:
    """Keep what the account says it can serve, from an authenticate() reply."""
    import json
    formats = ((info or {}).get("user_info") or {}).get("allowed_output_formats")
    if isinstance(formats, list) and formats:
        set_setting(db, f"livetv_output_formats_{provider_id}",
                    json.dumps([str(v).lower() for v in formats]))


def _live_extension(db, provider_id: int) -> str:
    """The extension channel URLs are written with, per the setting above."""
    fmt = _live_stream_format(db)
    if fmt in ("m3u8", "ts"):
        return fmt
    return "ts" if "ts" in _remembered_output_formats(db, provider_id) else "m3u8"


# Which channels are being RECORDED right now. Jellyfin opens the same tuner
# URL for a recording as for a viewer, so the request itself cannot say; its
# timers can. A timer that is InProgress names the channel by the id this
# lineup gave it -- ExternalChannelId "hdhr_<GuideNumber>", and GuideNumber
# is the channel's stream_id (see hdhr_lineup) -- which is all the mapping
# there is. Looked up at most every few seconds. A DVR front end that knows
# a recording is about to start (pre-padding) can also reserve the channel
# ahead of time through POST /api/live/reserve.
_RECORDING_LOOKUP_TTL = 5.0
# Longest a tuner open waits for the answer. A slow Jellyfin must not hold
# up a stream open (Jellyfin's own tuner timeout is what it would hit); past
# this the lookup finishes in the background and the last answer is used.
_RECORDING_LOOKUP_WAIT = 3.0
_recording_cache = {"at": -1e9, "sids": set(), "pending": None, "failures": 0, "retry_at": -1e9}
_reserved_channels: "dict[int, float]" = {}   # channel id -> loop time the reservation ends


def _jellyfin_timers(url: str, key: str):
    """GET /LiveTv/Timers with a short timeout of its own (a tuner open may
    be waiting on the answer) and without JellyfinService's per-call ERROR
    log for a bad key -- this runs every few seconds while live TV plays;
    failures are reported once per streak by _recording_lookup_done."""
    import requests
    with requests.get(f"{url.rstrip('/')}/LiveTv/Timers", headers={"X-Emby-Token": key},
                      timeout=(3.0, 5.0)) as r:
        if r.status_code != 200:
            raise RuntimeError(f"Jellyfin answered HTTP {r.status_code} for /LiveTv/Timers"
                               + (" (API key invalid?)" if r.status_code == 401 else ""))
        return r.json()


def _recording_stream_ids_from_jellyfin(url: str, key: str) -> "set[str] | None":
    """GuideNumbers of the channels Jellyfin is recording, or None if it
    gave no usable answer. Blocking; run it off the event loop. Raises when
    Jellyfin cannot be asked at all."""
    data = _jellyfin_timers(url, key)
    if not isinstance(data, dict):
        return None
    out = set()
    now = datetime.utcnow()
    for timer in data.get("Items") or []:
        ext = timer.get("ExternalChannelId") or ""
        if not ext.startswith("hdhr_"):
            continue
        status = timer.get("Status")
        if status == "InProgress":
            out.add(ext[len("hdhr_"):])
        elif status == "New" and _timer_is_imminent(timer, now):
            # Jellyfin opens the tuner stream BEFORE it marks the timer
            # InProgress, so the pull that starts a recording would be a
            # viewer for its first seconds -- and at capacity, a viewer
            # cannot take a slot from a viewer. A timer that is due counts
            # as recording already.
            out.add(ext[len("hdhr_"):])
    return out


_RECORDING_IMMINENT_SECONDS = 120.0


def _timer_is_imminent(timer: dict, now: "datetime") -> bool:
    """A New timer whose recording (start minus pre-padding) begins within
    _RECORDING_IMMINENT_SECONDS, or should already have begun -- and has
    not already ended (a timer Jellyfin never started stays "New" for ever;
    it must not keep its channel ranked as a recording)."""
    def _parse(value):
        try:
            return datetime.strptime((value or "")[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None

    def _seconds(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
    start = _parse(timer.get("StartDate"))
    if start is None:
        return False
    rec_start = start - timedelta(seconds=_seconds(timer.get("PrePaddingSeconds")))
    end = _parse(timer.get("EndDate"))
    if end is not None and end + timedelta(seconds=_seconds(timer.get("PostPaddingSeconds"))) <= now:
        return False
    return (rec_start - now).total_seconds() <= _RECORDING_IMMINENT_SECONDS


async def _recording_channel_ids(db, force: bool = False, background: bool = False,
                                 fresh_after: "float | None" = None) -> "set[int]":
    """LiveChannel ids being recorded now: reservations plus Jellyfin's
    in-progress timers (cached for _RECORDING_LOOKUP_TTL; `force` asks
    again regardless -- used when a slot is about to be taken from someone,
    so the victim is chosen on current information).

    `fresh_after` (recording protection): the answer must come from a
    lookup issued at or after that loop time. A lookup already in flight
    that was issued earlier -- the refresher's, say -- may predate a
    "record now" timer whose tuner open is asking right now; it is waited
    for, then a fresh one is issued. If that cannot be had in time the
    answer is not current (_recording_answer_is_current(since=...)) and
    the caller treats it as uncertain."""
    loop = asyncio.get_running_loop()
    now = loop.time()
    cache = _recording_cache
    stale = cache.get("pending")
    if fresh_after is not None and stale is not None and cache.get("pending_issued", -1e9) < fresh_after:
        try:
            await asyncio.wait_for(asyncio.shield(stale), _RECORDING_LOOKUP_WAIT)
        except Exception:
            pass
        if stale.done():
            _recording_lookup_done(stale)
        else:
            # Still no answer: do not wait a second time for the new one;
            # the caller sees an answer that is not current.
            sids = cache["sids"]
            reserved = {c for c, r in _reserved_channels.items() if r["until"] > loop.time()}
            ids = {row.id for row in db.query(LiveChannel).filter(LiveChannel.stream_id.in_(list(sids))).all()} if sids else set()
            return reserved | ids
        now = loop.time()
    for cid in [c for c, r in _reserved_channels.items() if r["until"] <= now]:
        del _reserved_channels[cid]
    reserved = set(_reserved_channels)
    # After a failed lookup, routine asks (the TTL path and the background
    # refresher) wait 5 s doubling to a minute. The at-capacity ask (`force`
    # from a tuner open) never does: a recording's own tuner open comes from
    # Jellyfin, so Jellyfin is up at that moment even if it was not a minute
    # ago -- and a stale "nothing is recording" there would refuse it.
    backed_off = now < cache.get("retry_at", -1e9) and (background or not force)
    if (force or now - cache["at"] >= _RECORDING_LOOKUP_TTL) and cache["pending"] is None \
            and not backed_off:
        url = (get_setting(db, "jellyfin_url", "") or "").strip()
        key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
        if url and key:
            cache["pending_issued"] = now
            cache["pending"] = asyncio.ensure_future(
                asyncio.to_thread(_recording_stream_ids_from_jellyfin, url, key))
            cache["pending"].add_done_callback(_recording_lookup_done)
        else:
            cache["at"], cache["sids"], cache["answer_issued_at"] = now, set(), now
    pending = cache["pending"]
    if pending is not None:
        try:
            await asyncio.wait_for(asyncio.shield(pending), _RECORDING_LOOKUP_WAIT)
        except asyncio.TimeoutError:
            logger.warning("[LiveTV] Jellyfin is slow to say which channels are recording; "
                           "using the last answer for this stream open")
        except Exception:
            pass
        if pending.done():
            # A done-callback runs a loop turn later than the await returns;
            # apply the answer now (idempotent) so THIS open sees it.
            _recording_lookup_done(pending)
    sids = cache["sids"]
    ids = {row.id for row in db.query(LiveChannel).filter(LiveChannel.stream_id.in_(list(sids))).all()} if sids else set()
    return reserved | ids


def _recording_answer_is_current(since: "float | None" = None) -> bool:
    """The last lookup of what is being recorded succeeded and is recent:
    nothing pending, no failure streak, within the cache TTL -- and, with
    `since`, issued at or after that loop time. Recording protection only
    refuses a live open, or stops a live pull, on such an answer -- a stale
    "not recording" must never cost a recording."""
    cache = _recording_cache
    if cache.get("pending") is not None or cache.get("failures"):
        return False
    if since is not None and cache.get("answer_issued_at", -1e9) < since:
        return False
    try:
        now = asyncio.get_running_loop().time()
    except RuntimeError:
        return False
    return now - cache.get("at", -1e9) <= _RECORDING_LOOKUP_TTL


_recording_refresher: "asyncio.Task | None" = None


async def _refresh_recordings_bounded() -> None:
    """_refresh_recordings_once for a stream deciding whether to take the
    account (#184): never raises, never waits longer than a lookup may."""
    try:
        await asyncio.wait_for(_refresh_recordings_once(), _RECORDING_LOOKUP_WAIT + 1.0)
    except Exception as e:
        logger.debug(f"[LiveTV] recording lookup before a re-resolve failed: {e}")


async def _refresh_recordings_once() -> None:
    """One fresh lookup, applied to the running leases (sync_recordings)."""
    db = SessionLocal()
    try:
        _stream_slots.set_protect(_protect_recordings(db))
        await _recording_channel_ids(db, force=True, background=True)
    finally:
        db.close()


async def _recording_refresh_loop():
    """While anything is being pulled, ask Jellyfin every few seconds which
    channels are recorded, so a recording that opened as a "viewer" (Jellyfin
    marks its timer InProgress only after the stream is open) is promoted
    without waiting for some other stream to open and ask."""
    try:
        while _live_leases_exist():
            await asyncio.sleep(_RECORDING_LOOKUP_TTL)
            if not _live_leases_exist():
                break
            try:
                await _refresh_recordings_once()
            except Exception as e:
                logger.debug(f"[LiveTV] recording refresh failed: {e}")
    finally:
        global _recording_refresher
        _recording_refresher = None


def _live_leases_exist() -> bool:
    """Only live pulls can be recordings; a film playing through /api/vod
    on its own is no reason to keep asking Jellyfin for its timers."""
    return any(lease.kind != "vod" for lease in list(_stream_slots.leases.values()))


def _ensure_recording_refresher():
    global _recording_refresher
    if _recording_refresher is None or _recording_refresher.done():
        _recording_refresher = asyncio.get_running_loop().create_task(_recording_refresh_loop())


def _recording_lookup_done(task):
    cache = _recording_cache
    if cache["pending"] is not task:
        return      # already applied (or superseded by a newer lookup)
    cache["pending"] = None
    issued = cache.get("pending_issued")
    failure = None
    try:
        sids = task.result()
        if sids is None:
            failure = "no usable answer"
    except Exception as e:
        sids, failure = None, str(e) or type(e).__name__
    now = asyncio.get_running_loop().time()
    cache["at"] = now
    if failure is not None:
        cache["failures"] = cache.get("failures", 0) + 1
        cache["retry_at"] = now + min(60.0, _RECORDING_LOOKUP_TTL * 2 ** (cache["failures"] - 1))
        if cache["failures"] == 1:
            logger.warning(f"[LiveTV] Could not ask Jellyfin which channels are recording ({failure}); "
                           f"keeping the last answer and retrying at most once a minute")
    else:
        if cache.get("failures"):
            logger.info("[LiveTV] Jellyfin is answering again about which channels are recording")
        cache["failures"], cache["retry_at"] = 0, -1e9
    if sids is not None:
        cache["ok_at"] = cache["at"]     # a lookup Jellyfin actually answered
        cache["sids"] = set(sids)
        cache["answer_issued_at"] = issued if issued is not None else now
        reserved_keys = {r["stream_key"] for r in _reserved_channels.values() if r.get("stream_key")}
        _stream_slots.sync_recordings(cache["sids"] | reserved_keys, issued_at=cache["answer_issued_at"])


# One upstream pull per channel, fanned out to every client watching it.
#
# Jellyfin opens a separate tuner stream per recording and per viewer, so
# recording a channel while watching that same channel used to pull
# byte-identical data from the provider twice: double the bandwidth, double the
# playlist and segment requests, and double the footprint in whatever
# connection accounting the provider keeps -- the accounting that answers 509,
# which is what truncates recordings. Identical requests are now served from a
# single upstream pull.
#
# Only the SAME channel shares; different channels each get their own upstream
# connection. How many of those an account will carry is not something the
# advertised max_connections settles (#87) -- the ceiling is
# livetv_max_concurrent_streams, above.
_shared_streams: "dict[int, _SharedUpstream] = {}"
_shared_streams = {}
_shared_lock: "asyncio.Lock | None" = None
# Channels whose shared upstream is being opened right now: channel_id -> Future
# resolved with the _SharedUpstream (or None when nothing shareable came of it).
_pending_opens: "dict[int, asyncio.Future]" = {}
# Longest an opener can take: the slot wait, the open budget with its backoff,
# and the redirect chain. A follower waits this long before opening its own.
_PENDING_OPEN_WAIT = 45.0

# Slack before a client is considered too slow. A consumer further behind than
# this is broken, and must not be allowed to stall the upstream or its peers.
# Counted in pieces AND bytes, and a client loses its oldest piece only when it
# is over BOTH: an HLS piece is a whole segment (usually 6s, so 32 of them is
# minutes), but the raw TS path sends ~128 KB pieces, and 32 of those is only a
# few seconds of an HD channel -- less than the backlog the provider hands over
# after any pause (a stalled event loop, a frozen container, the replay a panel
# sends on every re-dial). Dropping it then cut that much out of a recording.
_SUBSCRIBER_QUEUE_MAX = 32
_SUBSCRIBER_QUEUE_BYTES = 64 * 1024 * 1024


class _ClientQueue(asyncio.Queue):
    """An unbounded queue that knows how many bytes it holds; the slack above
    is enforced by _SharedUpstream._publish."""

    def _init(self, maxsize):
        super()._init(maxsize)
        self.nbytes = 0

    def _put(self, item):
        super()._put(item)
        self.nbytes += len(item) if item else 0

    def _get(self):
        item = super()._get()
        self.nbytes -= len(item) if item else 0
        return item

# How soon a pump that outlived its cancel() is cancelled again (see _cancel_pump).
_PUMP_RECANCEL_SECONDS = 1.0

# What each running upstream is doing right now, by channel id:
#   {"state": "streaming" | "reconnecting", "since": <loop time the state began>,
#    "opened_at": <loop time>, "last_error": str | None}
# A stream that is waiting out a provider refusal writes nothing to its
# subscribers for a while, so from the outside -- a DVR front end watching the
# recording's file size -- it is indistinguishable from a dead one. That is
# how a watchdog ends up cancelling a recording that would have recovered on
# its own. GET /api/live/streams tells the two apart.
_stream_status: "dict[int, dict]" = {}


def _status_open(channel_id: int, health: "dict | None" = None) -> dict:
    """A new upstream for this channel: a fresh entry, returned so that the
    stream which made it clears only its own (a channel closed and reopened
    within the same second must not lose the new entry to the old finally)."""
    now = asyncio.get_running_loop().time()
    entry = {"state": "streaming", "since": now, "opened_at": now, "last_error": None,
             "health": health if health is not None else _new_health()}
    _stream_status[channel_id] = entry
    return entry


# What went wrong during a stream's life (#137). Counted where the stream
# code already knows it -- a re-dial, a wait on a failing provider, a segment
# given up on -- so the figures are facts, not inferences. A recording that
# lost content looked exactly like a good one until it was played; now its
# end is logged with these numbers, and an Activity line says so.
_RECENT_STREAMS_MAX = 50
_recent_streams: "list[dict]" = []


def _new_health() -> dict:
    return {"reconnects": 0,              # outages recovered from: a raw TS connection
                                          # re-established, or an HLS run of failed
                                          # requests that ended in a success
            "reconnecting_seconds": 0.0,  # time spent with the provider failing
            "segments_skipped": 0,        # HLS: segments that never arrived
            "errors": 0,                  # failed requests that were retried
            "replays_joined": 0,          # raw TS: reconnects joined where the stream stopped (#368)
            "replay_bytes_skipped": 0}    # what the provider sent again on those


def _stream_ended(channel_id: int, entry: dict, recording: bool, hls: bool = False) -> None:
    """Log (and keep, for /api/live/streams) how a finished stream went.
    Synchronous and cheap: it runs from a generator's finally."""
    from datetime import timezone
    h = entry.get("health") or _new_health()
    started = h.pop("_failing_since", None)
    if started is not None:
        # Ended while still failing: the outage that never recovered counts
        # too, or a recording refused until its timer closed it reads "fine".
        # Under a second (one failed request as the client left) is not one.
        try:
            open_outage = asyncio.get_running_loop().time() - started
        except RuntimeError:
            open_outage = 0.0
        h["reconnecting_seconds"] += open_outage
        if open_outage >= 1.0:
            h["ended_on_error"] = True
    try:
        seconds = asyncio.get_running_loop().time() - entry["opened_at"]
    except RuntimeError:
        seconds = 0.0
    summary = {"channel_id": channel_id,
               "ended_at": datetime.now(timezone.utc).isoformat(),
               "seconds": round(seconds, 1), "recording": bool(recording),
               "reconnects": h["reconnects"],
               "reconnecting_seconds": round(h["reconnecting_seconds"], 1),
               "segments_skipped": h["segments_skipped"], "errors": h["errors"],
               "ended_on_error": bool(h.get("ended_on_error")),
               "revives": h.get("revives", 0),
               "replays_joined": h.get("replays_joined", 0),
               "replay_bytes_skipped": h.get("replay_bytes_skipped", 0)}
    _recent_streams.append(summary)
    del _recent_streams[:-_RECENT_STREAMS_MAX]
    # A raw TS reconnect is a gap unless it was joined where the stream
    # stopped (#368): then the provider's buffer covered the wait. An HLS
    # "interruption" can be one segment retried in place within the playlist
    # window -- nothing lost -- so for HLS only a skipped segment or a second
    # or more of waiting counts.
    joined = h.get("replays_joined", 0)
    uncovered = h["reconnecting_seconds"] - h.get("_covered_seconds", 0.0)
    damaged = ((h["reconnects"] > joined and not hls) or h["segments_skipped"]
               or uncovered >= 1.0 or h.get("ended_on_error"))
    what = "recording" if recording else "stream"
    text = (f"{h['reconnects']} interruption(s) recovered, {h['reconnecting_seconds']:.0f}s waiting "
            f"on the provider, {h['segments_skipped']} segment(s) skipped, "
            f"{h['errors']} failed request(s)")
    if joined:
        text += (f", {joined} rejoined where it stopped "
                 f"({h.get('replay_bytes_skipped', 0) / 1e6:.0f} MB sent again by the provider skipped)")
    if h.get("revives"):
        text += f", {h['revives']} fresh resolve(s) of a refused session"
    if h.get("ended_on_error"):
        failing = (f" ({h['reconnecting_seconds']:.0f}s)" if h["reconnecting_seconds"] >= 1.0 else "")
        text = f"ended while the provider was still failing{failing} — " + text
    if not damaged:
        if h["reconnects"] or h["errors"]:
            logger.info(f"[LiveTV] Channel {channel_id}: {what} ran {seconds:.0f}s — {text} "
                        f"(retried in time, nothing lost)")
        else:
            logger.info(f"[LiveTV] Channel {channel_id}: {what} ran {seconds:.0f}s with no upstream trouble")
        return
    logger.warning(f"[LiveTV] Channel {channel_id}: {what} ran {seconds:.0f}s — {text}")
    if not recording:
        return

    def _write():
        db = None
        try:
            db = SessionLocal()
            ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
            label = f"'{ch.name}'" if ch else f"channel {channel_id}"
            log_activity(db, "livetv_recording_damaged",
                         f"Live TV: a recording of {label} may be missing content — {text}",
                         detail=summary)
        except Exception as e:
            logger.debug(f"[LiveTV] Could not record the stream summary in Activity: {e}")
        finally:
            if db is not None:
                db.close()
    try:
        asyncio.get_running_loop().run_in_executor(None, _write)
    except RuntimeError:
        pass


def _status_set(channel_id: int, state: str, last_error: "str | None" = None):
    now = asyncio.get_running_loop().time()
    cur = _stream_status.get(channel_id)
    if cur is None:
        _stream_status[channel_id] = {"state": state, "since": now, "opened_at": now,
                                      "last_error": last_error,
                                      "last_ok": now if state == "streaming" else None}
        return
    if cur["state"] != state:
        cur["state"] = state
        cur["since"] = now
    if state == "streaming":
        cur["last_ok"] = now          # when it last delivered (#184's rival test)
        cur.pop("waiting_for", None)
    if last_error is not None:
        cur["last_error"] = last_error


def _account_key(provider) -> "tuple | None":
    """What the provider counts connections against: its server host and the
    username. Two provider rows for one account (an M3U and an Xtream entry,
    a copy with other groups) are one account."""
    if provider is None:
        return None
    from urllib.parse import urlparse
    try:
        host = (urlparse((provider.server_url or "").strip()).hostname or "").lower()
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    user = (provider.username or "").strip()
    return (host, user) if host else None


def _same_account(a: "_Lease", b: "_Lease") -> bool:
    if a.account is not None and b.account is not None:
        return a.account == b.account
    return a.provider_id is not None and a.provider_id == b.provider_id


def _rival_delivering(lease: "_Lease | None") -> "_Lease | None":
    """#184: another pull on the same provider account, of equal or higher
    priority (a recording for a recording; a recording or a viewer for a
    viewer), that delivered within _RIVAL_FRESH s -- the stream that holds the
    account now. None when there is none (or the account is unknown)."""
    if lease is None or (lease.provider_id is None and lease.account is None):
        return None
    now = asyncio.get_running_loop().time()
    for other in list(_stream_slots.leases.values()):
        if (other is lease or other.preempted or not _same_account(other, lease)
                or other.channel_id is None or other.channel_id == lease.channel_id):
            continue
        prio = other.priority
        if other.kind == "live" and _recording_cache.get("ok_at", -1e9) < other.started + _CLASSIFY_GRACE:
            # Jellyfin has not answered a lookup made well after this pull
            # opened (it marks a timer InProgress only AFTER the tuner stream
            # is open, and a lookup can fail or lag): it may be a recording
            # not promoted yet. Never take the account from it on a guess
            # (fuzz seeds 34, 7007, 7095).
            prio = _LEASE_PRIORITY["recording"]
        if prio > lease.priority:
            continue
        st = _stream_status.get(other.channel_id)
        if st and st.get("reviving_at") is not None and now - st["reviving_at"] <= _RIVAL_FRESH:
            # It is re-resolving right now: it is about to hold the account.
            # Two streams waiting behind the same outsider must not both take
            # it back at once (fuzz seed 234).
            return other
        last = st.get("last_ok") if st else None
        if last is None:
            # Still opening: it has just been handed a session -- on a
            # "newest wins" account it holds the account now (fuzz seeds
            # 110, 197).
            if now - other.started <= _RIVAL_FRESH:
                return other
            continue
        if now - last <= _RIVAL_FRESH:
            return other
        # Still "streaming" (not "reconnecting"): a slow segment, not a failure.
        if st.get("state") == "streaming" and now - last <= _RIVAL_STREAMING_FRESH:
            return other
    return None


def _status_clear(channel_id: int, entry: "dict | None" = None):
    if entry is None or _stream_status.get(channel_id) is entry:
        _stream_status.pop(channel_id, None)


def _get_shared_lock() -> "asyncio.Lock":
    global _shared_lock
    if _shared_lock is None:
        _shared_lock = asyncio.Lock()
    return _shared_lock


class _SharedUpstream:
    """A single upstream stream for one channel, with N subscribers."""

    def __init__(self, channel_id: int, release_sem, close_upstream=None):
        self.channel_id = channel_id
        self.subscribers: set = set()
        self.task = None
        self._release_sem = release_sem
        self._closed = False
        # How to give the provider connection back if the pump never runs:
        # a task cancelled before its first turn executes nothing, not even
        # its `finally`, so what the pump would have closed must be closed
        # here instead (the response's close_upstream, see _stream_proxy_inner).
        self._close_upstream = close_upstream
        self._pump_started = False

    def subscribe(self) -> "asyncio.Queue":
        q = _ClientQueue()
        self.subscribers.add(q)
        return q

    def _publish(self, item):
        for q in list(self.subscribers):
            q.put_nowait(item)
            # Past its slack, drop this client's oldest piece rather than
            # stalling the upstream (and therefore every other client on the
            # channel). Never the end-of-stream marker, which is the newest.
            while q.qsize() > _SUBSCRIBER_QUEUE_MAX and q.nbytes > _SUBSCRIBER_QUEUE_BYTES:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                logger.warning(
                    f"[LiveTV] Client on channel {self.channel_id} is behind — "
                    f"dropped a segment for it")

    async def _pump(self, body_iterator):
        self._pump_started = True
        try:
            async for piece in body_iterator:
                if self._closed:
                    # Retired, but the cancel() that should have stopped us was
                    # lost (see _cancel_pump): stop pulling here.
                    break
                self._publish(piece)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[LiveTV] Shared upstream for channel {self.channel_id} failed: {e}")
        finally:
            self._publish(None)   # EOF sentinel for every subscriber
            await self._retire()
            # Leaving the loop by `break` does not close an async generator,
            # and its `finally` is what closes the provider connection.
            aclose = getattr(body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()

    def _cancel_pump(self):
        """Cancel the pump, and keep at it until it is really gone.

        One task.cancel() is not enough. httpx connects through anyio, whose
        connect_tcp() cancels its own cancel scope as soon as a connection
        attempt wins, and a scope that is being cancelled takes any
        CancelledError raised in its host task for its own and swallows it. A
        cancel() of ours that lands in that window is lost, and the pump goes
        on pulling from the provider with no subscriber, no registration and
        no slot -- invisible to /api/live/capacity. The window is the connect
        for the first chunk: exactly where the pump is when a client opens a
        channel and leaves at once."""
        task = self.task
        if task is None or task.done():
            return
        task.cancel()
        asyncio.get_running_loop().call_later(_PUMP_RECANCEL_SECONDS, self._cancel_pump)

    async def _retire(self):
        if self._closed:
            return
        self._closed = True
        # Deregister and free the slot BEFORE any await: this runs from a
        # finally during cancellation, where an await can raise CancelledError
        # and would otherwise strand the registration and leak the slot.
        if _shared_streams.get(self.channel_id) is self:
            del _shared_streams[self.channel_id]
            _status_clear(self.channel_id)
        self._release_sem()
        logger.info(f"[LiveTV] Shared upstream for channel {self.channel_id} ended")
        if not self._pump_started and self._close_upstream is not None:
            # Retired before the pump ever ran (a recording took the slot in
            # the moment between open and first turn): close what it would have.
            try:
                await self._close_upstream()
            except Exception as e:
                logger.warning(f"[LiveTV] Closing the unstarted upstream for channel "
                               f"{self.channel_id} failed: {e}")

    async def unsubscribe(self, q):
        self.subscribers.discard(q)
        if not self.subscribers and not self._closed:
            # Nobody left watching: stop paying the provider for it.
            logger.info(f"[LiveTV] Last client left channel {self.channel_id} — "
                        f"closing the upstream")
            await self._retire()
            self._cancel_pump()

    async def preempt(self):
        """A more important pull (a recording) took this upstream's slot:
        end every client's stream cleanly and stop pulling."""
        if self._closed:
            return
        logger.warning(f"[LiveTV] Channel {self.channel_id}: stream stopped for "
                       f"{len(self.subscribers)} client(s) — a recording needed its connection slot")
        self._publish(None)
        await self._retire()
        self._cancel_pump()


class _SubscriberResponse(StreamingResponse):
    """A client's view of a shared upstream, which is ALWAYS unsubscribed.

    The client is subscribed when this object is built, but the body generator
    only unsubscribes from its own `finally` -- and an async generator that is
    never started never runs it. A client that went away between the headers
    and the first chunk therefore stayed subscribed for ever: the upstream kept
    pulling from the provider with nobody watching and its concurrency slot
    never came back. Unsubscribing when the response finishes, however it
    finishes, closes that; `unsubscribe` is idempotent."""

    def __init__(self, shared: "_SharedUpstream", q):
        super().__init__(
            _subscriber_body(shared, q),
            media_type="video/mp2t",
            headers={
                "Connection": "close",
                "Cache-Control": "no-cache, no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )
        self._shared = shared
        self._q = q

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._shared.unsubscribe(self._q)


async def _subscriber_body(shared: "_SharedUpstream", q):
    """Per-client view of a shared upstream."""
    try:
        while True:
            piece = await q.get()
            if piece is None:
                return
            yield piece
    finally:
        await shared.unsubscribe(q)


# ─── Background sync tracking ────────────────────────────────────────────────

_sync_status: dict[int, dict] = {}  # provider_id → {phase, progress, message, ...}
_sync_status_lock = threading.Lock()


def _set_sync_status(provider_id: int, status: dict):
    """Thread-safe update of sync status for a provider."""
    # GET /api/live/sync-status hands this straight to the dashboard, and an
    # error's text is whatever the HTTP library put in it -- for an Xtream or
    # M3U provider that is the full URL, username, password and token included.
    if isinstance(status.get("message"), str):
        from services.log_redaction import redact
        status = {**status, "message": redact(status["message"])}
    with _sync_status_lock:
        _sync_status[provider_id] = status


def _get_sync_status(provider_id: int, default: dict | None = None) -> dict:
    """Thread-safe read of sync status for a provider."""
    with _sync_status_lock:
        return _sync_status.get(provider_id, default or {"phase": "idle", "progress": 0, "message": "No sync running"}).copy()


# ─── Pydantic models ────────────────────────────────────────────────────────


CUSTOM_NAME_MAX = 64
EPG_ID_OVERRIDE_MAX = 200


class ChannelUpdate(BaseModel):
    enabled: Optional[bool] = None
    # "" clears it and puts the provider's name back.
    custom_name: Optional[str] = None
    # The XMLTV channel id to take the guide from; "" = match automatically.
    epg_id_override: Optional[str] = None
    channel_number: Optional[int] = None
    epg_channel_id: Optional[str] = None
    sort_order: Optional[int] = None


class BulkChannelUpdate(BaseModel):
    channel_ids: list[int]
    enabled: bool


class BulkChannelFilter(BaseModel):
    provider_id: int
    enabled: bool
    group: Optional[str] = None
    search: Optional[str] = None
    has_epg: Optional[bool] = None


class GroupUpdate(BaseModel):
    enabled: bool

class BulkGroupUpdate(BaseModel):
    group_ids: List[int]
    enabled: bool


class LiveProviderConfig(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None  # xtream, m3u_url, m3u_file
    server_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    m3u_url: Optional[str] = None
    epg_url: Optional[str] = None
    user_agent: Optional[str] = None
    live_tv_enabled: Optional[bool] = None


# ─── Provider config ────────────────────────────────────────────────────────


@router.get("/api/live/provider", dependencies=_admin)
def get_live_provider(db: Session = Depends(get_db)):
    """Get the live TV provider config."""
    provider = db.query(Provider).filter(Provider.live_tv_enabled == True).first()
    if not provider:
        # Fall back to any provider with detected live capability
        provider = db.query(Provider).filter(Provider.has_live == True).first()
    if not provider:
        return {"provider": None}

    return {
        "provider": {
            "id": provider.id,
            "name": provider.name,
            "provider_type": provider.provider_type or "xtream",
            "server_url": provider.server_url,
            "username": provider.username,
            "password": "••••••••" if provider.password else "",
            "m3u_url": provider.m3u_url or "",
            "epg_url": provider.epg_url or "",
            "user_agent": provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
            "live_tv_enabled": provider.live_tv_enabled,
            "last_live_sync": provider.last_live_sync.isoformat() if provider.last_live_sync else None,
        }
    }


def live_tv_providers(db: Session) -> list:
    """Every provider that serves Live TV.

    Selected on `live_tv_enabled` alone. `active` is the VOD sync switch, and
    save_live_provider() creates its provider with active=False because a Live
    TV provider must never get a VOD sync, so a filter on `active` matched no
    provider set up the normal way: the nightly guide refresh never ran (#174).
    """
    return db.query(Provider).filter(Provider.live_tv_enabled == True).all()  # noqa: E712


@router.post("/api/live/provider", dependencies=_admin)
def save_live_provider(body: LiveProviderConfig, db: Session = Depends(get_db)):
    """Create or update the live TV provider."""
    # Clean up any duplicate providers with masked passwords (from earlier bug)
    dupes = db.query(Provider).filter(
        Provider.live_tv_enabled == True,
        Provider.password == "••••••••",
    ).all()
    for d in dupes:
        db.delete(d)
    if dupes:
        db.flush()

    # Find existing live TV provider only — never reuse VOD providers
    provider = db.query(Provider).filter(Provider.live_tv_enabled == True).first()

    if not provider:
        # Create a dedicated live TV provider
        pwd = body.password if body.password and body.password != "••••••••" else ""
        provider = Provider(
            name=body.name or "Live TV",
            provider_type=body.provider_type or "xtream",
            server_url=body.server_url or "",
            username=body.username or "",
            password=pwd,
            m3u_url=body.m3u_url,
            epg_url=body.epg_url,
            user_agent=body.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
            live_tv_enabled=True,
            active=False,
        )
        db.add(provider)
    else:
        before = _xtream_login(provider)
        if body.name is not None:
            provider.name = body.name
        if body.provider_type is not None:
            provider.provider_type = body.provider_type
        if body.server_url is not None:
            provider.server_url = body.server_url
        if body.username is not None:
            provider.username = body.username
        if body.password is not None and body.password != "••••••••":
            provider.password = body.password
        if body.m3u_url is not None:
            provider.m3u_url = body.m3u_url
        if body.epg_url is not None:
            provider.epg_url = body.epg_url
        if body.user_agent is not None:
            provider.user_agent = body.user_agent
        if body.live_tv_enabled is not None:
            provider.live_tv_enabled = body.live_tv_enabled
        rewrite_xtream_channel_urls(provider, before, db)

    db.commit()
    db.refresh(provider)

    log_activity(db, "livetv_config", f"Live TV provider updated: {provider.name}")
    return {"success": True, "provider_id": provider.id}


@router.post("/api/live/provider/test", dependencies=_admin)
def test_live_provider(db: Session = Depends(get_db)):
    """Test connection to the live TV provider."""
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "Testing the provider")
    provider = db.query(Provider).filter(Provider.live_tv_enabled == True).first()
    if not provider:
        provider = db.query(Provider).first()
    if not provider:
        return {"success": False, "message": "No live TV provider configured"}

    provider_type = provider.provider_type or "xtream"

    if provider_type == "xtream":
        try:
            from services.xtream_client import XtreamClient
            client = XtreamClient(
                server=provider.server_url,
                username=provider.username,
                password=provider.password,
                user_agent=provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
            )
            info = client.authenticate()
            client.close()
            _remember_output_formats(db, provider.id, info)
            db.commit()
            return {
                "success": True,
                "message": "Connected",
                "info": {
                    "status": info.get("user_info", {}).get("status", "unknown"),
                    "exp_date": info.get("user_info", {}).get("exp_date"),
                    "max_connections": info.get("user_info", {}).get("max_connections"),
                    "active_connections": info.get("user_info", {}).get("active_cons"),
                    "allowed_output_formats": info.get("user_info", {}).get("allowed_output_formats"),
                    "live_stream_format": _live_stream_format(db),
                    "live_extension": _live_extension(db, provider.id),
                },
            }
        except Exception as e:
            from services.log_redaction import redact
            return {"success": False, "message": redact(str(e))}

    elif provider_type == "m3u_url":
        import requests
        try:
            resp = requests.head(
                provider.m3u_url or provider.server_url,
                headers={"User-Agent": provider.user_agent or "TiviMate/4.7.0"},
                timeout=10,
            )
            return {"success": resp.status_code == 200, "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            from services.log_redaction import redact
            return {"success": False, "message": redact(str(e))}

    return {"success": False, "message": f"Unknown provider type: {provider_type}"}


# ─── Channel sync ───────────────────────────────────────────────────────────


@router.post("/api/live/sync/{provider_id}", dependencies=_admin)
def sync_live_groups(provider_id: int, db: Session = Depends(get_db)):
    """Phase 1: Fetch categories/groups only (fast). No channels downloaded."""
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "A channel group sync")
    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")

    status = _get_sync_status(provider_id)
    if status.get("phase") == "running":
        return {"success": False, "message": "Sync already in progress", **status}

    provider_type = provider.provider_type or "xtream"
    provider_data = _snapshot_provider(provider, provider_type)

    _set_sync_status(provider_id, {"phase": "starting", "progress": 0, "message": "Fetching groups..."})

    thread = threading.Thread(target=_run_group_sync_background, args=(provider_data,), daemon=True)
    thread.start()

    return {"success": True, "message": "Group sync started", "status_url": "/api/live/sync-status"}


@router.post("/api/live/sync-channels/{provider_id}", dependencies=_admin)
def sync_live_channels(provider_id: int, db: Session = Depends(get_db)):
    """Phase 2: Fetch channels only for enabled groups. Call after enabling groups."""
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "A channel sync")
    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")

    status = _get_sync_status(provider_id)
    if status.get("phase") == "running":
        return {"success": False, "message": "Sync already in progress", **status}

    # Check that there are enabled groups
    enabled_groups = db.query(LiveChannelGroup).filter(
        LiveChannelGroup.provider_id == provider_id,
        LiveChannelGroup.enabled == True,
    ).all()
    if not enabled_groups:
        return {"success": False, "message": "No groups enabled. Enable groups first, then sync channels."}

    provider_type = provider.provider_type or "xtream"
    provider_data = _snapshot_provider(provider, provider_type)

    _set_sync_status(provider_id, {"phase": "starting", "progress": 0, "message": "Starting channel sync..."})

    thread = threading.Thread(target=_run_channel_sync_background, args=(provider_data,), daemon=True)
    thread.start()

    return {"success": True, "message": "Channel sync started", "status_url": "/api/live/sync-status"}


@router.get("/api/live/capacity", dependencies=_admin)
def live_capacity(db: Session = Depends(get_db)):
    """How many upstream pulls are running against the ceiling, and whether any
    stream has been refused since start -- the only trace a lost recording
    otherwise leaves is a log line."""
    return {
        "limit": _max_concurrent_streams(db),
        "active": _stream_slots.active,
        "refused_since_start": _stream_slots.refused,
        "last_refused": _stream_slots.last_refused,
        "reconnect_budget_seconds": _reconnect_budget(db),
        "streams": _stream_snapshot(db),
        **_protection_snapshot(db),
    }


def _stream_snapshot(db) -> list:
    """One entry per running upstream: what it is doing and for how long."""
    import time
    now = time.monotonic()
    out = []
    for channel_id, st in sorted(list(_stream_status.items())):
        ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
        shared = _shared_streams.get(channel_id)
        lease = _stream_slots.lease_for(f"channel:{channel_id}")
        out.append({
            "channel_id": channel_id,
            "channel": ch.name if ch else None,
            "client": None,
            # the GuideNumber Jellyfin knows this channel by (hdhr_<stream_id>)
            "stream_id": (ch.stream_id or str(ch.id)) if ch else None,
            "kind": lease.kind if lease else None,
            "state": st["state"],
            "for_seconds": round(max(0.0, now - st["since"]), 1),
            "open_seconds": round(max(0.0, now - st["opened_at"]), 1),
            "last_error": st.get("last_error"),
            # #184: silent on purpose -- a rival on this account is delivering
            "waiting_for": st.get("waiting_for"),
            "subscribers": len(shared.subscribers) if shared is not None else None,
            # reconnects / seconds waiting on the provider / segments skipped (#137)
            "health": {k: v for k, v in (st.get("health") or {}).items() if not k.startswith("_")},
        })
    # Provider VOD played through Tentacle holds a slot too (routers.vod).
    try:
        from routers import vod as _vod
        for pb in list(_vod._playbacks.values()):
            out.append({
                "channel_id": None,
                "channel": pb.owner,
                "client": pb.client,
                "stream_id": None,
                "kind": "vod",
                "state": "stopped" if pb.stopped.is_set() else ("streaming" if pb.active_bodies else "idle"),
                "for_seconds": round(max(0.0, now - pb.last_used), 1),
                "open_seconds": round(max(0.0, now - pb.started), 1),
                "last_error": None,
                "subscribers": pb.active_bodies,
            })
    except Exception as e:   # the live list must never fail because of VOD bookkeeping
        logger.debug(f"[LiveTV] VOD snapshot failed: {e}")
    return out


@router.get("/api/live/streams", dependencies=[Depends(require_internal_or_admin)])
def live_streams(db: Session = Depends(get_db)):
    """Every running upstream and whether it is streaming or waiting out a
    provider failure. A stream that is reconnecting writes nothing for a
    while, so a DVR front end judging the recording by its file size would
    take it for dead and cancel it -- which is what makes a second file. Ask
    here first: "reconnecting" means leave it alone; a channel that is not
    listed at all has no upstream any more. Reachable with the internal
    secret so a server-side scheduler can call it without a user session."""
    return {
        "streams": _stream_snapshot(db),
        "limit": _max_concurrent_streams(db),
        "active": _stream_slots.active,
        "preempted_since_start": _stream_slots.preempted_since_start,
        "reconnect_budget_seconds": _reconnect_budget(db),
        "reserved_channel_ids": sorted(list(_reserved_channels)),
        # Channels the provider answered with a placeholder (black.ts) instead
        # of the channel since Tentacle started, and how often (#140).
        "placeholders": [{"channel_id": cid, **info} for cid, info in sorted(_placeholders.items())],
        **_protection_snapshot(db),
        # the last streams that ended, and how they went (#137)
        "recent": list(_recent_streams),
    }


def _protection_snapshot(db) -> dict:
    """Recording protection, for /api/live/streams and /api/live/capacity."""
    return {
        "protect_recordings": _protect_recordings(db),
        "recording_active": _stream_slots.recording_active(),
        "protected_refusals": _stream_slots.protected_refusals,
        "protected_preemptions": _stream_slots.protected_preemptions,
        "last_protected_refusal": _stream_slots.last_protected_refusal,
    }


@router.get("/api/live/epg-coverage", dependencies=_admin)
def epg_coverage(provider_id: Optional[int] = None, db: Session = Depends(get_db)):
    """What the last EPG sync matched: how many channels have a guide, by which
    rule, and which do not and why (no tvg-id, a tvg-id the feed lacks, an
    ambiguous name). One report per Live TV provider (#141)."""
    import json
    out = []
    for p in live_tv_providers(db):
        if provider_id and p.id != provider_id:
            continue
        raw = get_setting(db, f"livetv_epg_coverage_{p.id}", "")
        try:
            report = json.loads(raw) if raw else None
        except ValueError:
            report = None
        out.append({"provider_id": p.id, "provider": p.name, "report": report})
    return {"providers": out}


class ReserveRequest(BaseModel):
    channel_id: "int | None" = None
    stream_id: "str | None" = None     # the GuideNumber Jellyfin uses (hdhr_<stream_id>)
    seconds: int = 1800


@router.post("/api/live/reserve", dependencies=[Depends(require_internal_or_admin)])
async def live_reserve(body: ReserveRequest, db: Session = Depends(get_db)):
    """Mark a channel as about to be recorded, so the pull that opens it is
    treated as a recording (and outranks viewers at capacity) even before
    Jellyfin's timer shows as InProgress -- the pre-padding window. For a
    DVR front end, which may name the channel by Tentacle's id or by the
    GuideNumber from Jellyfin's timer; the reservation lapses on its own."""
    q = db.query(LiveChannel)
    if body.channel_id is not None:
        ch = q.filter(LiveChannel.id == body.channel_id).first()
    elif body.stream_id:
        sid = str(body.stream_id).strip()
        if sid.startswith("hdhr_"):
            sid = sid[len("hdhr_"):]
        ch = q.filter(LiveChannel.stream_id == sid).first()
    else:
        raise HTTPException(422, "channel_id or stream_id is required")
    if not ch:
        raise HTTPException(404, "Channel not found")
    seconds = max(1, min(int(body.seconds), 24 * 3600))
    _reserved_channels[ch.id] = {"until": asyncio.get_running_loop().time() + seconds,
                                 "stream_key": ch.stream_id or str(ch.id)}
    return {"channel_id": ch.id, "channel": ch.name, "stream_id": ch.stream_id, "reserved_for_seconds": seconds}


@router.delete("/api/live/reserve/{channel_id}", dependencies=[Depends(require_internal_or_admin)])
async def live_unreserve(channel_id: int):
    _reserved_channels.pop(channel_id, None)
    return {"channel_id": channel_id, "reserved": False}


@router.get("/api/live/sync-status", dependencies=_admin)
def sync_status_endpoint(provider_id: Optional[int] = None):
    """Get sync progress for a provider or all providers."""
    if provider_id is not None:
        return _get_sync_status(provider_id)
    with _sync_status_lock:
        return {k: v.copy() for k, v in _sync_status.items()}


def _snapshot_provider(provider, provider_type: str) -> dict:
    """Snapshot provider ORM object into a plain dict for background threads."""
    return {
        "id": provider.id,
        "name": provider.name,
        "provider_type": provider_type,
        "server_url": provider.server_url,
        "username": provider.username,
        "password": provider.password,
        "user_agent": provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
        "m3u_url": provider.m3u_url,
        "epg_url": provider.epg_url,
    }


def _run_group_sync_background(provider_data: dict):
    """Phase 1: Fetch groups/categories only. Fast."""
    provider_id = provider_data["id"]
    db = SessionLocal()
    try:
        provider_type = provider_data["provider_type"]
        if provider_type == "xtream":
            result = _sync_groups_from_xtream(provider_data, db)
        elif provider_type in ("m3u_url", "m3u_file"):
            # For M3U, groups come from parsing the file — do a full sync since it's the only way
            if provider_type == "m3u_url":
                result = _sync_from_m3u_url(provider_data, db)
            else:
                result = _sync_from_m3u_file(provider_data, db)
        else:
            _set_sync_status(provider_id, {"phase": "error", "progress": 0, "message": f"Unknown provider type: {provider_type}"})
            return

        _set_sync_status(provider_id, {
            "phase": "complete",
            "progress": 100,
            **result,
            "message": _sync_done_message(result),
        })
    except Exception as e:
        logger.error(f"[LiveTV] Group sync failed for provider {provider_id}: {e}", exc_info=True)
        _set_sync_status(provider_id, {"phase": "error", "progress": 0, "message": str(e)})
    finally:
        db.close()


def _run_channel_sync_background(provider_data: dict):
    """Phase 2: Fetch channels for enabled groups only."""
    provider_id = provider_data["id"]
    db = SessionLocal()
    try:
        provider_type = provider_data["provider_type"]
        if provider_type == "xtream":
            result = _sync_channels_from_xtream(provider_data, db)
        elif provider_type in ("m3u_url", "m3u_file"):
            # M3U already synced channels in phase 1 — just report what's there
            total = db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).count()
            enabled = db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id, LiveChannel.enabled == True).count()
            result = {"new": 0, "updated": 0, "total": total, "enabled": enabled, "message": "M3U channels already synced"}
        else:
            _set_sync_status(provider_id, {"phase": "error", "progress": 0, "message": f"Unknown provider type: {provider_type}"})
            return

        # Auto-chain EPG sync after channel sync — so EPG badges are accurate immediately
        all_channels = db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).all()
        if all_channels:
            ch_msg = f"Channels: {result.get('new', 0)} new, {result.get('updated', 0)} updated."
            enabled_count = sum(1 for ch in all_channels if ch.enabled)
            epg_data = {
                **provider_data,
                "channels": [
                    {"stream_id": ch.stream_id, "epg_channel_id": ch.epg_channel_id, "name": ch.name}
                    for ch in all_channels
                ],
                "enabled_count": enabled_count,
            }
            db.close()
            db = None
            _set_sync_status(provider_id, {
                "phase": "running", "progress": 95,
                "message": f"{ch_msg} Syncing EPG guide data...",
            })
            _run_epg_sync_background(epg_data)
        else:
            _set_sync_status(provider_id, {
                "phase": "complete",
                "progress": 100,
                "message": f"Done: {result.get('new', 0)} new, {result.get('updated', 0)} updated, {result.get('total', 0)} total",
                **result,
            })

    except Exception as e:
        logger.error(f"[LiveTV] Channel sync failed for provider {provider_id}: {e}", exc_info=True)
        _set_sync_status(provider_id, {"phase": "error", "progress": 0, "message": str(e)})
    finally:
        if db is not None:
            db.close()


def _sync_groups_from_xtream(provider_data: dict, db: Session) -> dict:
    """Phase 1: Fetch categories/groups from Xtream only. No channels."""
    from services.xtream_client import XtreamClient

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]

    client = XtreamClient(
        server=provider_data["server_url"],
        username=provider_data["username"],
        password=provider_data["password"],
        user_agent=provider_data["user_agent"],
    )

    try:
        _set_sync_status(provider_id, {"phase": "running", "progress": 20, "message": "Fetching categories..."})
        categories = client.get_live_categories()
        logger.info(f"[LiveTV] {provider_name}: {len(categories)} categories fetched")

        # Count channels per category (single API call). This is the ONE
        # unbatched get_live_streams() call in the codebase and the client's
        # own docstring warns it "frequently times out"; when it fails we must
        # keep the stored counts rather than reporting every group as empty.
        _set_sync_status(provider_id, {"phase": "running", "progress": 50, "message": "Counting channels..."})
        channel_counts = {}
        counts_ok = False
        try:
            all_streams = client.get_live_streams()
            if isinstance(all_streams, list):
                for s in all_streams:
                    cid = str(s.get("category_id", ""))
                    channel_counts[cid] = channel_counts.get(cid, 0) + 1
                counts_ok = True
        except Exception as e:
            logger.warning(f"[LiveTV] Failed to count channels for {provider_name}: {e}")
        if not counts_ok:
            logger.warning(
                f"[LiveTV] {provider_name}: channel counts unavailable this run — "
                f"keeping the stored per-group counts"
            )

        _set_sync_status(provider_id, {"phase": "running", "progress": 70, "message": f"Saving {len(categories)} groups..."})
        _sync_groups(provider_id, categories, db, channel_counts if counts_ok else None)
        db.commit()

        total_channels = sum(channel_counts.values())
        count_note = "" if counts_ok else " (channel counts unavailable — kept previous)"
        log_activity(db, "livetv_sync", f"Live TV group sync for {provider_name}: {len(categories)} groups, {total_channels} channels{count_note}")
        result = {"groups": len(categories), "message": f"{len(categories)} groups synced. Enable the groups you want, then sync channels."}
        # This function is also called directly by the nightly discovery step,
        # which has no _run_group_sync_background wrapper to write a terminal
        # status. Leaving phase="running" makes every later sync request answer
        # "Sync already in progress" for the lifetime of the process.
        _set_sync_status(provider_id, {"phase": "complete", "progress": 100, **result})
        return result

    except Exception as e:
        logger.error(f"[LiveTV] Group sync failed for {provider_name}: {e}", exc_info=True)
        _set_sync_status(provider_id, {"phase": "error", "progress": 0, "message": str(e)})
        raise
    finally:
        client.close()


def _sync_channels_from_xtream(provider_data: dict, db: Session) -> dict:
    """Phase 2: Fetch channels only for enabled groups from Xtream."""
    from services.xtream_client import XtreamClient

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]

    client = XtreamClient(
        server=provider_data["server_url"],
        username=provider_data["username"],
        password=provider_data["password"],
        user_agent=provider_data["user_agent"],
    )

    try:
        # Get enabled groups and their category IDs
        enabled_groups = db.query(LiveChannelGroup).filter(
            LiveChannelGroup.provider_id == provider_id,
            LiveChannelGroup.enabled == True,
        ).all()

        category_ids = [g.category_id for g in enabled_groups if g.category_id]
        cat_map = {g.category_id: g.name for g in enabled_groups if g.category_id}

        logger.info(f"[LiveTV] {provider_name}: fetching channels for {len(category_ids)} enabled groups")
        _set_sync_status(provider_id, {
            "phase": "running", "progress": 5,
            "message": f"Fetching channels for {len(category_ids)} groups...",
        })

        # Fetch per-category (only enabled ones)
        all_streams = []
        for i, cat_id in enumerate(category_ids):
            try:
                streams = client.get_live_streams(category_id=cat_id)
                all_streams.extend(streams)
            except Exception as e:
                logger.warning(f"[LiveTV] {provider_name}: failed to fetch category {cat_id}: {e}")

            pct = 5 + int(((i + 1) / len(category_ids)) * 85)
            _set_sync_status(provider_id, {
                "phase": "running", "progress": pct,
                "message": f"Fetching: {i + 1}/{len(category_ids)} groups, {len(all_streams)} channels so far",
            })
            if (i + 1) % 20 == 0 or (i + 1) == len(category_ids):
                logger.info(f"[LiveTV] {provider_name}: {i + 1}/{len(category_ids)} groups, {len(all_streams)} channels")

        logger.info(f"[LiveTV] {provider_name}: {len(all_streams)} channels fetched for enabled groups")
        _set_sync_status(provider_id, {"phase": "running", "progress": 95, "message": f"Saving {len(all_streams)} channels..."})

        # Upsert channels
        # What the account can serve, for the "auto" stream format. One cheap
        # API call; not being able to read it is not a sync failure.
        try:
            auth = getattr(client, "authenticate", None)
            if auth is not None:
                _remember_output_formats(db, provider_id, auth() or {})
        except Exception as e:
            logger.debug(f"[LiveTV] {provider_name}: could not read account info: {e}")
        stats = _upsert_channels(provider_id, all_streams, cat_map, client, db,
                                 extension=_live_extension(db, provider_id))

        # Update provider timestamp
        provider = db.query(Provider).filter(Provider.id == provider_id).first()
        if provider:
            provider.last_live_sync = datetime.utcnow()
        db.commit()

        log_activity(db, "livetv_sync", f"Live TV channel sync for {provider_name}: {stats['new']} new, {stats['updated']} updated, {stats['total']} total")
        return stats

    except Exception as e:
        logger.error(f"[LiveTV] Channel sync failed for {provider_name}: {e}", exc_info=True)
        raise
    finally:
        client.close()


def _sync_from_m3u_url(provider_data: dict, db: Session) -> dict:
    """Sync live channels from an M3U URL."""
    from services.m3u_parser import parse_m3u_from_url

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]
    url = provider_data.get("m3u_url") or provider_data.get("server_url")
    if not url:
        raise ValueError("No M3U URL configured")

    _set_sync_status(provider_id, {"phase": "running", "progress": 10, "message": "Downloading M3U..."})

    channels = parse_m3u_from_url(url, user_agent=provider_data["user_agent"])
    logger.info(f"[LiveTV] {provider_name}: parsed {len(channels)} channels from M3U URL")

    _set_sync_status(provider_id, {"phase": "running", "progress": 80, "message": f"Saving {len(channels)} channels..."})
    stats = _upsert_channels_from_m3u(provider_id, channels, db)

    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if provider:
        provider.last_live_sync = datetime.utcnow()
    db.commit()

    _log_m3u_sync(db, f"Live TV M3U sync for {provider_name}", stats)
    return stats


def _sync_from_m3u_file(provider_data: dict, db: Session) -> dict:
    """Sync live channels from a local M3U file."""
    from services.m3u_parser import parse_m3u_from_file

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]
    path = provider_data.get("m3u_url")  # For file type, m3u_url stores the file path
    if not path:
        raise ValueError("No M3U file path configured")

    _set_sync_status(provider_id, {"phase": "running", "progress": 10, "message": "Reading M3U file..."})

    channels = parse_m3u_from_file(path)
    logger.info(f"[LiveTV] {provider_name}: parsed {len(channels)} channels from M3U file")

    _set_sync_status(provider_id, {"phase": "running", "progress": 80, "message": f"Saving {len(channels)} channels..."})
    stats = _upsert_channels_from_m3u(provider_id, channels, db)

    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if provider:
        provider.last_live_sync = datetime.utcnow()
    db.commit()

    _log_m3u_sync(db, f"Live TV file sync for {provider_name}", stats)
    return stats


def _sync_done_message(result: dict) -> str:
    """What the dashboard shows when a sync finishes. A sync that refused to
    delete channels has NOT simply "synced": say so where the admin is looking,
    not only in the activity feed."""
    msg = result.get("message", "Groups synced")
    refused = result.get("removals_refused")
    if refused:
        msg += (f" — but REFUSED to delete {refused} channel(s): the playlist looked "
                f"truncated or empty, so the existing channels were kept")
    return msg


def _log_m3u_sync(db: Session, prefix: str, stats: dict):
    """Activity-feed entry for an M3U sync, including any refused removal."""
    msg = f"{prefix}: {stats['new']} new, {stats['total']} total"
    if stats.get("removals_refused"):
        msg += (f" — REFUSED to delete {stats['removals_refused']} channel(s): "
                f"the playlist looked truncated or empty")
    log_activity(db, "livetv_sync", msg)


def _sync_groups(provider_id: int, categories: list[dict], db: Session, channel_counts: dict = None):
    """Upsert LiveChannelGroup records from Xtream categories.

    channel_counts=None means the provider's per-category channel counts could
    not be fetched this run — keep whatever is stored instead of overwriting
    every group with 0.
    """
    update_counts = channel_counts is not None
    if channel_counts is None:
        channel_counts = {}
    by_name = {
        g.name: g
        for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id).all()
    }

    # Xtream category names aren't unique, but a group is (provider, name)
    # with one category id (#462). Every category gets a group of its own:
    # the one it already has, else its name if no other category has that
    # name (the first listed wins; a group still follows its category to a
    # new id), else "NAME (id)", stable when the provider reorders its list.
    cats = {}
    for cat in categories:
        cats.setdefault(str(cat.get("category_id", "")), cat.get("category_name", ""))

    claimed = set()
    assigned = {}
    for cat_id, name in cats.items():
        for candidate in _group_names(name, cat_id):
            g = by_name.get(candidate)
            if g is None:
                if candidate == name:
                    continue
                break
            if g.category_id == cat_id and g.name not in claimed:
                assigned[cat_id] = g
                claimed.add(g.name)
                break

    for cat_id, name in cats.items():
        if cat_id in assigned:
            continue
        for candidate in _group_names(name, cat_id):
            g = by_name.get(candidate)
            if g is None:
                g = LiveChannelGroup(provider_id=provider_id, name=candidate,
                                     category_id=cat_id, enabled=False)
                db.add(g)
                by_name[candidate] = g
            elif candidate in claimed or (candidate != name and g.category_id != cat_id):
                continue
            g.category_id = cat_id
            assigned[cat_id] = g
            claimed.add(candidate)
            break

    for cat_id, g in assigned.items():
        if update_counts or g.channel_count is None:
            g.channel_count = channel_counts.get(cat_id, 0)
    db.flush()


def _group_names(name: str, cat_id: str):
    """The names a category's group may have, in order of preference."""
    yield name
    yield f"{name} ({cat_id})"
    n = 2
    while True:
        yield f"{name} ({cat_id}) {n}"
        n += 1


# A provider's separator rows: "##### EVENTS #####", "=== SPORTS ===", "-----".
# A row that begins AND ends with a run of two or more of # = * ~ _ | - ★ ▬
# ◉ ● •, or consists only of them, optionally after a two/three-letter
# "XX:" / "XX|" tag ("UK: ##### SPORTS #####", "★★ PPV EVENTS ★★"). "#1 Hits",
# "C-SPAN", "Sky Sports ---", "***Premium*** Movies" and names ending in one
# "◉" are channels.
_SEP_SYM = r"[#=*~_|\-★▬◉●•]"
# Underscores count at the ends only as a run of three: "__NAME__" is a
# name, "___ NEWS ___" a separator.
_SEP_END = r"[#=*~|\-★▬◉●•]"
_SEP_TAG = r"(?:[A-Za-z]{2,3}\s*[:|]\s*)?"
_SEPARATOR_RE = re.compile(
    rf"^\s*{_SEP_TAG}(?:{_SEP_END}{{2,}}|_{{3,}}).*(?:{_SEP_END}{{2,}}|_{{3,}})\s*$"
    rf"|^\s*{_SEP_TAG}(?:{_SEP_SYM}|\s)*{_SEP_SYM}(?:{_SEP_SYM}|\s)*$"
)


def _is_separator(name: str) -> bool:
    """A lineup heading dressed as a channel. A NEW one is created switched
    off even in an enabled group (#158): it plays nothing, and Jellyfin would
    list it. "#1 Hits", "C-SPAN" and "***Premium*** Movies" are channels."""
    return bool(_SEPARATOR_RE.match(name or ""))


def _enabled_group_names(db: Session, provider_id: int) -> set:
    """Names of this provider's groups the user has switched on.

    A NEW channel starts with its group's state (#158). The page says "enable
    the groups you want, then sync channels", but every new channel was created
    disabled and the group toggle only cascades to channels that already exist,
    so the first sync reported "N channels (0 enabled)" and a channel a provider
    added later to an enabled group stayed off. Existing channels keep the
    user's own setting either way.
    """
    return {
        name for (name,) in db.query(LiveChannelGroup.name).filter(
            LiveChannelGroup.provider_id == provider_id,
            LiveChannelGroup.enabled == True,  # noqa: E712
        )
    }


def _upsert_channels(
    provider_id: int,
    streams: list[dict],
    cat_map: dict[str, str],
    client,
    db: Session,
    extension: str = "m3u8",
) -> dict:
    """Upsert LiveChannel records from Xtream streams. `extension` is the
    stream format the URLs ask for (see _live_extension); an existing
    channel's URL is rewritten in place, so a format change is one sync away
    and keeps the row, its enabled flag and its number."""
    existing = {
        ch.stream_id: ch
        for ch in db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).all()
    }

    new_count = 0
    updated_count = 0
    seen_ids = set()
    enabled_groups = _enabled_group_names(db, provider_id)

    for stream in streams:
        sid = str(stream.get("stream_id", ""))
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)

        name = stream.get("name", "")
        group = cat_map.get(str(stream.get("category_id", "")), "")
        url = client.live_stream_url(int(sid), extension=extension)

        if sid in existing:
            ch = existing[sid]
            ch.name = name
            ch.stream_url = url
            ch.logo_url = stream.get("stream_icon") or ch.logo_url
            ch.group_title = group or ch.group_title
            ch.epg_channel_id = stream.get("epg_channel_id") or ch.epg_channel_id
            ch.updated_at = datetime.utcnow()
            updated_count += 1
        else:
            db.add(LiveChannel(
                provider_id=provider_id,
                name=name,
                stream_id=sid,
                stream_url=url,
                logo_url=stream.get("stream_icon") or None,
                group_title=group,
                epg_channel_id=stream.get("epg_channel_id") or None,
                enabled=bool(group) and group in enabled_groups and not _is_separator(name),
            ))
            new_count += 1

    db.flush()
    return {"new": new_count, "updated": updated_count, "total": len(seen_ids)}


def _xtream_login(provider) -> tuple:
    return (provider.server_url or "", provider.username or "", provider.password or "")


def rewrite_xtream_channel_urls(provider, before: tuple, db: Session) -> int:
    """After a provider save, point its Xtream channels at the saved server
    and login (#469). An Xtream channel's URL carries both
    ({server}/live/{user}/{pass}/{id}.{ext}), and a save changes only the
    provider row: without this, every tune and recording kept the old login
    or host until a channel sync. Writes what a sync with the new values
    would, keeping each channel's stream format; no request to the provider.
    `before` is _xtream_login(provider) from before the save. Doesn't commit.
    Returns the number of channels rewritten."""
    from services.xtream_client import live_stream_url

    server, username, password = after = _xtream_login(provider)
    if after == before or not server or (provider.provider_type or "xtream") != "xtream":
        return 0
    count = 0
    for ch in db.query(LiveChannel).filter(LiveChannel.provider_id == provider.id).all():
        # Only URLs a sync built: an M3U row left from an earlier provider
        # type has no /live/<user>/<pass>/<stream_id>.<ext> to rebuild.
        m = re.search(rf"/live/.+/{re.escape(ch.stream_id or '')}\.(\w+)$", ch.stream_url or "")
        if not ch.stream_id or not m:
            continue
        url = live_stream_url(server, username, password, ch.stream_id, m.group(1))
        if url != ch.stream_url:
            ch.stream_url = url
            ch.updated_at = datetime.utcnow()
            count += 1
    if count:
        logger.info(f"[LiveTV] {provider.name}: {count} channel URLs now use the saved server and login")
    return count


# A provider that answers an M3U request with a truncated body, a maintenance
# page or an empty playlist parses to few/zero channels. Deleting on that wipes
# the user's curated lineup (enabled flags, channel numbers, sort order) and
# empties the HDHomeRun lineup Jellyfin already scanned. Same guard shape as
# the VOD category strikes in services.sync and the EPG guard below: refuse a
# removal that looks like an outage rather than a real catalogue change.
M3U_MAX_REMOVAL_FRACTION = 0.2
M3U_MIN_REMOVAL_FLOOR = 25


def _m3u_stable_id(name: str, stream_url: str) -> str:
    """Generate a stable stream_id for M3U channels from name + URL.

    Unlike array indices, this doesn't shift when the M3U file order changes.
    """
    import hashlib
    return hashlib.sha256(f"{name}|{stream_url}".encode()).hexdigest()[:16]


def _m3u_channel_number(value) -> Optional[int]:
    """tvg-chno as a channel number, when it is a plain whole number.

    Providers write sub-channels ("5.1") and text there, and int() on those
    raised, which failed the whole M3U sync (#175). Anything else is left
    unnumbered. ASCII digits only: "²".isdigit() is true and int("²") raises,
    and a full-width "５" is no channel number either.
    """
    text = str(value or "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


def _dedupe_m3u(parsed_channels: list[dict]) -> list[dict]:
    """The first entry of each channel; later copies of it are dropped.

    Provider M3Us often list one channel under two groups ("Sports" and
    "Favourites"): same name, same URL, so the same stable id. Both were added
    and the flush failed on uq_live_channel_stream, so no channel of the
    playlist was saved (#175).
    """
    seen, kept = set(), []
    for ch in parsed_channels:
        sid = _m3u_stable_id(ch["name"], ch["stream_url"])
        if sid in seen:
            continue
        seen.add(sid)
        kept.append(ch)
    dropped = len(parsed_channels) - len(kept)
    if dropped:
        logger.info(f"[LiveTV] M3U lists {dropped} channel(s) more than once; the first entry of each is used")
    return kept


# Channels the user switched off that the M3U playlist then dropped (#158). An
# M3U sync deletes a channel that leaves the playlist, and one listed again is
# a NEW row, which starts with its group's state: a channel switched off in an
# enabled group came back on. Their names are kept per provider, so one that
# returns stays off. (Xtream channels are never deleted, so they keep the row.)
_M3U_SWITCHED_OFF_KEY = "livetv_m3u_switched_off_{}"
_M3U_SWITCHED_OFF_MAX = 2000


def _m3u_switched_off(db: Session, provider_id: int) -> list:
    import json
    try:
        names = json.loads(get_setting(db, _M3U_SWITCHED_OFF_KEY.format(provider_id), "") or "[]")
    except ValueError:
        return []
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _upsert_channels_from_m3u(
    provider_id: int,
    parsed_channels: list[dict],
    db: Session,
) -> dict:
    """Upsert LiveChannel records from parsed M3U data.

    Uses a stable hash of name+URL as stream_id so channel IDs don't shift
    when the M3U file order changes. Preserves user customizations (enabled,
    sort_order, channel_number) across syncs. Removes channels no longer in
    the M3U file.
    """
    parsed_channels = _dedupe_m3u(parsed_channels)
    enabled_groups = _enabled_group_names(db, provider_id)
    switched_off = _m3u_switched_off(db, provider_id)
    remembered_off = set(switched_off)
    returned_off = set()

    # Build lookup of existing channels by their match key: the hash of the
    # name + URL they were last seen with (m3u_key; stream_id for rows from
    # before it existed). stream_id itself is the GuideNumber and never moves.
    all_rows = db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).all()
    existing = {(ch.m3u_key or ch.stream_id): ch for ch in all_rows}
    taken_numbers = {ch.stream_id for ch in all_rows}

    groups = set()
    seen_ids = set()
    new_count = 0
    updated_count = 0

    # A channel whose URL changed is still the same channel. The stable id
    # hashes name + URL, so a rotated token or a new host used to read as "one
    # channel removed, one added" for the whole lineup on every sync: enabled
    # flags, channel numbers and sort order thrown away, and Jellyfin handed a
    # lineup of new channel ids. Where a name has exactly ONE row that is about
    # to be orphaned and exactly ONE new entry, it is that row that moved: give
    # it the new match key and keep everything else -- its stream_id too, which
    # is the GuideNumber Jellyfin keys the channel's timers and favourites on
    # (#259). Anything ambiguous (the same name twice) is left to the ordinary
    # add/remove path rather than guessed at.
    incoming = {_m3u_stable_id(ch["name"], ch["stream_url"]) for ch in parsed_channels}
    orphans: dict[str, list] = {}
    for row_key, row in existing.items():
        if row_key not in incoming:
            orphans.setdefault(row.name, []).append(row)
    arrivals: dict[str, list[str]] = {}
    for ch in parsed_channels:
        sid = _m3u_stable_id(ch["name"], ch["stream_url"])
        if sid not in existing:
            arrivals.setdefault(ch["name"], []).append(sid)
    for name, sids in arrivals.items():
        rows = orphans.get(name, [])
        if len(sids) == 1 and len(rows) == 1:
            row = rows[0]
            del existing[row.m3u_key or row.stream_id]
            row.m3u_key = sids[0]
            existing[sids[0]] = row
    # A name with a comma was stored as the text after its last comma (the
    # parser read the name from there). The row with that cut name and the
    # same URL is this channel: it keeps its number, as above.
    url_count = Counter(ch["stream_url"] for ch in parsed_channels)
    for ch in parsed_channels:
        sid = _m3u_stable_id(ch["name"], ch["stream_url"])
        if sid in existing or "," not in ch["name"] or url_count[ch["stream_url"]] != 1:
            continue
        cut = ch["name"].rsplit(",", 1)[1].strip()
        rows = [r for r in orphans.get(cut, []) if r.stream_url == ch["stream_url"]
                and existing.get(r.m3u_key or r.stream_id) is r]
        if len(rows) == 1:
            row = rows[0]
            del existing[row.m3u_key or row.stream_id]
            row.m3u_key = sid
            existing[sid] = row

    for ch in parsed_channels:
        name = ch["name"]
        stream_url = ch["stream_url"]
        sid = _m3u_stable_id(name, stream_url)
        seen_ids.add(sid)
        group = ch.get("group_title") or ""
        if group:
            groups.add(group)

        if sid in existing:
            # Update metadata, preserve user settings (enabled, sort_order, channel_number)
            row = existing[sid]
            row.name = name
            row.stream_url = stream_url
            row.logo_url = ch.get("logo_url") or row.logo_url
            row.group_title = group or row.group_title
            row.epg_channel_id = ch.get("epg_channel_id") or row.epg_channel_id
            number = _m3u_channel_number(ch.get("tvg_chno"))
            if number is not None and not row.channel_number:
                row.channel_number = number
            row.updated_at = datetime.utcnow()
            updated_count += 1
        else:
            # A new channel's number is its key, as always -- unless a row that
            # moved still holds that number (a URL that went and came back).
            number_id, n = sid, 0
            while number_id in taken_numbers:
                n += 1
                number_id = _m3u_stable_id(name, f"{stream_url}#{n}")
            taken_numbers.add(number_id)
            db.add(LiveChannel(
                provider_id=provider_id,
                name=name,
                stream_id=number_id,
                m3u_key=sid,
                stream_url=stream_url,
                logo_url=ch.get("logo_url"),
                group_title=group,
                epg_channel_id=ch.get("epg_channel_id"),
                channel_number=_m3u_channel_number(ch.get("tvg_chno")),
                enabled=(bool(group) and group in enabled_groups and not _is_separator(name)
                         and name not in remembered_off),
            ))
            if name in remembered_off:
                returned_off.add(name)      # the row carries the user's "off" again
            new_count += 1

    # Remove channels no longer in M3U — but never on a response that looks
    # like a failed download rather than a real catalogue change.
    removed_ids = set(existing.keys()) - seen_ids
    refused_removals = 0
    if removed_ids:
        # The fixed floor exists so a small, real removal is never blocked on
        # a big lineup. On a SMALL lineup (a curated playlist behind tuliprox
        # or Threadfin is often under 25 channels) it must not exceed half of
        # what is there, or the floor swallows the whole lineup: 20 channels
        # truncated to 2 deleted the other 18.
        floor = min(M3U_MIN_REMOVAL_FLOOR, len(existing) // 2)
        limit = max(floor, int(len(existing) * M3U_MAX_REMOVAL_FRACTION))
        # A failed or partial download is always SHORTER than the lineup it
        # replaces. A playlist at least as long as before whose entries all
        # changed (new host, rotated token) is a real change; refusing it would
        # keep every old row and add every new one on each sync, for ever.
        shrank = len(parsed_channels) < len(existing)
        if not parsed_channels or (shrank and len(removed_ids) > limit):
            refused_removals = len(removed_ids)
            removed_ids = set()
            logger.error(
                f"[LiveTV] Refusing to delete {refused_removals} of {len(existing)} "
                f"channels for provider {provider_id}: the playlist parsed to "
                f"{len(parsed_channels)} channel(s), which looks like a failed or "
                f"partial download, not a provider removal. Existing channels kept."
            )
        else:
            for sid in removed_ids:
                row = existing[sid]
                if (not row.enabled and row.group_title in enabled_groups
                        and not _is_separator(row.name)):
                    switched_off.append(row.name)   # the user's own "off"
            db.query(LiveChannel).filter(
                LiveChannel.provider_id == provider_id,
                LiveChannel.id.in_([existing[k].id for k in removed_ids]),
            ).delete(synchronize_session=False)

    kept_off = list(dict.fromkeys(n for n in switched_off if n not in returned_off))
    kept_off = kept_off[-_M3U_SWITCHED_OFF_MAX:]
    if kept_off != _m3u_switched_off(db, provider_id):
        # In this sync's transaction (set_setting would commit half of it).
        import json
        from models.database import Setting
        key = _M3U_SWITCHED_OFF_KEY.format(provider_id)
        row = db.query(Setting).filter(Setting.key == key).first()
        if row is None:
            db.add(Setting(key=key, value=json.dumps(kept_off)))
        else:
            row.value = json.dumps(kept_off)

    # Sync groups
    existing_groups = {
        g.name: g
        for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id).all()
    }
    for name in groups:
        if name not in existing_groups:
            db.add(LiveChannelGroup(
                provider_id=provider_id,
                name=name,
                enabled=False,
            ))

    db.flush()
    _update_group_counts(provider_id, db)

    return {"new": new_count, "updated": updated_count, "removed": len(removed_ids),
            "removals_refused": refused_removals, "total": len(parsed_channels)}


def _update_group_counts(provider_id: int, db: Session):
    """Update channel_count on each group."""
    groups = db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id).all()
    for g in groups:
        count = db.query(LiveChannel).filter(
            LiveChannel.provider_id == provider_id,
            LiveChannel.group_title == g.name,
        ).count()
        g.channel_count = count


# ─── EPG sync ───────────────────────────────────────────────────────────────


def _epg_provider_data(provider, all_channels) -> dict:
    """What _run_epg_sync_background is given for one provider."""
    return {
        "id": provider.id,
        "provider_type": provider.provider_type or "xtream",
        "server_url": provider.server_url,
        "username": provider.username,
        "password": provider.password,
        "user_agent": provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
        "epg_url": provider.epg_url,
        "channels": [
            {"stream_id": ch.stream_id, "epg_channel_id": ch.epg_channel_id, "name": ch.name}
            for ch in all_channels
        ],
        "enabled_count": sum(1 for ch in all_channels if ch.enabled),
    }


def _epg_feed_url(provider_data: dict) -> str | None:
    """The XMLTV feed a guide sync reads: the provider's EPG URL, else Xtream's own."""
    if provider_data.get("epg_url"):
        return provider_data["epg_url"]
    if provider_data["provider_type"] != "xtream":
        return None
    from services.xtream_client import XtreamClient
    client = XtreamClient(
        server=provider_data["server_url"],
        username=provider_data["username"],
        password=provider_data["password"],
        user_agent=provider_data["user_agent"],
    )
    try:
        return client.get_xmltv_url()
    finally:
        client.close()


def _epg_channel_info(rows) -> list:
    return [{"id": r.id, "name": r.name, "tvg_id": r.epg_channel_id,
             "override": r.epg_id_override, "enabled": bool(r.enabled)} for r in rows]


def _epg_sync_inputs(epg_url: str, chan_info: list) -> str | None:
    """Everything a guide sync reads, as one hash: the feed URL, the cached
    feed file, and each channel's override, tvg-id and name. None when the
    feed is not in the fresh disk cache: a sync would download it again."""
    import hashlib
    import json
    import os
    from services.xmltv import _get_cache_path, _is_cache_fresh
    path = _get_cache_path(epg_url)
    try:
        if not _is_cache_fresh(path):
            return None
        st = os.stat(path)
    except OSError:
        return None
    channels = sorted((c["id"], c["name"], c["tvg_id"], c["override"]) for c in chan_info)
    blob = json.dumps([epg_url, st.st_mtime_ns, st.st_size, channels])
    return hashlib.sha256(blob.encode()).hexdigest()


def _epg_resync_useless(db: Session, provider, missing: set) -> bool:
    """True when a guide sync now would read exactly what the last successful
    one read, and that one already left every id in `missing` without
    programmes: running it again can't give those channels a guide (#466)."""
    import json
    try:
        last = json.loads(get_setting(db, f"livetv_epg_synced_{provider.id}", "") or "null")
    except ValueError:
        return False
    if not isinstance(last, dict) or not missing <= set(last.get("empty") or ()):
        return False
    rows = db.query(LiveChannel).filter(LiveChannel.provider_id == provider.id).all()
    epg_url = _epg_feed_url(_epg_provider_data(provider, rows))
    inputs = _epg_sync_inputs(epg_url, _epg_channel_info(rows)) if epg_url else None
    return inputs is not None and inputs == last.get("inputs")


@router.post("/api/live/sync-epg/{provider_id}", dependencies=_admin)
def sync_epg(provider_id: int, db: Session = Depends(get_db)):
    """Sync EPG data for enabled channels only (runs in background)."""
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "An EPG sync")
    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")

    # Check if already running
    existing = _get_sync_status(provider_id)
    if existing.get("phase") == "epg" and existing.get("status") == "running":
        return {"success": True, "message": "EPG sync already in progress"}

    # Get ALL provider channels (EPG data stored for all, not just enabled)
    all_channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.provider_id == provider_id)
        .all()
    )
    enabled_count = sum(1 for ch in all_channels if ch.enabled)

    if not all_channels:
        return {"success": False, "message": "No channels synced yet — run channel sync first"}

    provider_data = _epg_provider_data(provider, all_channels)

    _set_sync_status(provider_id, {
        "phase": "epg",
        "status": "running",
        "progress": 0,
        "message": f"Fetching guide data for {len(all_channels)} channels ({enabled_count} enabled)...",
    })

    thread = threading.Thread(target=_run_epg_sync_background, args=(provider_data,), daemon=True)
    thread.start()
    return {"success": True, "message": f"EPG sync started for {len(all_channels)} channels ({enabled_count} enabled)"}


def _run_epg_sync_background(provider_data: dict):
    """Background EPG sync — stream-parses full XMLTV, keeps programs for ALL provider channels."""
    pid = provider_data["id"]
    channels = provider_data["channels"]
    total = len(channels)
    enabled_count = provider_data.get("enabled_count", total)

    try:
        db = SessionLocal()
        try:
            # Guide data is kept for ALL of the provider's channels, not just
            # the enabled ones, so a channel enabled later already has a guide.
            # Ids known before the feed is read: overrides and tvg-ids. Channels
            # with neither (or a tvg-id the feed lacks) are matched by name once
            # the feed's own channel list is in hand (#141).
            from services.epg_match import resolve_guide_ids
            rows = db.query(LiveChannel).filter(LiveChannel.provider_id == pid).all()
            chan_info = _epg_channel_info(rows)
            epg_ids = ({(c["override"] or "").strip() for c in chan_info}
                       | {(c["tvg_id"] or "").strip() for c in chan_info}) - {""}
            if not chan_info:
                _set_sync_status(pid, {
                    "phase": "epg", "status": "error", "progress": 0,
                    "message": "No channels yet. Sync channels first, then the guide.",
                })
                return False
            resolved: dict = {}

            def _resolve(feed_channels):
                resolved.clear()
                resolved.update(resolve_guide_ids(chan_info, feed_channels))
                return {r["guide_id"] for r in resolved.values() if r["guide_id"]}

            epg_url = _epg_feed_url(provider_data)
            if not epg_url:
                _set_sync_status(pid, {
                    "phase": "epg", "status": "error", "progress": 0,
                    "message": "No EPG URL available.",
                })
                return False

            # Progress callback
            def on_progress(pct, msg):
                _set_sync_status(pid, {"phase": "epg", "status": "running", "progress": pct, "message": msg})

            # Download + parse FIRST — the existing guide data stays untouched
            # until we're sure we have a good replacement. Providers sometimes
            # serve a throttled/empty-but-parseable XMLTV (especially at 3am);
            # the old delete-first flow committed that as a full guide wipe.
            # Retries bust the 8h disk cache so a bad cached file can't stick.
            import os as _os
            import time as _time
            from services.xmltv import stream_parse_xmltv, _get_cache_path

            def _drop_xmltv_cache():
                try:
                    _os.remove(_get_cache_path(epg_url))
                except OSError:
                    pass

            programs = None
            inputs = None
            last_err = None
            for attempt in range(1, 4):
                try:
                    suffix = f" (attempt {attempt}/3)" if attempt > 1 else ""
                    _set_sync_status(pid, {"phase": "epg", "status": "running", "progress": 5, "message": f"Downloading XMLTV guide{suffix}..."})
                    programs = stream_parse_xmltv(
                        url=epg_url,
                        channel_ids=epg_ids,
                        user_agent=provider_data["user_agent"],
                        on_progress=on_progress,
                        force_download=attempt > 1,
                        resolve_channels=_resolve,
                    )
                    if programs:
                        # The feed file just read, before anything can replace it.
                        inputs = _epg_sync_inputs(epg_url, chan_info)
                        break
                    last_err = "provider returned no programs for our channels"
                    logger.warning(f"[LiveTV] EPG attempt {attempt}/3: {last_err}")
                except Exception as e:
                    last_err = str(e)
                    logger.warning(f"[LiveTV] EPG download attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    _drop_xmltv_cache()  # don't let a bad cached file poison the retry
                    _time.sleep(30 * attempt)

            # Everything this provider's channels were, or are about to be,
            # stored under: a name match that moved leaves nothing behind.
            provider_channel_epg_ids = {
                gid for r in rows
                for gid in (r.guide_epg_id, r.epg_name_match, (r.epg_channel_id or "").strip())
                if gid
            }
            old_count = (
                db.query(EPGProgram).filter(EPGProgram.channel_id.in_(provider_channel_epg_ids)).count()
                if provider_channel_epg_ids else 0
            )

            # Sanity guards — never replace a healthy guide with a suspiciously
            # empty one. Keep the old data and surface the failure instead.
            if not programs:
                msg = f"EPG sync failed: {last_err or 'no programs'} — kept existing guide data ({old_count} programs)"
                logger.error(f"[LiveTV] {msg}")
                log_activity(db, "epg_sync_failed", msg)
                db.commit()
                _drop_xmltv_cache()
                _set_sync_status(pid, {"phase": "epg", "status": "error", "progress": 0, "message": msg})
                return False
            if old_count >= 1000 and len(programs) < old_count * 0.1:
                msg = (f"EPG sync aborted: provider returned only {len(programs)} programs "
                       f"(previously {old_count}) — looks like a bad/partial guide, kept existing data")
                logger.error(f"[LiveTV] {msg}")
                log_activity(db, "epg_sync_failed", msg)
                db.commit()
                _drop_xmltv_cache()
                _set_sync_status(pid, {"phase": "epg", "status": "error", "progress": 0, "message": msg})
                return False

            # Replace guide data — quick transaction, no network inside it
            if resolved:
                with_programmes = {p["channel_id"] for p in programs}
                # The parser kept programmes for every name match, including
                # the ones dropped below: their ids go into the delete set too,
                # so rows an earlier sync stored under them can't collide (#467).
                matched_ids = {v["guide_id"] for v in resolved.values() if v["guide_id"]}
                for r in rows:
                    match = (resolved.get(r.id) or {}).get("name_match")
                    tvg = (r.epg_channel_id or "").strip()
                    if match and tvg in with_programmes:
                        # The channel's own tvg-id brought programmes (a feed can
                        # carry a schedule it lists no <channel> for): that is its
                        # guide, and a name match must not replace it (#141).
                        match = None
                        resolved[r.id] = {**resolved[r.id], "method": "tvg-id", "guide_id": tvg,
                                          "name_match": None}
                    r.epg_name_match = match
                guide_ids = {v["guide_id"] for v in resolved.values() if v["guide_id"]}
                provider_channel_epg_ids |= matched_ids | guide_ids
                # A dropped match is no channel's guide: its programmes aren't kept.
                unused = matched_ids - guide_ids - epg_ids
                if unused:
                    programs = [p for p in programs if p["channel_id"] not in unused]
            if provider_channel_epg_ids:
                db.query(EPGProgram).filter(
                    EPGProgram.channel_id.in_(provider_channel_epg_ids)
                ).delete(synchronize_session=False)
                db.flush()

            # Insert into DB
            _set_sync_status(pid, {"phase": "epg", "status": "running", "progress": 90, "message": f"Saving {len(programs)} programs..."})
            inserted = 0
            seen_epg = set()
            batch = []
            for prog in programs:
                key = (prog["channel_id"], prog["start"])
                if key in seen_epg:
                    continue
                seen_epg.add(key)
                batch.append(EPGProgram(
                    channel_id=prog["channel_id"],
                    title=prog["title"] or "",
                    sub_title=prog.get("sub_title"),
                    description=prog.get("description"),
                    start=prog["start"],
                    stop=prog["stop"],
                    category=prog.get("category"),
                    icon_url=prog.get("icon_url"),
                ))
                inserted += 1
                if len(batch) >= 5000:
                    db.add_all(batch)
                    db.flush()
                    batch = []
            if batch:
                db.add_all(batch)
                db.flush()

            # How many channels actually have a guide, and why the rest do not:
            # "success" alone hid that most channels had nothing (#141).
            coverage_note = ""
            if resolved:
                import json
                from services.epg_match import coverage_report, coverage_summary
                report = coverage_report(chan_info, resolved, {p["channel_id"] for p in programs})
                report["at"] = datetime.utcnow().isoformat() + "Z"
                set_setting(db, f"livetv_epg_coverage_{pid}", json.dumps(report))
                coverage_note = f" — {coverage_summary(report)}"

            # What this sync read, and the guide ids it left empty: refresh-guide
            # re-runs a sync only when that could fill one of them (#466).
            import json
            kept = {p["channel_id"] for p in programs}
            set_setting(db, f"livetv_epg_synced_{pid}", json.dumps({
                "inputs": inputs,
                "empty": sorted({r.guide_epg_id for r in rows} - {None} - kept),
            }))

            db.commit()
            log_activity(db, "epg_sync", f"EPG sync: {inserted} programs for {total} channels "
                                         f"({enabled_count} enabled){coverage_note}")

            _set_sync_status(pid, {
                "phase": "epg",
                "status": "complete",
                "progress": 100,
                "message": f"{inserted} programs synced for {total} channels ({enabled_count} enabled){coverage_note}",
                "programs": inserted,
                "channels": total,
            })
            return True
        finally:
            db.close()

    except Exception as e:
        logger.error(f"[LiveTV] EPG sync failed: {e}")
        _set_sync_status(pid, {"phase": "epg", "status": "error", "progress": 0, "message": str(e)})
        return False


# ─── Channel management ────────────────────────────────────────────────────


def _guide_id_expr():
    """LiveChannel.guide_epg_id as SQL: override, else name match, else tvg-id."""
    return func.coalesce(
        func.nullif(func.trim(LiveChannel.epg_id_override), ""),
        LiveChannel.epg_name_match,
        func.nullif(func.trim(LiveChannel.epg_channel_id), ""),
    )


def _ids_with_programmes(db: Session, ids) -> set:
    found = set()
    for chunk in _chunked(ids):
        found |= {row[0] for row in db.query(EPGProgram.channel_id)
                  .filter(EPGProgram.channel_id.in_(chunk)).distinct()}
    return found


@router.get("/api/live/channels", dependencies=_admin)
def list_channels(
    provider_id: Optional[int] = None,
    group: Optional[str] = None,
    enabled: Optional[bool] = None,
    search: Optional[str] = None,
    has_epg: Optional[bool] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List live channels with filtering and pagination."""
    q = db.query(LiveChannel)
    if provider_id:
        q = q.filter(LiveChannel.provider_id == provider_id)
    if group:
        q = q.filter(LiveChannel.group_title == group)
    if enabled is not None:
        q = q.filter(LiveChannel.enabled == enabled)
    if search:
        q = q.filter(or_(LiveChannel.name.ilike(f"%{search}%"), LiveChannel.custom_name.ilike(f"%{search}%")))
    # Guide ids that actually have programs in the DB. EPG sync stores data
    # for ALL provider channels, so this is accurate after the first sync.
    # A channel's guide id is its override, a name match or its tvg-id (#141).
    guide_id = _guide_id_expr()
    epg_id_q = db.query(guide_id).filter(guide_id.isnot(None))
    if provider_id:
        epg_id_q = epg_id_q.filter(LiveChannel.provider_id == provider_id)
    all_epg_ids = {row[0] for row in epg_id_q.distinct().all()}
    epg_ids_with_programs = _ids_with_programmes(db, all_epg_ids) if all_epg_ids else set()

    # Filter by whether channel actually has EPG program data in the DB
    if has_epg is not None:
        if has_epg:
            if epg_ids_with_programs:
                q = q.filter(guide_id.in_(epg_ids_with_programs))
            else:
                q = q.filter(LiveChannel.id < 0)  # no results — no EPG data exists yet
        else:
            if epg_ids_with_programs:
                q = q.filter(guide_id.is_(None) | ~guide_id.in_(epg_ids_with_programs))
            # else: all channels have no EPG, no filter needed

    total = q.count()
    channels = q.order_by(LiveChannel.sort_order, LiveChannel.name).offset((page - 1) * per_page).limit(per_page).all()

    return {
        "channels": [
            {
                "id": ch.id,
                "name": ch.guide_name,
                "provider_name": ch.name,
                "custom_name": ch.custom_name,
                "channel_number": ch.channel_number,
                "stream_id": ch.stream_id,
                "stream_url": ch.stream_url,
                "logo_url": ch.logo_url,
                "group_title": ch.group_title,
                "epg_channel_id": ch.epg_channel_id,
                "epg_id_override": ch.epg_id_override,
                "epg_name_match": ch.epg_name_match,
                "guide_epg_id": ch.guide_epg_id,
                "epg_match": ch.epg_match,
                "has_epg_data": ch.guide_epg_id in epg_ids_with_programs if ch.guide_epg_id else False,
                "enabled": ch.enabled,
                "sort_order": ch.sort_order,
            }
            for ch in channels
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.put("/api/live/channels/{channel_id}", dependencies=_admin)
def update_channel(channel_id: int, update: ChannelUpdate, db: Session = Depends(get_db)):
    """Update a single channel."""
    ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(404, "Channel not found")

    if update.enabled is not None:
        ch.enabled = update.enabled
    if update.channel_number is not None:
        ch.channel_number = update.channel_number
    if update.epg_channel_id is not None:
        ch.epg_channel_id = update.epg_channel_id
    if update.sort_order is not None:
        ch.sort_order = update.sort_order
    if update.custom_name is not None:
        custom = " ".join(update.custom_name.split())
        if len(custom) > CUSTOM_NAME_MAX:
            raise HTTPException(400, f"Channel names are limited to {CUSTOM_NAME_MAX} characters")
        ch.custom_name = custom or None
    if update.epg_id_override is not None:
        # The feed's channel id to take the guide from; "" goes back to
        # matching automatically. Applied at the next EPG sync.
        override = update.epg_id_override.strip()
        if len(override) > EPG_ID_OVERRIDE_MAX:
            raise HTTPException(400, f"Guide ids are limited to {EPG_ID_OVERRIDE_MAX} characters")
        ch.epg_id_override = override or None

    ch.updated_at = datetime.utcnow()
    db.commit()
    return {"success": True}


# SQLite caps host parameters per statement (SQLITE_MAX_VARIABLE_NUMBER, 999 on
# older builds). A big IPTV provider can have thousands of channels/groups, so an
# unchunked IN (...) list raises "too many SQL variables" (an unhandled 500) when
# saving. Chunk the lists so bulk saves scale to any provider size.
_SQL_IN_CHUNK = 500


def _chunked(seq, size=_SQL_IN_CHUNK):
    """Yield successive `size`-length slices of `seq`."""
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


@router.post("/api/live/channels/bulk", dependencies=_admin)
def bulk_update_channels(update: BulkChannelUpdate, db: Session = Depends(get_db)):
    """Bulk enable/disable channels."""
    count = 0
    for chunk in _chunked(update.channel_ids):
        count += db.query(LiveChannel).filter(
            LiveChannel.id.in_(chunk)
        ).update({LiveChannel.enabled: update.enabled}, synchronize_session=False)
    db.commit()
    return {"success": True, "updated": count}


@router.post("/api/live/channels/bulk-filter", dependencies=_admin)
def bulk_update_channels_by_filter(update: BulkChannelFilter, db: Session = Depends(get_db)):
    """Bulk enable/disable channels matching filters (group, search)."""
    q = db.query(LiveChannel).filter(LiveChannel.provider_id == update.provider_id)
    if update.group:
        q = q.filter(LiveChannel.group_title == update.group)
    if update.search:
        q = q.filter(or_(LiveChannel.name.ilike(f"%{update.search}%"), LiveChannel.custom_name.ilike(f"%{update.search}%")))
    if update.has_epg is not None:
        guide_id = _guide_id_expr()
        if update.has_epg:
            q = q.filter(guide_id.isnot(None))
        else:
            q = q.filter(guide_id.is_(None))
    count = q.update({LiveChannel.enabled: update.enabled}, synchronize_session=False)
    db.commit()
    return {"success": True, "updated": count}


# ─── Group management ──────────────────────────────────────────────────────


@router.get("/api/live/groups", dependencies=_admin)
def list_groups(provider_id: Optional[int] = None, db: Session = Depends(get_db)):
    """List channel groups."""
    q = db.query(LiveChannelGroup)
    if provider_id:
        q = q.filter(LiveChannelGroup.provider_id == provider_id)

    groups = q.order_by(LiveChannelGroup.name).all()
    return {
        "groups": [
            {
                "id": g.id,
                "provider_id": g.provider_id,
                "name": g.name,
                "category_id": g.category_id,
                "enabled": g.enabled,
                "channel_count": g.channel_count,
            }
            for g in groups
        ]
    }


@router.put("/api/live/groups/bulk", dependencies=_admin)
def bulk_update_groups(update: BulkGroupUpdate, db: Session = Depends(get_db)):
    """Enable/disable multiple groups and their channels in one request."""
    groups = []
    for chunk in _chunked(update.group_ids):
        groups.extend(db.query(LiveChannelGroup).filter(LiveChannelGroup.id.in_(chunk)).all())
    if not groups:
        return {"success": True, "updated": 0}

    # Only cascade channel enable/disable for groups that are actually changing state.
    # Without this, re-saving already-enabled groups resets individually-disabled channels.
    changing_names = [g.name for g in groups if g.enabled != update.enabled]

    for chunk in _chunked(update.group_ids):
        db.query(LiveChannelGroup).filter(LiveChannelGroup.id.in_(chunk)).update(
            {LiveChannelGroup.enabled: update.enabled}, synchronize_session=False
        )

    if changing_names:
        provider_id = groups[0].provider_id
        for chunk in _chunked(changing_names):
            db.query(LiveChannel).filter(
                LiveChannel.provider_id == provider_id,
                LiveChannel.group_title.in_(chunk),
            ).update({LiveChannel.enabled: update.enabled}, synchronize_session=False)

    db.commit()
    return {"success": True, "updated": len(groups)}


@router.put("/api/live/groups/{group_id}", dependencies=_admin)
def update_group(group_id: int, update: GroupUpdate, db: Session = Depends(get_db)):
    """Enable/disable a group and all its channels."""
    group = db.query(LiveChannelGroup).filter(LiveChannelGroup.id == group_id).first()
    if not group:
        raise HTTPException(404, "Group not found")

    group.enabled = update.enabled

    db.query(LiveChannel).filter(
        LiveChannel.provider_id == group.provider_id,
        LiveChannel.group_title == group.name,
    ).update({LiveChannel.enabled: update.enabled}, synchronize_session=False)

    db.commit()
    return {"success": True}


# ─── Status ─────────────────────────────────────────────────────────────────


@router.get("/api/live/status", dependencies=_admin)
def live_status(db: Session = Depends(get_db)):
    """Overview of Live TV status."""
    total_channels = db.query(LiveChannel).count()
    enabled_channels = db.query(LiveChannel).filter(LiveChannel.enabled == True).count()
    total_groups = db.query(LiveChannelGroup).count()
    enabled_groups = db.query(LiveChannelGroup).filter(LiveChannelGroup.enabled == True).count()
    epg_programs = db.query(EPGProgram).count()

    # Get providers with live TV
    providers = db.query(Provider).filter(Provider.live_tv_enabled == True).all()
    provider_info = []
    for p in providers:
        ch_count = db.query(LiveChannel).filter(LiveChannel.provider_id == p.id, LiveChannel.enabled == True).count()
        provider_info.append({
            "id": p.id,
            "name": p.name,
            "type": p.provider_type or "xtream",
            "enabled_channels": ch_count,
            "last_sync": p.last_live_sync.isoformat() if p.last_live_sync else None,
        })

    return {
        "total_channels": total_channels,
        "enabled_channels": enabled_channels,
        "total_groups": total_groups,
        "enabled_groups": enabled_groups,
        "epg_programs": epg_programs,
        "providers": provider_info,
    }


# ─── Jellyfin Guide Refresh ────────────────────────────────────────────────


@router.post("/api/live/refresh-guide", dependencies=_admin)
def refresh_jellyfin_guide(db: Session = Depends(get_db)):
    """
    One-click Jellyfin refresh: checks for missing EPG data, re-syncs listing
    provider (forces channel-to-XMLTV remap), then triggers guide refresh.
    """
    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    if not jf_url or not jf_key:
        raise HTTPException(400, "Jellyfin URL or API key not configured")

    import requests as req
    headers = {"X-Emby-Token": jf_key, "Content-Type": "application/json"}

    # Pre-check: are there enabled channels missing EPG data?
    # If so, trigger a quick EPG sync first so new channels get guide data
    epg_resynced = False
    providers_with_channels = (
        db.query(Provider.id)
        .join(LiveChannel, LiveChannel.provider_id == Provider.id)
        .filter(LiveChannel.enabled == True)
        .distinct()
        .all()
    )
    for (pid,) in providers_with_channels:
        enabled_epg_ids = {
            ch.guide_epg_id
            for ch in db.query(LiveChannel).filter(
                LiveChannel.provider_id == pid,
                LiveChannel.enabled == True,
            ).all()
        } - {None}
        if not enabled_epg_ids:
            continue
        # Check if any enabled channel has zero EPG programs
        epg_with_data = {
            row.channel_id
            for row in db.query(EPGProgram.channel_id)
            .filter(EPGProgram.channel_id.in_(enabled_epg_ids))
            .distinct()
            .all()
        }
        missing = enabled_epg_ids - epg_with_data
        if missing:
            provider = db.query(Provider).filter(Provider.id == pid).first()
            if provider and _epg_resync_useless(db, provider, missing):
                logger.info(f"[LiveTV] {len(missing)} enabled channels of provider {pid} have no guide in its feed; "
                            f"nothing changed since the last EPG sync, not syncing again")
                continue
            from services.provider_activity import recording_protected
            if recording_protected(db):
                logger.warning(f"[LiveTV] {len(missing)} enabled channels have no EPG data, but a recording is "
                               f"running and recording protection is on — not downloading the guide now")
                continue
            logger.info(f"[LiveTV] {len(missing)} enabled channels missing EPG data for provider {pid} — triggering EPG sync")
            # Trigger EPG sync synchronously (inline, not background thread)
            # so Jellyfin gets fresh data when we refresh
            if provider:
                all_channels = db.query(LiveChannel).filter(LiveChannel.provider_id == pid).all()
                provider_data = _epg_provider_data(provider, all_channels)
                # Close current DB session before background sync uses its own
                db.close()
                _run_epg_sync_background(provider_data)
                # Re-open session for the rest of this endpoint
                db = SessionLocal()
                epg_resynced = True

    try:
        from services.jellyfin_guide import GuideRefreshError, refresh_jellyfin_guide
        refresh_jellyfin_guide(jf_url, jf_key)
        msg = "Jellyfin guide refresh triggered"
        if epg_resynced:
            msg = "EPG data synced for new channels + Jellyfin guide refresh triggered"
        logger.info(f"[LiveTV] {msg}")
        return {"success": True, "message": msg}
    except GuideRefreshError as e:
        raise HTTPException(404, str(e))
    except req.RequestException as e:
        logger.error(f"[LiveTV] Failed to trigger Jellyfin guide refresh: {e}")
        raise HTTPException(502, f"Failed to connect to Jellyfin: {e}")


# ─── HDHomeRun Emulation ───────────────────────────────────────────────────


@router.get("/discover.json")
@router.get("/hdhr/discover.json")
def hdhr_discover(request: Request, db: Session = Depends(get_db)):
    """HDHomeRun device discovery endpoint."""
    tuner_count = int(get_setting(db, "hdhr_tuner_count", "3"))
    device_id = get_setting(db, "hdhr_device_id", "TENTACLE1")

    # Use explicit setting if configured, otherwise derive from request
    # (request.base_url returns localhost inside Docker — useless for Jellyfin in another container)
    base_url = get_setting(db, "hdhr_base_url", "").strip()
    if not base_url:
        # Try X-Forwarded-Host first (reverse proxy), then Host header, then request.base_url
        forwarded_host = request.headers.get("x-forwarded-host")
        scheme = request.headers.get("x-forwarded-proto", "http")
        if forwarded_host:
            base_url = f"{scheme}://{forwarded_host}"
        else:
            host = request.headers.get("host")
            if host:
                base_url = f"http://{host}"
            else:
                base_url = str(request.base_url).rstrip("/")
    base_url = base_url.rstrip("/")

    return {
        "FriendlyName": "Tentacle",
        "Manufacturer": "Silicondust",
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomerun5_atsc",
        "FirmwareVersion": "20231001",
        "DeviceID": device_id,
        "DeviceAuth": "tentacle",
        "TunerCount": tuner_count,
        "BaseURL": base_url,
        "LineupURL": f"{base_url}/lineup.json",
    }


@router.get("/lineup.json")
@router.get("/hdhr/lineup.json")
def hdhr_lineup(request: Request, db: Session = Depends(get_db)):
    """HDHomeRun channel lineup — only enabled channels.
    URLs point to our stream proxy which handles UA spoofing, 302 redirect
    following, and HLS playlist rewriting."""
    channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.enabled == True)
        .order_by(LiveChannel.sort_order, LiveChannel.channel_number, LiveChannel.name)
        .all()
    )

    # Build base URL same way as discover.json
    base_url = get_setting(db, "hdhr_base_url", "").strip()
    if not base_url:
        forwarded_host = request.headers.get("x-forwarded-host")
        scheme = request.headers.get("x-forwarded-proto", "http")
        if forwarded_host:
            base_url = f"{scheme}://{forwarded_host}"
        else:
            host = request.headers.get("host")
            if host:
                base_url = f"http://{host}"
            else:
                base_url = str(request.base_url).rstrip("/")
    base_url = base_url.rstrip("/")

    lineup = []
    for ch in channels:
        # Use stream_id as stable channel number — never shifts when channels are added/removed
        number = ch.stream_id or str(ch.id)
        entry = {
            "GuideNumber": str(number),
            "GuideName": ch.guide_name,
            "URL": f"{base_url}/api/live/stream/{ch.id}",
        }
        if ch.logo_url:
            entry["LogoUrl"] = ch.logo_url
        lineup.append(entry)

    # YouTube channels the user exposed to Live TV. They aren't LiveChannel rows
    # (that table requires a provider_id and a YouTube channel is not an IPTV
    # provider), so they are unioned in here and play through their own endpoint.
    for yt in youtube_livetv.live_channels(db):
        entry = {
            "GuideNumber": yt["guide_number"],
            "GuideName": yt["name"],
            # Raw MPEG-TS, not HLS: Jellyfin's tuner reads the response body as
            # video, so a playlist gets copied as if it were video data.
            "URL": f"{base_url}/api/youtube/live/{yt['youtube_channel_id']}/stream.ts",
        }
        if yt["logo_url"]:
            entry["LogoUrl"] = yt["logo_url"]
        lineup.append(entry)

    return lineup


@router.get("/device.xml")
@router.get("/hdhr/device.xml")
def hdhr_device_xml(request: Request, db: Session = Depends(get_db)):
    """UPnP device descriptor — mimics a real HDHomeRun (Silicondust HDTC-2US).
    Jellyfin uses this for device identification and capability detection."""
    device_id = get_setting(db, "hdhr_device_id", "TENTACLE1")
    base_url = get_setting(db, "hdhr_base_url", "").strip()
    if not base_url:
        forwarded_host = request.headers.get("x-forwarded-host")
        scheme = request.headers.get("x-forwarded-proto", "http")
        if forwarded_host:
            base_url = f"{scheme}://{forwarded_host}"
        else:
            host = request.headers.get("host")
            if host:
                base_url = f"http://{host}"
            else:
                base_url = str(request.base_url).rstrip("/")
    base_url = base_url.rstrip("/")

    xml_content = f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>Tentacle</friendlyName>
    <manufacturer>Silicondust</manufacturer>
    <modelName>HDTC-2US</modelName>
    <modelNumber>HDTC-2US</modelNumber>
    <serialNumber></serialNumber>
    <UDN>uuid:{device_id}</UDN>
  </device>
  <URLBase>{base_url}</URLBase>
</root>"""
    return Response(content=xml_content, media_type="application/xml")


@router.get("/lineup_status.json")
@router.get("/hdhr/lineup_status.json")
def hdhr_lineup_status():
    """HDHomeRun lineup scan status."""
    return {
        "ScanInProgress": 0,
        "ScanPossible": 1,
        "Source": "Cable",
        "SourceList": ["Cable"],
    }


@router.post("/lineup.post")
@router.post("/hdhr/lineup.post")
def hdhr_lineup_post():
    """HDHomeRun lineup scan trigger (no-op, Jellyfin calls this)."""
    return Response(status_code=200)


# An HLS "master" playlist lists variant PLAYLISTS, not media segments. Our
# tuner response body is read by Jellyfin as raw video, so a variant URI has to
# be resolved here — piping the variant playlist's text through sends ASCII
# where MPEG-TS is expected, and the master (which never carries
# #EXT-X-ENDLIST) then loops for ever yielding nothing.
_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF", re.IGNORECASE)
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)", re.IGNORECASE)
_MAX_VARIANT_HOPS = 3


def _select_hls_variant(playlist_text: str, base_url: str):
    """Highest-bandwidth variant URI of an HLS master playlist.

    Returns None when `playlist_text` is already a media playlist (no
    #EXT-X-STREAM-INF tags), which is the common case.
    """
    from urllib.parse import urljoin

    lines = playlist_text.splitlines()
    best = None
    best_bw = -1
    for idx, line in enumerate(lines):
        if not _STREAM_INF_RE.match(line.strip()):
            continue
        m = _BANDWIDTH_RE.search(line)
        bw = int(m.group(1)) if m else 0
        for nxt in lines[idx + 1:]:
            nxt = nxt.strip()
            if not nxt or nxt.startswith("#"):
                continue
            if bw >= best_bw:
                best_bw = bw
                best = urljoin(base_url, nxt)
            break
    return best


@router.head("/api/live/stream/{channel_id}")
async def stream_head(channel_id: int, db: Session = Depends(get_db)):
    """HEAD handler for stream URLs — Jellyfin sends HEAD to validate before playing."""
    channel = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "Channel not found")
    return Response(
        status_code=200,
        headers={
            "Content-Type": "video/mp2t",
            "Connection": "close",
            "Cache-Control": "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/api/live/stream/{channel_id}")
async def stream_proxy(channel_id: int, db: Session = Depends(get_db)):
    """Stream proxy for IPTV channels.

    Strategy: resolve the provider's redirect chain (requires TiviMate UA)
    to get the tokenized URL on the real streaming server, then either:
      1. 302 redirect Jellyfin there (if the server serves raw TS), or
      2. Proxy the HLS stream as continuous MPEG-TS bytes (fetch m3u8,
         download chunks, pipe raw bytes).
    """
    # Already pulling this channel? Attach to it instead of opening a second
    # upstream connection for byte-identical data (recording + watching the
    # same channel is the common case). Costs the provider nothing and needs
    # no concurrency slot of its own.
    #
    # Being OPENED right now counts too. The open takes a while (slot wait,
    # redirect chain, 509 backoff) and used to happen outside the lock, so a
    # second client arriving in that window found no entry, took its own slot
    # and opened a second provider connection — two timers on one channel
    # starting the same minute, exactly when the sharing matters. The first
    # arrival registers as the opener; the rest wait for it and attach.
    loop = asyncio.get_running_loop()
    while True:
        async with _get_shared_lock():
            shared = _shared_streams.get(channel_id)
            if shared is not None and not shared._closed:
                q = shared.subscribe()
                logger.info(f"[LiveTV] Channel {channel_id} already streaming — "
                            f"attaching client ({len(shared.subscribers)} now)")
                return _SubscriberResponse(shared, q)
            pending = _pending_opens.get(channel_id)
            if pending is None:
                pending = loop.create_future()
                _pending_opens[channel_id] = pending
                break  # this request opens the channel
        logger.info(f"[LiveTV] Channel {channel_id} is being opened by another client — waiting to attach")
        try:
            await asyncio.wait_for(asyncio.shield(pending), timeout=_PENDING_OPEN_WAIT)
        except Exception:
            pass  # the opener failed or timed out; look again, maybe open it ourselves

    try:
        return await _open_shared_upstream(channel_id, db, pending)
    finally:
        if not pending.done():
            pending.set_result(None)
        async with _get_shared_lock():
            if _pending_opens.get(channel_id) is pending:
                del _pending_opens[channel_id]


async def _open_shared_upstream(channel_id: int, db: Session, pending: "asyncio.Future"):
    """Open the upstream for a channel this request is the first to ask for."""
    # Cap concurrent upstream pulls (see _StreamSlots). A refusal here is how a
    # scheduled recording silently becomes a zero-byte file -- Jellyfin shows the
    # timer as having run -- so it is logged as an error, by channel name, and
    # counted where the dashboard can see it (GET /api/live/capacity).
    limit = _max_concurrent_streams(db)
    ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
    # A recording outranks a viewer at capacity (see _StreamSlots). Whether
    # this pull is one comes from Jellyfin's own timers, or a reservation.
    # When a slot would have to be taken from someone, ask afresh so the
    # victim is chosen on current information (a recording that opened a
    # moment ago may still be classified as a viewer: sync_recordings).
    at_capacity = limit > 0 and _stream_slots.active >= limit
    # Recording protection decides on current information too: whether this
    # open is a recording (refused if not, while one runs), and which running
    # pulls are (the others are stopped for it).
    _stream_slots.set_protect(_protect_recordings(db))
    protect_decides = _stream_slots.protect and _stream_slots.active > 0
    open_began = asyncio.get_running_loop().time()
    recording_ids = await _recording_channel_ids(db, force=at_capacity or protect_decides,
                                                 fresh_after=open_began if protect_decides else None)
    kind = "recording" if channel_id in recording_ids else "live"
    certain = _recording_answer_is_current(since=open_began if protect_decides else None)
    if protect_decides and not certain:
        logger.warning(f"[LiveTV] Recording protection: Jellyfin did not say in time what is being "
                       f"recorded — channel {channel_id} is opened as a {kind} and no viewer is stopped for it")
    try:
        lease = await _stream_slots.acquire_lease(limit, _SLOT_WAIT_SECONDS, kind, f"channel:{channel_id}",
                                                  stream_key=(ch.stream_id or str(ch.id)) if ch else None,
                                                  certain=certain)
    except RecordingProtected:
        raise _TunerRefusal(PROTECTED_REFUSAL_DETAIL)
    if lease is None:
        name = ch.name if ch else f"channel {channel_id}"
        _stream_slots.refused += 1
        _stream_slots.last_refused = {"channel_id": channel_id, "channel": name, "kind": kind,
                                      "at": datetime.utcnow().isoformat() + "Z", "limit": limit}
        logger.error(f"[LiveTV] At capacity ({limit} streams) — REFUSED {kind} '{name}' (channel {channel_id}). "
                     f"If this was a recording it is lost. Raise livetv_max_concurrent_streams "
                     f"(0 = no limit) if this server and provider can carry more.")
        raise _TunerRefusal(f"Too many concurrent live streams (limit {limit})")
    sem = _stream_slots
    # Ownership of the release is handed to the streaming generator on the
    # success paths; on every early-exit / error path below we release here.
    sem_released = False

    def _release_sem():
        nonlocal sem_released
        if not sem_released:
            sem_released = True
            sem.release_lease(lease)

    try:
        channel = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
        if not channel:
            raise HTTPException(404, "Channel not found")

        provider = db.query(Provider).filter(Provider.id == channel.provider_id).first()
        user_agent = (provider.user_agent if provider else None) or "TiviMate/4.7.0 (Linux; Android 12)"
        lease.provider_id, lease.channel_id = channel.provider_id, channel_id
        lease.account = _account_key(provider)

        stream_url = channel.stream_url
        logger.info(f"[LiveTV] Stream request for channel {channel_id} ({channel.name}): {stream_url}")

        # SSRF guard: this endpoint is public, so refuse to fetch anything that
        # resolves to a private/loopback/link-local/metadata address. A provider
        # the admin deliberately configured on a LAN address (a local
        # re-streamer: tuliprox, xTeVe, Threadfin) is the one exception, and
        # only for its own origin -- see services.ssrf.lan_origin_guard (#76).
        # Both resolve DNS with a blocking getaddrinfo: off the event loop, so a
        # resolver that hangs (seconds per lookup in an outage) doesn't freeze
        # every running stream with it. The log says what the name resolved to,
        # or that it didn't resolve: a DNS outage is not an SSRF refusal.
        guard = (await asyncio.to_thread(lan_origin_guard, provider.server_url)) if provider else is_safe_url
        allowed, why = await asyncio.to_thread(explain_url, guard, stream_url)
        if not allowed:
            logger.warning(f"[LiveTV] Blocked stream URL for channel {channel_id} "
                           f"({why or 'non-public host'}): {stream_url}")
            raise HTTPException(502, "Stream URL points to a non-public host")

        upstream = await _stream_proxy_inner(channel_id, user_agent, stream_url,
                                             _release_sem, guard,
                                             failure_budget=_reconnect_budget(db),
                                             is_recording=lambda: lease.kind == "recording",
                                             rival_delivering=lambda: _rival_delivering(lease),
                                             refresh_recordings=_refresh_recordings_bounded)
        if lease.preempted:
            # A recording took this slot while the open was still in flight
            # (there was no pump yet to stop). Going on would run one more
            # upstream than the ceiling allows, uncounted. The body generator
            # has never started, so closing IT runs nothing: the response
            # carries an explicit way to give the connection back.
            close = getattr(upstream, "close_upstream", None)
            if close is not None:
                await close()
            raise _TunerRefusal("A recording needed this connection slot")
        if not isinstance(upstream, StreamingResponse):
            # A raw-TS channel is answered with a redirect; Jellyfin then talks
            # to the provider directly and there is nothing here to share.
            return upstream

        # Become the shared upstream for this channel, so any further client
        # attaches above instead of opening its own provider connection. The
        # concurrency slot is now owned by the shared pump, not by this client.
        shared = _SharedUpstream(channel_id, _release_sem,
                                 close_upstream=getattr(upstream, "close_upstream", None))
        lease.on_preempt = shared.preempt
        q = shared.subscribe()
        _ensure_recording_refresher()
        async with _get_shared_lock():
            _shared_streams[channel_id] = shared
        shared.task = asyncio.create_task(shared._pump(upstream.body_iterator))
        if not pending.done():
            pending.set_result(shared)
        return _SubscriberResponse(shared, q)
    except BaseException:
        _release_sem()
        raise


async def _stream_proxy_inner(channel_id: int, user_agent: str, stream_url: str, _release_sem,
                              guard=None, failure_budget: float = _DEFAULT_RECONNECT_BUDGET,
                              is_recording=None, rival_delivering=None, refresh_recordings=None):
    """Inner stream proxy logic. `_release_sem()` is called when the concurrency
    slot can be freed: immediately on early-exit paths, or by the streaming
    generator's `finally` once the long-lived stream ends.

    `guard` validates every URL fetched from here on -- redirect hops, HLS
    variants and chunks. stream_proxy passes one scoped to the channel's own
    provider (#76); the default holds everything to is_safe_url().

    `failure_budget` is how many seconds of unbroken upstream failure the
    running stream waits out before ending (see _reconnect_budget); 0 means
    for as long as the client keeps reading. `is_recording`, when given, is
    asked at the moment the budget would run out: a recording never gives
    up while its client is attached, whatever the budget (a viewer who is
    cut off changes channel; a recording cut off is gone for good), so the
    budget is a viewer's setting.

    `rival_delivering`, when given, returns the pull on the same account that
    holds it now (or None): an HLS stream refused for _REVIVE_AFTER s
    re-resolves the channel URL only when it returns None (#184). Without
    it nothing is re-resolved on a refusal -- the safe default.
    `refresh_recordings`, when given, is awaited before such a decision so
    it is taken on a fresh answer from Jellyfin about what is recording."""
    guard = guard or is_safe_url

    def _budget_spent(waited: float, extra: float = 0.0) -> bool:
        if not failure_budget or waited + extra <= failure_budget:
            return False
        return not (is_recording is not None and is_recording())
    import httpx

    # One GET opens the stream. _send_checked walks the provider's redirect chain
    # with the required UA, re-validating every hop (#73), and hands back the
    # open response from the tokenized URL on the real streaming server -- whose
    # headers say what it serves, and whose body is the stream (raw TS) or the
    # first playlist (HLS). This used to be up to three GETs of the tokenized
    # URL: one to resolve redirects (body discarded), one to probe the content
    # type, and for raw TS a third to actually stream. Each is a connection in
    # the provider's accounting at the very moment it is most likely to say 509.
    #
    # Streaming GETs only, never client.get(): that buffers the WHOLE body
    # first, which never ends for a channel serving continuous MPEG-TS.
    client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(connect=15.0, read=120.0, write=10.0, pool=15.0),
    )
    resp = None
    try:
        # Same regime as the running HLS worker (#86), on a much shorter leash: a
        # 429/509 here usually clears within seconds, but refusing the tuner costs
        # far more than those seconds -- Jellyfin re-tries a failed open only once
        # a minute, so a recording starts a minute late or not at all. A tuner
        # client is blocked on this response though, so the budget is small, and a
        # status that will never fix itself still fails at once.
        import asyncio
        import random
        loop = asyncio.get_running_loop()
        open_started = loop.time()
        open_backoff = 1.0
        open_slept = 0.0
        open_url = stream_url
        open_reresolved = False
        refused_token_url = None
        while True:
            resp = None
            if refused_token_url is not None:
                # Q5, decided on current information right before the re-walk:
                # a rival that started delivering meanwhile keeps the account.
                if refresh_recordings is not None:
                    await refresh_recordings()
                if rival_delivering() is not None:
                    open_url = refused_token_url
                    logger.info(f"[LiveTV] Opening channel {channel_id}: another stream on this account "
                                f"is delivering now; not resolving the channel URL again")
                refused_token_url = None
            try:
                resp = await _send_checked(client, open_url, {"User-Agent": user_agent}, guard)
                resp.raise_for_status()
                if "mpegurl" in resp.headers.get("content-type", "").lower():
                    # The first playlist, bounded like the variant read below:
                    # headers followed by no body (or a byte now and then) is
                    # waited out as a failed open, not for the client's 120 s
                    # read timeout while the tuner gives up and the slot and a
                    # provider connection stay held.
                    try:
                        playlist_bytes = await asyncio.wait_for(resp.aread(), _OPEN_PLAYLIST_READ)
                    except asyncio.TimeoutError:
                        raise httpx.ReadTimeout(f"the playlist did not arrive within "
                                                f"{_OPEN_PLAYLIST_READ:.0f}s", request=resp.request)
                break
            except httpx.HTTPError as e:
                if resp is not None:
                    # Retry the server that refused, not the whole chain: that
                    # URL has already passed the guard, and re-walking the
                    # redirects would cost the provider an extra request per try.
                    open_url = str(resp.url)
                    await resp.aclose()
                    resp = None
                retryable = (
                    _raw_retryable(e.response.status_code)
                    if isinstance(e, httpx.HTTPStatusError)
                    else isinstance(e, httpx.TransportError)
                )
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 407:
                    # The session this token belongs to has ended (#298):
                    # asking the same token again cannot work, a fresh walk
                    # from the channel URL hands out a new one.
                    open_url = stream_url
                # Whichever is larger: wall clock (slow connects count) or the waits
                # we chose (so the bound holds even if the clock is not advancing).
                waited = max(loop.time() - open_started, open_slept)
                if not retryable or waited + open_backoff > _OPEN_RETRY_BUDGET:
                    logger.error(f"[LiveTV] Tokenized URL failed for channel {channel_id}"
                                 f"{f' after {waited:.0f}s of retries' if waited >= 1 else ''}: {e}")
                    # The status or the error type, not the text: httpx puts the
                    # request URL in it, and for Xtream that path holds the
                    # account (/live/<user>/<pass>/). The log line above is redacted.
                    why = (f"the provider answered {e.response.status_code}"
                           if isinstance(e, httpx.HTTPStatusError) else type(e).__name__)
                    raise HTTPException(502, f"Failed to connect to stream: {why}")
                if (not open_reresolved and open_url != stream_url and rival_delivering is not None
                        and isinstance(e, httpx.HTTPStatusError)
                        and e.response.status_code in _REFUSAL_STATUS
                        and rival_delivering() is None):
                    # The session this open was handed was ended while it
                    # opened (a newer connection took the account): asking
                    # its token URL again answers 509 for good. Resolve the
                    # channel URL once more -- unless a rival on the account
                    # is delivering, as for a running stream (#184, Q5).
                    open_reresolved = True
                    refused_token_url = open_url
                    open_url = stream_url
                    logger.warning(f"[LiveTV] Opening channel {channel_id}: the session was refused "
                                   f"({e.response.status_code}) and nothing else on this account is "
                                   f"delivering; resolving the channel URL once more")
                logger.warning(f"[LiveTV] Opening channel {channel_id} refused "
                               f"(retry in {open_backoff:.0f}s, {waited:.0f}s so far): {e}")
                delay = open_backoff * (0.8 + random.random() * 0.4)
                open_slept += delay
                await asyncio.sleep(delay)
                open_backoff = min(open_backoff * 2, 5.0)

        tokenized_url = str(resp.url)
        logger.info(f"[LiveTV] Resolved tokenized URL for channel {channel_id}: {tokenized_url}")
        placeholder = _placeholder_name(tokenized_url)
        if placeholder:
            await _refuse_placeholder(channel_id, placeholder)

        content_type = resp.headers.get("content-type", "")
        is_hls = "mpegurl" in content_type.lower()

        if is_hls:
            # HLS playlist — read the playlist text, then we're done with this client
            playlist_text = playlist_bytes.decode("utf-8", errors="replace")
            playlist_base = tokenized_url
            # Look at the media playlist before answering: a provider with
            # nothing for the channel serves one that holds only a placeholder
            # segment, and once the response has started it can only end, not
            # fail. The first variant is read here rather than by the worker
            # (the same request, earlier); if it does not come back quickly
            # the worker reads it on its usual retry terms.
            for _hop in range(_MAX_VARIANT_HOPS):
                variant = _select_hls_variant(playlist_text, playlist_base)
                if not variant or not await asyncio.to_thread(guard, variant):
                    break
                placeholder = _placeholder_name(variant)
                if placeholder:
                    await _refuse_placeholder(channel_id, placeholder)
                v_resp = None
                try:
                    v_resp = await asyncio.wait_for(
                        _send_checked(client, variant, {"User-Agent": user_agent}, guard), 10.0)
                    v_resp.raise_for_status()
                    placeholder = _placeholder_name(str(v_resp.url))
                    if placeholder:
                        await _refuse_placeholder(channel_id, placeholder)
                    variant_text = (await asyncio.wait_for(v_resp.aread(), 10.0)).decode(
                        "utf-8", errors="replace")
                except _PlaceholderRefusal:
                    raise
                except Exception as e:
                    # Anything else (a refusal, a blocked redirect) is the
                    # stream's to handle, on the terms it always had.
                    logger.info(f"[LiveTV] Variant for channel {channel_id} not read at open ({e}); "
                                f"the stream reads it")
                    break
                finally:
                    if v_resp is not None:
                        await v_resp.aclose()
                playlist_text, playlist_base = variant_text, variant
            if not _select_hls_variant(playlist_text, playlist_base):
                segments = _media_segments(playlist_text, playlist_base)
                placeholders = [_placeholder_name(u) for u in segments]
                if segments and all(placeholders):
                    await _refuse_placeholder(channel_id, placeholders[0])
    except BaseException:
        if resp is not None:
            await resp.aclose()
        await client.aclose()
        raise

    if is_hls:
        await resp.aclose()
        await client.aclose()
    else:
        # Raw TS or other binary stream — pipe THIS response. The generator takes
        # ownership of it and of the client and cleans both up.
        logger.info(f"[LiveTV] Raw stream (CT: {content_type}) — proxying bytes for channel {channel_id}")
        upstream_ct = _raw_media_type(content_type)
        raw_client, raw_resp = client, resp

        async def stream_generator():
            # A raw stream is one long GET, so the HLS worker's resilience (#86)
            # never reached it: when the provider dropped the connection the
            # stream simply ended, and Jellyfin cut the recording there and
            # started a new file. Reconnect instead -- from the CHANNEL url, since
            # a tokenized URL is often good for one connection only -- on the
            # same terms as the HLS worker: a growing delay, and give up only
            # after an unbroken run of failure. A connection has to last a while
            # to count as a recovery, or a provider that serves a few bytes and
            # hangs up would be re-dialled once a second for ever.
            nonlocal raw_resp
            FAILURE_BUDGET = failure_budget   # 0 = until the client leaves
            HEALTHY_AFTER = 10.0
            backoff = 1.0
            backoff_cap = _BACKOFF_CAP        # grows to _REFUSAL_BACKOFF_CAP after a 429/509
            failing_since = None
            slept = 0.0          # the bound must hold even if the clock stands still
            ua = {"User-Agent": user_agent}
            # MPEG-TS is 188-byte packets. Only whole packets go downstream, so
            # the tail of a packet cut off by a drop never reaches the recording
            # in front of the fresh connection's first sync byte.
            align = "mp2t" in upstream_ct.lower()
            first = True
            status_entry = _status_open(channel_id)
            health = status_entry["health"]
            dropped_at = None   # while re-dialling: when the data stopped
            redialled = False   # this connection came from a re-dial, not yet delivering
            splicer = _ReplaySplicer(channel_id, health)   # #368: a re-dial's replay goes out once
            try:
                while True:
                    opened_at = loop.time()
                    _status_set(channel_id, "streaming")
                    reason = "the provider closed the stream"
                    conn_ct = raw_resp.headers.get("content-type", "")
                    conn_first = True
                    # Batch into ~128 KB pieces ourselves. aiter_bytes(chunk_size=)
                    # does the same, but keeps its partial batch to itself when the
                    # connection breaks -- the last fraction of a second before
                    # every drop would be lost on top of the drop.
                    pending = b""
                    try:
                        last_mark = loop.time()
                        last_piece = None
                        async for piece in _decidable_start(raw_resp.aiter_bytes()):
                            if conn_first:
                                conn_first = False
                                if _looks_like_error_page(conn_ct, piece):
                                    # Not the channel (#299): nothing of it is
                                    # sent, and it is waited out like a refusal.
                                    reason = (f"the provider answered with an error page "
                                              f"({conn_ct or 'no content type'}): "
                                              f"{piece[:120].decode('utf-8', 'replace')!r}")
                                    health["errors"] += 1
                                    backoff_cap = _REFUSAL_BACKOFF_CAP
                                    logger.warning(f"[LiveTV] Raw stream for channel {channel_id}: {reason}")
                                    break
                                k = _ts_sync_offset(piece)
                                if k:
                                    # Started mid-packet: the partial packet in
                                    # front is unusable, and without it the
                                    # stream stays cut on packet boundaries.
                                    piece = piece[k:]
                                if redialled:
                                    # An outage recovered from: counted once
                                    # the fresh connection really delivers.
                                    redialled = False
                                    outage = loop.time() - dropped_at
                                    health["reconnects"] += 1
                                    health["reconnecting_seconds"] += outage
                                    dropped_at = None
                                    if align:
                                        splicer.begin(loop.time(), outage)
                            last_piece = loop.time()
                            if loop.time() - last_mark >= _RAW_MARK_EVERY:
                                # Still delivering: an HLS stream on the same
                                # account must see it as the holder (#184).
                                last_mark = loop.time()
                                _status_set(channel_id, "streaming")
                            if first:
                                first = False
                                align = align and piece[:1] == b"G"
                            if failing_since is not None and loop.time() - opened_at >= HEALTHY_AFTER:
                                failing_since, slept, backoff, backoff_cap = None, 0.0, 1.0, _BACKOFF_CAP
                            pending += splicer.feed(piece, loop.time())
                            if len(pending) >= 131072:
                                cut = len(pending) - (len(pending) % 188) if align else len(pending)
                                out, pending = pending[:cut], pending[cut:]
                                if align:
                                    splicer.sent(out)
                                yield out
                    except httpx.HTTPError as e:
                        reason = str(e) or type(e).__name__
                    pending += splicer.abort()
                    # When it last delivered is when the last bytes came, not the
                    # last periodic mark (up to _RAW_MARK_EVERY earlier): a waiting
                    # HLS stream judges "delivering" by it (fuzz seed 7173).
                    st_now = _stream_status.get(channel_id)
                    if st_now is not None and last_piece is not None:
                        st_now["last_ok"] = max(st_now.get("last_ok") or last_piece, last_piece)
                    # What arrived before the stream stopped still goes out -- whole
                    # packets only; the tail of a cut packet is unusable.
                    cut = len(pending) - (len(pending) % 188) if align else len(pending)
                    if cut:
                        if align:
                            splicer.sent(pending[:cut])
                        yield pending[:cut]
                    # A recovery only if it delivered past HEALTHY_AFTER: an error
                    # page, nothing, or a packet and then silence until the close
                    # is still a failure, or a viewer's budget would never run out.
                    if last_piece is not None and last_piece - opened_at >= HEALTHY_AFTER:
                        failing_since, slept, backoff, backoff_cap = None, 0.0, 1.0, _BACKOFF_CAP
                    if dropped_at is None:      # else: the outage never ended
                        dropped_at = loop.time()
                    redialled = False
                    await raw_resp.aclose()

                    # Re-open, waiting out refusals, until it works or the budget is spent.
                    while True:
                        now = loop.time()
                        if failing_since is None:
                            failing_since = now
                        waited = max(now - failing_since, slept)
                        if _budget_spent(waited, backoff):
                            logger.error(f"[LiveTV] Raw stream for channel {channel_id} could not be "
                                         f"re-established after {waited:.0f}s, stopping: {reason}")
                            return
                        logger.warning(f"[LiveTV] Raw stream for channel {channel_id} dropped "
                                       f"(reconnect in {backoff:.0f}s, {waited:.0f}s so far): {reason}")
                        _status_set(channel_id, "reconnecting", reason)
                        delay = backoff * (0.8 + random.random() * 0.4)
                        slept += delay
                        await asyncio.sleep(delay)
                        backoff = min(backoff * 2, backoff_cap)
                        new_resp = None
                        try:
                            new_resp = await _send_checked(raw_client, stream_url, ua, guard)
                            new_resp.raise_for_status()
                            placeholder = _placeholder_name(str(new_resp.url))
                            if placeholder:
                                raise _ProviderPlaceholder(placeholder)
                            if "mpegurl" in new_resp.headers.get("content-type", "").lower():
                                raise httpx.HTTPError("the channel now answers with a playlist")
                        except _ProviderPlaceholder as e:
                            # Not the channel: waited out like a refusal, and
                            # its bytes never reach the recording (#140).
                            await new_resp.aclose()
                            reason = str(e)
                            backoff_cap = _REFUSAL_BACKOFF_CAP
                            await _note_placeholder(channel_id, e.segment)
                            continue
                        except HTTPException as e:
                            logger.error(f"[LiveTV] Raw stream for channel {channel_id} cannot be "
                                         f"re-opened, stopping: {e.detail}")
                            return
                        except httpx.HTTPError as e:
                            if new_resp is not None:
                                await new_resp.aclose()
                            status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                            if isinstance(e, httpx.TransportError) or _raw_retryable(status):
                                reason = str(e) or type(e).__name__
                                health["errors"] += 1
                                if status in _REFUSAL_STATUS or status == 407:
                                    backoff_cap = _REFUSAL_BACKOFF_CAP
                                continue
                            logger.error(f"[LiveTV] Raw stream for channel {channel_id} cannot be "
                                         f"re-opened, stopping: {e}")
                            return
                        raw_resp = new_resp
                        redialled = True
                        break
            finally:
                if dropped_at is not None:     # ended while still re-dialling
                    health["reconnecting_seconds"] += loop.time() - dropped_at
                    health["ended_on_error"] = True
                _stream_ended(channel_id, status_entry,
                              bool(is_recording is not None and is_recording()))
                _status_clear(channel_id, status_entry)
                await raw_resp.aclose()
                await raw_client.aclose()
                _release_sem()
                logger.info(f"[LiveTV] Stream ended for channel {channel_id}")

        response = StreamingResponse(
            stream_generator(),
            media_type=upstream_ct,
            headers={
                "Connection": "close",
                "Cache-Control": "no-cache, no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )

        async def close_upstream():
            """Give the connection back without streaming. aclose() on a
            generator that has not started runs nothing -- not its finally --
            so a caller that must abandon an opened stream calls this."""
            try:
                await raw_resp.aclose()
                await raw_client.aclose()
            finally:
                _release_sem()
        response.close_upstream = close_upstream
        return response

    logger.info(f"[LiveTV] HLS stream for channel {channel_id} — proxying chunks as MPEG-TS")

    health = _new_health()

    async def hls_to_mpegts():
        """Wrapper that releases the concurrency slot once the stream ends."""
        status_entry = _status_open(channel_id, health)
        try:
            async for chunk in _hls_worker():
                yield chunk
        finally:
            _stream_ended(channel_id, status_entry,
                          bool(is_recording is not None and is_recording()), hls=True)
            _status_clear(channel_id, status_entry)
            _release_sem()
            logger.info(f"[LiveTV] Stream ended for channel {channel_id}")

    async def _hls_worker():
        """Continuously fetch the HLS playlist and pipe chunk data as raw MPEG-TS."""
        import asyncio
        import random
        seen_chunks: set[str] = set()
        yielded_any = False
        last_seq = None     # media sequence number of the last segment sent
        last_disc = None    # and the discontinuity sequence it was sent under
        last_payload_hash = None   # sha1 of the last segment sent (re-resolve dedupe)
        current_playlist = playlist_text
        current_base = playlist_base
        ua_headers = {"User-Agent": user_agent}
        # An Xtream provider answers 429/509 while the account's connection
        # allowance is momentarily saturated -- two recordings whose short HLS
        # requests collide, say -- and is fine again a second later. Counting
        # those toward a six-strike limit ended the stream after well under a
        # minute of squeeze, and Jellyfin turned that into a truncated
        # recording plus a new file. So wait transient failures out on a
        # growing delay and give up only after an unbroken run of them; a
        # status that will never fix itself still stops the stream at once.
        # Which statuses are transient: _raw_retryable (see _is_retryable).
        FAILURE_BUDGET = failure_budget   # seconds of unbroken failure; 0 = until the client leaves
        BACKOFF_START = 1.0
        backoff_cap = _BACKOFF_CAP   # short while the tuner reader waits; longer after a 429/509
        CHUNK_RETRIES_IN_PLACE = 3
        # How long this stream's playlist and segment bodies may take (_aread_within).
        playlist_stretch, segment_stretch = {"x": 1.0}, {"x": 1.0}
        # A segment that will never arrive (404 / 410 / 403 on ONE chunk) is
        # skipped, not fatal: panels routinely 404 a segment that is not written
        # yet or has just expired, and ending the stream there is the truncated
        # recording #86 exists to prevent. A run of them is a dead stream.
        MAX_FATAL_CHUNK_SKIPS = 10
        fatal_chunk_skips = 0
        failing_since = None
        backoff = BACKOFF_START
        # guard() resolves the host with a blocking getaddrinfo. Called for
        # every playlist line on every reload it would put N lookups on every
        # stream every few seconds. The answer depends only on the origin, so
        # resolve each origin once for the life of this stream, in a worker
        # thread: a resolver that hangs must not freeze every stream (#464).
        from urllib.parse import urlparse as _urlparse
        _origin_verdicts: dict = {}

        async def line_guard(url: str) -> bool:
            try:
                p = _urlparse(url)
                key = (p.scheme, (p.hostname or "").lower(), p.port)
            except ValueError:
                return await asyncio.to_thread(guard, url)
            if key not in _origin_verdicts:
                _origin_verdicts[key] = await asyncio.to_thread(guard, url)
            return _origin_verdicts[key]
        # When the playlist now in hand was read; reloads are timed from here.
        playlist_loaded_at = asyncio.get_running_loop().time()

        # A playlist that holds nothing but a placeholder is the provider
        # saying "not now": waited out like a 509, never fetched or recorded.
        in_placeholder = False

        def _is_retryable(exc) -> bool:
            if isinstance(exc, _ProviderPlaceholder):
                return True
            if isinstance(exc, httpx.HTTPStatusError):
                # The same rule as the open and the raw re-dial: any 5xx,
                # including 513 and Cloudflare's 520-524, is waited out. Ending
                # on one cut running recordings for good (#298 on the raw path).
                return _raw_retryable(exc.response.status_code)
            # Timeouts, resets and refused connections are all worth another go.
            return isinstance(exc, httpx.TransportError)

        def _note_success(real_data: bool = False):
            nonlocal failing_since, backoff, backoff_cap, in_placeholder
            if in_placeholder and not real_data:
                # A playlist that answers is not the channel coming back while
                # it still holds only the placeholder: the spell, and its
                # failure budget, run on until a real segment arrives.
                return
            in_placeholder = False
            if failing_since is not None:
                health["reconnecting_seconds"] += asyncio.get_running_loop().time() - failing_since
                health["reconnects"] += 1   # an outage recovered from
            failing_since = None
            health.pop("_failing_since", None)
            backoff = BACKOFF_START
            backoff_cap = _BACKOFF_CAP
            _status_set(channel_id, "streaming")

        def _note_failure(exc, what: str) -> bool:
            """Record a failure. True means the stream should stop."""
            nonlocal failing_since, backoff_cap
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in _REFUSAL_STATUS:
                backoff_cap = _REFUSAL_BACKOFF_CAP
            if not _is_retryable(exc):
                logger.error(f"[LiveTV] {what} failed fatally for channel "
                             f"{channel_id}, stopping: {exc}")
                health["errors"] += 1
                health["ended_on_error"] = True
                return True
            now = asyncio.get_running_loop().time()
            health["errors"] += 1
            if failing_since is None:
                failing_since = now
                health["_failing_since"] = now   # an outage still open when the stream ends
            waited = now - failing_since
            if _budget_spent(waited):
                logger.error(f"[LiveTV] {what} still failing after {waited:.0f}s "
                             f"for channel {channel_id}, stopping: {exc}")
                health["reconnecting_seconds"] += waited
                health.pop("_failing_since", None)
                health["ended_on_error"] = True
                return True
            logger.warning(f"[LiveTV] {what} failed for channel {channel_id} "
                           f"(retry in {backoff:.0f}s, {waited:.0f}s so far): {exc}")
            _status_set(channel_id, "reconnecting", f"{what}: {exc}")
            return False

        async def _backoff_sleep():
            nonlocal backoff
            # Jitter so several streams that were squeezed at the same moment
            # don't all come back at the same moment and squeeze it again.
            await asyncio.sleep(backoff * (0.8 + random.random() * 0.4))
            backoff = min(backoff * 2, backoff_cap)

        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        ) as hls_client:
            placeholder_noted = False

            async def _placeholder_refusal(name: str, where: str) -> bool:
                """A re-resolve that landed on a stand-in clip (#140). True
                means end the stream: nothing sent yet, or the budget is
                spent; else it is waited out like a refusal."""
                nonlocal placeholder_noted, backoff_cap
                if not placeholder_noted:
                    placeholder_noted = True
                    try:
                        await _note_placeholder(channel_id, name)
                    except Exception as e:   # reporting must never end the stream
                        logger.debug(f"[LiveTV] Could not report the placeholder for channel {channel_id}: {e}")
                if not yielded_any:
                    return True
                stop = _note_failure(_ProviderPlaceholder(name), where)
                backoff_cap = _REFUSAL_BACKOFF_CAP
                return stop

            reresolve_run = 0          # re-resolves since a segment last arrived
            after_reresolve = False    # the playlist in hand came from a fresh resolve
            refused_since = None       # first 429/509 of the current refresh run (#184)
            revive = {"last": None, "gap": _REVIVE_COOLDOWN, "held": False, "flow_since": None}
            gone_since = None          # first 404/410 of the current run (recordings)
            stream_answer_since = None # first non-playlist answer of the channel URL

            def _token_expired(exc) -> bool:
                return (isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code in _TOKEN_EXPIRED_STATUS)

            async def _resolve_channel_playlist():
                """GET the channel URL as the open did: (playlist text, final
                URL). Raises what the request raised, _ProviderPlaceholder,
                or _NotAPlaylist for a continuous stream -- never read as a
                playlist: it has no end."""
                r = await _send_checked(hls_client, stream_url, ua_headers, guard)
                try:
                    placeholder = _placeholder_name(str(r.url))
                    if placeholder:
                        raise _ProviderPlaceholder(placeholder)
                    r.raise_for_status()
                    if "mpegurl" not in r.headers.get("content-type", "").lower():
                        raise _NotAPlaylist("the channel now answers with a stream, not a playlist")
                    body = await _aread_within(r, _HLS_PLAYLIST_READ, playlist_stretch)
                    return body.decode("utf-8", errors="replace"), str(r.url)
                finally:
                    await r.aclose()

            async def _maybe_revive(exc) -> str:
                """#184: after _REVIVE_AFTER s of unbroken refusals on the
                playlist refresh, the session may be gone for good. Resolve
                the channel URL again -- unless a rival on this account is
                delivering (it holds the account; taking it back would kick it
                and it would kick back). "ok" = a fresh playlist is in hand,
                "stop" = end the stream, "" = go on waiting."""
                nonlocal current_playlist, current_base, playlist_loaded_at
                nonlocal after_reresolve, backoff_cap
                if rival_delivering is None or refused_since is None:
                    return ""
                now = asyncio.get_running_loop().time()
                if now - refused_since < _REVIVE_AFTER:
                    return ""
                if revive["last"] is not None and now - revive["last"] < revive["gap"]:
                    return ""
                rival = rival_delivering()
                if rival is None and refresh_recordings is not None:
                    # Decide on current information: a pull that became a
                    # recording since the last lookup (a recording joining a
                    # channel a viewer had open) is a rival (fuzz seed 157).
                    await refresh_recordings()
                    rival = rival_delivering()
                if rival is not None:
                    what = "recording" if rival.kind == "recording" else "stream"
                    st = _stream_status.get(channel_id)
                    if st is not None:
                        st["waiting_for"] = f"another {what} on this account is delivering"
                    if not revive["held"]:
                        revive["held"] = True
                        logger.warning(f"[LiveTV] Channel {channel_id}: refused for {now - refused_since:.0f}s "
                                       f"while {rival.kind} '{rival.owner}' on the same account is "
                                       f"delivering; waiting rather than taking the account from it")
                    return ""
                st = _stream_status.get(channel_id)
                if st is not None:
                    # Claimed in the same loop turn as the check above, so a
                    # second waiting stream sees this one as the holder.
                    st["reviving_at"] = asyncio.get_running_loop().time()
                if revive["last"] is not None:
                    revive["gap"] = min(revive["gap"] * 2, _REVIVE_COOLDOWN_CAP)
                revive["last"] = now
                health["revives"] = health.get("revives", 0) + 1
                logger.warning(f"[LiveTV] Channel {channel_id}: refused for {now - refused_since:.0f}s and "
                               f"nothing else on this account is delivering -- the session may be gone; "
                               f"resolving the channel URL again (next attempt no sooner than "
                               f"{revive['gap']:.0f}s)")
                try:
                    text, final = await _resolve_channel_playlist()
                except _ProviderPlaceholder as e:
                    return "stop" if await _placeholder_refusal(e.segment, "Re-resolving after refusals") else ""
                except Exception as e:
                    # Counted like the refusal it answers: within a viewer's
                    # budget, for as long as a recording stays attached.
                    stop = _note_failure(httpx.TransportError(
                        f"Re-resolving after refusals: {e}"), "Re-resolving after refusals")
                    backoff_cap = _REFUSAL_BACKOFF_CAP
                    return "stop" if stop else ""
                current_playlist, current_base = text, final
                playlist_loaded_at = asyncio.get_running_loop().time()
                after_reresolve = True
                st = _stream_status.get(channel_id)
                if st is not None:
                    st.pop("waiting_for", None)
                logger.info(f"[LiveTV] Channel {channel_id}: re-resolved the channel URL after refusals, "
                            f"continuing the same stream")
                return "ok"

            async def _reresolve(what: str, exc) -> bool:
                """The tokenized URL expired: resolve the channel URL again,
                exactly as the open did, and carry on in the SAME response.
                Counted as an interruption, inside the viewer's budget (for
                a recording: for as long as it stays attached). True means
                stop."""
                nonlocal reresolve_run, after_reresolve, current_playlist
                nonlocal current_base, playlist_loaded_at, backoff_cap, stream_answer_since
                loop_now = asyncio.get_running_loop().time
                reresolve_run += 1
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                counted = True
                if _gone_grace(status):
                    # A 404/410 blip on a recording: not the account refusing
                    # yet (see _gone_grace); waited out on the refusal cap.
                    reresolve_run -= 1
                    counted = False
                    backoff_cap = _REFUSAL_BACKOFF_CAP
                if reresolve_run > _MAX_RERESOLVE:
                    logger.error(f"[LiveTV] {what} for channel {channel_id} still answers {status} after "
                                 f"{_MAX_RERESOLVE} fresh resolves of the channel URL, stopping: {exc}")
                    health["ended_on_error"] = True
                    return True
                if _note_failure(httpx.TransportError(f"{what}: {status} — the stream URL expired"), what):
                    return True
                logger.warning(f"[LiveTV] {what} for channel {channel_id} answered {status} — the "
                               f"tokenized URL has expired; re-resolving the channel URL "
                               f"({reresolve_run}/{_MAX_RERESOLVE})")
                if reresolve_run > 1 or not counted:
                    await _backoff_sleep()
                try:
                    text, final = await _resolve_channel_playlist()
                except _ProviderPlaceholder as e:
                    # Not an answer about the token: does not count toward
                    # _MAX_RERESOLVE (a recording must ride it out) -- but it
                    # is waited out like a refusal, never re-asked at once.
                    reresolve_run -= 1
                    if await _placeholder_refusal(e.segment, what):
                        return True
                    await _backoff_sleep()
                    return False
                except Exception as e:
                    if _token_expired(e):
                        # counted: the next failure resolves again, up to the limit
                        st = e.response.status_code
                        if st in (401, 403, 407):
                            backoff_cap = _REFUSAL_BACKOFF_CAP
                        if st == 407 and is_recording is not None and is_recording():
                            # The channel URL itself answered 407: an ended
                            # session while the token is renewed, not a refused
                            # login. Not counted for a recording; waited out on
                            # the refusal cap, as the raw path does (#298).
                            reresolve_run -= 1
                            await _backoff_sleep()
                            return False
                        if counted and _gone_grace(st):
                            reresolve_run -= 1
                            backoff_cap = _REFUSAL_BACKOFF_CAP
                            await _backoff_sleep()
                        return False
                    if isinstance(e, _NotAPlaylist):
                        # The channel turned into a continuous stream. For a
                        # recording, waiting for ever records nothing until the
                        # timer ends; after _STREAM_ANSWER_LIMIT of this, end,
                        # so Jellyfin re-opens (the raw path takes the stream)
                        # and the content lands in a second file instead.
                        now = loop_now()
                        if stream_answer_since is None:
                            stream_answer_since = now
                        elif (is_recording is not None and is_recording()
                              and now - stream_answer_since >= _STREAM_ANSWER_LIMIT):
                            logger.error(f"[LiveTV] Channel {channel_id}: the channel URL has answered with a "
                                         f"continuous stream instead of a playlist for "
                                         f"{now - stream_answer_since:.0f}s, ending so the recording re-opens "
                                         f"on it")
                            health["ended_on_error"] = True
                            return True
                    # A 509, a transport error, a stream instead of a playlist:
                    # the provider being busy, not the account refusing.
                    reresolve_run -= 1
                    if _note_failure(e, "Re-resolving the stream URL"):
                        return True
                    await _backoff_sleep()
                    return False
                stream_answer_since = None
                current_playlist, current_base = text, final
                playlist_loaded_at = asyncio.get_running_loop().time()
                after_reresolve = True
                logger.info(f"[LiveTV] Channel {channel_id}: re-resolved the channel URL, "
                            f"continuing the same stream")
                return False

            def _gone_grace(status) -> bool:
                """True while a 404/410 on a RECORDING should not yet count
                toward _MAX_RERESOLVE: a panel blip answers 404 for seconds,
                and three quick 1-2 s re-resolves ended a recording over a
                7.5 s blip. It counts only after _GONE_GRACE of unbroken
                404/410 (a channel really removed). Viewers keep the 3-try
                rule; 401/403/407 are never graced here -- a 407 from the
                channel URL on a recording is waited out in _reresolve
                instead (keyed on the channel URL's answer: the token URL's
                407 that triggers every re-resolve must not be)."""
                nonlocal gone_since
                if status not in (404, 410) or not (is_recording is not None and is_recording()):
                    return False
                now = asyncio.get_running_loop().time()
                if gone_since is None:
                    gone_since = now
                return now - gone_since < _GONE_GRACE

            while True:
                # Master playlist? Follow the best variant before treating any
                # line as a media segment. Each hop goes through the same
                # checked sender as everything else (#73): a variant behind a
                # CDN redirect is normal, and every hop is re-validated.
                variant_retry = False
                for hop in range(_MAX_VARIANT_HOPS + 1):
                    variant = _select_hls_variant(current_playlist, current_base)
                    if not variant:
                        break
                    if hop == _MAX_VARIANT_HOPS:
                        logger.error(f"[LiveTV] Too many HLS variant hops for channel {channel_id}")
                        return
                    if not await line_guard(variant):
                        logger.warning(
                            f"[LiveTV] Blocked HLS variant on non-public host for "
                            f"channel {channel_id}: {variant}")
                        return
                    logger.info(f"[LiveTV] Master playlist for channel {channel_id} — following variant")
                    try:
                        v_resp = await _send_checked(hls_client, variant, ua_headers, guard)
                        try:
                            v_resp.raise_for_status()
                            variant_text = (await _aread_within(v_resp, _HLS_PLAYLIST_READ, playlist_stretch)).decode(
                                "utf-8", errors="replace")
                        finally:
                            await v_resp.aclose()
                    except Exception as e:
                        if _token_expired(e):
                            if await _reresolve("Variant playlist fetch", e):
                                return
                            variant_retry = True
                            break
                        # Same retry regime as chunks and refreshes (#86): a 509
                        # here is the provider being momentarily busy, not a
                        # reason to end a recording.
                        if _note_failure(e, "Variant playlist fetch"):
                            return
                        await _backoff_sleep()
                        variant_retry = True
                        break
                    _note_success()
                    current_base = variant
                    current_playlist = variant_text
                    playlist_loaded_at = asyncio.get_running_loop().time()

                if variant_retry:
                    # Still holding a master playlist: its lines are variant
                    # URIs, not segments, so re-read it rather than falling
                    # through and piping playlist text out as video (#68).
                    continue

                # Parse chunk URLs from playlist
                lines = current_playlist.splitlines()
                chunk_urls = []
                chunk_seq: dict = {}         # chunk URL -> media sequence number
                media_seq = None             # #EXT-X-MEDIA-SEQUENCE, when the playlist has one
                disc_seq = None              # #EXT-X-DISCONTINUITY-SEQUENCE, when it has one
                uri_index = 0
                target_duration = 5  # default segment length
                is_live = "#EXT-X-ENDLIST" not in current_playlist

                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("#EXT-X-TARGETDURATION:"):
                        try:
                            target_duration = int(stripped.split(":")[1])
                        except (ValueError, IndexError):
                            pass
                    elif stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                        try:
                            media_seq = int(stripped.split(":")[1])
                        except (ValueError, IndexError):
                            pass
                    elif stripped.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                        try:
                            disc_seq = int(stripped.split(":")[1])
                        except (ValueError, IndexError):
                            pass
                    elif stripped and not stripped.startswith("#"):
                        chunk_url = urljoin(current_base, stripped)
                        if media_seq is not None:
                            chunk_seq[chunk_url] = media_seq + uri_index
                        uri_index += 1
                        if not await line_guard(chunk_url):
                            logger.warning(f"[LiveTV] Skipping HLS chunk on non-public host for channel {channel_id}: {chunk_url}")
                            continue
                        chunk_urls.append(chunk_url)

                # Placeholder segments are never fetched. A playlist of nothing
                # else means the channel is unavailable right now (#140).
                placeholder_segments = [u for u in chunk_urls if _placeholder_name(u)]
                if placeholder_segments:
                    chunk_urls = [u for u in chunk_urls if not _placeholder_name(u)]
                    if not any(u not in seen_chunks for u in chunk_urls):
                        segment = _placeholder_name(placeholder_segments[0])
                        if not in_placeholder:
                            in_placeholder = True
                            await _note_placeholder(channel_id, segment)
                        if not is_live:
                            if not (is_recording is not None and is_recording()):
                                logger.error(f"[LiveTV] Channel {channel_id}: the provider ended the "
                                             f"stream with a placeholder ({segment}), stopping")
                                return
                            # A recording that ends here is filed by Jellyfin as
                            # complete and never retried: the rest of the event is
                            # lost. Wait it out as for a live placeholder, and ask
                            # the CHANNEL url again -- this playlist has ended, so
                            # re-reading it would only repeat the placeholder.
                            if _note_failure(_ProviderPlaceholder(segment), "Channel"):
                                return
                            backoff_cap = _REFUSAL_BACKOFF_CAP
                            await _backoff_sleep()
                            try:
                                current_playlist, current_base = await _resolve_channel_playlist()
                                playlist_loaded_at = asyncio.get_running_loop().time()
                            except _ProviderPlaceholder:
                                pass        # still the placeholder: wait again
                            except Exception as e:
                                if _note_failure(e, "Re-resolving after a placeholder"):
                                    return
                            continue
                        if _note_failure(_ProviderPlaceholder(segment), "Channel"):
                            return
                        await _backoff_sleep()

                if after_reresolve and last_seq is not None and chunk_seq:
                    # A fresh token renames every segment URL, so segments
                    # already sent come back as "new". Skip them by media
                    # sequence -- only when the new window straddles the last
                    # segment sent, i.e. the numbering really continues. A
                    # restarted numbering (a window that does not reach the
                    # last one sent, or a changed discontinuity sequence) is
                    # left alone: better a repeat than a hole.
                    lo, hi = min(chunk_seq.values()), max(chunk_seq.values())
                    same_disc = last_disc is None or disc_seq is None or last_disc == disc_seq
                    if same_disc and lo <= last_seq + 1 <= hi + 1:
                        # The numbers line up -- but a server whose counter
                        # moved by one would lose a real segment per re-resolve
                        # to a skip by number. Where the new window holds a
                        # segment numbered like the last one sent, fetch it and
                        # skip only if it IS that segment (same bytes).
                        probe = next((u for u, n in chunk_seq.items() if n == last_seq), None)
                        same = probe is None   # nothing to compare: nothing <= last_seq either
                        if probe is not None and last_payload_hash is not None:
                            try:
                                pr = await _send_checked(hls_client, probe, ua_headers, guard)
                                try:
                                    pr.raise_for_status()
                                    body = await _aread_within(pr, _segment_read_limit(target_duration),
                                                               segment_stretch)
                                    same = hashlib.sha1(body).digest() == last_payload_hash
                                finally:
                                    await pr.aclose()
                            except Exception as e:
                                logger.debug(f"[LiveTV] Could not compare the segment after a re-resolve "
                                             f"for channel {channel_id}: {e}")
                        if same:
                            for u, n in chunk_seq.items():
                                if n <= last_seq:
                                    seen_chunks.add(u)
                        else:
                            logger.info(f"[LiveTV] Channel {channel_id}: the fresh playlist's numbering does "
                                        f"not continue the old one; sending its window again rather than "
                                        f"risking a hole")
                    elif (last_disc is not None and disc_seq is not None and last_disc == disc_seq
                          and lo > last_seq + 1):
                        # The window moved on while the URL was re-resolved:
                        # those segments are gone for good. Say so -- only when
                        # the discontinuity sequence proves it is one numbering,
                        # and never more than a window.
                        health["segments_skipped"] += min(lo - last_seq - 1, len(chunk_seq))
                    after_reresolve = False
                restart = False

                # Fetch new chunks
                got_new = False
                chunk_pending = False
                for chunk_url in chunk_urls:
                    if chunk_url in seen_chunks:
                        continue
                    # A live playlist is a short sliding window. Going back to it
                    # after a refused chunk costs the backoff, what is left of the
                    # reload wait AND a playlist refresh that can be refused
                    # too -- long enough for the chunk to roll out of the window
                    # and leave a hole. Its URL is still good, so try it again in
                    # place a few times first.
                    payload = None
                    for attempt in range(CHUNK_RETRIES_IN_PLACE + 1):
                        try:
                            chunk_resp = await _send_checked(hls_client, chunk_url, ua_headers, guard)
                            try:
                                # A segment URL can redirect to the placeholder: its
                                # bytes are black, not the channel, and are never
                                # written (#140). Waited out like one in the playlist.
                                placeholder = _placeholder_name(str(chunk_resp.url))
                                if placeholder:
                                    raise _ProviderPlaceholder(placeholder)
                                chunk_resp.raise_for_status()
                                payload = await _aread_within(chunk_resp, _segment_read_limit(target_duration),
                                                              segment_stretch)
                            finally:
                                await chunk_resp.aclose()
                            break
                        except Exception as e:
                            chunk_error = e
                            if isinstance(e, _ProviderPlaceholder):
                                if not in_placeholder:
                                    in_placeholder = True
                                    await _note_placeholder(channel_id, e.segment)
                                break   # it will not turn into the channel in a second
                            if (attempt == CHUNK_RETRIES_IN_PLACE or _token_expired(e)
                                    or not _is_retryable(e)):
                                break
                            if _note_failure(e, "Chunk fetch"):
                                return
                            await _backoff_sleep()
                    if payload is None:
                        if (_token_expired(chunk_error)
                                and chunk_error.response.status_code in (401, 403, 407)):
                            # The token behind the segment URLs was refused:
                            # not a lost segment. Re-resolve and re-read. (A
                            # 404/410 segment is one that left the window,
                            # skipped as before.)
                            if await _reresolve("Chunk fetch", chunk_error):
                                return
                            restart = True
                            break
                        if not _is_retryable(chunk_error):
                            fatal_chunk_skips += 1
                            health["segments_skipped"] += 1
                            seen_chunks.add(chunk_url)
                            if fatal_chunk_skips > MAX_FATAL_CHUNK_SKIPS:
                                logger.error(f"[LiveTV] {fatal_chunk_skips} segments in a row are gone for "
                                             f"channel {channel_id}, stopping: {chunk_error}")
                                health["ended_on_error"] = True
                                return
                            logger.warning(f"[LiveTV] Segment gone for channel {channel_id} "
                                           f"({fatal_chunk_skips} in a row), skipping it: {chunk_error}")
                            continue
                        if _note_failure(chunk_error, "Chunk fetch"):
                            return
                        # Deliberately NOT marked seen: a chunk lost to a
                        # transient error is still in the next playlist, and
                        # dropping it silently puts a hole in the recording.
                        # Stop here so chunks stay in order, wait, re-read the
                        # playlist and try this one again.
                        await _backoff_sleep()
                        chunk_pending = True
                        break
                    seen_chunks.add(chunk_url)
                    got_new = True
                    fatal_chunk_skips = 0
                    yielded_any = True
                    reresolve_run, after_reresolve, gone_since = 0, False, None
                    refused_since = None
                    # The cooldown resets only after _REVIVE_RESET_AFTER of
                    # unbroken delivery (Q3); a failure run restarts the clock.
                    t_now = asyncio.get_running_loop().time()
                    if failing_since is not None or revive["flow_since"] is None:
                        revive["flow_since"] = t_now
                    revive["held"] = False
                    if (revive["last"] is not None
                            and t_now - revive["flow_since"] >= _REVIVE_RESET_AFTER):
                        revive.update(last=None, gap=_REVIVE_COOLDOWN)
                    last_payload_hash = hashlib.sha1(payload).digest()
                    if chunk_url in chunk_seq:
                        last_seq, last_disc = chunk_seq[chunk_url], disc_seq
                    yield payload
                    _note_success(real_data=True)

                if restart:
                    continue        # the re-resolved playlist is in hand

                if not is_live:
                    # VOD-style playlist — we're done after all chunks
                    return

                # Live stream: wait and re-fetch playlist for new chunks.
                #
                # One reload per segment, as RFC 8216 6.3.4 has it: a playlist
                # that brought something new is good for a whole target
                # duration; only one that brought nothing (or left a chunk
                # still owed) is asked for again after half of one. Reloading
                # every half segment regardless doubled this stream's request
                # rate against the provider's connection accounting for no
                # gain -- every other reload is unchanged by construction.
                # The wait is timed from when the playlist was READ, so time
                # spent downloading chunks or backing off is not added on top
                # and a slow pass cannot let segments roll out of the window.
                reload_after = target_duration if (got_new and not chunk_pending) else target_duration / 2
                remaining = playlist_loaded_at + reload_after - asyncio.get_running_loop().time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                try:
                    pl_resp = await _send_checked(hls_client, current_base, ua_headers, guard)
                    try:
                        pl_resp.raise_for_status()
                        refreshed = (await _aread_within(pl_resp, _HLS_PLAYLIST_READ, playlist_stretch)).decode(
                            "utf-8", errors="replace")
                    finally:
                        await pl_resp.aclose()
                except Exception as e:
                    if _token_expired(e):
                        refused_since = None
                        if await _reresolve("Playlist refresh", e):
                            return
                        continue
                    if _note_failure(e, "Playlist refresh"):
                        return
                    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in _REFUSAL_STATUS:
                        if refused_since is None:
                            refused_since = asyncio.get_running_loop().time()
                        rv = await _maybe_revive(e)
                        if rv == "stop":
                            return
                        if rv == "ok":
                            continue
                    else:
                        refused_since = None
                    await _backoff_sleep()
                    continue
                current_playlist = refreshed
                playlist_loaded_at = asyncio.get_running_loop().time()
                refused_since = None
                _note_success()

    response = StreamingResponse(
        hls_to_mpegts(),
        media_type="video/mp2t",
        headers={
            "Connection": "close",
            "Cache-Control": "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )

    async def close_upstream():
        # The playlist client is already closed; only the slot is held.
        _release_sem()
    response.close_upstream = close_upstream
    return response


def _m3u_text(text) -> str:
    """One M3U field: a CR or LF in a name would start a new line, and an M3U
    tuner would read what follows as another channel (#260)."""
    return re.sub(r"[\r\n]+", " ", str(text)) if text else ""


@router.get("/api/live/playlist.m3u")
def live_playlist_m3u(request: Request, db: Session = Depends(get_db)):
    """Generate M3U playlist for Jellyfin M3U tuner import.
    All stream URLs point to our local proxy (like Threadfin's direct mode)."""
    channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.enabled == True)
        .order_by(LiveChannel.sort_order, LiveChannel.channel_number, LiveChannel.name)
        .all()
    )

    base_url = str(request.base_url).rstrip("/")
    lines = ["#EXTM3U"]
    for ch in channels:
        number = ch.stream_id or str(ch.id)
        epg_id = ch.guide_epg_id or f"tentacle-{ch.id}"
        logo = f' tvg-logo="{_m3u_text(ch.logo_url)}"' if ch.logo_url else ""
        group = f' group-title="{_m3u_text(ch.group_title)}"' if ch.group_title else ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{_m3u_text(epg_id)}" tvg-chno="{_m3u_text(number)}"{logo}{group},'
            f'{_m3u_text(ch.guide_name)}'
        )
        lines.append(f"{base_url}/api/live/stream/{ch.id}")

    # The same YouTube channels the HDHomeRun lineup carries — a user who set
    # Tentacle up as an M3U tuner gets the same channel list either way.
    for yt in youtube_livetv.live_channels(db):
        logo = f' tvg-logo="{_m3u_text(yt["logo_url"])}"' if yt["logo_url"] else ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{yt["guide_number"]}" tvg-chno="{yt["guide_number"]}"'
            f'{logo} group-title="{_m3u_text(yt["group_title"])}",{_m3u_text(yt["name"])}'
        )
        lines.append(f"{base_url}/api/youtube/live/{yt['youtube_channel_id']}/stream.ts")

    content = "\n".join(lines) + "\n"
    return Response(
        content=content,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": "inline; filename=tentacle.m3u"},
    )


def _emit_sub_titles(db) -> bool:
    """Whether the served guide carries each programme's <sub-title> (setting
    `livetv_emit_subtitles`, default off). Jellyfin 10.11 treats a programme
    with an episode title as a series: its recordings are named
    "<title> - <sub-title>" and get tvshow/episodedetails NFOs instead of a
    <movie> one, so serving sub-titles by default would re-file every recording
    of a feed that sends them (#148). They are always stored."""
    return (get_setting(db, "livetv_emit_subtitles", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _provider_hosts(db, channels) -> "set[str]":
    """Hosts that belong to a Live TV provider: its server, its guide (EPG)
    URL and its channels' stream hosts."""
    from urllib.parse import urlparse
    hosts = set()
    providers = live_tv_providers(db)
    for url in ([p.server_url for p in providers] + [getattr(p, "epg_url", None) for p in providers]
                + [ch.stream_url for ch in channels]):
        try:
            host = urlparse(url or "").hostname
        except ValueError:
            host = None
        if host:
            hosts.add(host.lower())
    return hosts


def _third_party_icon(url: Optional[str], provider_hosts: "set[str]") -> Optional[str]:
    """A programme icon the provider hosts itself is dropped: Jellyfin fetches
    it as a recording starts, one more connection to a panel that may be
    carrying that recording (#147). Art from elsewhere (TMDB, YouTube) is kept."""
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None     # only web art: never a file:, data: or other scheme
    return None if host in provider_hosts else url


@router.get("/hdhr/xmltv.xml")
@router.get("/api/live/xmltv.xml")
def hdhr_xmltv(db: Session = Depends(get_db)):
    """Serve XMLTV guide data for enabled channels."""
    from services.xmltv import iter_xmltv

    # Get enabled channels with EPG IDs
    channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.enabled == True)
        .order_by(LiveChannel.sort_order, LiveChannel.channel_number, LiveChannel.name)
        .all()
    )

    # Use stream_id as stable channel ID (same as lineup.json GuideNumber)
    # This ensures IDs never shift when channels are added/removed.
    xmltv_channels = []
    epg_ids = set()
    # One EPG ID can map to multiple channels (e.g. CP24 HD + CP24 HD BACKUP)
    epg_id_to_guide_numbers: dict[str, list[str]] = {}
    guide_number_group: dict[str, str] = {}
    for ch in channels:
        guide_number = str(ch.stream_id or ch.id)
        xmltv_channels.append({
            "id": guide_number,
            "name": ch.guide_name,
            "logo_url": ch.logo_url,
        })
        # Channel group feeds the category inference below when a programme
        # title says nothing about its genre.
        guide_number_group[guide_number] = ch.group_title
        if ch.guide_epg_id:
            epg_ids.add(ch.guide_epg_id)
            epg_id_to_guide_numbers.setdefault(ch.guide_epg_id, []).append(guide_number)

    # YouTube Live TV channels, with their own guide ids.
    for yt in youtube_livetv.live_channels(db):
        xmltv_channels.append({
            "id": yt["guide_number"],
            "name": yt["name"],
            "logo_url": yt["logo_url"],
        })
        guide_number_group[yt["guide_number"]] = yt["group_title"]
        epg_ids.add(yt["epg_channel_id"])
        epg_id_to_guide_numbers.setdefault(yt["epg_channel_id"], []).append(yt["guide_number"])

    # Programmes for enabled channels, remapping channel_id to GuideNumber(s).
    # When multiple channels share an EPG ID, programmes are repeated for each.
    # Rows are read in batches and written out as they come, so a large
    # lineup with a long guide no longer holds every programme (as ORM rows,
    # dicts, an element tree and one string) in memory per request.
    emit_sub_titles = _emit_sub_titles(db)
    provider_hosts = _provider_hosts(db, channels)

    def programs():
        if not epg_ids:
            return
        inferred_categories = 0
        rows = (
            db.query(EPGProgram.channel_id, EPGProgram.title, EPGProgram.sub_title,
                     EPGProgram.description, EPGProgram.start, EPGProgram.stop,
                     EPGProgram.category, EPGProgram.icon_url)
            .filter(EPGProgram.channel_id.in_(epg_ids))
            .filter(EPGProgram.stop >= datetime.utcnow())
            .yield_per(2000)
        )
        for p in rows:
            guide_numbers = epg_id_to_guide_numbers.get(p.channel_id, [])
            for gn in guide_numbers:
                # Xtream providers commonly send no category, and without one
                # Jellyfin never sets IsSports/IsNews/IsKids/IsMovie — so no
                # sports badge, empty genre filters, and the sports DVR padding
                # defaults never apply.
                category = p.category
                if not category:
                    category = infer_category(p.title, guide_number_group.get(gn))
                    if category:
                        inferred_categories += 1
                yield {
                    "channel_id": gn,
                    "title": p.title,
                    "sub_title": p.sub_title if emit_sub_titles else None,
                    "description": p.description,
                    "start": p.start,
                    "stop": p.stop,
                    "category": category,
                    # Stored for YouTube Live and provider programmes alike, and
                    # dropped here until #147: Jellyfin saves it as the art.
                    "icon_url": _third_party_icon(p.icon_url, provider_hosts),
                }
        if inferred_categories:
            logger.info(
                f"[LiveTV] XMLTV: inferred a category for {inferred_categories} programme(s) "
                f"the provider sent none for"
            )

    # Written to a file first, then sent: the database is read only while the
    # guide is written (one snapshot, as before), never for as long as a slow
    # client takes to download it.
    path = _write_guide_file(iter_xmltv(xmltv_channels, programs()))
    return _GuideFileResponse(path, media_type="application/xml")


_GUIDE_FILE_PREFIX = "tentacle-xmltv-"


def _write_guide_file(chunks) -> str:
    """The guide in a temporary file; the file is removed if writing fails.
    Leftovers of a process that stopped mid-write are removed first."""
    import glob
    import os
    import tempfile
    import time
    for old in glob.glob(os.path.join(tempfile.gettempdir(), _GUIDE_FILE_PREFIX + "*")):
        try:
            if time.time() - os.path.getmtime(old) > 3600:
                os.unlink(old)
        except OSError:
            pass
    fd, path = tempfile.mkstemp(prefix=_GUIDE_FILE_PREFIX, suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
    except BaseException:
        os.unlink(path)
        raise
    return path


class _GuideFileResponse(FileResponse):
    """A guide file sent once and then removed, also when the client goes
    away mid-download."""

    async def __call__(self, scope, receive, send):
        import os
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                os.unlink(self.path)
            except OSError:
                pass

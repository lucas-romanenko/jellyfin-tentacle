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
import time
from array import array
from collections import deque

import httpx
from datetime import datetime, timedelta
from typing import Optional, List

from services import provider_health
from services.epg_categories import infer_category
from services.exceptions import ProviderDataError, ProviderRefusedError
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
from routers.auth import get_user_from_request, require_admin, require_internal_or_admin
from services.ssrf import LookupFailed, check_url, is_safe_url, lan_origin_guard, refusal_text
from urllib.parse import urljoin, urlsplit

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

    A URL that names a provider placeholder (black.ts, ...: _placeholder_name),
    the first one or a redirect target, is never fetched: it raises
    _ProviderPlaceholder, and then no response is open (#624).

    `guard` validates every hop; callers pass one scoped to the channel's
    provider (#76) so a deliberately-configured LAN re-streamer works while
    everything else is still held to is_safe_url().
    """
    guard = guard or is_safe_url
    current = url
    for _ in range(_MAX_STREAM_REDIRECTS):
        placeholder = _placeholder_name(current)
        if placeholder:
            raise _ProviderPlaceholder(placeholder)
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
        # A name that didn't resolve is asked again before giving up: the
        # provider sends every open to a fresh random subdomain.
        allowed, why = await check_url(guard, current)
        if not allowed:
            logger.warning(f"[LiveTV] Blocked redirect ({why or 'non-public host'}): {current}")
            raise HTTPException(502, refusal_text(why, "Stream redirect"))
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
    stop at once: at the channel URL they mean the login or the channel --
    except 401/403 on a running recording's re-dial, waited out for
    _DENIED_GRACE first (#646)."""
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
# connection right after the last byte sent, but only when that is proven:
# every packet it drops equals the packet sent at that place (#520: a provider
# that loops aired packets verbatim can repeat a frame start that was sent
# right after packets that never were). A connection closed again before it
# reaches that point is dropped on the same proof (#609). Anything else goes
# out as it comes (duplicates at worst, never a loss).
_SPLICE_HOLD_BYTES = 32 * 1024 * 1024   # the most of a replay held: half a client's byte slack
_SPLICE_HOLD_SECONDS = 30.0             # the longest the output pauses for one
_SPLICE_DECIDE_PACKETS = 2048           # the first frame start must come within this
_SPLICE_INDEX_MAX = 16384               # frame starts remembered (minutes of a channel)


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
    sit exactly where it was sent (a gap in between shifts them), and every
    packet before the join point must equal the one sent at that place (a
    hash per packet sent, kept as far back as a hold reaches). Then the held
    replay is dropped and the stream goes on from there. A connection that
    ends before the join point after all it held was proven so is dropped
    too (#609). Anything else (a first frame never sent: a real gap or no
    replay; a replay that differs, is too large or too slow; a break before
    anything was proven) releases what was held."""

    def __init__(self, channel_id: int, health: dict):
        self.channel_id, self.health = channel_id, health
        self.sent_total = 0
        self._index: dict = {}          # frame-start packet -> where it was sent (the newest)
        self._order = deque()           # (offset, packet), oldest first
        # hash(packet) of every packet sent, by packet number modulo its size
        self._ring = array("q", bytes(8 * (_SPLICE_HOLD_BYTES // 188 + 1)))
        self._breaks = deque([0])       # where a connection not joined began in the output
        self._hold = None               # the re-dialled connection's bytes while it is judged
        self._carried = 0.0             # outages of replays dropped unjoined, until one joins (#609)

    def sent(self, data: bytes) -> None:
        """Whole packets that went out, in order."""
        base, ring, size = self.sent_total, self._ring, len(self._ring)
        p = base // 188
        for i in range(0, len(data) - 187, 188):
            key = data[i:i + 188]
            ring[p % size] = hash(key)
            p += 1
            if data[i + 1] & 0x40 and _ts_frame_start(key):
                self._index[key] = base + i
                self._order.append((base + i, key))
        self.sent_total += len(data)
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
        self._checked = 0               # held packets found equal to the ones sent there
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
                # What came before that frame (tables, the end of the previous
                # frame) is dropped too: it must have gone out right before it,
                # not be cut off by an earlier connection's start (and below,
                # be the very packets sent there).
                if any(sent_at - n < b <= sent_at for b in self._breaks):
                    return self._release("what precedes its first frame was not sent")
                self._first = (n, sent_at)
                self._join = n + self.sent_total - sent_at
                if self._join > _SPLICE_HOLD_BYTES:
                    return self._release(f"a replay of {self._join / 1e6:.0f} MB is too large to hold")
            elif sent_at != self._first[1] + n - self._first[0]:
                return self._release("its frames are not where they were sent")
        if self._join is not None:
            # Every packet dropped must be the one sent at that place (#520):
            # held packet k was output packet sent_total/188 - join/188 + k.
            # Checked as it arrives, so a large replay isn't hashed at once.
            ring, size = self._ring, len(self._ring)
            at = self.sent_total // 188 - self._join // 188
            upto = min(len(hold), self._join) // 188
            for k in range(self._checked, upto):
                if hash(bytes(hold[k * 188:k * 188 + 188])) != ring[(at + k) % size]:
                    return self._release("the bytes before the join point differ")
            self._checked = upto
        if self._join is not None and len(hold) >= self._join:
            out = bytes(hold[self._join:])
            self.health["replays_joined"] += 1
            self.health["replay_bytes_skipped"] += self._join
            self.health["_covered_seconds"] = (self.health.get("_covered_seconds", 0.0)
                                               + self._outage + self._carried)
            self._carried = 0.0
            logger.info(f"[LiveTV] Raw stream for channel {self.channel_id}: rejoined where it "
                        f"stopped, {self._join / 1e6:.1f} MB the provider sent again skipped")
            self._hold = None
            return out
        if now - self._since > _SPLICE_HOLD_SECONDS:
            return self._release(f"no join point within {_SPLICE_HOLD_SECONDS:.0f}s")
        return b""

    def abort(self) -> bytes:
        """The connection ended while it was being judged. A proven replay
        (first frame sent, every whole packet the one sent there) brought
        nothing new: dropped (#609). Anything else goes out."""
        if self._hold is None:
            return b""
        if self._join is None or self._checked != len(self._hold) // 188:
            return self._release("the connection ended before the join point")
        # Its torn last packet never goes out either: only whole packets do.
        skipped, self._hold = self._checked * 188, None
        self.health["replays_joined"] += 1
        self.health["replay_bytes_skipped"] += skipped
        # Its wait is covered only if a later re-dial joins.
        self._carried += self._outage
        logger.info(f"[LiveTV] Raw stream for channel {self.channel_id}: closed during its replay, "
                    f"{skipped / 1e6:.1f} MB the provider sent again skipped")
        return b""

    def _release(self, why: str) -> bytes:
        out, self._hold = bytes(self._hold), None
        self._carried = 0.0             # the waits before it were not covered
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
# (_raw_retryable). 401/403 there are waited out for _DENIED_GRACE on a
# recording (#646), then end it: the login is refused.
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
# A connection that delivered this long is a stream that works (#335): it
# ends a raw re-dial's outage, and tells provider health (#652).
_HEALTHY_AFTER = 10.0
# A raw stream goes out in ~128 KB pieces (fewer, larger writes for every
# client), but not its start: the provider's opening burst is passed on as it
# comes until this much has gone out, so Jellyfin's tuner, probe and ffmpeg
# begin at once (a 2 Mbit/s channel took half a second to fill one piece).
_FAST_START_BYTES = 2 * 1024 * 1024
# For a recording, a 404/410 counts toward _MAX_RERESOLVE only after this long
# of unbroken 404/410 (a panel blip is seconds); viewers keep the 3-try rule.
_GONE_GRACE = 180.0
# For a recording, a 401/403 is waited out on the refusal cap for this long of
# unbroken 401/403 before it ends the stream (#646: an IP block, Cloudflare or
# a panel's flood protection refuses everything for a minute or two). Raw TS:
# the re-dial; HLS: the channel URL's answer to a re-resolve. Viewers end at once.
_DENIED_GRACE = 180.0
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
# name, never fetched (redirect targets included: _send_checked), and refused with a 503 while the channel is opening, so
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


# The start of the 502 detail a channel open fails with when the provider
# didn't deliver (refused, timed out, no data)
_PROVIDER_FAILED = "Failed to connect to stream"


class _PlaceholderRefusal(_TunerRefusal):
    """The 503 a channel open answers when the provider served a placeholder."""


async def _refuse_placeholder(channel_id: int, segment: str):
    await _note_placeholder(channel_id, segment)
    raise _PlaceholderRefusal(f"Channel unavailable: the provider served a placeholder ({segment})")


def _detail_code(detail) -> "int | None":
    """The HTTP status in a _PROVIDER_FAILED detail ("... answered 504")."""
    m = re.search(r"answered (\d{3})", str(detail or ""))
    return int(m.group(1)) if m else None


def _provider_slate(bind, provider_id, channel_id) -> "tuple | None":
    """What the "Not coming through" picture says about this provider now
    (down, refusing, the account, this channel only), or None. A thread
    with a session of its own on the request's database."""
    from sqlalchemy.orm import Session as _Session
    from services import provider_status
    db = _Session(bind=bind)
    try:
        return provider_status.slate_lines(db, provider_id, channel_id)
    except Exception as e:
        logger.debug(f"[LiveTV] provider status for the slate not read: {e}")
        return None
    finally:
        db.close()


def _placeholder_lines(ch) -> tuple:
    from services import livetv_slate
    return livetv_slate.unavailable(_channel_event(ch), ch.guide_name if ch is not None else "")


async def _placeholder_picture(ch) -> "bytes | None":
    from services import livetv_slate
    return await livetv_slate.picture(_placeholder_lines(ch))


def _channel_event(ch):
    """livetv_events' reading of the channel's name (cached), None for most channels."""
    if ch is None:
        return None
    from services.livetv_catalog import _event_for
    return _event_for(ch.name, ch.group_title)


async def _nothing_on_response(channel_id: int, lines: tuple,
                               reason: str = "nothing on right now") -> "StreamingResponse | None":
    """A viewer's answer when a channel has nothing on (or can't be had): a
    short stream that says so (services/livetv_slate.py), or None when it
    can't be made."""
    from services import livetv_slate
    data = await livetv_slate.picture(lines)
    if not data:
        return None
    logger.info(f"[LiveTV] Channel {channel_id}: {reason} ({' '.join(lines).strip()}) "
                f"-- the viewer is told so")
    return StreamingResponse(livetv_slate.paced(data), media_type="video/mp2t", headers={
        "Connection": "close",
        "Cache-Control": "no-cache, no-store",
        "X-Tentacle-Reason": reason,
    })


def _media_segments(playlist_text: str, base_url: str) -> list:
    """Segment URLs of an HLS media playlist, resolved against its URL."""
    return [urljoin(base_url, line.strip()) for line in playlist_text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


# What an upstream pull is for. Lower number = more important. A recording
# outranks a viewer (tvheadend: 300 vs 100): a viewer who is cut off changes
# channel; a recording that is cut off is gone for good.
_LEASE_PRIORITY = {"recording": 0, "live": 10, "vod": 20}

# Where a recording is in its timer (#300), from the Jellyfin timer the
# recording lookup reads anyway: "pre" (its pre-padding), "programme"
# (StartDate to EndDate, _PHASE_GRACE_SECONDS either side) or "post" (its
# post-padding: the programme is over). Jellyfin pads 5 min before / 30 min
# after (sports 15 / 90), so on an account that carries one stream, a
# recording in its post-padding held the only slot while the next programme
# on another channel was refused until that one had passed. Now:
#   * at the limit, a recording whose programme has NOT ended (pre or
#     programme) may take the slot of a recording in its post-padding -- the
#     one whose programme ended first -- on a lookup answer issued after the
#     newcomer began to open. Jellyfin never reopens a recording after its
#     EndDate: the stopped one ends as one file, its programme complete;
#   * a recording in its pre-padding is never stopped (Jellyfin does not
#     reopen a tuner stream that ended: it would lose its whole programme),
#     and two recordings inside their programmes never take from each other
#     (#150's rule);
#   * with no current answer from Jellyfin every recording counts as inside
#     its programme: nothing is stopped for another recording;
#   * #184's rival test ranks a recording in its post-padding just above a
#     viewer (_PADDING_PRIORITY), so a recording inside its programme may
#     take an account back from it.
_PHASE_GRACE_SECONDS = 10.0
_PADDING_PRIORITY = 5
# A recording that opens just as another's programme ends (back to back)
# waits this long for the lookup that sees the other in its post-padding,
# instead of being refused into Jellyfin's one-minute retry.
_HANDOVER_WAIT_SECONDS = 20.0

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
                 "provider_id", "channel_id", "account", "phase", "programme_end")

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
        # A recording's phase and its programme's end (UTC), #300; a recording
        # counts as inside its programme until a lookup says otherwise.
        self.phase = "programme"
        self.programme_end = None


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
        self.padding_handovers = 0     # #300: recordings in post-padding stopped for one starting
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
        self._waiting_provider: "dict[int, int | None]" = {}
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

    def _grant(self, kind: str, owner: "str | None", stream_key: "str | None" = None,
               provider_id: "int | None" = None) -> _Lease:
        lease = _Lease(self._next_id, kind, owner, stream_key)
        lease.provider_id = provider_id
        self._next_id += 1
        self.leases[lease.id] = lease
        return lease

    # Several providers (#704): each account carries its own number of
    # streams (provider_limit), within the overall ceiling (limit). A pull
    # that names no provider counts against the overall ceiling only.
    def provider_active(self, provider_id: "int | None") -> int:
        return sum(1 for l in self.leases.values() if l.provider_id == provider_id and not l.preempted)

    def _own_full(self, provider_id, provider_limit: int) -> bool:
        return provider_id is not None and provider_limit > 0 and self.provider_active(provider_id) >= provider_limit

    def _fits(self, limit: int, provider_id, provider_limit: int) -> bool:
        return (limit <= 0 or len(self.leases) < limit) and not self._own_full(provider_id, provider_limit)

    def _competes(self, waiter_id: int, provider_id) -> bool:
        """A waiter is after the same slot: the same provider's, or either
        of the two names none (then only the overall ceiling holds them)."""
        theirs = self._waiting_provider.get(waiter_id)
        return theirs is None or provider_id is None or theirs == provider_id

    def sync_recordings(self, stream_keys, issued_at: "float | None" = None,
                        phases: "dict | None" = None) -> int:
        """Bring running pulls into line with which channels are recorded NOW.

        Promote: Jellyfin marks a timer InProgress only AFTER its tuner
        stream is open (RecordingsManager.RecordStream: OpenLiveStreamInternal,
        then timer.Status = InProgress), so the pull that starts a recording
        is classified "live" at the moment it opens; the next lookup corrects
        it here, before anything could take its slot.

        Demote: a recording that has ended while a viewer keeps watching the
        same channel must go back to viewer priority, or it would hold a
        recording's rank for ever and the next scheduled recording on another
        channel could be refused for it.

        Phases (#300): `phases` maps a stream key to (phase, programme end);
        a key it lacks (a reservation), or no `phases` at all, is inside its
        programme. Returns how many ranks or phases changed."""
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
            phase, end = ((phases or {}).get(lease.stream_key) or ("programme", None)) \
                if lease.kind == "recording" else ("programme", None)
            if (lease.phase, lease.programme_end) != (phase, end):
                if lease.kind == "recording" and phase != lease.phase:
                    n += 1
                    if phase == "post" and end is not None:
                        logger.info(f"[LiveTV] '{lease.owner}' is in its post-padding (its programme ended "
                                    f"{end:%H:%M} UTC): a recording whose programme is starting may take its slot")
                lease.phase, lease.programme_end = phase, end
        if n:
            self._wake()        # a demoted pull may now be a waiting recording's to take
        if self.protect:
            # Every current answer, not only one that changed a rank: a
            # viewer let through while Jellyfin could not answer, or left
            # running because protection was switched on from a stale answer,
            # is stopped as soon as Jellyfin says it is not a recording.
            self._enforce_soon(live_before=issued_at)
        return n

    def reset_phases(self) -> None:
        """Jellyfin could not be asked: every recording counts as inside its
        programme again (#300), so nothing is stopped for another recording."""
        for lease in self.leases.values():
            lease.phase, lease.programme_end = "programme", None

    def _padding_victim(self, provider_id=None, own_full: bool = False) -> "_Lease | None":
        """#300: the recording in its post-padding whose programme ended
        first (of the full account's when that is what is full)."""
        candidates = [l for l in self.leases.values()
                      if l.kind == "recording" and l.phase == "post" and not l.preempted
                      and (not own_full or l.provider_id == provider_id)]
        if not candidates:
            return None
        return min(candidates, key=lambda l: (l.programme_end or datetime.max, l.started))

    def _handover_due(self) -> bool:
        """A running recording's programme ends within _HANDOVER_WAIT_SECONDS
        (or has ended): a recording opening now may get its slot soon."""
        soon = datetime.utcnow() + timedelta(seconds=_HANDOVER_WAIT_SECONDS + _PHASE_GRACE_SECONDS)
        return any(l.kind == "recording" and not l.preempted and l.phase != "pre"
                   and l.programme_end is not None and l.programme_end <= soon
                   for l in self.leases.values())

    def _wake(self):
        if self._freed is not None:
            self._freed.set()
            self._freed = asyncio.Event()

    def _victim_for(self, kind: str, provider_id=None, own_full: bool = False) -> "_Lease | None":
        """The least important running pull this kind may take a slot from:
        strictly lower priority only; among equals, the most recent. When
        the provider's own account is what is full, only one of its pulls."""
        prio = _LEASE_PRIORITY.get(kind, _LEASE_PRIORITY["live"])
        candidates = [l for l in self.leases.values() if l.priority > prio and not l.preempted
                      and (not own_full or l.provider_id == provider_id)]
        if not candidates:
            return None
        return max(candidates, key=lambda l: (l.priority, l.started))

    async def _take_from(self, victim: _Lease, kind: str, owner: "str | None",
                         stream_key: "str | None", provider_id: "int | None" = None) -> _Lease:
        """Move the victim's slot to the newcomer in one step -- the slot is
        never free in between, so nobody woken meanwhile can take it and put
        the count over the limit -- then stop the victim."""
        victim.preempted = True
        self.leases.pop(victim.id, None)
        lease = self._grant(kind, owner, stream_key, provider_id)
        self.preempted_since_start += 1
        if victim.kind == "recording" and victim.phase == "post":
            self.padding_handovers += 1
            logger.warning(f"[LiveTV] At capacity: recording '{owner}' takes the slot of recording "
                           f"'{victim.owner}', which is stopped — its programme is over; only its "
                           f"post-padding is lost")
        else:
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
        mine = self._waiting_provider.get(waiter_id)
        rivals = {k: v for k, v in self._waiting.items() if k == waiter_id or self._competes(k, mine)}
        best = min(rivals.items(), key=lambda kv: kv[1])[0] if rivals else None
        return best == waiter_id

    async def acquire_lease(self, limit: int, wait: float, kind: str = "live",
                            owner: "str | None" = None, stream_key: "str | None" = None,
                            certain: bool = True, provider_id: "int | None" = None,
                            provider_limit: int = 0, phase: str = "programme",
                            fresh_since: "float | None" = None) -> "_Lease | None":
        """A slot for one upstream pull, or None when refused at capacity.

        With recording protection on (self.protect): a recording first stops
        every viewer and film pull; a viewer or film is refused with
        RecordingProtected while a recording runs. `certain` is False when
        the caller could not learn from Jellyfin just now what is being
        recorded -- then a live pull is not refused (it may be the next
        recording) and a recording stops films only.

        `provider_id` / `provider_limit`: the account this pull is on and how
        many streams it carries at once (0: no limit of its own).

        `phase` (#300): where this recording is in its timer. One whose
        programme has not ended may take the slot of a recording in its
        post-padding, on a lookup answer issued at or after `fresh_since`
        (when it began to open); it waits up to _HANDOVER_WAIT_SECONDS for
        that answer when a running recording's programme is just ending."""
        # The background stream checker never holds a connection anyone
        # wants: its check in flight is stopped (and closed) first.
        await _preempt_checker(f"{kind} '{owner}' is opening")
        prio = _LEASE_PRIORITY.get(kind, _LEASE_PRIORITY["live"])
        pid, plimit = provider_id, provider_limit
        if self.protect:
            if kind == "recording":
                lease = await self._clear_and_grant(kind, owner, stream_key, films_only=not certain)
                if lease is not None:
                    lease.provider_id = pid
                    return lease
            elif self.recording_active() and (certain or kind == "vod"):
                self._refuse_protected(kind, owner)
        if limit <= 0 and (pid is None or plimit <= 0):
            return self._grant(kind, owner, stream_key, pid)
        # A free slot goes to the newcomer only if nobody as important is
        # already waiting for one.
        if self._fits(limit, pid, plimit) and not any(
                p <= prio for k, (p, _) in self._waiting.items() if self._competes(k, pid)):
            return self._grant(kind, owner, stream_key, pid)
        def victim_now():
            v = self._victim_for(kind, pid, self._own_full(pid, plimit))
            if v is None and kind == "recording" and phase != "post" \
                    and _recording_answer_is_current(since=fresh_since):
                v = self._padding_victim(pid, self._own_full(pid, plimit))
            return v

        victim = victim_now()
        if victim is not None:
            return await self._take_from(victim, kind, owner, stream_key, pid)
        if kind == "recording" and phase != "post" and self._handover_due():
            wait = max(wait, _HANDOVER_WAIT_SECONDS)
        if self._freed is None:
            self._freed = asyncio.Event()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        me = self._next_waiter
        self._next_waiter += 1
        self._waiting[me] = (prio, me)
        self._waiting_provider[me] = pid
        try:
            while True:
                freed = self._freed          # before checking: a set after this is not missed
                if self.protect and kind != "recording" and self.recording_active() \
                        and (certain or kind == "vod"):
                    self._refuse_protected(kind, owner)     # a recording started while we waited
                if self._fits(limit, pid, plimit) and self._first_in_line(me):
                    return self._grant(kind, owner, stream_key, pid)
                # Something may have become takeable while we waited (a
                # recording demoted back to a viewer when its timer ended).
                victim = victim_now()
                if victim is not None and self._first_in_line(me):
                    return await self._take_from(victim, kind, owner, stream_key, pid)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(freed.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
        finally:
            self._waiting.pop(me, None)
            self._waiting_provider.pop(me, None)
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


async def _preempt_checker(reason: str) -> None:
    """Stop the background stream checker's test stream before a real one
    opens (services/livetv_checker.py); never fails the open."""
    try:
        from services import livetv_checker
        await livetv_checker.preempt(reason)
    except Exception as e:
        logger.debug(f"[LiveTV] stream checker pre-empt failed: {e}")


def _max_concurrent_streams(db) -> int:
    """The configured ceiling; 0 means unlimited. A value that does not parse
    falls back to the default rather than silently removing the cap."""
    raw = get_setting(db, "livetv_max_concurrent_streams", "")
    try:
        return max(0, int(raw)) if raw.strip() else _DEFAULT_MAX_CONCURRENT_STREAMS
    except (ValueError, AttributeError):
        return _DEFAULT_MAX_CONCURRENT_STREAMS


def provider_stream_limit(db, provider) -> int:
    """Streams this provider's account carries at once (#704): its own
    number from its card, else the Live TV default. 0 means no limit."""
    own = getattr(provider, "live_max_streams", None) if provider is not None else None
    if own is not None and own > 0:
        return own
    return _max_concurrent_streams(db)


def overall_stream_limit(db) -> int:
    """The ceiling over every provider together: the sum of each one's own
    limit (one provider: its limit, as before). 0 when any is unlimited."""
    providers = [p for p in db.query(Provider).all()
                 if p.live_tv_enabled or p.active or getattr(p, "live_max_streams", None)]
    if not providers:
        return _max_concurrent_streams(db)
    limits = [provider_stream_limit(db, p) for p in providers]
    return 0 if any(n <= 0 for n in limits) else sum(limits)


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
# "m3u8" is HLS: the pump reloads the playlist every few seconds and fetches
# every segment -- about 3,600 short requests over a three-hour game -- and
# on a connection-limited account every one of them is a chance for the
# provider to answer 509 because something else opened a connection
# meanwhile (seen live: two of three concurrent HLS channels on a
# 1-connection account died on playlist refreshes within 30 s). "ts" is the
# continuous MPEG-TS stream Xtream panels serve at /live/.../<id>.ts: ONE
# connection for the whole programme, taken through the raw-TS path (#103)
# with its re-dial; many concurrent streams on such an account run for
# hours (#87, #184). "auto" (the default) takes ts unless the account says
# it cannot serve it (user_info.allowed_output_formats, remembered at sync
# time); and since a panel that does not advertise its formats almost
# always serves ts anyway, a channel whose ts open is refused outright is
# opened as m3u8 instead (_live_open_urls). "m3u8" and "ts" are overrides.
# The format is applied when a channel is opened, so a change (or this
# default) takes effect at once, without a channel sync; a sync writes it
# into the stored URLs too.
_LIVE_STREAM_FORMATS = ("auto", "m3u8", "ts")
_DEFAULT_LIVE_STREAM_FORMAT = "auto"


def _live_stream_format(db) -> str:
    raw = (get_setting(db, "livetv_stream_format", "") or "").strip().lower()
    return raw if raw in _LIVE_STREAM_FORMATS else _DEFAULT_LIVE_STREAM_FORMAT


# Settings kept per provider, keyed by its id. Deleted with the provider
# (providers.delete_provider): SQLite gives the next provider the same id, which
# would inherit them (#625). A new per-provider key goes in PROVIDER_SETTING_KEYS.
_OUTPUT_FORMATS_KEY = "livetv_output_formats_{}"
_EPG_COVERAGE_KEY = "livetv_epg_coverage_{}"
_EPG_SYNCED_KEY = "livetv_epg_synced_{}"
_M3U_SWITCHED_OFF_KEY = "livetv_m3u_switched_off_{}"
_GUIDE_TOP_UP_KEY = "livetv_guide_top_up_{}"
PROVIDER_SETTING_KEYS = (_OUTPUT_FORMATS_KEY, _EPG_COVERAGE_KEY, _EPG_SYNCED_KEY,
                         _M3U_SWITCHED_OFF_KEY, "provider_address_probation_{}",
                         "livetv_m3u_guide_url_{}",   # services.m3u_source.GUIDE_URL_KEY
                         _GUIDE_TOP_UP_KEY)


def _remembered_output_formats(db, provider_id: int) -> list:
    import json
    raw = get_setting(db, _OUTPUT_FORMATS_KEY.format(provider_id), "") or ""
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
        set_setting(db, _OUTPUT_FORMATS_KEY.format(provider_id),
                    json.dumps([str(v).lower() for v in formats]))


def _live_extension(db, provider_id: int) -> str:
    """The extension channel URLs ask for, per the setting above: auto is ts
    unless the account advertises formats without it."""
    fmt = _live_stream_format(db)
    if fmt in ("m3u8", "ts"):
        return fmt
    formats = _remembered_output_formats(db, provider_id)
    return "m3u8" if formats and "ts" not in formats else "ts"


# Channels whose ts open was refused outright while m3u8 then opened: their
# panel does not serve ts for them, so later opens go straight to m3u8.
# In memory only: a restart tries ts again, at the cost of one refusal.
_ts_refused_channels: "set[int]" = set()


def _live_open_urls(db, provider, channel) -> "tuple[str, str | None]":
    """The URL to open a channel with, and the one to fall back to if that
    open is refused outright (or None). An Xtream channel's stored URL ends
    in .<ext> as its last sync wrote it; the open asks for the format the
    setting picks now. Under auto a ts open has m3u8 as its fallback."""
    url = channel.stream_url or ""
    if provider is None:
        return url, None
    if (provider.provider_type or "xtream") != "xtream":
        return _m3u_open_urls(db, provider, channel, url)
    if not channel.stream_id:
        return url, None
    m = re.search(rf"/live/.+/{re.escape(channel.stream_id)}\.(m3u8|ts)$", url)
    if not m:
        return url, None
    base = url[:m.start(1)]
    ext = _live_extension(db, provider.id)
    if ext == "ts" and _live_stream_format(db) == "auto":
        if channel.id in _ts_refused_channels:
            return base + "m3u8", None
        return base + "ts", base + "m3u8"
    return base + ext, None


def _m3u_open_urls(db, provider, channel, url: str) -> "tuple[str, str | None]":
    """An M3U channel whose link is an Xtream panel's (/live/u/p/123.m3u8,
    /u/p/123 ...: most provider playlists) opens like an Xtream channel: as
    one continuous .ts, with .m3u8 as the fallback, per the same setting.
    That is what lets one connection carry several channels (#700): an HLS
    pull asks the panel again for every segment. Any other link is opened
    as listed."""
    from services.m3u_parser import panel_link
    link = panel_link(url)
    if link is None or link.kind != "live":
        return url, None
    as_ts = url if link.ext == "ts" else link.live_url("ts")
    as_m3u8 = url if link.ext == "m3u8" else link.live_url("m3u8")
    ext = _live_extension(db, provider.id)
    if ext == "ts" and _live_stream_format(db) == "auto":
        if channel.id in _ts_refused_channels:
            return as_m3u8, None
        return as_ts, as_m3u8
    return (as_ts if ext == "ts" else as_m3u8), None


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


# Tentacle's own M3U tuners (the household lineup since v1.13, and the
# per-person tuners of v1.11-1.12): Jellyfin names their channels
# "m3u_" + MD5(tuner URL) + MD5(stream URL) (livetv_lineups.m3u_external_id),
# which hashes the exact address the tuner was FIRST given -- so the ids are
# made from the saved tuner URLs, never from Tentacle's address now (#760).
_TENTACLE_M3U_TTL = 60.0
_tentacle_m3u = {"at": -1e9, "ids": {}}


def _tentacle_m3u_ids(db=None) -> "dict[str, str]":
    """Jellyfin's ExternalChannelId -> the GuideNumber of the channel that
    stream address plays now, for every channel Tentacle's M3U tuners carry
    (or carried under a key that became another). Kept a minute. Opens its
    own session when given none (it runs in the lookup's worker thread)."""
    import time
    now = time.monotonic()
    if db is None and now - _tentacle_m3u["at"] < _TENTACLE_M3U_TTL:
        return _tentacle_m3u["ids"]
    own = db is None
    try:
        if own:
            db = SessionLocal()
        try:
            ids = _build_tentacle_m3u_ids(db)
        finally:
            if own:
                db.close()
    except Exception as e:   # never cost the hdhr_ answer, nor a stream open
        logger.warning(f"[LiveTV] Tentacle's own tuner channels not read: {e}")
        return _tentacle_m3u["ids"]
    if own:
        _tentacle_m3u.update(at=now, ids=ids)
    return ids


def _build_tentacle_m3u_ids(db) -> "dict[str, str]":
    from models.database import LiveUserLineup
    from services import livetv_lineups as L
    from services.livetv_tuners import ALL_TUNER_URL_KEY
    urls = [(get_setting(db, ALL_TUNER_URL_KEY, "") or "").strip()]
    urls += [(r.tuner_url or "").strip() for r in db.query(LiveUserLineup).all()]
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        return {}
    channels = L.pool(db)
    entries = L.lineup(db, channels)
    # Event channels with no event now are out of the lineup, but Jellyfin
    # keeps them until its next guide refresh, and a timer may be on one.
    shown = {e["key"] for e in entries}
    entries += [c for c in channels if c["key"] not in shown]
    # pool key -> the LiveChannel it streams (a network's or a place's: the
    # variant it plays now; YouTube channels are not the provider's)
    cid_of = {}
    for e in entries:
        k = e["key"]
        if k[:1] == "c" and k[1:].isdigit():
            cid_of[k] = int(k[1:])
        elif k[:1] == "n" and e.get("channel_id") is not None:
            cid_of[k] = int(e["channel_id"])
    keys = dict.fromkeys(cid_of)
    for old, new in L.final_aliases(db, entries).items():
        if new in cid_of:
            cid_of.setdefault(old, cid_of[new])
            keys[old] = None
    rows = {ch.id: ch for ch in db.query(LiveChannel).filter(LiveChannel.id.in_(set(cid_of.values()))).all()}
    offsets = _offsets(db)
    out = {}
    for url in urls:
        base = L.tuner_base(url)
        for k in keys:
            ch = rows.get(cid_of[k])
            path = L.stream_path_of_key(k)
            if ch is not None and path:
                out[L.m3u_external_id(url, base + path)] = guide_number(ch, offsets)
    return out


STOPPED_FOR_RECORDING = "Stopped for a recording"
_background_tasks: "set[asyncio.Task]" = set()


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _channel_item_ids(db, channel_id: int) -> "set[str]":
    """Jellyfin's item ids (no dashes) for a channel: on Tentacle's M3U
    tuners (every lineup entry that streams it now) and the HDHomeRun one."""
    from services import livetv_lineups as L
    ch = db.get(LiveChannel, channel_id)
    if ch is None:
        return set()
    number = guide_number(ch, _offsets(db))
    exts = {e for e, n in _tentacle_m3u_ids().items() if n == number}
    exts.add(L.hdhr_external_id(number))
    return {L.channel_item_id(e) for e in exts}


def _tell_stopped_for_recording(channel_id: int) -> int:
    """A recording took this channel's connection: every Jellyfin session
    playing it is stopped and told so ("Stopped for a recording"), on
    Jellyfin web and the TV app alike (both act on Jellyfin's Stop and
    DisplayMessage). Without this the player sat on the last frame. Blocking;
    never raises. Returns how many sessions were told."""
    import requests
    db = SessionLocal()
    try:
        url = (get_setting(db, "jellyfin_url", "") or "").strip().rstrip("/")
        key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
        if not url or not key:
            return 0
        ids = _channel_item_ids(db, channel_id)
    except Exception as e:
        logger.warning(f"[LiveTV] Could not tell the viewers of channel {channel_id} why it stopped: {e}")
        return 0
    finally:
        db.close()
    headers = {"X-Emby-Token": key}
    told = 0
    try:
        r = requests.get(f"{url}/Sessions", params={"activeWithinSeconds": 960}, headers=headers, timeout=5)
        r.raise_for_status()
        for session in r.json() or []:
            item = (session.get("NowPlayingItem") or {}).get("Id") or ""
            sid = session.get("Id")
            if not sid or item.replace("-", "").lower() not in ids:
                continue
            requests.post(f"{url}/Sessions/{sid}/Playing/Stop", headers=headers, timeout=5)
            requests.post(f"{url}/Sessions/{sid}/Message", headers=headers, timeout=5,
                          # Jellyfin puts "Message from Server" in for an empty header
                          json={"Header": "Live TV", "Text": STOPPED_FOR_RECORDING, "TimeoutMs": 10000})
            told += 1
    except Exception as e:
        logger.warning(f"[LiveTV] Could not tell the viewers of channel {channel_id} why it stopped: {e}")
    if told:
        logger.info(f"[LiveTV] Channel {channel_id}: told {told} viewer(s) it stopped for a recording")
    return told


def _timer_guide_number(ext: str, m3u_ids) -> "str | None":
    """The GuideNumber a timer's ExternalChannelId means: hdhr_<n> from the
    HDHomeRun tuner, or a channel of Tentacle's own M3U tuner. None for a
    tuner that isn't Tentacle's."""
    if ext.startswith("hdhr_"):
        return ext[len("hdhr_"):]
    if ext.startswith("m3u_"):
        ids = m3u_ids() if callable(m3u_ids) else m3u_ids
        return (ids or {}).get(ext)
    return None


class _RecordingAnswer(set):
    """GuideNumbers being recorded, plus `phases`: GuideNumber -> (phase,
    programme end UTC), see _timer_phase (#300)."""

    def __init__(self, *args):
        super().__init__(*args)
        self.phases: "dict[str, tuple]" = {}


_PHASE_ORDER = {"programme": 0, "pre": 1, "post": 2}


def _timer_phase(timer: dict, now: "datetime") -> tuple:
    """(phase, programme end) of a timer being recorded: "pre" before its
    StartDate, "post" after its EndDate (_PHASE_GRACE_SECONDS of grace each
    side), else "programme". A timer without dates is inside its programme."""
    start, end = _parse_timer_date(timer.get("StartDate")), _parse_timer_date(timer.get("EndDate"))
    grace = timedelta(seconds=_PHASE_GRACE_SECONDS)
    if start is not None and now < start - grace:
        return "pre", end
    if end is not None and now > end + grace:
        return "post", end
    return "programme", end


def _add_phase(phases: dict, number: str, phase: tuple) -> None:
    """A channel with several timers (back to back) takes the most protected
    phase: inside a programme, else pre-padding; the latest end."""
    old = phases.get(number)
    if old is None or _PHASE_ORDER[phase[0]] < _PHASE_ORDER[old[0]]:
        phases[number] = phase
    elif _PHASE_ORDER[phase[0]] == _PHASE_ORDER[old[0]] and phase[1] and (old[1] is None or phase[1] > old[1]):
        phases[number] = phase


def _recording_stream_ids_from_jellyfin(url: str, key: str, m3u_ids=None) -> "set[str] | None":
    """GuideNumbers of the channels Jellyfin is recording, or None if it
    gave no usable answer. Blocking; run it off the event loop. Raises when
    Jellyfin cannot be asked at all. `m3u_ids`: Tentacle's M3U channel ids
    (default _tentacle_m3u_ids, read only when a timer has such an id).
    The answer's `phases` say where each recording is in its timer (#300)."""
    data = _jellyfin_timers(url, key)
    if not isinstance(data, dict):
        return None
    out = _RecordingAnswer()
    now = datetime.utcnow()
    items = data.get("Items") or []
    m3u = m3u_ids if m3u_ids is not None else _tentacle_m3u_ids
    for timer in items:
        status = timer.get("Status")
        if status not in ("InProgress", "New"):
            continue
        number = _timer_guide_number(timer.get("ExternalChannelId") or "", m3u)
        if number is None:
            continue
        if status == "InProgress":
            _remember_fired_timer(timer, now)
            out.add(number)
            _add_phase(out.phases, number, _timer_phase(timer, now))
        elif _timer_counts(timer, now):
            # Jellyfin opens the tuner stream BEFORE it marks the timer
            # InProgress, so the pull that starts a recording would be a
            # viewer for its first seconds -- and at capacity, a viewer
            # cannot take a slot from a viewer. A timer that is due counts
            # as recording already.
            out.add(number)
            _add_phase(out.phases, number, _timer_phase(timer, now))
    present = {t.get("Id") for t in items}
    for tid, expiry in list(_fired_timers.items()):
        if tid not in present or now >= expiry:
            del _fired_timers[tid]
            _fired_starts.pop(tid, None)
    return out


# Jellyfin timers Tentacle has seen fire (Id -> end of the recording: EndDate
# plus post-padding). When a recording's tuner open fails, Jellyfin retries
# it every minute -- and rewrites the timer as it does so: PrePaddingSeconds
# 0, and at each fire StartDate reset to the programme's start. A retry in
# the pre-padding therefore reads as "starts in 14 minutes, no padding",
# which is not imminent: it was ranked as a viewer, refused at capacity and
# under recording protection, and after ten retries Jellyfin deleted the
# timer before its programme began. A timer that has fired, is New again
# with no pre-padding and has not ended is such a retry. Only read and
# written by _recording_stream_ids_from_jellyfin, which runs one at a time
# (the single pending lookup). Lost on restart: then as before.
#
# A guide refresh during a retry cycle re-arms the timer for its programme's
# start (padding stays 0) and Jellyfin opens nothing until then; if the
# programme moved later, the timer must not keep counting. So a remembered
# timer counts only while it still starts by the programme start seen when
# it fired (_fired_starts) plus the imminent window.
_fired_timers: "dict[str, datetime]" = {}
_fired_starts: "dict[str, datetime]" = {}
_FIRED_GRACE_SECONDS = 30.0


def _remember_fired_timer(timer: dict, now: "datetime") -> None:
    tid = timer.get("Id")
    end = _parse_timer_date(timer.get("EndDate"))
    if not tid or end is None:
        return
    expiry = end + timedelta(seconds=_timer_seconds(timer.get("PostPaddingSeconds")))
    if expiry > now:
        _fired_timers[tid] = max(expiry, _fired_timers.get(tid, expiry))
        start = _parse_timer_date(timer.get("StartDate"))
        if start is not None:
            # The programme start seen at a fire. The latest: a lookup in the
            # 30 s before a retry fires sees StartDate = the retry time, which
            # is earlier than the programme start the fire then resets it to.
            _fired_starts[tid] = max(start, _fired_starts.get(tid, start))


def _timer_counts(timer: dict, now: "datetime") -> bool:
    """A New timer counts as recording when it is imminent, or when it is
    Jellyfin's retry of a timer Tentacle saw fire (see _fired_timers)."""
    if _timer_is_imminent(timer, now):
        start = _parse_timer_date(timer.get("StartDate"))
        pre = timedelta(seconds=_timer_seconds(timer.get("PrePaddingSeconds")))
        if start is not None and start - pre <= now + timedelta(seconds=_FIRED_GRACE_SECONDS):
            _remember_fired_timer(timer, now)     # due: Jellyfin fires it now
        return True
    tid = timer.get("Id") or ""
    expiry = _fired_timers.get(tid)
    start, seen = _parse_timer_date(timer.get("StartDate")), _fired_starts.get(tid)
    return (expiry is not None and now < expiry
            and _timer_seconds(timer.get("PrePaddingSeconds")) == 0
            and start is not None and (seen is None or start <= seen + timedelta(seconds=_RECORDING_IMMINENT_SECONDS)))


def _parse_timer_date(value) -> "datetime | None":
    try:
        return datetime.strptime((value or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


def _timer_seconds(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


_RECORDING_IMMINENT_SECONDS = 120.0


def _timer_is_imminent(timer: dict, now: "datetime") -> bool:
    """A New timer whose recording (start minus pre-padding) begins within
    _RECORDING_IMMINENT_SECONDS, or should already have begun -- and has
    not already ended (a timer Jellyfin never started stays "New" for ever;
    it must not keep its channel ranked as a recording)."""
    _parse, _seconds = _parse_timer_date, _timer_seconds
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
            ids = {row.id for row in channels_for_guide_numbers(db, sids)}
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
    ids = {row.id for row in channels_for_guide_numbers(db, sids)}
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
        cache["phases"] = {}
        _stream_slots.reset_phases()      # #300: no answer, every recording is inside its programme
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
        cache["phases"] = dict(getattr(sids, "phases", None) or {})
        cache["answer_issued_at"] = issued if issued is not None else now
        reserved_keys = {r["stream_key"] for r in _reserved_channels.values() if r.get("stream_key")}
        _stream_slots.sync_recordings(cache["sids"] | reserved_keys, issued_at=cache["answer_issued_at"],
                                      phases={k: v for k, v in cache["phases"].items() if k not in reserved_keys})
        if _stream_slots._waiting:
            # A recording waiting at the limit decides on this answer (#300):
            # the one it may take over may have been in its post-padding already.
            _stream_slots._wake()


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
# A client that has had bytes waiting this long and taken none of them is not
# slow, it is gone: nothing reads its queue any more (a request whose response
# never ran, 27 Oct 2026). It is let go, and the upstream closes when it was the
# last, so a ghost can't hold the provider connection (one, on many accounts)
# while every real channel fights it. Every real reader takes something within
# seconds: Jellyfin copies the stream to its buffer file as it comes, a
# multi-view tile and a preview's ffmpeg read continuously.
_IDLE_SUBSCRIBER_SECONDS = 60.0
# A viewer joining a running MPEG-TS upstream starts inside its last bytes
# (services/ts_start finds a keyframe with sound in them) instead of waiting
# for the next ones: on a channel whose audio runs ~9 s ahead that wait is 9 s
# of live time. Recordings never get it: they start where they joined.
_BACKLOG_BYTES = 32 * 1024 * 1024
_BACKLOG_SECONDS = 15.0


class _ClientQueue(asyncio.Queue):
    """An unbounded queue that knows how many bytes it holds and since when
    something has been waiting in it untaken; the slack above and the idle
    limit are enforced by _SharedUpstream._publish."""

    def _init(self, maxsize):
        super()._init(maxsize)
        self.nbytes = 0
        self.waiting_since = None   # monotonic time: bytes waiting, none taken since

    def _put(self, item):
        if not self._queue:
            self.waiting_since = time.monotonic()
        super()._put(item)
        self.nbytes += len(item) if item else 0

    def _get(self):
        item = super()._get()
        self.nbytes -= len(item) if item else 0
        self.waiting_since = time.monotonic() if self._queue else None
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
    if h.get("ended_on_error") and h.get("_refused") in (401, 403):
        _tell_provider_health(channel_id, False, h["_refused"])
    elif h.get("ended_on_error") and not recording:
        _tell_channel_failed(channel_id)
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
        if now - cur["since"] >= _HEALTHY_AFTER and cur.get("heard_since") != cur["since"]:
            # Delivering for _HEALTHY_AFTER: once per stretch, so a stream
            # that recovers mid-recording is heard within seconds (#652).
            cur["heard_since"] = cur["since"]
            _tell_provider_health(channel_id, True)
    if last_error is not None:
        cur["last_error"] = last_error


def _refusal_heard(health: dict, status) -> None:
    """The provider's latest answer to this stream's channel URL: a 401/403
    is kept until something else answers, so a stream that ends on it tells
    provider health (#652). 407, 404/410 and 429/509 are not refusals."""
    if status in (401, 403):
        health["_refused"] = status
    else:
        health.pop("_refused", None)


def _delivering_channels(except_id: int) -> "list[int]":
    """Channels other than except_id whose stream delivers now: streaming,
    this stretch heard past _HEALTHY_AFTER, marked within
    _RIVAL_STREAMING_FRESH (#674). Reads _stream_status: event loop only."""
    try:
        now = asyncio.get_running_loop().time()
    except RuntimeError:
        return []
    out = []
    for cid, st in list(_stream_status.items()):
        if cid == except_id or st.get("state") != "streaming":
            continue
        if st.get("heard_since") is None or st.get("heard_since") != st.get("since"):
            continue
        last = st.get("last_ok")
        if last is not None and now - last <= _RIVAL_STREAMING_FRESH:
            out.append(cid)
    return out


def _tell_provider_health(channel_id: int, ok: bool, code=None):
    """What a live stream learned about its provider, for provider health
    (#652). In the default executor with a session of its own: the event
    loop never touches the database. A refusal while another channel of the
    same provider delivers is about that channel, not the provider: not
    noted (#674). Never raises; the future (None without a running loop) is
    only awaited by tests."""
    delivering = [] if ok else _delivering_channels(channel_id)

    def _write():
        db = None
        try:
            db = SessionLocal()
            provider_id = (db.query(LiveChannel.provider_id)
                           .filter(LiveChannel.id == channel_id).scalar())
            if not isinstance(provider_id, int):
                return
            if ok:
                from services import provider_status
                provider_status.channel_ok(provider_id, channel_id)
            if delivering and db.query(LiveChannel.id).filter(
                    LiveChannel.id.in_(delivering),
                    LiveChannel.provider_id == provider_id).first() is not None:
                logger.info(f"[LiveTV] Channel {channel_id} was refused (HTTP {code}) while another "
                            f"channel of the same provider delivers: not counted against the provider")
                return
            provider_health.note(db, provider_id, ok, "stream", code)
        except Exception as e:
            logger.debug(f"[LiveTV] Could not tell provider health about channel {channel_id}: {e}")
        finally:
            if db is not None:
                db.close()
    try:
        return asyncio.get_running_loop().run_in_executor(None, _write)
    except RuntimeError:
        return None


def _tell_channel_failed(channel_id: int):
    """A viewer's stream ended while the provider still failed (any reason,
    not only a refusal): per channel, services/provider_status. In the
    default executor (it reads the channel's provider). Never raises."""
    def _write():
        db = None
        try:
            db = SessionLocal()
            ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
            if ch is not None:
                from services import provider_status
                provider_status.channel_failed(ch.provider_id, channel_id, ch.guide_name or "")
        except Exception as e:
            logger.debug(f"[LiveTV] Could not note channel {channel_id}'s failure: {e}")
        finally:
            if db is not None:
                db.close()
    try:
        return asyncio.get_running_loop().run_in_executor(None, _write)
    except RuntimeError:
        return None


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


def _rank(lease: "_Lease") -> int:
    """A pull's rank for #184's rival test: a recording in its post-padding
    ranks just above a viewer (#300), below a recording inside its programme."""
    if lease.kind == "recording" and lease.phase == "post":
        return _PADDING_PRIORITY
    return lease.priority


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
        prio = _rank(other)
        if other.kind == "live" and _recording_cache.get("ok_at", -1e9) < other.started + _CLASSIFY_GRACE:
            # Jellyfin has not answered a lookup made well after this pull
            # opened (it marks a timer InProgress only AFTER the tuner stream
            # is open, and a lookup can fail or lag): it may be a recording
            # not promoted yet. Never take the account from it on a guess
            # (fuzz seeds 34, 7007, 7095).
            prio = _LEASE_PRIORITY["recording"]
        if prio > _rank(lease):
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
        self._backlog = deque()      # (loop time, piece) of the last MPEG-TS pieces
        self._backlog_bytes = 0
        self._is_ts = None

    def subscribe(self, backlog: bool = False) -> "asyncio.Queue":
        q = _ClientQueue()
        if backlog:
            for _, piece in self._backlog:
                q.put_nowait(piece)
        self.subscribers.add(q)
        return q

    def _keep(self, piece) -> None:
        if self._is_ts is None:
            self._is_ts = piece[:1] == b"G"
        if not self._is_ts:
            return
        now = asyncio.get_running_loop().time()
        self._backlog.append((now, piece))
        self._backlog_bytes += len(piece)
        while self._backlog and (self._backlog_bytes > _BACKLOG_BYTES
                                 or now - self._backlog[0][0] > _BACKLOG_SECONDS):
            self._backlog_bytes -= len(self._backlog.popleft()[1])

    def _publish(self, item):
        if item:
            self._keep(item)
        now = time.monotonic()
        for q in list(self.subscribers):
            if q.waiting_since is not None and now - q.waiting_since > _IDLE_SUBSCRIBER_SECONDS:
                self._let_go(q, now - q.waiting_since)
                continue
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

    def _let_go(self, q, idle: float) -> None:
        """A subscriber that took nothing for _IDLE_SUBSCRIBER_SECONDS: drop
        it (and what it never read), end its stream in case something still
        listens, and close the upstream when it was the last."""
        self.subscribers.discard(q)
        while not q.empty():
            q.get_nowait()
        q.put_nowait(None)
        left = len(self.subscribers)
        logger.warning(f"[LiveTV] Channel {self.channel_id}: a client took nothing for {idle:.0f}s — "
                       f"let go of it (nothing reads its stream any more); "
                       f"{left} client(s) left" + ("" if left else ", closing the upstream"))
        if not left:
            _spawn(self.unsubscribe(q))

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
        end every client's stream cleanly and stop pulling. The people
        watching are told why (_tell_stopped_for_recording) instead of
        being left on a frozen picture."""
        if self._closed:
            return
        logger.warning(f"[LiveTV] Channel {self.channel_id}: stream stopped for "
                       f"{len(self.subscribers)} client(s) — a recording needed its connection slot")
        self._publish(None)
        await self._retire()
        self._cancel_pump()
        _spawn(asyncio.to_thread(_tell_stopped_for_recording, self.channel_id))


class _SubscriberResponse(StreamingResponse):
    """A client's view of a shared upstream, which is ALWAYS unsubscribed.

    The client is subscribed when this object is built, but the body generator
    only unsubscribes from its own `finally` -- and an async generator that is
    never started never runs it. A client that went away between the headers
    and the first chunk therefore stayed subscribed for ever: the upstream kept
    pulling from the provider with nobody watching and its concurrency slot
    never came back. Unsubscribing when the response finishes, however it
    finishes, closes that; `unsubscribe` is idempotent."""

    def __init__(self, shared: "_SharedUpstream", q, viewer: bool = False):
        super().__init__(
            _subscriber_body(shared, q, viewer),
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


async def _subscriber_body(shared: "_SharedUpstream", q, viewer: bool = False):
    """Per-client view of a shared upstream. A viewer's starts on a keyframe
    that has its sound with it (services/ts_start); a recording's is the
    upstream's bytes as they come."""
    from services.ts_start import AvStart
    start = AvStart() if viewer else None
    try:
        while True:
            piece = await q.get()
            if piece is None:
                if start is not None:
                    rest = start.flush()
                    if rest:
                        yield rest
                return
            if start is not None:
                held = not start.done
                piece = start.feed(piece)
                if held and start.done:
                    logger.info(f"[LiveTV] Channel {shared.channel_id}: a viewer's stream starts {start.result}")
                if not piece:
                    continue
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


# ─── Provider config ────────────────────────────────────────────────────────


def live_tv_providers(db: Session) -> list:
    """Every provider that serves Live TV.

    Selected on `live_tv_enabled` alone (the card's "Use for Live TV"):
    `active` is the VOD sync switch, and a provider used for Live TV only has
    it off; a filter on `active` once kept the nightly guide refresh from
    running (#174).
    """
    return db.query(Provider).filter(Provider.live_tv_enabled == True).all()  # noqa: E712


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
        "limit": overall_stream_limit(db),
        "active": _stream_slots.active,
        # Each account's own (#704): what the provider cards show
        "providers": [{"id": p.id, "name": p.name, "limit": provider_stream_limit(db, p),
                       "own_limit": p.live_max_streams, "account_max": p.max_connections,
                       "active": _stream_slots.provider_active(p.id)}
                      for p in db.query(Provider).order_by(Provider.id).all()
                      if p.live_tv_enabled or p.active],
        "refused_since_start": _stream_slots.refused,
        "last_refused": _stream_slots.last_refused,
        # #300: recordings in their post-padding stopped for one whose programme starts
        "padding_handovers": _stream_slots.padding_handovers,
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
            "stream_id": guide_number(ch, _offsets(db)) if ch else None,
            "kind": lease.kind if lease else None,
            # #300: "pre" / "programme" / "post" (padding) for a recording
            "recording_phase": lease.phase if lease is not None and lease.kind == "recording" else None,
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
        "limit": overall_stream_limit(db),
        "active": _stream_slots.active,
        "preempted_since_start": _stream_slots.preempted_since_start,
        "padding_handovers": _stream_slots.padding_handovers,
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


# ── Live previews: may a TV guide play a muted preview of the focused channel? ──
#
# A preview is one more provider connection, so it may only run while the
# household has spare ones: after the preview at least one stays free for
# someone who really presses play, and a recording that starts soon counts as
# taken already. Every TV with the guide open asks every ~15 s, so the answer
# is made from what Tentacle already knows (its slots) plus Jellyfin's timers,
# read at most every _PREVIEW_TIMERS_TTL by one caller at a time. No provider
# is ever asked.
_PREVIEW_SOON_SECONDS = 600          # a recording starting (minus pre-padding) within 10 min
_PREVIEW_TIMERS_TTL = 30.0
_PREVIEW_TIMERS_STALE = 300.0        # older than this, a failed read means "can't tell"
_PREVIEW_SPARE = 1                   # connections that stay free besides the preview
_preview_timers: dict = {"at": -1e9, "ok_at": -1e9, "windows": None}
_preview_timers_lock: "asyncio.Lock | None" = None


def _recording_windows(items: list, m3u_ids) -> list:
    """(GuideNumber, recording start, recording end) of every timer Jellyfin
    will (or does) record on Tentacle's channels: New or InProgress, start
    and end widened by the padding. Blocking (m3u ids may read the DB)."""
    out = []
    for t in items or []:
        if t.get("Status") not in ("InProgress", "New"):
            continue
        start = _parse_timer_date(t.get("StartDate"))
        if start is None:
            continue
        number = _timer_guide_number(t.get("ExternalChannelId") or "", m3u_ids)
        if number is None:
            continue
        end = _parse_timer_date(t.get("EndDate")) or start
        out.append((number, start - timedelta(seconds=_timer_seconds(t.get("PrePaddingSeconds"))),
                    end + timedelta(seconds=_timer_seconds(t.get("PostPaddingSeconds")))))
    return out


def _preview_timer_windows(url: str, key: str) -> list:
    return _recording_windows((_jellyfin_timers(url, key) or {}).get("Items") or [], _tentacle_m3u_ids)


def preview_answer(limit: int, active: int, held_keys, windows, now: "datetime",
                   previewing: bool = False, soon_seconds: int = _PREVIEW_SOON_SECONDS) -> dict:
    """The rule. `held_keys`: GuideNumbers already pulled (a recording on one
    of them needs no new connection). `windows`: _recording_windows, or None
    when Jellyfin's timers can't be told (then no previews: a recording might
    be about to start). `previewing`: the caller's own preview is running and
    counted in `active`, so it needs no further slot."""
    held = {str(k) for k in held_keys or ()}
    soon = set()
    for number, rec_start, rec_end in windows or ():
        if rec_end > now and (rec_start - now).total_seconds() <= soon_seconds and number not in held:
            soon.add(number)
    out = {"limit": limit, "active": active, "recordings_soon": len(soon), "known": windows is not None}
    if limit <= 0:
        out.update(previews=True, free=None)
        return out
    free = max(0, limit - active - len(soon))
    needed = _PREVIEW_SPARE + (0 if previewing else 1)
    out.update(previews=windows is not None and free >= needed, free=free)
    return out


async def _preview_windows(db) -> "list | None":
    """Jellyfin's recording windows, read at most every _PREVIEW_TIMERS_TTL;
    [] when Jellyfin isn't connected (nothing can record), None when it
    can't be read and the last good answer is too old."""
    global _preview_timers_lock
    url = (get_setting(db, "jellyfin_url", "") or "").strip()
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    if not url or not key:
        return []
    import time
    cache = _preview_timers
    if time.monotonic() - cache["at"] >= _PREVIEW_TIMERS_TTL:
        if _preview_timers_lock is None:
            _preview_timers_lock = asyncio.Lock()
        async with _preview_timers_lock:
            if time.monotonic() - cache["at"] >= _PREVIEW_TIMERS_TTL:
                try:
                    cache["windows"] = await asyncio.to_thread(_preview_timer_windows, url, key)
                    cache["ok_at"] = time.monotonic()
                except Exception as e:
                    logger.debug(f"[LiveTV] preview: Jellyfin's timers not read: {e}")
                cache["at"] = time.monotonic()
    if time.monotonic() - cache["ok_at"] > _PREVIEW_TIMERS_STALE:
        return None
    return cache["windows"]


@router.get("/api/live/preview")
async def live_preview(previewing: bool = False, user=Depends(get_user_from_request),
                       db: Session = Depends(get_db)):
    """May a guide play a muted preview of the focused channel now? For
    anyone signed in (the TV app, through the plugin's
    /TentacleDiscover/LivePreview). {"previews", "limit", "active", "free",
    "recordings_soon", "known"}; `?previewing=true` when the caller's own
    preview is already running (it is in `active`)."""
    windows = await _preview_windows(db)
    loop_now = asyncio.get_running_loop().time()     # the clock reservations end on
    leases = list(_stream_slots.leases.values())
    held = {l.stream_key for l in leases if l.stream_key}
    # A channel reserved ahead of its recording (POST /api/live/reserve) is
    # about to be pulled: it counts like a timer that starts now.
    reserved = [(r["stream_key"], datetime.min, datetime.max)
                for r in list(_reserved_channels.values())
                if r.get("stream_key") and r.get("until", 0) > loop_now]
    if windows is not None:
        windows = list(windows) + reserved
    return preview_answer(overall_stream_limit(db), len(leases), held, windows,
                          datetime.utcnow(), previewing=previewing)


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
        raw = get_setting(db, _EPG_COVERAGE_KEY.format(p.id), "")
        try:
            report = json.loads(raw) if raw else None
        except ValueError:
            report = None
        out.append({"provider_id": p.id, "provider": p.name, "report": report})
    return {"providers": out}


class ReserveRequest(BaseModel):
    channel_id: "int | None" = None
    stream_id: "str | None" = None     # the GuideNumber, or the ExternalChannelId of Jellyfin's timer
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
        if sid.startswith(("hdhr_", "m3u_")):
            sid = _timer_guide_number(sid, lambda: _tentacle_m3u_ids(db)) or ""
        ch = next(iter(channels_for_guide_numbers(db, [sid])), None) if sid else None
    else:
        raise HTTPException(422, "channel_id or stream_id is required")
    if not ch:
        raise HTTPException(404, "Channel not found")
    seconds = max(1, min(int(body.seconds), 24 * 3600))
    _reserved_channels[ch.id] = {"until": asyncio.get_running_loop().time() + seconds,
                                 "stream_key": guide_number(ch, _offsets(db))}
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


def _sync_groups_from_xtream(provider_data: dict, db: Session, gap: float = 0.0) -> dict:
    """Phase 1: Fetch categories/groups from Xtream only. No channels.
    `gap`: at least this many seconds between its two requests (the nightly
    discovery's, services/discovery.DISCOVERY_GAP_SECONDS)."""
    from services.xtream_client import XtreamClient

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]

    client = XtreamClient(
        server=provider_data["server_url"],
        username=provider_data["username"],
        password=provider_data["password"],
        user_agent=provider_data["user_agent"],
    )
    guard = getattr(client, "guard", None)
    if gap and guard is not None:
        guard.gap = max(guard.gap, gap)

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


def _sync_channels_from_xtream(provider_data: dict, db: Session, gap: float = 0.0) -> dict:
    """Phase 2: Fetch channels only for enabled groups from Xtream.
    `gap`: at least this many seconds between its requests (the nightly
    job's, services/discovery.CHANNEL_GAP_SECONDS)."""
    from services.xtream_client import XtreamClient

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]

    client = XtreamClient(
        server=provider_data["server_url"],
        username=provider_data["username"],
        password=provider_data["password"],
        user_agent=provider_data["user_agent"],
    )
    guard = getattr(client, "guard", None)
    if gap and guard is not None:
        guard.gap = max(guard.gap, gap)

    all_failed = False
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
        failed_groups = 0
        answered = set()        # groups that listed at least one channel
        last_error = None
        for i, cat_id in enumerate(category_ids):
            try:
                streams = client.get_live_streams(category_id=cat_id)
                all_streams.extend(streams)
                if streams:
                    answered.add(cat_map[cat_id])
            except ProviderRefusedError:
                raise   # the provider refuses this server: no more groups, no lineup to save (#149)
            except Exception as e:
                failed_groups += 1
                last_error = e
                logger.warning(f"[LiveTV] {provider_name}: failed to fetch category {cat_id}: {e}")

            pct = 5 + int(((i + 1) / len(category_ids)) * 85)
            _set_sync_status(provider_id, {
                "phase": "running", "progress": pct,
                "message": f"Fetching: {i + 1}/{len(category_ids)} groups, {len(all_streams)} channels so far",
            })
            if (i + 1) % 20 == 0 or (i + 1) == len(category_ids):
                logger.info(f"[LiveTV] {provider_name}: {i + 1}/{len(category_ids)} groups, {len(all_streams)} channels")

        # Every enabled group failed (fewer refusals than the guard's limit,
        # or 5xx): an error, not a sync of 0 channels (#630). The requests
        # error text holds the URL with the login: redact it.
        if category_ids and failed_groups == len(category_ids):
            from services.log_redaction import redact
            all_failed = True
            raise ProviderDataError(
                f"none of the {len(category_ids)} enabled group(s) could be read ({redact(str(last_error))})")

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
        if not failed_groups:
            stats.update(_mark_gone_channels(db, provider_id, provider_name, all_streams, answered))

        # Update provider timestamp
        provider = db.query(Provider).filter(Provider.id == provider_id).first()
        if provider:
            provider.last_live_sync = datetime.utcnow()
        db.commit()

        gone_note = f", {stats['gone']} no longer listed" if stats.get("gone") else ""
        log_activity(db, "livetv_sync", f"Live TV channel sync for {provider_name}: {stats['new']} new, "
                                        f"{stats['updated']} updated, {stats['total']} total{gone_note}")
        if category_ids:
            # At least one group read: the provider answers this server (#652)
            provider_health.note(db, provider_id, True, "api")
        return stats

    except Exception as e:
        logger.error(f"[LiveTV] Channel sync failed for {provider_name}: {e}", exc_info=True)
        # Refused (the guard stopped, or every group answered 401/403):
        # provider health hears it, even while its own check stands aside
        # for a recording (#652). Never one group a panel restricts.
        guard = getattr(client, "guard", None)
        refused = guard.refused(all_failed) if guard is not None else None
        if refused in (401, 403):
            provider_health.note(db, provider_id, False, "api", refused)
        raise
    finally:
        client.close()


def _sync_from_m3u_url(provider_data: dict, db: Session) -> dict:
    """Sync live channels from an M3U URL."""
    if not (provider_data.get("m3u_url") or provider_data.get("server_url")):
        raise ValueError("No M3U URL configured")
    return _sync_from_m3u(provider_data, db, "Downloading the playlist...")


def _sync_from_m3u_file(provider_data: dict, db: Session) -> dict:
    """Sync live channels from a local M3U file."""
    if not provider_data.get("m3u_url"):  # For file type, m3u_url stores the file path
        raise ValueError("No M3U file path configured")
    return _sync_from_m3u(provider_data, db, "Reading the playlist file...")


def _sync_from_m3u(provider_data: dict, db: Session, reading: str) -> dict:
    """Live channels from an M3U provider's playlist: only its channels (a
    provider's playlist lists its films and episodes too, which the VOD import
    takes), and what the playlist says about the provider (its panel login,
    its guide address: services/m3u_source.py)."""
    from services import m3u_source

    provider_id = provider_data["id"]
    provider_name = provider_data["name"]
    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if provider is None:
        raise ValueError("Provider not found")
    if not provider_data.get("m3u_url") and provider_data.get("server_url"):
        provider.m3u_url = provider.m3u_url or provider_data["server_url"]   # older callers

    _set_sync_status(provider_id, {"phase": "running", "progress": 10, "message": reading})
    playlist = m3u_source.read(provider)
    channels = playlist.of_kind("live")
    counts = playlist.counts()
    logger.info(f"[LiveTV] {provider_name}: {counts['live']} channels in the playlist "
                f"(and {counts['movie']} films, {counts['series']} episodes, left to the VOD import)")
    m3u_source.learn(db, provider, playlist)
    if channels and not provider.has_live:
        provider.has_live = True

    _set_sync_status(provider_id, {"phase": "running", "progress": 80, "message": f"Saving {len(channels)} channels..."})
    stats = _upsert_channels_from_m3u(provider_id, channels, db)

    provider.last_live_sync = datetime.utcnow()
    db.commit()

    _log_m3u_sync(db, f"Live TV playlist sync for {provider_name}", stats)
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

    first_setup = not by_name
    created = []
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
                created.append(g)
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
    if created:
        # New from the provider: waiting for the admin's review (not on a
        # provider's first list, its setup).
        from services import provider_new
        provider_new.note_created(db, created, first_setup)
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


def _shown_channels(db: Session) -> list:
    """The switched-on channels, in the tuner's order, minus separator rows:
    what Jellyfin, the guide, My Channels and search carry. A separator
    switched on before #158 (or with its whole group) plays nothing and
    would sit among the channels as "####### SPORTSNET PPV #######"; one the
    admin gave a name of their own is a channel."""
    rows = (db.query(LiveChannel)
            .filter(LiveChannel.enabled == True)  # noqa: E712
            .order_by(LiveChannel.sort_order, LiveChannel.channel_number, LiveChannel.name)
            .all())
    now = datetime.utcnow()
    return [ch for ch in rows if not _is_separator(ch.guide_name) and not _gone(ch, now)]


def _hidden_separators(db: Session) -> list:
    """Ids of the switched-on separator rows _shown_channels() leaves out."""
    rows = db.query(LiveChannel).filter(LiveChannel.enabled == True).all()  # noqa: E712
    return sorted(ch.id for ch in rows if _is_separator(ch.guide_name))


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
    renamed_count = 0
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
            if ch.name != name:
                renamed_count += 1
            ch.name = name
            ch.provider_missing_since = None
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
    return {"new": new_count, "updated": updated_count, "renamed": renamed_count, "total": len(seen_ids)}


# An Xtream channel the provider no longer lists: never deleted (the row
# holds the admin's number, name and switch, and Jellyfin keys its
# favourites and timers on the stream id), but marked, and left out of the
# lineup once it has been missing GONE_DAYS: one that comes back is the same
# channel again. Judged only after a complete read (every enabled group
# answered), only in groups that listed channels (an empty answer may be a
# blip), and never more than GONE_MAX_FRACTION of them at once (at least
# GONE_FLOOR): a provider answering with half its lineup is an outage.
GONE_DAYS = 3
GONE_MAX_FRACTION = 0.2
GONE_FLOOR = 25


def _mark_gone_channels(db: Session, provider_id: int, provider_name: str, streams: list,
                        answered: set) -> dict:
    """Mark the channels of the groups in `answered` that `streams` (the
    complete read) no longer lists. Returns {"gone": newly marked} or
    {"gone_refused": n}. Doesn't commit."""
    listed = {str(s.get("stream_id", "")) for s in streams}
    judged = [ch for ch in db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).all()
              if ch.group_title in answered]
    newly = [ch for ch in judged if ch.stream_id not in listed and ch.provider_missing_since is None]
    if not newly:
        return {}
    if len(newly) > max(GONE_FLOOR, int(len(judged) * GONE_MAX_FRACTION)):
        msg = (f"Live TV channel sync for {provider_name}: {len(newly)} of {len(judged)} channels missing "
               f"from the provider's list — looks like an incomplete answer, none marked as gone")
        logger.warning(f"[LiveTV] {msg}")
        log_activity(db, "livetv_sync", msg)
        return {"gone_refused": len(newly)}
    now = datetime.utcnow()
    for ch in newly:
        ch.provider_missing_since = now
    logger.info(f"[LiveTV] {provider_name}: {len(newly)} channel(s) no longer listed by the provider "
                f"(kept; out of the lineup after {GONE_DAYS} days): "
                f"{', '.join(ch.guide_name for ch in newly[:5])}")
    db.flush()
    return {"gone": len(newly)}


def _gone(ch, now: "datetime | None" = None) -> bool:
    """Missing from the provider's list for GONE_DAYS or more."""
    since = getattr(ch, "provider_missing_since", None)
    return isinstance(since, datetime) and since <= (now or datetime.utcnow()) - timedelta(days=GONE_DAYS)


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
# enabled group came back on. Their names are kept per provider
# (_M3U_SWITCHED_OFF_KEY), so one that returns stays off. (Xtream channels are
# never deleted, so they keep the row.)
_M3U_SWITCHED_OFF_MAX = 2000


def _m3u_switched_off(db: Session, provider_id: int) -> list:
    import json
    try:
        names = json.loads(get_setting(db, _M3U_SWITCHED_OFF_KEY.format(provider_id), "") or "[]")
    except ValueError:
        return []
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _drop_m3u_vod_rows(provider_id: int, db: Session) -> int:
    """Films and episodes an older sync stored as channels (it took every line
    of a playlist that lists all three), and the groups only they filled. Not
    a removal the "failed download" guard below should weigh: they were never
    channels."""
    from services.m3u_parser import classify
    rows = db.query(LiveChannel.id, LiveChannel.stream_url, LiveChannel.name, LiveChannel.group_title).filter(
        LiveChannel.provider_id == provider_id).all()
    vod = [r for r in rows if classify({"stream_url": r.stream_url, "name": r.name}) != "live"]
    if not vod:
        return 0
    ids = [r.id for r in vod]
    for i in range(0, len(ids), 500):
        db.query(LiveChannel).filter(LiveChannel.id.in_(ids[i:i + 500])).delete(synchronize_session=False)
    dropped = set(ids)
    gone_groups = {r.group_title for r in vod} - {r.group_title for r in rows if r.id not in dropped}
    if gone_groups:
        db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id,
                                          LiveChannelGroup.name.in_(list(gone_groups)),
                                          LiveChannelGroup.enabled == False).delete(synchronize_session=False)  # noqa: E712
    logger.info(f"[LiveTV] Provider {provider_id}: {len(ids)} films/episodes an older sync took for channels "
                f"removed from Live TV ({len(gone_groups)} group(s) only they filled)")
    return len(ids)


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
    for ch in parsed_channels:
        # A channel with no group still gets one, or it could never be switched on
        ch["group_title"] = (ch.get("group_title") or "").strip() or "Uncategorized"
    _drop_m3u_vod_rows(provider_id, db)
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

    # A name with a comma was read by older builds as the text after its last
    # comma ("UFC 300, Pereira vs Hill" stored as "Pereira vs Hill", #525), so
    # its row's key hashes that cut name. Same URL, cut name: it is that row;
    # move it to the whole name's key and keep its stream_id, as above. Two
    # rows cut to the same name are told apart by their URLs.
    for ch in parsed_channels:
        sid = _m3u_stable_id(ch["name"], ch["stream_url"])
        if sid in existing or "," not in ch["name"]:
            continue
        old = _m3u_stable_id(ch["name"].rsplit(",", 1)[-1].strip(), ch["stream_url"])
        if old in existing and old not in incoming:
            row = existing.pop(old)
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
    created = []
    for name in groups:
        if name not in existing_groups:
            g = LiveChannelGroup(provider_id=provider_id, name=name, enabled=False)
            db.add(g)
            created.append(g)
    if created:
        from services import provider_new
        provider_new.note_created(db, created, first_setup=not existing_groups)

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
        "m3u_guide_url": _m3u_guide_url(provider),
        "channels": [
            {"stream_id": ch.stream_id, "epg_channel_id": ch.epg_channel_id, "name": ch.name}
            for ch in all_channels
        ],
        "enabled_count": sum(1 for ch in all_channels if ch.enabled),
    }


def _m3u_guide_url(provider) -> str:
    """The guide address an M3U provider's playlist names (url-tvg), or ""."""
    if (provider.provider_type or "xtream") == "xtream":
        return ""
    from sqlalchemy.orm import object_session
    from services import m3u_source
    db = object_session(provider)
    if db is not None:
        return m3u_source.guide_url(db, provider.id)
    db = SessionLocal()
    try:
        return m3u_source.guide_url(db, provider.id)
    finally:
        db.close()


def _epg_feed_url(provider_data: dict) -> str | None:
    """The XMLTV feed a guide sync reads: the provider's EPG URL, else the one
    its playlist names (M3U), else its Xtream panel's own (an Xtream provider,
    or an M3U one whose links are a panel's)."""
    if provider_data.get("epg_url"):
        return provider_data["epg_url"]
    if provider_data.get("m3u_guide_url"):
        return provider_data["m3u_guide_url"]
    if provider_data["provider_type"] != "xtream" and not (
            provider_data.get("server_url") and provider_data.get("username")):
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
        last = json.loads(get_setting(db, _EPG_SYNCED_KEY.format(provider.id), "") or "null")
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


def _run_epg_sync_background(provider_data: dict, attempts: int = 3):
    """Background EPG sync — stream-parses full XMLTV, keeps programs for ALL provider channels.
    Every download's outcome is remembered for the guide retry (run_guide_top_up);
    a sync with nothing to download (no channels, no guide URL) is not one."""
    ok = _sync_epg_once(provider_data, attempts)
    if ok is not None:
        _note_epg_attempt(provider_data["id"], bool(ok))
    return bool(ok)


def _sync_epg_once(provider_data: dict, attempts: int = 3):
    """One guide sync of one provider (see _run_epg_sync_background): up to
    `attempts` downloads, 30 s and 60 s apart. None: nothing to download."""
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
                return None
            resolved: dict = {}

            def _resolve(feed_channels):
                resolved.clear()
                resolved.update(resolve_guide_ids(chan_info, feed_channels))
                return {gid for r in resolved.values() for gid in (r["guide_id"], r.get("fallback")) if gid}

            epg_url = _epg_feed_url(provider_data)
            if not epg_url:
                _set_sync_status(pid, {
                    "phase": "epg", "status": "error", "progress": 0,
                    "message": "No EPG URL available.",
                })
                return None

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
            for attempt in range(1, attempts + 1):
                try:
                    suffix = f" (attempt {attempt}/{attempts})" if attempt > 1 else ""
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
                    _epg_last_failure[pid] = {"error": "the feed had no programmes for these channels"}
                    logger.warning(f"[LiveTV] EPG attempt {attempt}/{attempts}: {last_err}")
                except Exception as e:
                    last_err = str(e)
                    _epg_last_failure[pid] = {"error": _guide_error(e), "retry_after": _retry_after_seconds(e)}
                    logger.warning(f"[LiveTV] EPG download attempt {attempt}/{attempts} failed: {e}")
                if attempt < attempts:
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
                _epg_last_failure[pid] = {"error": f"a partial feed ({len(programs)} programmes)"}
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
                matched_ids = {gid for v in resolved.values()
                               for gid in (v["guide_id"], v.get("fallback")) if gid}
                for r in rows:
                    res = resolved.get(r.id) or {}
                    if (res.get("fallback") in with_programmes
                            and res.get("guide_id") not in with_programmes):
                        # Its own guide id brought nothing; the same channel
                        # under the feed's other id did (epg_match).
                        resolved[r.id] = {**res, "method": "name", "guide_id": res["fallback"],
                                          "name_match": res["fallback"]}
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
                # Programmes are keyed by guide id across providers: an id
                # another provider's channel uses as its guide is deleted only
                # when this sync stores it again (#516).
                gid = _guide_id_expr()
                others = {row[0] for row in db.query(gid).filter(
                    LiveChannel.provider_id != pid, gid.isnot(None)).distinct()}
                provider_channel_epg_ids -= others - {p["channel_id"] for p in programs}
            if provider_channel_epg_ids:
                db.query(EPGProgram).filter(
                    EPGProgram.channel_id.in_(provider_channel_epg_ids)
                ).delete(synchronize_session=False)
                db.flush()

            # Insert into DB
            _set_sync_status(pid, {"phase": "epg", "status": "running", "progress": 90, "message": f"Saving {len(programs)} programs..."})
            # One schedule per guide id: a feed that merges several guide
            # sources under one id overlaps itself (services/xmltv.one_schedule).
            from services.xmltv import one_schedule
            programs, trimmed = one_schedule(programs)
            if trimmed:
                logger.info(f"[LiveTV] EPG: {trimmed} programme(s) left out or shortened "
                            f"where the feed's schedules overlap")
            inserted = 0
            batch = []
            for prog in programs:
                batch.append(EPGProgram(
                    channel_id=prog["channel_id"],
                    title=prog["title"] or "",
                    sub_title=prog.get("sub_title"),
                    description=prog.get("description"),
                    start=prog["start"],
                    stop=prog["stop"],
                    category=prog.get("category"),
                    icon_url=prog.get("icon_url"),
                    previously_shown=prog.get("previously_shown"),
                ))
                inserted += 1
                if len(batch) >= 5000:
                    db.add_all(batch)
                    db.flush()
                    batch = []
            if batch:
                db.add_all(batch)
                db.flush()

            # What this sync read, and the guide ids it left empty: refresh-guide
            # re-runs a sync only when that could fill one of them (#466). Read
            # from the rows before the coverage setting below commits: a commit
            # expires them, and a channel removed meanwhile (an M3U playlist sync
            # can do that) would then fail the whole sync on reload (#518).
            import json
            kept = {p["channel_id"] for p in programs}
            synced_record = json.dumps({
                "inputs": inputs,
                "empty": sorted({r.guide_epg_id for r in rows} - {None} - kept),
            })

            # How many channels actually have a guide, and why the rest do not:
            # "success" alone hid that most channels had nothing (#141).
            coverage_note = ""
            if resolved:
                from services.epg_match import coverage_report, coverage_summary
                report = coverage_report(chan_info, resolved, kept)
                report["at"] = datetime.utcnow().isoformat() + "Z"
                set_setting(db, _EPG_COVERAGE_KEY.format(pid), json.dumps(report))
                coverage_note = f" — {coverage_summary(report)}"

            set_setting(db, _EPG_SYNCED_KEY.format(pid), synced_record)

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
        _epg_last_failure[pid] = {"error": "the guide could not be stored"}
        _set_sync_status(pid, {"phase": "epg", "status": "error", "progress": 0, "message": str(e)})
        return False


# ─── Guide top-up and retry ─────────────────────────────────────────────────
#
# The nightly job downloads the guide once a day and, if the download fails,
# keeps the old guide until the next night. On a live install the provider's
# feed reaches only a day or so ahead (a sync at 07:17 UTC kept programmes up
# to 05:15 the next morning), and the provider refused the nightly download
# on 19 of 20 nights (403) while it served the same feed at other hours, often
# minutes later: the nightly VOD sync had just read its categories, and the
# provider refused that account's API for a while after the burst. "Kept
# existing guide data" kept a guide that had already ended, and every channel
# showed an empty guide for days (#709). So every few minutes the guide of each
# Live TV provider is checked:
# - a guide sync that failed (nightly, manual or this one; 403, timeout, a bad
#   feed) is tried again after _GUIDE_RETRY_AFTER: 5, 15, 30 minutes, then
#   every hour until one works (longer when the provider sent Retry-After);
# - a guide that worked is synced again once what is stored ends within
#   GUIDE_TOP_UP_BELOW, at most every _GUIDE_RESYNC_AFTER;
# - a guide failing for GUIDE_ALERT_AFTER, or one that has ended, is a problem
#   for the admins (guide_problem: services.admin_alerts tells them once, in
#   Jellyfin and on Pushover, and the dashboard's provider banner shows it).

GUIDE_TOP_UP_BELOW = timedelta(hours=12)
GUIDE_TOP_UP_EVERY = timedelta(minutes=5)
# A check comes every GUIDE_TOP_UP_EVERY, its first one at start-up's pace,
# not the failure's: a retry due a few seconds after a check would otherwise
# wait for the next one.
_GUIDE_CHECK_SLACK = timedelta(minutes=1)
_GUIDE_RETRY_AFTER = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=1))
# A Retry-After longer than this is read as this: the guide must not wait a day.
_GUIDE_RETRY_AFTER_MAX = timedelta(hours=6)
# After a sync that worked: what it could get is stored, another one within
# this long would read the same feed (8 h disk cache) or a barely newer one.
_GUIDE_RESYNC_AFTER = timedelta(hours=6)
GUIDE_ALERT_AFTER = timedelta(hours=6)
# Each provider's last guide sync in the setting _GUIDE_TOP_UP_KEY:
# {"at": UTC end, "failures": failed syncs in a row, "since": the first of
# them, "error": the last one in a few words, "retry_at": the provider's
# Retry-After}. A restart (some installs
# restart many times a day) neither forgets a failure nor retries it sooner,
# and the first check comes a few minutes after start-up (main.setup_scheduler).
_guide_attempt_lock = threading.Lock()
_guide_top_up_lock = threading.Lock()
# What the last failed download of each provider said (_sync_epg_once), read
# by _note_epg_attempt right after it: {"error": text, "retry_after": seconds}.
_epg_last_failure: dict = {}
# A provider-health verdict older than this doesn't slow the retries (see
# _guide_server_refuses): two hourly checks.
_GUIDE_HEALTH_FRESH = timedelta(hours=2)


def _guide_error(e) -> str:
    """A failed download in a few words for the banner and the phone: never a
    URL (it carries the login)."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status:
        return f"HTTP {status}"
    import requests as _req
    if isinstance(e, _req.Timeout):
        return "no answer (timed out)"
    if isinstance(e, _req.ConnectionError):
        return "could not connect"
    return "unreadable feed"


def _retry_after_seconds(e):
    """The provider's Retry-After (seconds or an HTTP date) on a refused
    download, or None."""
    from services.xtream_client import retry_after_seconds
    return retry_after_seconds(e)


def _epg_record(db: Session, provider_id: int) -> dict | None:
    """The provider's guide sync record (see _GUIDE_TOP_UP_KEY), or None."""
    import json
    try:
        rec = json.loads(get_setting(db, _GUIDE_TOP_UP_KEY.format(provider_id), "") or "null")
        rec["at"] = datetime.fromisoformat(rec["at"])
        rec["failures"] = int(rec.get("failures") or 0)
        for key in ("since", "retry_at"):
            rec[key] = datetime.fromisoformat(rec[key]) if rec.get(key) else None
        return rec
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _epg_attempt(db: Session, provider_id: int):
    """(UTC end of the provider's last guide sync, failed syncs in a row), or None."""
    rec = _epg_record(db, provider_id)
    return (rec["at"], rec["failures"]) if rec else None


def _save_epg_record(db: Session, provider_id: int, rec: dict) -> None:
    import json
    set_setting(db, _GUIDE_TOP_UP_KEY.format(provider_id), json.dumps(
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in rec.items()}))


def _note_epg_attempt(provider_id: int, ok: bool) -> None:
    """Remember a guide sync's outcome for the top-up. Never raises."""
    failure = _epg_last_failure.pop(provider_id, None) or {}
    db = SessionLocal()
    try:
        with _guide_attempt_lock:
            if db.get(Provider, provider_id) is None:
                return      # deleted while it synced: its id may go to the next provider (#625)
            prev = _epg_record(db, provider_id) or {}
            now = datetime.utcnow()
            if ok:
                rec = {"at": now, "failures": 0}
            else:
                failures = int(prev.get("failures") or 0) + 1
                retry = failure.get("retry_after")
                rec = {"at": now, "failures": failures,
                       "since": (prev.get("since") if failures > 1 else None) or now,
                       "error": failure.get("error") or "error",
                       "retry_at": now + min(timedelta(seconds=retry), _GUIDE_RETRY_AFTER_MAX) if retry else None}
                wait = _guide_retry_wait(rec)
                logger.warning(f"[LiveTV] Guide download of provider {provider_id} failed "
                               f"({failures} in a row, {rec['error']}): trying again in "
                               f"{int(wait.total_seconds() // 60)} min")
            _save_epg_record(db, provider_id, rec)
    except Exception as e:
        logger.warning(f"[LiveTV] Could not note the guide sync of provider {provider_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _guide_retry_wait(rec: dict, server_refuses: bool = False) -> timedelta:
    """How long after its last failed sync the guide is tried again."""
    wait = _GUIDE_RETRY_AFTER[min(rec["failures"], len(_GUIDE_RETRY_AFTER)) - 1]
    if server_refuses:
        # Provider health finds the server refusing its API: the quick retries
        # would only be refused too. Once an hour still asks.
        wait = max(wait, _GUIDE_RETRY_AFTER[-1])
    if rec.get("retry_at"):
        wait = max(wait, rec["retry_at"] - rec["at"])
    return wait


def _guide_server_refuses(db: Session, provider) -> bool:
    """Provider health has lately confirmed that the provider's server refuses
    it (login, 403, expired, not answering: the "api" step), and the guide
    comes from that server: a download now would likely be refused too. A
    refused stream says nothing about the guide; a guide from another host
    (the provider's EPG URL) isn't judged by it. Only a recent verdict counts:
    the hourly check stands aside while live TV plays, and a failure only a
    check or sync that works clears could otherwise slow the guide all
    evening after the provider recovered."""
    entry = provider_health.load_state(db).get(str(provider.id))
    if not isinstance(entry, dict) or entry.get("state") != "failing" or entry.get("step") != "api":
        return False
    try:
        checked = datetime.fromisoformat(entry.get("checked_at") or "")
    except (TypeError, ValueError):
        return False
    if checked.tzinfo is None or datetime.now().astimezone() - checked > _GUIDE_HEALTH_FRESH:
        return False
    if not provider.epg_url:
        return (provider.provider_type or "xtream") == "xtream"     # Xtream's own xmltv.php

    def host(url):
        try:
            return (urlsplit(url or "").hostname or "").lower() or None
        except ValueError:
            return None
    return host(provider.epg_url) in {host(provider.server_url), host(provider.m3u_url)} - {None}


def _guide_ends_at(db: Session, provider_id: int):
    """When the stored guide of the provider's enabled channels ends: the
    median of each guide's last programme (None: none of them has any). Not
    the latest: on a live install one channel's schedule ran six weeks ahead
    while the rest ended the next morning, and the latest would never have
    come within GUIDE_TOP_UP_BELOW."""
    gid = _guide_id_expr()
    ids = sorted({row[0] for row in db.query(gid).filter(
        LiveChannel.provider_id == provider_id, LiveChannel.enabled == True,  # noqa: E712
        gid.isnot(None)).distinct()})
    ends = []
    for chunk in _chunked(ids):
        ends += [last for _, last in db.query(EPGProgram.channel_id, func.max(EPGProgram.stop)).filter(
            EPGProgram.channel_id.in_(chunk)).group_by(EPGProgram.channel_id) if last is not None]
    if not ends:
        return None
    ends.sort()
    return ends[(len(ends) - 1) // 2]


def _has_enabled_channels(db: Session, provider_id: int) -> bool:
    return db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id,
                                        LiveChannel.enabled == True).count() > 0  # noqa: E712


def guide_top_up_due(db: Session, provider, now: datetime | None = None) -> str | None:
    """Why the provider's guide should be synced now, or None."""
    now = now or datetime.utcnow()
    if not _has_enabled_channels(db, provider.id):
        return None
    status = _get_sync_status(provider.id)
    if status.get("phase") == "epg" and status.get("status") == "running":
        return None
    rec = _epg_record(db, provider.id)
    if rec is not None and rec["failures"]:
        # A failed sync is tried again whatever the stored guide still covers.
        if now - rec["at"] < _guide_retry_wait(rec, _guide_server_refuses(db, provider)) - _GUIDE_CHECK_SLACK:
            return None
        return f"failed to download {rec['failures']} time(s) in a row ({rec.get('error') or 'error'})"
    if rec is not None and now - rec["at"] < _GUIDE_RESYNC_AFTER:
        return None
    end = _guide_ends_at(db, provider.id)
    if end is None:
        # No enabled channel has ever had programmes: a feed that matches
        # none of them is what the coverage report explains, and downloading
        # it every few hours can't change that.
        return None
    if end - now > GUIDE_TOP_UP_BELOW:
        return None
    if _guide_server_refuses(db, provider):
        return None     # the next check after provider health clears it
    if end <= now:
        return f"ran out at {end:%Y-%m-%d %H:%M} UTC"
    return f"ends at {end:%Y-%m-%d %H:%M} UTC"


def _local(utc: datetime) -> datetime:
    """A naive UTC time as the container's local time (what the banner shows)."""
    from datetime import timezone
    return utc.replace(tzinfo=timezone.utc).astimezone()


def guide_problem(db: Session, provider, now: datetime | None = None) -> str | None:
    """What is wrong with the provider's guide, for the admin, or None: its
    download has failed for GUIDE_ALERT_AFTER, or the stored guide of its
    enabled channels has ended."""
    now = now or datetime.utcnow()
    if not provider.live_tv_enabled or not _has_enabled_channels(db, provider.id):
        return None
    rec = _epg_record(db, provider.id) or {}
    end = _guide_ends_at(db, provider.id)
    failing = rec.get("failures") and rec.get("since") and now - rec["since"] >= GUIDE_ALERT_AFTER
    ended = end is not None and end <= now
    if not (failing or ended):
        return None
    local_now = _local(now)
    tries = (f"{rec['failures']} download(s) failed since "
             f"{provider_health._clock(_local(rec.get('since') or rec['at']), local_now)} "
             f"({rec.get('error') or 'error'})") if rec.get("failures") else ""
    if ended:
        text = f"The Live TV guide ran out at {provider_health._clock(_local(end), local_now)}"
        return f"{text}; {tries}. Tentacle keeps trying." if tries else f"{text}."
    return f"The Live TV guide isn't downloading: {tries}. Tentacle keeps trying."


def start_jellyfin_guide_refresh(db: Session) -> None:
    """Start Jellyfin's Refresh Guide task, so it reads the guide just synced."""
    import requests as req
    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    if not (jf_url and jf_key):
        return
    try:
        headers = {"X-Emby-Token": jf_key}
        tasks = req.get(f"{jf_url}/ScheduledTasks", headers=headers, timeout=10).json()
        guide_task = next((t for t in tasks if t.get("Key") == "RefreshGuide"), None)
        if guide_task:
            req.post(f"{jf_url}/ScheduledTasks/Running/{guide_task['Id']}", headers=headers, timeout=10)
            logger.info("Jellyfin guide refresh triggered after EPG sync")
    except Exception as e:
        logger.error(f"Jellyfin guide refresh failed: {e}")


def refresh_jellyfin_guide_after_sync(db: Session) -> bool:
    """What the Live TV page does once a channel sync and its guide are done
    (POST /api/live/refresh-guide): re-create Tentacle's guide source in
    Jellyfin, so channels new to the lineup get their programmes, run its
    Refresh Guide, then tag the channels it made and tell the open clients
    (services/livetv_tuners.py). The nightly job's, after its channel and
    guide sync. False when Jellyfin isn't set up or couldn't be asked."""
    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    if not (jf_url and jf_key):
        return False
    try:
        from services.jellyfin_guide import refresh_jellyfin_guide
        refresh_jellyfin_guide(jf_url, jf_key, then_tag=True)
        logger.info("Jellyfin guide refresh triggered after the channel and EPG sync")
        return True
    except Exception as e:
        logger.error(f"Jellyfin guide refresh failed: {e}")
        return False


# When the nightly job's own guide step last finished (main._run_scheduled_sync):
# after it, the rest of the nightly job asks no provider anything, so a failed
# nightly download is retried while it still runs.
nightly_guide_done_at: datetime | None = None


def _nightly_holds_guide() -> bool:
    from routers.sync import _after_sync
    started = _after_sync.get("nightly")
    if not started:
        return False
    return nightly_guide_done_at is None or nightly_guide_done_at < started


def run_guide_top_up() -> None:
    """Scheduled every GUIDE_TOP_UP_EVERY: sync the guide of every Live TV
    provider whose last sync failed or whose guide is running out
    (guide_top_up_due), one provider and one download at a time. Stands aside
    for a protected recording, like the nightly download, and while a VOD
    sync or the nightly job up to its own guide step runs (one more request
    in the middle of that burst is one more the provider may refuse); the
    next check tries again. One download per sync: the back-off is the retry
    (the nightly's quick retries are for a one-off bad read)."""
    from services.provider_activity import recording_protected
    from routers.sync import _running_syncs
    if _running_syncs or _nightly_holds_guide():
        logger.info("[LiveTV] Guide top-up waits: a sync is running")
        return
    if not _guide_top_up_lock.acquire(blocking=False):
        return      # one already runs: never two downloads at once
    db = SessionLocal()
    try:
        synced = False
        for provider in live_tv_providers(db):
            reason = guide_top_up_due(db, provider)
            if not reason:
                continue
            if recording_protected(db):
                logger.info("[LiveTV] Guide top-up waits: a recording is running and recording protection is on")
                break
            rows = db.query(LiveChannel).filter(LiveChannel.provider_id == provider.id).all()
            logger.info(f"[LiveTV] The guide of '{provider.name}' {reason}: syncing it again")
            _set_sync_status(provider.id, {"phase": "epg", "status": "running", "progress": 0,
                                           "message": f"The guide {reason}: syncing it again..."})
            if _run_epg_sync_background(_epg_provider_data(provider, rows), attempts=1):
                synced = True
        if synced:
            start_jellyfin_guide_refresh(db)
    except Exception as e:
        logger.error(f"[LiveTV] Guide top-up failed: {e}")
    finally:
        db.close()
        _guide_top_up_lock.release()


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
                "custom_logo_url": ch.custom_logo_url,
                "guide_logo": ch.guide_logo,
                "group_title": ch.group_title,
                "epg_channel_id": ch.epg_channel_id,
                "epg_id_override": ch.epg_id_override,
                "epg_name_match": ch.epg_name_match,
                "guide_epg_id": ch.guide_epg_id,
                "epg_match": ch.epg_match,
                "has_epg_data": ch.guide_epg_id in epg_ids_with_programs if ch.guide_epg_id else False,
                "enabled": ch.enabled,
                "sort_order": ch.sort_order,
                "provider_missing_since": (ch.provider_missing_since.isoformat() + "Z"
                                           if ch.provider_missing_since else None),
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
    if update.enabled is not None or update.custom_name is not None or update.sort_order is not None:
        _pool_changed(f"channel '{ch.guide_name}' changed")
    return {"success": True}


def _pool_changed(what: str) -> None:
    """The channels the household has changed: the lineup's tuner in
    Jellyfin follows (services/livetv_tuners.py, a few seconds later)."""
    from services.livetv_tuners import schedule_sync
    schedule_sync(what, refresh=True)


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
    _pool_changed("channels switched on or off")
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
    _pool_changed("channels switched on or off")
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
    if changing_names:
        _pool_changed("channel groups switched on or off")
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
    _pool_changed("a channel group switched on or off")
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
        refresh_jellyfin_guide(jf_url, jf_key, then_tag=True)
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


# ─── Channel numbers with several Live TV providers ────────────────────────
#
# Jellyfin's HDHomeRun tuner names a channel hdhr_<GuideNumber> and keys its
# timers, favourites and guide on it. The GuideNumber is the channel's
# stream_id, which is unique only within one provider: two Live TV providers
# could hand Jellyfin the same number, and Jellyfin would merge the two
# channels. Each provider's numbers get an offset of their own instead:
# the first Live TV provider keeps its own numbers (offset 0, so an install
# that had one provider sees no change), every other one is given a round
# offset above all the numbers in use, once, and keeps it
# (Provider.channel_number_offset, shown and editable on the provider card).

def _numeric_ids(db) -> list:
    return [int(sid) for (sid,) in db.query(LiveChannel.stream_id).all() if str(sid or "").isdigit()]


def ensure_channel_offsets(db) -> dict:
    """provider id -> offset for every provider with Live TV on, giving
    one to a provider that has none yet. Commits only when it gave one."""
    providers = (db.query(Provider).filter(Provider.live_tv_enabled == True)  # noqa: E712
                 .order_by(Provider.id).all())
    taken = {p.channel_number_offset for p in providers if p.channel_number_offset is not None}
    changed = False
    for p in providers:
        if p.channel_number_offset is not None:
            continue
        if 0 not in taken:
            p.channel_number_offset = 0
        else:
            ids = _numeric_ids(db)
            step = 10 ** max(6, len(str(max(ids))) if ids else 0)
            n = 1
            while n * step in taken:
                n += 1
            p.channel_number_offset = n * step
        taken.add(p.channel_number_offset)
        changed = True
    if changed:
        db.commit()
    return {p.id: p.channel_number_offset or 0 for p in providers}


def _offsets(db) -> dict:
    """provider id -> offset, without writing: providers not given one yet
    count as 0 (ensure_channel_offsets gives them theirs)."""
    return {pid: off or 0 for pid, off in db.query(Provider.id, Provider.channel_number_offset).all()}


def guide_number(ch, offsets: dict) -> str:
    """The number Jellyfin knows this channel by (lineup GuideNumber, XMLTV
    and M3U channel id, hdhr_<n> in its timers)."""
    sid = str(ch.stream_id or ch.id)
    offset = offsets.get(ch.provider_id) or 0
    if not offset:
        return sid
    return str(offset + int(sid)) if sid.isdigit() else f"{offset}{sid}"


def channels_for_guide_numbers(db, numbers) -> list:
    """The channels Jellyfin means by these GuideNumbers."""
    numbers = {str(n) for n in numbers or ()}
    if not numbers:
        return []
    offsets = _offsets(db)
    # The stream ids these numbers can stand for, one per offset in use
    sids = set(numbers)
    for off in {o for o in offsets.values() if o}:
        for n in numbers:
            if n.isdigit() and int(n) > off:
                sids.add(str(int(n) - off))
            elif n.startswith(str(off)):
                sids.add(n[len(str(off)):])
    rows = db.query(LiveChannel).filter(LiveChannel.stream_id.in_(list(sids))).all()
    ids_only = [n for n in numbers if n.isdigit()]
    if ids_only:   # a channel without a stream_id goes by its row id
        rows += db.query(LiveChannel).filter(LiveChannel.stream_id.is_(None),
                                             LiveChannel.id.in_([int(n) for n in ids_only])).all()
    return [ch for ch in rows if guide_number(ch, offsets) in numbers]


def _unique_numbers(db, channels: list) -> list:
    """(channel, number) for the lineup, the XMLTV and the M3U: a number
    already taken (two providers' ranges overlapping after an edit) is left
    out, and said once, rather than handed to Jellyfin twice."""
    offsets = {**_offsets(db), **ensure_channel_offsets(db)}
    seen, out = set(), []
    for ch in channels:
        n = guide_number(ch, offsets)
        if n in seen:
            if (ch.id, n) not in _number_clash_logged:
                _number_clash_logged.add((ch.id, n))
                logger.warning(f"[LiveTV] Channel '{ch.name}' would have number {n}, which another provider's "
                               f"channel has: left out. Change one provider's channel numbers on its card.")
            continue
        seen.add(n)
        out.append((ch, n))
    return out


_number_clash_logged: set = set()


# ─── HDHomeRun Emulation ───────────────────────────────────────────────────


def _hdhr_tuner_count(db) -> int:
    """How many tuners the device reports: at least the stream ceiling over
    every provider, so Jellyfin (which takes this count when it discovers
    the device, and lists that many tuners) never refuses a stream Tentacle
    would carry. The real caps are each provider's "Streams at once";
    hdhr_tuner_count only raises it, or sets it when there is no ceiling."""
    try:
        configured = max(1, int(get_setting(db, "hdhr_tuner_count", "3")))
    except (ValueError, TypeError):
        configured = 3
    return max(configured, overall_stream_limit(db))


@router.get("/discover.json")
@router.get("/hdhr/discover.json")
def hdhr_discover(request: Request, db: Session = Depends(get_db)):
    """HDHomeRun device discovery endpoint."""
    tuner_count = _hdhr_tuner_count(db)
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
    channels = _shown_channels(db)

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
    for ch, number in _unique_numbers(db, channels):
        # stream_id (plus the provider's offset) is the stable channel number:
        # it never shifts when channels are added or removed
        entry = {
            "GuideNumber": str(number),
            "GuideName": ch.guide_name,
            "URL": f"{base_url}/api/live/stream/{ch.id}",
        }
        if ch.guide_logo:
            entry["LogoUrl"] = ch.guide_logo
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


def _tuner_channel(db: Session, channel_id: int):
    """The channel the tuner may stream: one it lists. The lineup, the M3U and
    the guide all list enabled channels only, so the stream routes answer a
    disabled channel the same as an unknown id."""
    return db.query(LiveChannel).filter(LiveChannel.id == channel_id,
                                        LiveChannel.enabled == True).first()  # noqa: E712


@router.head("/api/live/stream/{channel_id}")
async def stream_head(channel_id: int, db: Session = Depends(get_db)):
    """HEAD handler for stream URLs — Jellyfin sends HEAD to validate before playing."""
    channel = _tuner_channel(db, channel_id)
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


@router.get("/api/live/logo/{name}")
def channel_logo_copy(name: str):
    """Tentacle's copy of a channel logo (services/channel_logos.py, #137):
    what the guide gets for a provider logo that went dead. Open like the
    tuner's own addresses (Jellyfin fetches it without a sign-in); the name
    is the copy's hash, nothing else is reachable."""
    from services import channel_logos
    found = channel_logos.cached_file(name)
    if not found:
        raise HTTPException(404, "No such logo")
    path, media_type = found
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/live/stream/{channel_id}")
async def stream_proxy(channel_id: int, db: Session = Depends(get_db)):
    """Stream proxy for IPTV channels.

    Strategy: resolve the provider's redirect chain (requires TiviMate UA)
    to get the tokenized URL on the real streaming server, then either:
      1. 302 redirect Jellyfin there (if the server serves raw TS), or
      2. Proxy the HLS stream as continuous MPEG-TS bytes (fetch m3u8,
         download chunks, pipe raw bytes).
    """
    # A channel the tuner does not list (disabled) takes no slot, stops no
    # viewer and attaches to nothing: refuse it before anything else.
    if not _tuner_channel(db, channel_id):
        raise HTTPException(404, "Channel not found")
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
    # A viewer joining starts inside the upstream's last seconds, on a
    # keyframe with sound (services/ts_start); a recording as it is now.
    viewer = None
    while True:
        if viewer is None and (channel_id in _shared_streams or channel_id in _pending_opens):
            viewer = _joins_as_viewer(channel_id, await _recording_channel_ids(db))
        async with _get_shared_lock():
            shared = _shared_streams.get(channel_id)
            if shared is not None and not shared._closed:
                q = shared.subscribe(backlog=bool(viewer))
                logger.info(f"[LiveTV] Channel {channel_id} already streaming — "
                            f"attaching {'viewer' if viewer else 'recording'} ({len(shared.subscribers)} now)")
                return _SubscriberResponse(shared, q, viewer=bool(viewer))
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


def _joins_as_viewer(channel_id: int, recording_ids: set) -> bool:
    """Whether a client attaching to a channel's running pull is a person
    watching (keyframe start from the backlog) or the recording arriving
    (its bytes as they come).

    "The channel is being recorded" alone said recording, so someone who
    tuned to a channel while it recorded got the recording's start: no
    backlog and no keyframe, a second or two of nothing on a Jellyfin that
    opens a tuner stream per client (stream sharing off, or the channel on
    two tuners; measured 1.6 s on a 2 s GOP). Once the pull is the
    recording's own (its lease says so), whoever attaches next is watching.
    A second recording of the same channel then also gets a viewer's start,
    which only puts a few earlier seconds and a clean keyframe at its head."""
    if channel_id not in recording_ids:
        return True
    lease = _stream_slots.lease_for(f"channel:{channel_id}")
    return lease is not None and lease.kind == "recording"


async def _open_shared_upstream(channel_id: int, db: Session, pending: "asyncio.Future"):
    """Open the upstream for a channel this request is the first to ask for."""
    # Free the provider from the stream checker now, not after the recording
    # lookup below (acquire_lease asks again: harmless).
    await _preempt_checker(f"channel {channel_id} is opening")
    # Cap concurrent upstream pulls (see _StreamSlots). A refusal here is how a
    # scheduled recording silently becomes a zero-byte file -- Jellyfin shows the
    # timer as having run -- so it is logged as an error, by channel name, and
    # counted where the dashboard can see it (GET /api/live/capacity).
    ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
    ch_provider = db.get(Provider, ch.provider_id) if ch is not None and ch.provider_id else None
    limit = overall_stream_limit(db)
    provider_limit = provider_stream_limit(db, ch_provider) if ch_provider is not None else 0
    # A recording outranks a viewer at capacity (see _StreamSlots). Whether
    # this pull is one comes from Jellyfin's own timers, or a reservation.
    # When a slot would have to be taken from someone, ask afresh so the
    # victim is chosen on current information (a recording that opened a
    # moment ago may still be classified as a viewer: sync_recordings).
    at_capacity = (limit > 0 and _stream_slots.active >= limit) or (
        provider_limit > 0 and _stream_slots.provider_active(ch.provider_id if ch else None) >= provider_limit)
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
    if kind == "live":
        # An event that starts later: the provider would answer slowly with its
        # placeholder. Say so at once, without a connection.
        from services import livetv_slate
        lines = livetv_slate.early(_channel_event(ch))
        slate = await _nothing_on_response(channel_id, lines) if lines else None
        if slate is not None:
            return slate
    stream_key = guide_number(ch, _offsets(db)) if ch else None
    # #300: a reservation is inside its programme; a timer says where it is.
    reserved_now = channel_id in _reserved_channels
    phase = "programme" if reserved_now else ((_recording_cache.get("phases") or {}).get(stream_key)
                                              or ("programme", None))[0]
    try:
        lease = await _stream_slots.acquire_lease(limit, _SLOT_WAIT_SECONDS, kind, f"channel:{channel_id}",
                                                  stream_key=stream_key,
                                                  certain=certain,
                                                  provider_id=ch.provider_id if ch else None,
                                                  provider_limit=provider_limit,
                                                  phase=phase, fresh_since=open_began)
    except RecordingProtected:
        raise _TunerRefusal(PROTECTED_REFUSAL_DETAIL)
    if lease is None:
        name = ch.name if ch else f"channel {channel_id}"
        _stream_slots.refused += 1
        own = provider_limit > 0 and _stream_slots.provider_active(ch.provider_id if ch else None) >= provider_limit
        shown = provider_limit if own else limit
        _stream_slots.last_refused = {"channel_id": channel_id, "channel": name, "kind": kind,
                                      "at": datetime.utcnow().isoformat() + "Z", "limit": shown,
                                      "provider": ch_provider.name if own and ch_provider else None}
        where = f"{ch_provider.name}'s {provider_limit}" if own and ch_provider else f"{limit}"
        logger.error(f"[LiveTV] At capacity ({where} streams) — REFUSED {kind} '{name}' (channel {channel_id}). "
                     f"If this was a recording it is lost. Raise the provider's \"Streams at once\" on its "
                     f"card (Sources) if its account can carry more.")
        if kind == "live":
            # A viewer is told, on the player, that the connections are taken
            from services import livetv_slate
            slate = await _nothing_on_response(channel_id, livetv_slate.connection_in_use(shown),
                                               "connection in use")
            if slate is not None:
                return slate
        raise _TunerRefusal(f"Too many concurrent live streams (limit {shown})")
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
        channel = _tuner_channel(db, channel_id)
        if not channel:
            raise HTTPException(404, "Channel not found")

        provider = db.query(Provider).filter(Provider.id == channel.provider_id).first()
        user_agent = (provider.user_agent if provider else None) or "TiviMate/4.7.0 (Linux; Android 12)"
        lease.provider_id, lease.channel_id = channel.provider_id, channel_id
        lease.account = _account_key(provider)

        stream_url, fallback_url = _live_open_urls(db, provider, channel)
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
        from services.m3u_source import guard_origin
        guard = (await asyncio.to_thread(lan_origin_guard, guard_origin(provider))) if provider else is_safe_url
        allowed, why = await check_url(guard, stream_url)
        if not allowed:
            logger.warning(f"[LiveTV] Blocked stream URL for channel {channel_id} "
                           f"({why or 'non-public host'}): {stream_url}")
            raise HTTPException(502, refusal_text(why))

        upstream = await _stream_proxy_inner(channel_id, user_agent, stream_url,
                                             _release_sem, guard,
                                             failure_budget=_reconnect_budget(db),
                                             is_recording=lambda: lease.kind == "recording",
                                             rival_delivering=lambda: _rival_delivering(lease),
                                             refresh_recordings=_refresh_recordings_bounded,
                                             fallback_url=fallback_url,
                                             nothing_on=lambda: _placeholder_picture(ch))
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
        return _SubscriberResponse(shared, q, viewer=lease.kind == "live")
    except _PlaceholderRefusal:
        _release_sem()
        slate = await _nothing_on_response(channel_id, _placeholder_lines(ch)) if lease.kind == "live" else None
        if slate is None:
            raise
        return slate
    except HTTPException as e:
        _release_sem()
        if e.status_code == 502 and str(e.detail).startswith(_PROVIDER_FAILED) and ch is not None:
            # Per channel, for "some channels only" and a quick look at the
            # provider when several fail (services/provider_status)
            from services import provider_status
            provider_status.channel_failed(ch.provider_id, channel_id, ch.guide_name or ch.name,
                                           _detail_code(e.detail), bind=db.get_bind())
        if lease.kind != "live" or e.status_code != 502 or not str(e.detail).startswith(_PROVIDER_FAILED):
            raise
        # The provider didn't deliver: the viewer is told so on the player
        # (not a spinner, then "playback error"). While another stream of
        # this provider runs here, its refusal is most likely the account's
        # connection limit. Asked afresh whether it's a recording ("Record
        # now" may be younger than the cached answer): a recording keeps the
        # refusal, so Jellyfin retries it instead of recording the slate.
        if channel_id in await _recording_channel_ids(db, force=True):
            raise
        from services import livetv_slate
        others = _stream_slots.provider_active(ch.provider_id if ch else None)
        said = None if others or ch is None else await asyncio.to_thread(
            _provider_slate, db.get_bind(), ch.provider_id, channel_id)
        lines, reason = ((livetv_slate.connection_in_use(provider_limit or limit), "connection in use") if others
                         else (said, "provider not delivering") if said
                         else (livetv_slate.not_coming_through(), "provider not delivering"))
        slate = await _nothing_on_response(channel_id, lines, reason)
        if slate is None:
            raise
        return slate
    except BaseException:
        _release_sem()
        raise


async def _stream_proxy_inner(channel_id: int, user_agent: str, stream_url: str, _release_sem,
                              guard=None, failure_budget: float = _DEFAULT_RECONNECT_BUDGET,
                              is_recording=None, rival_delivering=None, refresh_recordings=None,
                              fallback_url=None, nothing_on=None):
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
    it is taken on a fresh answer from Jellyfin about what is recording.

    `fallback_url`, when given, is opened instead if `stream_url` is refused
    with a status that will not fix itself (a panel that does not serve the
    channel as .ts: see _live_open_urls); the stream then runs on it.

    `nothing_on`, when given, makes the "Nothing on right now" picture
    (bytes, or None): a viewer's raw stream that meets the provider's
    placeholder before it delivered anything plays it and ends, instead of
    waiting the placeholder out."""
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
        fell_back = False
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
            except _ProviderPlaceholder as e:
                await _refuse_placeholder(channel_id, e.segment)
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
                if (fallback_url is not None and not retryable
                        and isinstance(e, httpx.HTTPStatusError)):
                    logger.warning(f"[LiveTV] Opening channel {channel_id}: the provider answered "
                                   f"{e.response.status_code} for the continuous stream; opening it as HLS")
                    stream_url = open_url = fallback_url
                    fallback_url = None
                    fell_back = True
                    continue
                if not retryable or waited + open_backoff > _OPEN_RETRY_BUDGET:
                    logger.error(f"[LiveTV] Tokenized URL failed for channel {channel_id}"
                                 f"{f' after {waited:.0f}s of retries' if waited >= 1 else ''}: {e}")
                    # The status or the error type, not the text: httpx puts the
                    # request URL in it, and for Xtream that path holds the
                    # account (/live/<user>/<pass>/). The log line above is redacted.
                    why = (f"the provider answered {e.response.status_code}"
                           if isinstance(e, httpx.HTTPStatusError) else type(e).__name__)
                    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                        _tell_provider_health(channel_id, False, e.response.status_code)
                    raise HTTPException(502, f"{_PROVIDER_FAILED}: {why}")
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

        if fell_back:
            _ts_refused_channels.add(channel_id)
        tokenized_url = str(resp.url)
        logger.info(f"[LiveTV] Resolved tokenized URL for channel {channel_id}: {tokenized_url}")

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
                if not variant or not (await check_url(guard, variant))[0]:
                    break
                v_resp = None
                try:
                    v_resp = await asyncio.wait_for(
                        _send_checked(client, variant, {"User-Agent": user_agent}, guard), 10.0)
                    v_resp.raise_for_status()
                    variant_text = (await asyncio.wait_for(v_resp.aread(), 10.0)).decode(
                        "utf-8", errors="replace")
                except _PlaceholderRefusal:
                    raise
                except _ProviderPlaceholder as e:
                    await _refuse_placeholder(channel_id, e.segment)
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
            HEALTHY_AFTER = _HEALTHY_AFTER
            backoff = 1.0
            backoff_cap = _BACKOFF_CAP        # grows to _REFUSAL_BACKOFF_CAP after a 429/509
            failing_since = None
            slept = 0.0          # the bound must hold even if the clock stands still
            denied_at = None     # recordings: (clock, slept) at the first 401/403 of this run (#646)
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
            sent_bytes = 0      # batching starts after _FAST_START_BYTES: the first picture waits for nothing
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
                                denied_at = None
                            pending += splicer.feed(piece, loop.time())
                            if len(pending) >= (131072 if sent_bytes >= _FAST_START_BYTES else 188):
                                cut = len(pending) - (len(pending) % 188) if align else len(pending)
                                out, pending = pending[:cut], pending[cut:]
                                if align:
                                    splicer.sent(out)
                                sent_bytes += len(out)
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
                        denied_at = None
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
                            _refusal_heard(health, getattr(new_resp, "status_code", None))
                            new_resp.raise_for_status()
                            if "mpegurl" in new_resp.headers.get("content-type", "").lower():
                                raise httpx.HTTPError("the channel now answers with a playlist")
                        except _ProviderPlaceholder as e:
                            # Not the channel: waited out like a refusal, and
                            # its bytes never reach the recording (#140), and
                            # _send_checked never fetched it (#624).
                            reason = str(e)
                            backoff_cap = _REFUSAL_BACKOFF_CAP
                            await _note_placeholder(channel_id, e.segment)
                            if (sent_bytes == 0 and nothing_on is not None
                                    and not (is_recording is not None and is_recording())):
                                from services import livetv_slate
                                data = await nothing_on()
                                if data:
                                    logger.info(f"[LiveTV] Channel {channel_id}: nothing on right now "
                                                f"-- the viewer is told so")
                                    async for piece in livetv_slate.paced(data):
                                        yield piece
                                    return
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
                            denied_for = None
                            if status in (401, 403) and is_recording is not None and is_recording():
                                # #646: an IP block or flood protection answers
                                # 403 to everything for a minute or two. A
                                # recording waits that out on the refusal cap;
                                # a login refused for longer still ends it.
                                if denied_at is None:
                                    denied_at = (loop.time(), slept)
                                denied_for = max(loop.time() - denied_at[0], slept - denied_at[1])
                                if denied_for < _DENIED_GRACE:
                                    reason = str(e)
                                    health["errors"] += 1
                                    backoff_cap = _REFUSAL_BACKOFF_CAP
                                    continue
                            refused = f" (refused for {denied_for:.0f}s)" if denied_for is not None else ""
                            logger.error(f"[LiveTV] Raw stream for channel {channel_id} cannot be "
                                         f"re-opened{refused}, stopping: {e}")
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
                return (await check_url(guard, url))[0]
            if key not in _origin_verdicts:
                allowed, why = await check_url(guard, url)
                if not allowed and isinstance(why, LookupFailed):
                    # Not kept: a lookup that failed even after its retries
                    # is asked again for the next line, or one DNS blip
                    # would skip that origin's chunks for the whole stream.
                    logger.warning(f"[LiveTV] {why} (channel {channel_id})")
                    return False
                _origin_verdicts[key] = allowed
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
            health.pop("_refused", None)
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
            denied_since = None        # first channel-URL 401/403 of the current run (recordings, #646)
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
                _refusal_heard(health, getattr(r, "status_code", None))
                try:
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
                        if _denied_grace(st):
                            # The channel URL itself refuses too: an IP block
                            # or flood protection, for a minute or two (#646).
                            # Not counted for a recording until _DENIED_GRACE
                            # of it; waited out on the refusal cap.
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
                instead, and a 401/403 there by _denied_grace (keyed on the
                channel URL's answer: the token URL's 401/403/407 that
                triggers every re-resolve must not be)."""
                nonlocal gone_since
                if status not in (404, 410) or not (is_recording is not None and is_recording()):
                    return False
                now = asyncio.get_running_loop().time()
                if gone_since is None:
                    gone_since = now
                return now - gone_since < _GONE_GRACE

            def _denied_grace(status) -> bool:
                """True while the channel URL's 401/403 on a RECORDING should
                not yet count toward _MAX_RERESOLVE (#646): an IP block,
                Cloudflare or a panel's flood protection refuses everything
                for a minute or two. It counts only after _DENIED_GRACE of
                unbroken refusal (the login really refused); viewers keep
                the 3-try rule."""
                nonlocal denied_since
                if status not in (401, 403) or not (is_recording is not None and is_recording()):
                    return False
                now = asyncio.get_running_loop().time()
                if denied_since is None:
                    denied_since = now
                return now - denied_since < _DENIED_GRACE

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
                    except _ProviderPlaceholder as e:
                        # The variant points (or redirects) at a stand-in
                        # clip: never read as a playlist (#624).
                        if await _placeholder_refusal(e.segment, "Variant playlist fetch"):
                            return
                        await _backoff_sleep()
                        variant_retry = True
                        break
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
                                chunk_resp.raise_for_status()
                                payload = await _aread_within(chunk_resp, _segment_read_limit(target_duration),
                                                              segment_stretch)
                            finally:
                                await chunk_resp.aclose()
                            break
                        except Exception as e:
                            chunk_error = e
                            # A segment URL can redirect to the placeholder:
                            # _send_checked refuses it unfetched (#624), and it is
                            # waited out like one in the playlist (#140).
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
                    reresolve_run, after_reresolve, gone_since, denied_since = 0, False, None, None
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
                except _ProviderPlaceholder as e:
                    # The playlist URL now redirects to a stand-in clip: not a
                    # playlist (#624). Keep the one in hand and ask again;
                    # in_placeholder keeps the failure clock running until a
                    # real segment arrives.
                    if not in_placeholder:
                        in_placeholder = True
                        await _note_placeholder(channel_id, e.segment)
                    if _note_failure(e, "Playlist refresh"):
                        return
                    backoff_cap = _REFUSAL_BACKOFF_CAP
                    refused_since = None
                    await _backoff_sleep()
                    continue
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
    from services import livetv_lineups
    channels = _shown_channels(db)

    base_url = str(request.base_url).rstrip("/")
    lines = ["#EXTM3U"]
    for ch, number in _unique_numbers(db, channels):
        epg_id = ch.guide_epg_id or f"tentacle-{ch.id}"
        logo = f' tvg-logo="{_m3u_text(ch.guide_logo)}"' if ch.guide_logo else ""
        group = f' group-title="{_m3u_text(ch.group_title)}"' if ch.group_title else ""
        name = _m3u_text(ch.guide_name)
        name = livetv_lineups.extinf_name(name.replace('"', "'")) if livetv_lineups.jellyfin_alters(name) \
            else "," + name
        lines.append(
            f'#EXTINF:-1 tvg-id="{_m3u_text(epg_id)}" tvg-chno="{_m3u_text(number)}"{logo}{group}{name}'
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


def _tuner_m3u(request: Request, entries: list, tuner_url: str) -> Response:
    """A tuner's M3U. The streams sit under the address the tuner was given,
    not the one this request came in on: Jellyfin keys every channel on the
    tuner URL and the stream URL, so they must stay the same whatever it
    calls."""
    from services import livetv_lineups
    base = livetv_lineups.tuner_base(tuner_url) if tuner_url else str(request.base_url).rstrip("/")
    return Response(content=livetv_lineups.m3u(entries, base), media_type="audio/x-mpegurl",
                    headers={"Content-Disposition": "inline; filename=tentacle.m3u"})


@router.get("/api/live/tuner/all.m3u")
def live_tuner_all(request: Request, db: Session = Depends(get_db)):
    """The household's lineup: Jellyfin's one Tentacle tuner, numbered 1..N
    in the admin's order (services/livetv_lineups.py). Like the playlist it
    has no login (Jellyfin reads it as a tuner)."""
    from services import livetv_lineups
    from services.livetv_tuners import all_tuner_url
    return _tuner_m3u(request, livetv_lineups.lineup(db), all_tuner_url(db))


@router.get("/api/live/users/{user_id}/tuner.m3u")
def live_tuner_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    """A person's own tuner of before (v1.11-1.12): their saved list, still
    answered while the sync moves them onto the lineup and removes it."""
    from services import livetv_lineups
    if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", user_id or ""):
        raise HTTPException(404, "No such tuner")
    row = livetv_lineups.lineup_row(db, user_id)
    entries = livetv_lineups.legacy_lineup(db, user_id) if row else []
    return _tuner_m3u(request, entries, row.tuner_url if row else "")


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
    channels = _shown_channels(db)

    # Use stream_id as stable channel ID (same as lineup.json GuideNumber)
    # This ensures IDs never shift when channels are added/removed.
    xmltv_channels = []
    epg_ids = set()
    # One EPG ID can map to multiple channels (e.g. CP24 HD + CP24 HD BACKUP)
    epg_id_to_guide_numbers: dict[str, list[str]] = {}
    guide_number_group: dict[str, str] = {}
    numbered = _unique_numbers(db, channels)
    # A folded network's variant shows the network's guide, which may be a
    # sibling's: "CA EN: TLC" is tagged "TLC.rs" (Serbian), "CA: TLC CANADA"
    # has "tlc.ca" (livetv_catalog.network_guides).
    try:
        from services.livetv_catalog import variant_guides
        network_guide = variant_guides(db)
    except Exception as e:  # the guide must never fail on the catalog
        logger.warning(f"[LiveTV] XMLTV: network guides left out: {e}")
        network_guide = {}
    for ch, guide_number in numbered:
        xmltv_channels.append({
            "id": guide_number,
            "name": ch.guide_name,
            "logo_url": ch.guide_logo,
        })
        # Channel group feeds the category inference below when a programme
        # title says nothing about its genre.
        guide_number_group[guide_number] = ch.group_title
        gid = network_guide.get(ch.id) or ch.guide_epg_id
        if gid:
            epg_ids.add(gid)
            epg_id_to_guide_numbers.setdefault(gid, []).append(guide_number)

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
        # Event / PPV channels with no guide of their own: the event their
        # name carries ("NHL | 02 - 7:30pm Panthers at Senators"), one
        # programme each (services/livetv_events.py).
        try:
            from services.livetv_catalog import event_programmes
            events = event_programmes(db, numbered)
        except Exception as e:  # the guide must never fail on a channel name
            logger.warning(f"[LiveTV] XMLTV: event programmes left out: {e}")
            events = []
        yield from events

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

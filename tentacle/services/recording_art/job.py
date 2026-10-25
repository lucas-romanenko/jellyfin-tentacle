"""The recording-pictures pass: every PASS_MINUTES on the scheduler, a few
finished recordings at a time, one at a time.

For each recording in the library that holds Jellyfin's recordings folder:

- still recording, or ended less than SETTLE ago (Jellyfin is still
  reading it): later;
- it has a picture Tentacle didn't set: never touched (state theirs);
- it has none: Tentacle makes one in two shapes, a 2:3 poster as Primary
  (poster cards) and a 16:9 picture as Thumb (wide cards), and keeps the
  tags Jellyfin gave them (state set). The first that works:
    a. a game: the matchup card (cards.py; the booking or the title says
       the league and teams);   a league's event: the league card
    b. a show or film: TMDB's backdrop and poster on a strict title match
       (the VOD threshold, the year when the title has one)
    c. the guide's own picture for the programme, when it is 16:9
    d. the best of a few frames from the recording (frames.py), never while
       Jellyfin records or is about to
    e. a title card;
  a picture with no poster of its own (a frame, the guide's) is put on a
  poster with the title (cards.poster_from_wide);
- a picture Tentacle set that is no longer there, or no longer the one it
  set: a person changed or removed it (state left), never set again.

Pictures set before posters existed (16:9 as both, primary_shape empty)
get their poster a few per pass, while they are still Tentacle's.

A title folder of several recordings gets the newest recording's picture,
or for games the league card, and follows the newest while the picture is
still Tentacle's.

Off removes Tentacle's own pictures (only where Jellyfin still has the very
picture Tentacle set). A library that saves pictures next to the media is
left alone: the recordings folder is never written to.
"""
import base64
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import (EPGProgram, LiveChannel, RecordingArtwork, RecordingBooking, RecordingRule,
                             SessionLocal, get_setting, set_setting)
from services.recording_art import cards as C
from services.recording_art import frames as F
from services.recording_art import what as W
from services.recording_rules import match as M

logger = logging.getLogger(__name__)

SETTING = "recording_art_enabled"
PASS_MINUTES = 10
SETTLE = timedelta(minutes=10)      # after a recording ends: Jellyfin has read it by then
PER_PASS = 4                        # recordings given a picture per pass
POSTERS_PER_PASS = 8                # older pictures given their poster per pass
POSTER = "poster"
MAX_ATTEMPTS = 4
RETRY_AFTER = timedelta(hours=1)
ICON_BYTES = 8 * 1024 * 1024
FIELDS = "Path,DateCreated,Overview,Genres,MediaSources,ParentId,ChannelInfo,ProviderIds"
_lock = threading.Lock()
_timer: Optional[threading.Timer] = None
_timer_lock = threading.Lock()
_last: dict = {"at": None, "summary": None, "blocked": None, "error": None}


class Defer(Exception):
    """Not now (a frame is needed and Jellyfin records): the next pass."""


# ── Jellyfin ────────────────────────────────────────────────────────────────

class Jellyfin:
    """The calls the pass makes, with Tentacle's key in a header (never in a URL)."""

    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.headers = {"X-Emby-Token": key}

    def get(self, path: str, **params):
        r = requests.get(self.url + path, headers=self.headers, params=params or None, timeout=30)
        r.raise_for_status()
        return r.json() if r.content else None

    def upload(self, item_id: str, kind: str, data: bytes) -> None:
        # Jellyfin takes the picture base64-encoded in the body
        r = requests.post(f"{self.url}/Items/{item_id}/Images/{kind}", data=base64.b64encode(data),
                          headers={**self.headers, "Content-Type": "image/jpeg"}, timeout=60)
        r.raise_for_status()

    def remove(self, item_id: str, kind: str) -> None:
        r = requests.delete(f"{self.url}/Items/{item_id}/Images/{kind}", headers=self.headers, timeout=30)
        if r.status_code != 404:
            r.raise_for_status()

    def image(self, item_id: str, kind: str = "Primary") -> bytes:
        r = requests.get(f"{self.url}/Items/{item_id}/Images/{kind}", headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.content

    def read(self, item_id: str, start: int, length: int) -> bytes:
        """`length` bytes of the recording from `start`, through Jellyfin's stream."""
        with requests.get(f"{self.url}/Videos/{item_id}/stream", params={"static": "true"},
                          headers={**self.headers, "Range": f"bytes={start}-{start + length - 1}"},
                          stream=True, timeout=F.READ_TIMEOUT) as r:
            r.raise_for_status()
            if r.status_code != 206 and start > 0:
                raise RuntimeError("Jellyfin didn't send a part of the file")
            out = bytearray()
            for block in r.iter_content(256 * 1024):
                out += block
                if len(out) >= length:
                    break
            return bytes(out[:length])


def jellyfin_for(db: Session) -> Optional[Jellyfin]:
    url = (get_setting(db, "jellyfin_url", "") or "").strip()
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    return Jellyfin(url, key) if url and key else None


def _id(value) -> str:
    return (value or "").replace("-", "")


def _parse(value) -> Optional[datetime]:
    try:
        return datetime.strptime((value or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


def _tags(item: dict) -> tuple:
    tags = item.get("ImageTags") or {}
    return tags.get("Primary"), tags.get("Thumb")


def recordings_libraries(jf) -> tuple:
    """([libraries holding Jellyfin's recordings folder that Tentacle may
    give pictures], why there are none or some are left out)."""
    cfg = jf.get("/System/Configuration/livetv") or {}
    paths = [p for p in [cfg.get("RecordingPath"), cfg.get("MovieRecordingPath"), cfg.get("SeriesRecordingPath")]
             + list(cfg.get("MediaLocationsCreated") or []) if p]
    paths = {p.rstrip("/\\") for p in paths}
    ok, local = [], []
    for lib in jf.get("/Library/VirtualFolders") or []:
        locs = [(loc or "").rstrip("/\\") for loc in lib.get("Locations") or []]
        if not any(p == loc or p.startswith(loc + "/") or loc.startswith(p + "/") for p in paths for loc in locs
                   if loc):
            continue
        if (lib.get("LibraryOptions") or {}).get("SaveLocalMetadata"):
            local.append(lib.get("Name") or "Recordings")
        else:
            ok.append({"id": _id(lib.get("ItemId")), "name": lib.get("Name"), "roots": set(locs) | paths})
    why = None
    if local:
        # an item in two libraries over one folder may be saved by either:
        # any of them saving next to the media rules the folder out
        ok = []
        names = ", ".join(f"“{n}”" for n in local)
        why = (f"Jellyfin's {names} library saves pictures next to the recordings (“Save artwork into "
               f"media folders”), and Tentacle never writes into the recordings folder. To get pictures, "
               f"turn that off for this library in Jellyfin (Dashboard → Libraries).")
    elif not ok:
        why = "Jellyfin has no library with its recordings folder yet: it adds one with the first recording."
    return ok, why


def _children(jf, lib_id: str, folders: bool) -> list:
    params = {"ParentId": lib_id, "Recursive": "true", "Fields": FIELDS, "EnableImages": "true",
              "EnableUserData": "false"}
    if folders:
        params.update({"IsFolder": "true"})
    else:
        params.update({"IsFolder": "false", "MediaTypes": "Video"})
    return (jf.get("/Items", **params) or {}).get("Items") or []


def _item(jf, item_id: str) -> Optional[dict]:
    items = (jf.get("/Items", Ids=item_id, Fields=FIELDS, EnableImages="true", EnableUserData="false")
             or {}).get("Items") or []
    return items[0] if items else None


# ── What a recording is ─────────────────────────────────────────────────────

def _booking_of(db: Session, name: str, created: datetime) -> Optional[RecordingBooking]:
    """The rule's booking this recording came from: the same title, and
    Jellyfin started the file inside its padded window."""
    want = M.fold(M.strip_prefix(name))
    rows = db.query(RecordingBooking).filter(RecordingBooking.state.in_(("ended", "recording")),
                                             RecordingBooking.start >= created - timedelta(days=1),
                                             RecordingBooking.start <= created + timedelta(days=1)).all()
    best = None
    for b in rows:
        if M.fold(M.strip_prefix(b.title)) != want:
            continue
        lo = b.start - timedelta(seconds=b.pre_padding_seconds or 0) - timedelta(minutes=15)
        hi = b.stop + timedelta(seconds=b.post_padding_seconds or 0)
        if lo <= created <= hi and (best is None or abs(b.start - created) < abs(best.start - created)):
            best = b
    return best


def _programme_of(db: Session, name: str, created: datetime) -> Optional[EPGProgram]:
    """The guide's programme: the same title, starting within the usual
    pre-padding after Jellyfin started the file."""
    want = M.fold(M.strip_prefix(name))
    rows = db.query(EPGProgram).filter(EPGProgram.start >= created - timedelta(minutes=2),
                                       EPGProgram.start <= created + timedelta(minutes=45)).all()
    rows = [p for p in rows if M.fold(M.strip_prefix(p.title)) == want]
    return min(rows, key=lambda p: abs(p.start - created)) if rows else None


def context(db: Session, item: dict) -> dict:
    """Everything known about a recording: Jellyfin's item, Tentacle's
    booking, the guide's programme."""
    name = item.get("Name") or ""
    created = _parse(item.get("DateCreated")) or datetime.utcnow()
    ticks = item.get("RunTimeTicks") or 0
    sources = item.get("MediaSources") or [{}]
    ctx = {"name": name, "description": item.get("Overview") or "", "sub_title": item.get("EpisodeTitle") or "",
           "channel": item.get("ChannelName") or "", "category": "", "genres": item.get("Genres") or [],
           "created": created, "duration": ticks / 10_000_000 if ticks else 0.0,
           "size": int(sources[0].get("Size") or 0), "container": (item.get("Container") or sources[0].get("Container")
                                                                   or "").lower(),
           "start": None, "stop": None, "icon_url": None, "booking": None, "booking_state": None}
    b = _booking_of(db, name, created)
    if b is not None:
        rule = db.get(RecordingRule, b.rule_id) if b.rule_id else None
        ctx.update(start=b.start, stop=b.stop, channel=b.channel_name or ctx["channel"],
                   sub_title=b.sub_title or ctx["sub_title"], description=b.description or ctx["description"],
                   booking_state=b.state)
        if rule is not None:
            ctx["booking"] = {"kind": rule.kind, "league": rule.league, "team": rule.team, "why": b.why or ""}
    p = None
    if b is not None:
        p = db.query(EPGProgram).filter(EPGProgram.start == b.start).all()
        p = next((x for x in p if M.fold(M.strip_prefix(x.title)) == M.fold(M.strip_prefix(b.title))), None)
    if p is None:
        p = _programme_of(db, name, created)
    if p is not None:
        ctx.update(start=ctx["start"] or p.start, stop=ctx["stop"] or p.stop, category=p.category or "",
                   icon_url=p.icon_url, sub_title=ctx["sub_title"] or p.sub_title or "",
                   description=ctx["description"] or p.description or "")
        if not ctx["channel"]:
            ch = db.query(LiveChannel).filter(LiveChannel.epg_channel_id == p.channel_id).first()
            ctx["channel"] = ch.guide_name if ch is not None else ""
    return ctx


def subject_of(ctx: dict) -> W.Subject:
    return W.identify(ctx["name"], ctx["sub_title"], ctx["description"], ctx["channel"], ctx["category"],
                      ctx["genres"], booking=ctx["booking"])


# ── The pictures, first that works ──────────────────────────────────────────

def _tmdb_match(subject: W.Subject, tmdb) -> Optional[dict]:
    """TMDB's show or film, on a strict match only. Raises nothing: TMDB
    down or no match is None."""
    if tmdb is None or not subject.title:
        return None
    year = str(subject.year) if subject.year else None
    order = ("search_movie", "search_series") if subject.film else ("search_series", "search_movie")
    for how in order:
        try:
            found = getattr(tmdb, how)(subject.title, year, strict=True)
        except Exception as e:
            logger.info(f"[Recording art] TMDB not asked about “{subject.title}”: {e}")
            return None
        if found:
            return found
    return None


def _tmdb_image(subject: W.Subject, path: Optional[str], size: str) -> Optional[bytes]:
    from services.tmdb import TMDB_IMAGE_BASE
    if not path:
        return None
    try:
        r = requests.get(f"{TMDB_IMAGE_BASE}/{size}{path}", timeout=20)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.info(f"[Recording art] TMDB's picture for “{subject.title}” not read: {e}")
        return None


def _tmdb_pictures(subject: W.Subject, tmdb) -> tuple:
    """(TMDB's backdrop as 16:9, its poster as 2:3 or None) for a show or
    film, or (None, None): no match, no backdrop, TMDB down."""
    found = _tmdb_match(subject, tmdb)
    if not found:
        return None, None
    data = _tmdb_image(subject, found.get("backdrop_path"), "w1280")
    wide = C.to_16x9(data) if data else None
    if not wide:
        return None, None    # matched, but TMDB has no backdrop
    data = _tmdb_image(subject, found.get("poster_path"), "w780")
    return wide, (C.to_2x3(data) if data else None)


def _guide_picture(url: Optional[str]) -> Optional[bytes]:
    """The guide's programme picture, when it is about 16:9."""
    from services.ssrf import is_safe_url
    if not url or not is_safe_url(url):
        return None
    try:
        with requests.get(url, timeout=15, stream=True, allow_redirects=False) as r:
            if r.status_code != 200:
                return None
            data = bytearray()
            for block in r.iter_content(256 * 1024):
                data += block
                if len(data) > ICON_BYTES:
                    return None
    except Exception as e:
        logger.debug(f"[Recording art] guide picture not read: {e}")
        return None
    return C.to_16x9(bytes(data), only_if_wide=True)


def _frame(jf, item_id: str, ctx: dict, busy: bool, grabber=None) -> tuple:
    """(the best frame as a 16:9 JPEG or None, notes). Raises Defer while
    Jellyfin records (or is about to)."""
    if ctx["container"] not in F.TS_CONTAINERS or not ctx["size"] or not ctx["duration"]:
        return None, ["not an MPEG-TS recording"]
    if busy:
        raise Defer("Jellyfin is recording")
    pre = (ctx["start"] - ctx["created"]).total_seconds() if ctx["start"] else 0.0
    length = (ctx["stop"] - ctx["start"]).total_seconds() if ctx["start"] and ctx["stop"] else None
    at = F.offsets(ctx["duration"], pre, length)
    try:
        best, notes = F.best_frame(lambda s, n: jf.read(item_id, s, n), ctx["size"], ctx["duration"], at,
                                   grabber=grabber or F.grab)
    except F.FramesFailed as e:
        return None, [str(e)]
    return (C.to_16x9(best.data) if best else None), notes


def _cards(subject: W.Subject, ctx: dict) -> Optional[tuple]:
    """(16:9, poster, source) for a game, a league's event or nothing else
    known (the title card); None for anything else."""
    when, channel = ctx["start"] or ctx["created"], ctx["channel"]
    if subject.kind == "game" and subject.teams:
        return (C.matchup_card(subject.league, subject.teams, subject.at, when, channel),
                C.matchup_poster(subject.league, subject.teams, subject.at, when, channel), "matchup")
    if subject.kind in ("game", "event"):
        return (C.league_card(subject.league, subject.title, when, channel),
                C.league_poster(subject.league, subject.title, when, channel), "league")
    return None


def _title_cards(subject: W.Subject, ctx: dict) -> tuple:
    when, channel = ctx["start"] or ctx["created"], ctx["channel"]
    title = subject.title or ctx["name"]
    return C.title_card(title, when, channel), C.title_poster(title, when, channel)


def _poster_of_wide(wide: bytes, subject: W.Subject, ctx: dict) -> Optional[bytes]:
    return C.poster_from_wide(wide, subject.title or ctx["name"], ctx["start"] or ctx["created"], ctx["channel"])


def make_picture(jf, item_id: str, ctx: dict, subject: W.Subject, tmdb=None, busy: bool = False,
                 grabber=None) -> tuple:
    """(16:9 JPEG, 2:3 JPEG, source, what it shows): the first source that works."""
    cards = _cards(subject, ctx)
    if cards:
        return cards[0], cards[1], cards[2], subject.describe()
    wide, poster = _tmdb_pictures(subject, tmdb)
    if wide:
        return wide, poster or _poster_of_wide(wide, subject, ctx), "tmdb", subject.describe()
    wide = _guide_picture(ctx["icon_url"])
    if wide:
        return wide, _poster_of_wide(wide, subject, ctx), "guide", "the guide's picture"
    wide, notes = _frame(jf, item_id, ctx, busy, grabber)
    if wide:
        return wide, _poster_of_wide(wide, subject, ctx), "frame", "; ".join(notes)[:250]
    wide, poster = _title_cards(subject, ctx)
    return wide, poster, "title", ("no frame: " + "; ".join(notes))[:250] if notes else subject.title


def make_poster(jf, item_id: str, ctx: dict, subject: W.Subject, source: Optional[str], tmdb=None) -> Optional[bytes]:
    """The poster for a recording whose 16:9 picture Tentacle set before
    posters existed: drawn again for a card, TMDB's for a TMDB match, else
    the 16:9 picture (its Thumb) on a poster."""
    cards = _cards(subject, ctx)
    if source in ("matchup", "league") and cards:
        return cards[1]
    if source == "title":
        return _title_cards(subject, ctx)[1]
    if source == "tmdb":
        found = _tmdb_match(subject, tmdb)
        data = _tmdb_image(subject, (found or {}).get("poster_path"), "w780")
        poster = C.to_2x3(data) if data else None
        if poster:
            return poster
    return _poster_of_wide(jf.image(item_id, "Thumb"), subject, ctx)


# ── The pass ────────────────────────────────────────────────────────────────

def enabled(db: Session) -> bool:
    return (get_setting(db, SETTING, "true") or "true").strip().lower() not in ("false", "0", "off", "no")


def _busy(jf, now: datetime) -> tuple:
    """(Jellyfin records now or within 20 min, [(folded name, recording start)] of what records now)."""
    from routers.recordings import busy_from_timers
    from routers.livetv import _parse_timer_date, _timer_seconds
    timers = (jf.get("/LiveTv/Timers") or {}).get("Items") or []
    b = busy_from_timers(timers, now)
    now_on = []
    for t in timers:
        start = _parse_timer_date(t.get("StartDate"))
        end = _parse_timer_date(t.get("EndDate"))
        if start is None or t.get("Status") not in ("InProgress", "New"):
            continue
        s = start - timedelta(seconds=_timer_seconds(t.get("PrePaddingSeconds")))
        e = (end or start) + timedelta(seconds=_timer_seconds(t.get("PostPaddingSeconds")))
        if t.get("Status") == "InProgress" or s <= now <= e:
            now_on.append((M.fold(M.strip_prefix(t.get("Name"))), s))
    return bool(b["recording"] or b["starting_soon"]), now_on


def _finished(item: dict, ctx: dict, now: datetime, now_on: list) -> bool:
    if ctx["booking_state"] == "recording":
        return False
    name = M.fold(M.strip_prefix(item.get("Name")))
    if any(n == name and ctx["created"] >= s - timedelta(minutes=2) for n, s in now_on):
        return False
    if not ctx["duration"]:
        return ctx["created"] + timedelta(hours=24) <= now   # never probed: a day later
    return ctx["created"] + timedelta(seconds=ctx["duration"]) + SETTLE <= now


def _set(jf, item_id: str, wide: bytes, poster: Optional[bytes]) -> tuple:
    """Primary the poster (the 16:9 picture when there is none), Thumb the 16:9."""
    jf.upload(item_id, "Primary", poster or wide)
    jf.upload(item_id, "Thumb", wide)
    after = _item(jf, item_id) or {}
    return _tags(after)


def _row(db: Session, rows: dict, item: dict, kind: str) -> RecordingArtwork:
    iid = _id(item.get("Id"))
    row = rows.get(iid)
    if row is None:
        row = RecordingArtwork(item_id=iid, kind=kind, name=(item.get("Name") or "")[:200], attempts=0)
        db.add(row)
        rows[iid] = row
    return row


def _still_ours(row: RecordingArtwork, item: dict) -> bool:
    return _tags(item) == (row.primary_tag, row.thumb_tag)


def reconcile(db: Session, rows: dict, items: dict) -> int:
    """Pictures Tentacle set that a person changed or removed since: left
    for good. Rows of items that are gone are dropped. Returns how many
    changed."""
    n = 0
    for iid, row in list(rows.items()):
        item = items.get(iid)
        if item is None:
            db.delete(row)
            rows.pop(iid)
            n += 1
        elif row.state == "set" and not _still_ours(row, item):
            row.state, row.updated_at = "left", datetime.utcnow()
            row.detail = "changed or removed in Jellyfin: left as it is"
            n += 1
    return n


def give_picture(db: Session, jf, item: dict, row: RecordingArtwork, tmdb=None, busy: bool = False,
                 grabber=None, ctx: Optional[dict] = None) -> str:
    """One recording: make its picture and set it. Returns the row's state."""
    iid = _id(item.get("Id"))
    ctx = ctx or context(db, item)
    subject = subject_of(ctx)
    wide, poster, source, detail = make_picture(jf, iid, ctx, subject, tmdb=tmdb, busy=busy, grabber=grabber)
    # someone (or Jellyfin's own frame grab) may have set one meanwhile
    fresh = _item(jf, iid)
    if fresh is None:
        return "gone"
    if any(_tags(fresh)):
        row.state, row.detail = "theirs", "had a picture Tentacle didn't set"
        return row.state
    primary, thumb = _set(jf, iid, wide, poster)
    row.state, row.source, row.detail = "set", source, (detail or "")[:250]
    row.primary_tag, row.thumb_tag = primary, thumb
    row.primary_shape = POSTER if poster else None
    row.made_from = subject.league["id"] if subject.league and subject.kind in ("game", "event") else None
    row.updated_at = datetime.utcnow()
    logger.info(f"[Recording art] “{item.get('Name')}”: {source} ({subject.describe()})")
    return row.state


def _pictures_of(jf, item: dict, title: Optional[str]) -> tuple:
    """(16:9, poster) from a recording's own pictures, for its title folder:
    its Thumb (else its Primary) as the 16:9, its Primary as the poster
    when that is one, else the 16:9 on a poster."""
    iid = _id(item.get("Id"))
    primary, thumb = _tags(_item(jf, iid) or item)    # this pass may just have set them
    primary_data = jf.image(iid, "Primary") if primary else None
    wide = C.to_16x9(jf.image(iid, "Thumb") if thumb else primary_data)
    if not wide:
        return None, None
    poster = C.to_2x3(primary_data) if primary_data else None
    return wide, poster or C.poster_from_wide(wide, W.clean_title(title or item.get("Name"))[0], None, None)


def upgrade_posters(db: Session, jf, items: dict, rows: dict, tmdb=None, limit: int = POSTERS_PER_PASS) -> int:
    """Recordings whose 16:9 picture Tentacle set as both Primary and Thumb
    before posters existed: the Primary becomes the poster, while both are
    still the pictures Tentacle set. Returns how many."""
    n = 0
    for iid, row in rows.items():
        if n >= limit:
            break
        item = items.get(iid)
        if row.kind != "recording" or row.state != "set" or row.primary_shape == POSTER or item is None \
                or not _still_ours(row, item) or not row.thumb_tag:
            continue
        try:
            ctx = context(db, item)
            poster = make_poster(jf, iid, ctx, subject_of(ctx), row.source, tmdb=tmdb)
            if not poster:
                continue
            fresh = _item(jf, iid)
            if fresh is None or not _still_ours(row, fresh):
                continue    # a person changed it meanwhile: reconcile() leaves it
            jf.upload(iid, "Primary", poster)
            primary, thumb = _tags(_item(jf, iid) or {})
            row.primary_tag, row.thumb_tag = primary, thumb
            row.primary_shape, row.updated_at = POSTER, datetime.utcnow()
            n += 1
        except Exception as e:
            logger.warning(f"[Recording art] poster for “{item.get('Name')}”: {e}")
        db.commit()
    return n


def _folders(db: Session, jf, folders: list, videos: list, rows: dict, roots: set = frozenset()) -> int:
    """Title folders of several recordings: the newest recording's picture,
    or the league card when they are games. Returns pictures set. The
    recordings folder itself (`roots`) is the library's, never a title's."""
    by_parent: dict = {}
    for v in videos:
        by_parent.setdefault(_id(v.get("ParentId")), []).append(v)
    n = 0
    for f in folders:
        fid = _id(f.get("Id"))
        if (f.get("Path") or "").rstrip("/\\") in roots or f.get("Type") in ("CollectionFolder", "UserRootFolder"):
            continue
        kids = by_parent.get(fid) or []
        if not kids:
            continue
        newest = max(kids, key=lambda v: v.get("DateCreated") or "")
        nrow = rows.get(_id(newest.get("Id")))
        row = rows.get(fid)
        if row is not None and row.state in ("left", "theirs"):
            continue
        if row is None and any(_tags(f)):
            _row(db, rows, f, "folder").state = "theirs"
            continue
        if row is not None and row.state == "set" and not _still_ours(row, f):
            continue    # reconcile() marks it left
        # the newest's picture now (this pass may just have set it)
        picture = nrow.primary_tag if nrow is not None and nrow.state == "set" else _tags(newest)[0]
        if nrow is not None and nrow.state == "set" and nrow.made_from:
            want = f"league:{nrow.made_from}"
        elif picture:
            want = f"child:{_id(newest.get('Id'))}:{picture}"
        else:
            continue    # the newest has no picture yet
        if row is not None and row.state == "set" and row.made_from == want and row.primary_shape == POSTER:
            continue
        try:
            if want.startswith("league:"):
                from services.recording_rules import leagues as LG
                lg = LG.league(nrow.made_from)
                ctx = context(db, newest)
                title = W.clean_title(f.get("Name"))[0]
                wide = C.league_card(lg, title, None, ctx["channel"]) if lg else None
                poster = C.league_poster(lg, title, None, ctx["channel"]) if lg else None
            else:
                wide, poster = _pictures_of(jf, newest, f.get("Name"))
            if not wide:
                continue
            row = _row(db, rows, f, "folder")
            primary, thumb = _set(jf, fid, wide, poster)
            row.primary_shape = POSTER if poster else None
            row.state, row.source, row.made_from = "set", want.split(":")[0], want
            row.primary_tag, row.thumb_tag, row.updated_at = primary, thumb, datetime.utcnow()
            row.detail = "the league card" if want.startswith("league:") else f"the picture of “{newest.get('Name')}”"
            n += 1
        except Exception as e:
            logger.warning(f"[Recording art] folder “{f.get('Name')}”: {e}")
        db.commit()
    return n


def remove_all(db: Session, jf) -> int:
    """Off: Tentacle's own pictures go (only where Jellyfin still has the very
    picture Tentacle set); what people set stays. Returns pictures removed."""
    n = 0
    rows = db.query(RecordingArtwork).filter(RecordingArtwork.state.in_(("set", "left"))).all()
    for row in [r for r in rows if r.primary_tag or r.thumb_tag]:
        item = _item(jf, row.item_id)
        if item is not None and row.state == "set" and not _still_ours(row, item):
            row.state = "left"     # a person changed or removed one of the two: theirs from now on
            row.detail = "changed or removed in Jellyfin: left as it is"
        if item is not None:
            primary, thumb = _tags(item)
            for kind, ours, now in (("Primary", row.primary_tag, primary), ("Thumb", row.thumb_tag, thumb)):
                if ours and now == ours:
                    jf.remove(row.item_id, kind)
                    n += 1
        if row.state == "set" or item is None:
            db.delete(row)
        else:
            row.primary_tag = row.thumb_tag = None   # a person's choice stays theirs
        db.commit()
    return n


def run_pass(db: Session, jf=None, now: Optional[datetime] = None, tmdb=None, grabber=None,
             limit: int = PER_PASS) -> dict:
    """One pass. Returns what it did."""
    now = now or datetime.utcnow()
    jf = jf if jf is not None else jellyfin_for(db)
    out = {"set": 0, "theirs": 0, "left": 0, "posters": 0, "folders": 0, "removed": 0, "waiting": 0, "failed": 0}
    if jf is None:
        _last.update(blocked="Jellyfin isn't connected")
        return out
    with _lock:
        if not enabled(db):
            out["removed"] = remove_all(db, jf)
            _last.update(blocked=None)
            return out
        libs, why = recordings_libraries(jf)
        _last.update(blocked=why if not libs else None, note=why if libs else None)
        if not libs:
            return out
        videos, folders = {}, {}
        for lib in libs:
            # two libraries may hold the same folder: one item each
            videos.update((_id(i.get("Id")), i) for i in _children(jf, lib["id"], folders=False))
            folders.update((_id(i.get("Id")), i) for i in _children(jf, lib["id"], folders=True))
        videos, folders = list(videos.values()), list(folders.values())
        rows = {r.item_id: r for r in db.query(RecordingArtwork).all()}
        items = {_id(i.get("Id")): i for i in videos + folders}
        out["left"] = reconcile(db, rows, items)
        db.commit()
        busy, now_on = _busy(jf, now)
        done = 0
        for item in sorted(videos, key=lambda v: v.get("DateCreated") or "", reverse=True):
            if done >= limit:
                break
            iid = _id(item.get("Id"))
            row = rows.get(iid)
            if row is not None and row.state in ("set", "left", "theirs"):
                continue
            if row is not None and row.state == "failed" and (
                    (row.attempts or 0) >= MAX_ATTEMPTS or (row.updated_at and row.updated_at + RETRY_AFTER > now)):
                continue
            if any(_tags(item)):
                _row(db, rows, item, "recording").state = "theirs"
                rows[iid].detail = "had a picture Tentacle didn't set"
                out["theirs"] += 1
                db.commit()
                continue
            ctx = context(db, item)
            if not _finished(item, ctx, now, now_on):
                continue
            done += 1
            row = _row(db, rows, item, "recording")
            try:
                state = give_picture(db, jf, item, row, tmdb=tmdb, busy=busy, grabber=grabber, ctx=ctx)
                out["set" if state == "set" else "theirs" if state == "theirs" else "waiting"] += 1
            except Defer as e:
                if row.id is None:
                    db.expunge(row)
                    rows.pop(iid, None)
                out["waiting"] += 1
                logger.info(f"[Recording art] “{item.get('Name')}” waits: {e}")
            except Exception as e:
                row.state, row.attempts = "failed", (row.attempts or 0) + 1
                row.detail, row.updated_at = f"{type(e).__name__}: {e}"[:250], now
                out["failed"] += 1
                logger.warning(f"[Recording art] “{item.get('Name')}”: no picture: {e}")
            db.commit()
        out["posters"] = upgrade_posters(db, jf, items, rows, tmdb=tmdb)
        roots = set().union(*(lib.get("roots") or set() for lib in libs))
        out["folders"] = _folders(db, jf, folders, videos, rows, roots)
        db.commit()
    return out


def run_scheduled() -> None:
    """The scheduler's pass (and the one a setting change asks for)."""
    db = SessionLocal()
    try:
        tmdb = None
        if enabled(db):
            try:
                from services.tmdb import TMDBService, get_tmdb_token
                tmdb = TMDBService(get_tmdb_token(db), get_setting(db, "data_dir", "/data") or "/data",
                                   float(get_setting(db, "tmdb_match_threshold", "0.7") or 0.7))
            except Exception as e:
                logger.info(f"[Recording art] TMDB not set up: {e}")
        summary = run_pass(db, tmdb=tmdb)
        _last.update(at=datetime.utcnow(), summary=summary, error=None)
        if any(summary.values()):
            logger.info(f"[Recording art] pass: {summary}")
            from services import live_updates
            live_updates.publish("recordings", admins=True)   # the settings page's counts
    except Exception as e:
        db.rollback()
        _last.update(at=datetime.utcnow(), error=str(e)[:200])
        logger.warning(f"[Recording art] pass failed: {e}")
    finally:
        db.close()


def request_pass(delay: float = 2.0) -> None:
    """A pass in a moment (the setting changed)."""
    global _timer
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(delay, run_scheduled)
        _timer.daemon = True
        _timer.start()


def set_enabled(db: Session, on: bool) -> None:
    set_setting(db, SETTING, "true" if on else "false")
    request_pass()


def status(db: Session) -> dict:
    """For the settings page: on or off, how many pictures from where, and
    why nothing can be set when that's so."""
    counts: dict = {}
    sources: dict = {}
    for row in db.query(RecordingArtwork).all():
        counts[row.state] = counts.get(row.state, 0) + 1
        if row.state == "set" and row.kind == "recording":
            sources[row.source or "?"] = sources.get(row.source or "?", 0) + 1
    return {"enabled": enabled(db), "counts": counts, "sources": sources,
            "blocked": _last.get("blocked"), "note": _last.get("note"),
            "last_at": _last["at"].isoformat() + "Z" if _last.get("at") else None,
            "last": _last.get("summary"), "error": _last.get("error")}

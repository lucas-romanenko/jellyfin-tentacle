"""Live TV channel logos: a fallback chain and a logo cache (#137).

A provider's logo is often wrong without being missing. Panels give whole
groups one picture: on a live install one image was the logo of 462 channels
(ABC News, Global, Yahoo Finance, Zenlife ...), another of 286 (Nick Jr,
Comedy Central, MTV, TBS, PBS ...). A logo address can also stop answering,
or answer with an error page. Jellyfin's guide then shows the group's
picture, or nothing.

For every enabled channel, a pass picks the first of these that works:

  1. the provider's logo, unless it is a group picture (below); when it
     went dead, Tentacle's own copy of it from when it still worked;
  2. the same channel elsewhere: another channel with the same name key
     (services/channel_names.py: "CA EN: MTV HD" and "US: MTV" are "mtv"),
     any provider, whose logo is no group picture;
  3. the guide's icon: the <icon> of the channel's guide id in the cached
     XMLTV feeds;
  4. the logo library: iptv-org's open channel database (public domain;
     about 31,000 channels with logos), by guide id ("Crave1.ca"), else by
     name in the channel's country;
  5. the provider's logo after all (a group picture is better than none).

A group picture is a logo address that three or more channels with
different names share, where no word is in most of those names: "FOX 10
Phoenix", "FOX 11 Tucson" ... share FOX's logo rightly; "ABC News",
"Global", "Zenlife" share no brand.

"Works" means checked: Tentacle fetches each logo it would use (at most
CHECKS_PER_PASS per pass, one at a time) and keeps a copy in the logo cache
(DATA_DIR/channel_logos). A logo that fails twice in a row is dead; one that
answers with no picture, or a tiny placeholder, is bad. Checked again after
OK_RECHECK / FAILED_RECHECK. A dead logo's copy is served instead of it
(GET /api/live/logo/<file>, step 1 and any other step), so a provider logo that disappears does not
blank the row.

The pick is stored on the channel (LiveChannel.auto_logo_url / _source;
NULL when the provider's own is used), so LiveChannel.guide_logo (the
admin's logo first, always) gives every lineup, tuner and guide the result
without asking anything here. When a pass changes a pick, the Live TV sync
runs: services/livetv_tuners.stale_logos drops the channel's old image in
Jellyfin and the guide refresh sets the new one, and open clients follow.

Runs from main.py's scheduler every PASS_MINUTES; never in tests.
"""
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

import requests

from models.database import LiveChannel, LiveLogo, SessionLocal, tentacle_address
from services.channel_names import channel_name_key, channel_name_words

logger = logging.getLogger(__name__)

PASS_MINUTES = 60
CHECKS_PER_PASS = 150          # logo fetches per pass, one at a time
CHECK_GAP_SECONDS = 0.2
OK_RECHECK = timedelta(days=7)
FAILED_RECHECK = timedelta(days=1)
FAILS_FOR_DEAD = 2
MAX_BYTES = 1024 * 1024        # a logo bigger than this is no logo
MIN_SIDE = 16                  # a picture smaller than this is a placeholder
TIMEOUT = 10

GROUP_MIN_NAMES = 3            # a group picture: this many different names ...
GROUP_BRAND_SHARE = 0.6        # ... and no word in this share of them

LIBRARY_CHANNELS = "https://iptv-org.github.io/api/channels.json"
LIBRARY_LOGOS = "https://iptv-org.github.io/api/logos.json"
LIBRARY_MAX_AGE = timedelta(days=7)
LIBRARY_RETRY = timedelta(hours=6)

FILE_RE = re.compile(r"^[0-9a-f]{20}\.(png|jpg|gif|webp|svg)$")
CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif",
                 "webp": "image/webp", "svg": "image/svg+xml"}

# Words that say nothing about which brand a channel is: they don't make a
# shared picture a brand logo ("PPV 1", "PPV 2" share no brand by "ppv").
_NOT_A_BRAND = {"ppv", "live", "event", "events", "no", "streaming", "exclusive", "tv", "channel",
                "the", "and", "of", "east", "west", "plus", "network", "replay", "vip"}


def _data_dir() -> str:
    return os.getenv("DATA_DIR", "/data")


def cache_dir() -> str:
    return os.path.join(_data_dir(), "channel_logos")


def logo_base(url: str) -> str:
    """A logo address without its query ("929.jpg?39673" and "929.jpg?31191"
    are one picture), the key a LiveLogo row and a group picture go by."""
    return (url or "").split("?", 1)[0].strip()


def _base_key(url: str) -> str:
    return logo_base(url).lower()


# ── Group pictures ──────────────────────────────────────────────────────────

def group_logos(named_logos) -> set:
    """The logo addresses (lower-case, no query) that are one group's picture,
    from (name, logo url) pairs: GROUP_MIN_NAMES or more different name keys
    share it, and no word is in GROUP_BRAND_SHARE of those names."""
    names = defaultdict(dict)
    for name, url in named_logos:
        if url:
            names[_base_key(url)].setdefault(channel_name_key(name) or name, name)
    out = set()
    for base, by_key in names.items():
        if len(by_key) < GROUP_MIN_NAMES:
            continue
        words = Counter()
        for name in by_key.values():
            words.update({w for w in channel_name_words(name)
                          if len(w) > 1 and not w.isdigit() and w not in _NOT_A_BRAND})
        top = words.most_common(1)
        if not top or top[0][1] < GROUP_BRAND_SHARE * len(by_key):
            out.add(base)
    return out


# ── The logo library (iptv-org) ─────────────────────────────────────────────

_PREFIX_CC_RE = re.compile(r"^\s*[|\[(]?\s*([A-Za-z]{2})(?:[ /-][A-Za-z]{2})?\s*[|\]):]")


def _library_path() -> str:
    return os.path.join(cache_dir(), "library.json")


def _pick_library_logo(logos: list) -> Optional[str]:
    """One logo of a library channel: the channel's own (not a feed's), in
    use, a PNG or SVG, the widest up to 1000 px."""
    def rank(l):
        width = l.get("width") or 0
        return (l.get("feed") is not None, not l.get("in_use", True),
                (l.get("format") or "").upper() not in ("PNG", "SVG"), -min(width, 1000))
    good = [l for l in logos if str(l.get("url") or "").lower().startswith("https://")]
    return min(good, key=rank)["url"] if good else None


def build_library(channels: list, logos: list) -> dict:
    """iptv-org's channels.json + logos.json reduced to what matching needs:
    {"ids": {id lower: [id, country, logo]}, "names": {name key: [id lower, ...]}}.
    Closed channels and ones without a logo are left out."""
    by_channel = defaultdict(list)
    for l in logos:
        if isinstance(l, dict) and l.get("channel"):
            by_channel[l["channel"]].append(l)
    ids, names = {}, defaultdict(list)
    for c in channels:
        if not isinstance(c, dict) or not c.get("id") or c.get("closed") or c.get("is_nsfw"):
            continue
        url = _pick_library_logo(by_channel.get(c["id"], []))
        if not url:
            continue
        cid = c["id"].lower()
        ids[cid] = [c["id"], (c.get("country") or "").upper(), url]
        for name in [c.get("name") or ""] + list(c.get("alt_names") or []):
            key = channel_name_key(name)
            if key and cid not in names[key]:
                names[key].append(cid)
    return {"ids": ids, "names": dict(names)}


_library = {"data": None, "mtime": 0.0, "tried": 0.0}


def library(fetch: bool = True) -> dict:
    """The logo library, from its cached copy; fetched again (two files,
    about 14 MB) when the copy is older than LIBRARY_MAX_AGE. Empty when it
    can't be had: the chain goes on without it."""
    path = _library_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    stale = time.time() - mtime > LIBRARY_MAX_AGE.total_seconds()
    if fetch and stale and time.time() - _library["tried"] > LIBRARY_RETRY.total_seconds():
        _library["tried"] = time.time()
        try:
            chans = requests.get(LIBRARY_CHANNELS, timeout=60).json()
            logos = requests.get(LIBRARY_LOGOS, timeout=60).json()
            data = build_library(chans, logos)
            os.makedirs(cache_dir(), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
            logger.info(f"[LiveTV] Logo library: {len(data['ids'])} channels with a logo")
            mtime = os.path.getmtime(path)
        except (requests.RequestException, ValueError, OSError) as e:
            logger.info(f"[LiveTV] Logo library not fetched ({type(e).__name__}); trying again later")
    if _library["data"] is None or _library["mtime"] != mtime:
        try:
            with open(path, encoding="utf-8") as f:
                _library["data"] = json.load(f)
        except (OSError, ValueError):
            _library["data"] = {"ids": {}, "names": {}}
        _library["mtime"] = mtime
    return _library["data"]


def library_match(lib: dict, guide_id: Optional[str], names: list, countries: list) -> Optional[str]:
    """A library logo for a channel: its guide id when the library has it
    ("Crave1.ca"), else a channel with its name in the first of `countries`
    that has one, else the name's only channel in the library."""
    ids = lib.get("ids") or {}
    if guide_id and guide_id.lower() in ids:
        return ids[guide_id.lower()][2]
    for name in names:
        cands = [ids[i] for i in (lib.get("names") or {}).get(channel_name_key(name), []) if i in ids]
        if not cands:
            continue
        for cc in countries:
            same = sorted(c for c in cands if c[1] == cc)
            if same:
                return same[0][2]
        if len({c[0] for c in cands}) == 1:
            return cands[0][2]
    return None


def channel_countries(ch: LiveChannel, home: list) -> list:
    """Where a channel is from, most sure first: its guide id's country
    ("tlc.ca"), the provider's name prefix ("CA EN:"), then the household's
    usual countries."""
    out = []
    gid = ch.guide_epg_id or ""
    if "." in gid:
        tail = gid.rsplit(".", 1)[1].upper()
        if len(tail) == 2 and tail.isalpha():
            out.append(tail)
    m = _PREFIX_CC_RE.match(ch.name or "")
    if m:
        out.append(m.group(1).upper())
    out += home
    seen = set()
    return [c for c in out if not (c in seen or seen.add(c))]


# ── The guide's icons ───────────────────────────────────────────────────────

def guide_icons() -> dict:
    """{guide id lower: icon url} from the cached XMLTV feeds' <channel>s."""
    from services import xmltv
    out = {}
    try:
        files = sorted(os.listdir(xmltv.XMLTV_CACHE_DIR))
    except OSError:
        return out
    for name in files:
        if not (name.startswith("xmltv_") and name.endswith(".xml")):
            continue
        try:
            for ch in xmltv.read_feed_channels(os.path.join(xmltv.XMLTV_CACHE_DIR, name)):
                if ch.get("icon") and ch.get("id"):
                    out.setdefault(ch["id"].lower(), ch["icon"])
        except Exception as e:   # one unreadable feed must not stop the others
            logger.info(f"[LiveTV] Logos: guide feed {name} unreadable: {e}")
    return out


# ── Checking and keeping a copy ─────────────────────────────────────────────

def sniff(data: bytes) -> Optional[str]:
    """The picture format of `data` ("png", "jpg", "gif", "webp", "svg"), or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:1024].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "svg"
    return None


def _too_small(data: bytes, fmt: str) -> bool:
    if fmt == "svg":
        return len(data) < 100
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as img:
            return min(img.size) < MIN_SIDE
    except Exception:
        return True   # says it is a picture, but nothing can draw it


def fetch_logo(url: str) -> tuple:
    """("ok", data, fmt) / ("bad", None, detail) / ("dead", None, detail)."""
    try:
        with requests.get(url, timeout=TIMEOUT, stream=True,
                          headers={"User-Agent": "Tentacle (channel logo check)"}) as r:
            if r.status_code >= 400:
                return "dead", None, f"HTTP {r.status_code}"
            data = b""
            for chunk in r.iter_content(64 * 1024):
                data += chunk
                if len(data) > MAX_BYTES:
                    return "bad", None, "bigger than 1 MB"
    except requests.RequestException as e:
        return "dead", None, type(e).__name__
    fmt = sniff(data)
    if not fmt:
        return "bad", None, "not a picture"
    if _too_small(data, fmt):
        return "bad", None, "a placeholder (too small)"
    return "ok", data, fmt


def check(db, url: str, row: Optional[LiveLogo] = None) -> LiveLogo:
    """Fetch one logo, keep its copy, record the verdict."""
    base = logo_base(url)
    row = row or db.get(LiveLogo, base) or LiveLogo(url=base, fails=0)
    state, data, detail = fetch_logo(url)
    row.checked_at = datetime.utcnow()
    if state == "ok":
        name = hashlib.sha1(base.encode()).hexdigest()[:20] + "." + detail
        os.makedirs(cache_dir(), exist_ok=True)
        tmp = os.path.join(cache_dir(), name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(cache_dir(), name))
        if row.file and row.file != name:
            try:
                os.remove(os.path.join(cache_dir(), row.file))
            except OSError:
                pass
        row.state, row.fails, row.file, row.detail = "ok", 0, name, None
    elif state == "bad":
        row.state, row.fails, row.detail = "bad", 0, detail
    else:
        row.fails = (row.fails or 0) + 1
        row.detail = detail
        if row.fails >= FAILS_FOR_DEAD:
            row.state = "dead"
    db.merge(row)
    return row


def _due(row: Optional[LiveLogo], now: datetime) -> bool:
    if row is None or row.checked_at is None:
        return True
    wait = OK_RECHECK if row.state == "ok" else FAILED_RECHECK
    return now - row.checked_at >= wait


# ── The chain ───────────────────────────────────────────────────────────────

def candidates(ch: LiveChannel, groups: set, by_key: dict, icons: dict, lib: dict, home: list) -> list:
    """[(source, url)] for one channel, in the chain's order (a dead one's
    copy is decided with the checks, in choose())."""
    out = []
    own = ch.logo_url
    if own and _base_key(own) not in groups:
        out.append(("provider", own))
    for other in by_key.get(channel_name_key(ch.name), []):
        if other.id != ch.id and other.logo_url and _base_key(other.logo_url) not in groups:
            out.append(("same channel", other.logo_url))
    gid = (ch.guide_epg_id or "").lower()
    if gid and icons.get(gid) and _base_key(icons[gid]) not in groups:
        out.append(("guide", icons[gid]))
    found = library_match(lib, ch.guide_epg_id, [ch.name, ch.guide_name], channel_countries(ch, home))
    if found:
        out.append(("library", found))
    if own and _base_key(own) in groups:
        out.append(("provider", own))
    seen, uniq = set(), []
    for source, url in out:
        if _base_key(url) not in seen:
            seen.add(_base_key(url))
            uniq.append((source, url))
    return uniq


def choose(cands: list, states: dict, copy_url) -> tuple:
    """(source, url) the channel shows: the first candidate that works, or
    isn't checked yet (until a check says otherwise; one failure is not dead
    yet); a dead one with a copy gives its copy. (None, None) when nothing
    is left."""
    for source, url in cands:
        row = states.get(logo_base(url))
        if row is None or row.state in (None, "ok"):
            return source, url
        if row.state == "dead" and row.file and copy_url and copy_url(row.file):
            return "cache", copy_url(row.file)
    return None, None


def _home_countries(channels: list, lib: dict) -> list:
    """The countries most of the household's channels are from (their guide
    ids), so a name the library has in several countries picks the usual one."""
    ids = lib.get("ids") or {}
    count = Counter(ids[g][1] for g in ((c.guide_epg_id or "").lower() for c in channels) if g in ids)
    return [cc for cc, _ in count.most_common(3)]


def run_pass(db, fetch: bool = True, budget: int = CHECKS_PER_PASS) -> dict:
    """One pass over the enabled channels: check what is due, store each
    channel's pick. Returns what it did."""
    now = datetime.utcnow()
    every = db.query(LiveChannel).all()
    enabled = [c for c in every if c.enabled]
    groups = group_logos((c.name, c.logo_url) for c in every)
    by_key = defaultdict(list)
    for c in sorted(every, key=lambda c: (not c.enabled, c.id)):
        if c.logo_url:
            by_key[channel_name_key(c.name)].append(c)
    icons = guide_icons()
    groups |= group_logos((c.name, icons.get((c.guide_epg_id or "").lower())) for c in every)
    lib = library(fetch=fetch)
    home = _home_countries(enabled, lib)
    states = {r.url: r for r in db.query(LiveLogo).all()}
    address = tentacle_address(db)

    def copy_url(file):
        return f"{address}/api/live/logo/{file}" if address else None

    done = {"channels": len(enabled), "checked": 0, "changed": 0, "group_pictures": len(groups),
            "sources": Counter()}
    for ch in sorted(enabled, key=lambda c: c.id):
        cands = candidates(ch, groups, by_key, icons, lib, home)
        # Check the candidates in order until one works, within the budget.
        for _, url in cands:
            row = states.get(logo_base(url))
            if _due(row, now) and done["checked"] < budget:
                if fetch:
                    if done["checked"]:
                        time.sleep(CHECK_GAP_SECONDS)
                    row = check(db, url, row)
                    states[row.url] = row
                done["checked"] += 1
            if row is not None and row.state == "ok":
                break
        source, url = choose(cands, states, copy_url)
        if source == "provider" and url == ch.logo_url:
            source = url = None   # the provider's own: nothing to store
        if (ch.auto_logo_url, ch.auto_logo_source) != (url, source):
            ch.auto_logo_url, ch.auto_logo_source = url, source
            done["changed"] += 1
        done["sources"][source or "provider"] += 1
    db.commit()
    done["sources"] = dict(done["sources"])
    return done


def run_scheduled() -> None:
    """The scheduler's pass; a pick that changed reaches Jellyfin through the
    Live TV sync (its stale_logos)."""
    db = SessionLocal()
    try:
        done = run_pass(db)
        logger.info(f"[LiveTV] Channel logos: {done['channels']} channels, {done['checked']} checked, "
                    f"{done['changed']} changed; {done['sources']}")
        if done["changed"]:
            from services.livetv_tuners import schedule_sync
            schedule_sync("channel logos changed", refresh=True)
    except Exception as e:
        db.rollback()
        logger.warning(f"[LiveTV] Channel logos pass failed: {e}")
    finally:
        db.close()


def cached_file(name: str) -> Optional[tuple]:
    """(path, content type) of a copy in the logo cache, or None."""
    if not FILE_RE.match(name or ""):
        return None
    path = os.path.join(cache_dir(), name)
    if not os.path.isfile(path):
        return None
    return path, CONTENT_TYPES[name.rsplit(".", 1)[1]]

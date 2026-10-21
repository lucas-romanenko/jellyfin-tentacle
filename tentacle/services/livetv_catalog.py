"""The channel catalog: networks, types, guide marks and events for the pool.

- Networks: channels that are one network in different places or qualities
  ("US: NBC (KNBC) LOS ANGELES", "NBC WNBC NEW YORK", "NBC HD ⁽ᴮᴷ⁾") fold into
  one LiveNetwork, pool key "n<id>", streamed at /api/live/net/<id> from the
  variant the admin chose (default: a working one people already watch, else
  one in the admin's region, else the best quality). Folding is by name
  (services/livetv_names.py) over every channel the provider lists, so
  switching groups on and off never changes keys. The admin can split a
  wrong merge, merge a missed one, choose the variant, take a network out of
  the pool and correct types; folding leaves what the admin decided alone
  (pinned channels, dissolved networks).
- Keys: a pool key that stops being one leaves an alias (live_key_aliases)
  to the key it became, so people's lists, favourites and recordings follow
  (services/livetv_lineups.py, services/livetv_tuners.py).
- Types (Sports, News, ...): the admin's, else from the guide's programme
  categories / titles, the provider's group name and the channel name.
- has_guide: guide data for the next 24 h in the last refresh.
- Events: services/livetv_events.py, read from event channel names.
- new: the entry joined the pool in the last NEW_DAYS days (My channels
  marks it New). Every key the pool ever had is kept (live_pool_arrivals);
  a key that took over a known one (folding) isn't new.

refresh() rebuilds the folding when the channel list changed (a hash of every
channel's id, name, group and switch, kept in the settings); pool() calls
ensure_fresh() first, so it is cheap when nothing changed.
"""
import hashlib
import logging
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models.database import (EPGProgram, LiveChannel, LiveChannelCatalog, LiveKeyAlias, LiveNetwork,
                             LivePoolArrival, LiveUserChannel, get_setting, set_setting)
from services import livetv_events, livetv_names

logger = logging.getLogger(__name__)

TYPES = ["Sports", "News", "Movies", "Kids", "Entertainment", "Local", "Music", "Documentary",
         "Lifestyle", "Religious", "International", "Event/PPV"]

_FP_KEY = "livetv_catalog_fingerprint"
_lock = threading.RLock()


# ── Folding ─────────────────────────────────────────────────────────────────

def _fingerprint(rows) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r.id}\x1f{r.name}\x1f{r.group_title}\x1f{int(bool(r.enabled))}\x1e".encode("utf-8", "replace"))
    return h.hexdigest()


def _channel_rows(db: Session):
    return (db.query(LiveChannel.id, LiveChannel.name, LiveChannel.group_title, LiveChannel.enabled,
                     LiveChannel.sort_order, LiveChannel.provider_id)
            .order_by(LiveChannel.id).all())


def _inputs_fingerprint(db: Session, rows) -> str:
    """The channel list, plus what the default variants depend on: which
    guide ids have a guide and which channels the checker found broken."""
    h = hashlib.sha256(_fingerprint(rows).encode())
    try:
        h.update("\x1f".join(sorted(guide_facts(db)["with_guide"])).encode("utf-8", "replace"))
    except Exception:
        pass
    h.update(",".join(str(c) for c, st in sorted(_check_states(db).items()) if st == "broken").encode())
    return h.hexdigest()


def ensure_fresh(db: Session) -> bool:
    """refresh() if the channel list (or the guide / checker facts the
    default variants use) changed since the last one. True when it ran."""
    rows = _channel_rows(db)
    fp = _inputs_fingerprint(db, rows)
    if get_setting(db, _FP_KEY, "") == fp:
        return False
    with _lock:
        if get_setting(db, _FP_KEY, "") == fp:
            return False
        refresh(db, rows=rows, fingerprint=fp)
    return True


def _foldable(name: str, group: Optional[str]) -> bool:
    return not livetv_names.is_separator(name) and not livetv_events.is_event_channel(name, group)


def _network_of(nets: dict, nid: Optional[int]) -> Optional[LiveNetwork]:
    """A network, following merges."""
    hops = 0
    while nid is not None and hops < 20:
        n = nets.get(nid)
        if n is None:
            return None
        if n.merged_into is None:
            return n
        nid = n.merged_into
        hops += 1
    return None


def _types(row) -> Optional[list]:
    """The admin's types on a network or catalog row (None = automatic)."""
    return [t for t in (row.types or "").split(",") if t] or None


def refresh(db: Session, rows=None, fingerprint: Optional[str] = None) -> dict:
    """Fold every channel into its network, choose default variants, record
    aliases for keys that changed. Returns counts."""
    t0 = time.monotonic()
    rows = _channel_rows(db) if rows is None else rows
    by_id = {r.id: r for r in rows}
    cats = {c.channel_id: c for c in db.query(LiveChannelCatalog).all()}
    nets = {n.id: n for n in db.query(LiveNetwork).all()}
    by_key = {}
    for n in sorted(nets.values(), key=lambda n: n.id):
        if n.fold_key and not n.dissolved:
            by_key.setdefault(n.fold_key, n)

    # Channels the provider no longer lists leave the catalog.
    for cid in [cid for cid in cats if cid not in by_id]:
        db.delete(cats.pop(cid))

    # What each channel folds into by name.
    keys, analysis = {}, {}
    for r in rows:
        if not _foldable(r.name, r.group_title):
            continue
        a = livetv_names.analyse(r.name)
        if a["key"]:
            keys[r.id] = a["key"]
            analysis[r.id] = a
    count = Counter(keys.values())

    member_of = {}
    for r in rows:
        cat = cats.get(r.id)
        if cat is not None and cat.pinned:
            n = _network_of(nets, cat.network_id)
            member_of[r.id] = n.id if n is not None else None
            continue
        k = keys.get(r.id)
        if not k or count[k] < 2:
            member_of[r.id] = None
            continue
        n = by_key.get(k)
        if n is None:
            dissolved = any(x.fold_key == k and x.dissolved for x in nets.values())
            if dissolved:
                member_of[r.id] = None
                continue
            n = LiveNetwork(fold_key=k, name=k)
            db.add(n)
            db.flush()
            nets[n.id] = n
            by_key[k] = n
        n = _network_of(nets, n.id)
        member_of[r.id] = n.id if n is not None else None

    members = defaultdict(list)
    for cid, nid in member_of.items():
        if nid is not None:
            members[nid].append(cid)

    # Names: the most common spelling among the members, unless the admin named it.
    for nid, cids in members.items():
        n = nets[nid]
        if n.name_set:
            continue
        spellings = Counter(livetv_names.display_name(analysis[c]["display"]) for c in cids if c in analysis)
        if spellings:
            best = max(spellings.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
            if n.name != best:
                n.name = best

    _choose_defaults(db, nets, members, by_id)

    # Catalog rows and aliases.
    current = {}   # channel id -> pool key now (None = not in the pool)
    for r in rows:
        nid = member_of.get(r.id)
        if not r.enabled:
            current[r.id] = None
        elif nid is not None:
            current[r.id] = None if nets[nid].excluded else f"n{nid}"
        else:
            current[r.id] = f"c{r.id}"
    pool_keys = {k for k in current.values() if k}
    aliases = {a.old_key: a for a in db.query(LiveKeyAlias).all()}
    # The chosen variant first: a dissolved network's key goes to it.
    chosen = {_effective_choice(n) for n in nets.values()}
    order = sorted(rows, key=lambda r: (0 if r.id in chosen else 1, r.id))
    new_aliases = 0
    keys_moved = 0
    now = datetime.utcnow()
    for r in order:
        cat = cats.get(r.id)
        if cat is None:
            cat = LiveChannelCatalog(channel_id=r.id)
            db.add(cat)
            cats[r.id] = cat
        if cat.network_id != member_of.get(r.id):
            cat.network_id = member_of.get(r.id)
        now_key = current[r.id]
        if now_key is None:
            continue   # off or taken out: keep the key it had, no alias
        last = cat.pool_key or f"c{r.id}"   # before the catalog every channel was c<id>
        if last != now_key:
            if last not in pool_keys and last not in aliases:
                db.add(LiveKeyAlias(old_key=last, new_key=now_key, created_at=now))
                aliases[last] = None
                new_aliases += 1
            elif last not in pool_keys and aliases.get(last) is not None and aliases[last].new_key != now_key:
                # Pointed at a key that is gone itself? Point it here.
                if aliases[last].new_key not in pool_keys:
                    aliases[last].new_key = now_key
            cat.pool_key = now_key
            keys_moved += 1
    # A key that is a pool key again is nobody's alias any more.
    for old, a in list(aliases.items()):
        if a is not None and old in pool_keys:
            db.delete(a)

    if fingerprint is None:
        fingerprint = _inputs_fingerprint(db, rows)
    set_setting(db, _FP_KEY, fingerprint)
    db.commit()
    _invalidate()
    if keys_moved:
        # People's channels changed keys: the tuner sync carries their
        # favourites and recordings over before Jellyfin drops the old ones.
        try:
            from services.livetv_tuners import schedule_sync
            schedule_sync(f"channel catalog: {keys_moved} channel(s) changed key", refresh=True)
        except Exception as e:
            logger.warning(f"[LiveTV] Catalog: tuner sync not scheduled: {e}")
    stats = {"channels": len(rows), "networks": len(members),
             "folded_channels": sum(len(v) for v in members.values()), "new_aliases": new_aliases,
             "seconds": round(time.monotonic() - t0, 2)}
    logger.info(f"[LiveTV] Catalog: {stats['folded_channels']} channels fold into {stats['networks']} networks"
                f" ({new_aliases} keys moved, {stats['seconds']} s)")
    return stats


def _effective_choice(n: LiveNetwork) -> Optional[int]:
    return n.chosen_channel_id or n.auto_channel_id


# ── Choosing a network's variant ────────────────────────────────────────────

def _region(db: Session) -> tuple:
    """(country code, place words nearest first) the admin is in: the setting
    `livetv_region` ("CA: Toronto, Ontario, Buffalo, East"), else the server's
    time zone."""
    text = (get_setting(db, "livetv_region", "") or "").strip()
    if text:
        country, _, places = text.partition(":") if ":" in text else ("", "", text)
        words = [w.strip().upper() for w in places.replace(";", ",").split(",") if w.strip()]
        return country.strip().upper() or None, words
    try:
        import os
        tz = os.environ.get("TZ") or ""
        if not tz:
            tz = str(datetime.now().astimezone().tzinfo or "")
    except Exception:
        tz = ""
    return _TZ_REGIONS.get(tz, (None, [tz.rsplit("/", 1)[-1].replace("_", " ").upper()] if "/" in tz else []))


# Time zone -> (country, places nearest it; US border markets for Canada).
_TZ_REGIONS = {
    "America/Toronto": ("CA", ["TORONTO", "ONTARIO", "BUFFALO", "DETROIT", "EAST", "EASTERN"]),
    "America/Montreal": ("CA", ["MONTREAL", "QUEBEC", "BURLINGTON", "PLATTSBURGH", "EAST", "EASTERN"]),
    "America/Vancouver": ("CA", ["VANCOUVER", "BRITISH COLUMBIA", "BC", "SEATTLE", "PACIFIC", "WEST"]),
    "America/Edmonton": ("CA", ["EDMONTON", "CALGARY", "ALBERTA", "SPOKANE", "WEST", "MOUNTAIN"]),
    "America/Winnipeg": ("CA", ["WINNIPEG", "MANITOBA", "FARGO", "CENTRAL"]),
    "America/Regina": ("CA", ["REGINA", "SASKATOON", "SASKATCHEWAN", "MINOT"]),
    "America/Halifax": ("CA", ["HALIFAX", "NOVA SCOTIA", "MARITIMES", "ATLANTIC", "BANGOR", "EAST"]),
    "America/St_Johns": ("CA", ["ST JOHNS", "NEWFOUNDLAND", "ATLANTIC", "EAST"]),
    "America/New_York": ("US", ["NEW YORK", "EAST", "EASTERN"]),
    "America/Chicago": ("US", ["CHICAGO", "CENTRAL"]),
    "America/Denver": ("US", ["DENVER", "MOUNTAIN", "WEST"]),
    "America/Los_Angeles": ("US", ["LOS ANGELES", "PACIFIC", "WEST"]),
    "Europe/London": ("UK", ["LONDON"]),
}

_QUALITY_RANK = [("BACKUP", -2), ("BK", -2), ("SD", -1), ("4K", 1), ("UHD", 1), ("FHD", 2), ("HD", 2)]


def _quality(name: str) -> int:
    toks = set(livetv_names._tokens(livetv_names._plain(name)))
    for word, rank in _QUALITY_RANK:
        if word in toks:
            return rank
    return 0


def _choose_defaults(db: Session, nets: dict, members: dict, by_id: dict) -> None:
    """Each network's default variant (data.auto_channel_id): among switched-on
    channels, one that works, then one people already have in their lists
    (they keep watching what they watched), then the admin's region, then
    one with a guide, then the admin's country, then quality; else the first."""
    if not members:
        return
    in_lists = Counter()
    for (key,) in db.query(LiveUserChannel.channel_key).all():
        if key and key[0] == "c" and key[1:].isdigit():
            in_lists[int(key[1:])] += 1
    states = _check_states(db)
    country, places = _region(db)
    with_guide = guide_facts(db)["with_guide"]
    guided = set()
    for cid, override, by_name, tvg in db.query(LiveChannel.id, LiveChannel.epg_id_override,
                                                LiveChannel.epg_name_match, LiveChannel.epg_channel_id):
        gid = (override or "").strip() or by_name or (tvg or "").strip()
        if gid and gid in with_guide:
            guided.add(cid)
    for nid, cids in members.items():
        n = nets[nid]
        on = [c for c in cids if by_id[c].enabled] or cids

        def score(cid):
            name = by_id[cid].name
            words = livetv_names.region_words(name)
            st = states.get(cid)
            return (
                1 if st == "works" else 0 if st is None else -1,
                in_lists[cid],
                # The nearest place named first: Toronto, then Buffalo, then Detroit.
                max((len(places) - i for i, p in enumerate(places) if p in words), default=0),
                1 if cid in guided else 0,
                1 if country and country in words else 0,
                _quality(name),
                -(by_id[cid].sort_order or 0),
                -cid,
            )
        best = max(on, key=score)
        if n.auto_channel_id != best:
            n.auto_channel_id = best


def _check_states(db: Session) -> dict:
    """channel id -> "works" / "broken" from the stream checker, if there is one."""
    try:
        from services import livetv_checker
        res = livetv_checker.results(db, networks={})
    except Exception:
        return {}
    out = {}
    for k, v in (res or {}).items():
        if k[:1] == "c" and k[1:].isdigit() and v.get("state") in ("works", "broken"):
            out[int(k[1:])] = v["state"]
    return out


def chosen_variants(db: Session) -> dict:
    """network id -> the LiveChannel id it streams (the admin's choice if
    still switched on, else the default), for networks in the pool."""
    out = {}
    for nid, info in _network_members(db).items():
        if info["variant"] is not None:
            out[nid] = info["variant"]
    return out


def _network_members(db: Session) -> dict:
    """network id -> {"network", "members": [enabled channel ids], "all": [ids],
    "variant": id or None} for networks that are not merged away."""
    nets = {n.id: n for n in db.query(LiveNetwork).filter(LiveNetwork.merged_into.is_(None)).all()}
    rows = (db.query(LiveChannelCatalog.channel_id, LiveChannelCatalog.network_id, LiveChannel.enabled)
            .join(LiveChannel, LiveChannel.id == LiveChannelCatalog.channel_id)
            .filter(LiveChannelCatalog.network_id.isnot(None)).all())
    out = {}
    for cid, nid, enabled in rows:
        if nid not in nets:
            continue
        info = out.setdefault(nid, {"network": nets[nid], "members": [], "all": [], "variant": None})
        info["all"].append(cid)
        if enabled:
            info["members"].append(cid)
    for nid, info in out.items():
        n = info["network"]
        on = info["members"]
        for c in (n.chosen_channel_id, n.auto_channel_id):
            if c in on:
                info["variant"] = c
                break
        else:
            info["variant"] = min(on) if on else None
    return out


def network_channel(db: Session, nid: int) -> Optional[int]:
    """The LiveChannel id /api/live/net/<nid> streams (None: not in the pool)."""
    ensure_fresh(db)
    n = db.query(LiveNetwork).filter(LiveNetwork.id == nid).first()
    if n is None or n.merged_into is not None or n.excluded:
        return None
    return chosen_variants(db).get(nid)


# ── Aliases ─────────────────────────────────────────────────────────────────

_alias_cache = {"value": None}


def aliases(db: Session) -> dict:
    """old key -> current key for keys that stopped being pool keys (chains
    followed). Cheap: cached until the folding or the admin changes keys; it
    never refolds (pool() does that, through ensure_fresh())."""
    if _alias_cache["value"] is not None:
        return dict(_alias_cache["value"])
    raw = {a.old_key: a.new_key for a in db.query(LiveKeyAlias).all() if a.old_key != a.new_key}
    out = {}
    for old, new in raw.items():
        hops = 0
        while new in raw and hops < 20:   # chains end at the key it is now
            new = raw[new]
            hops += 1
        if new != old:
            out[old] = new
    _alias_cache["value"] = out
    return dict(out)


def _add_alias(db: Session, old: str, new: str) -> None:
    if not old or old == new:
        return
    _alias_cache["value"] = None
    a = db.query(LiveKeyAlias).filter(LiveKeyAlias.old_key == old).first()
    if a is None:
        db.add(LiveKeyAlias(old_key=old, new_key=new, created_at=datetime.utcnow()))
    else:
        a.new_key = new


# ── Guide facts: has guide, types from the guide ─────────────────────────────

_GUIDE_TTL = 600.0
_guide_cache = {"at": -1e9, "value": None}
_event_cache = {"at": -1e9, "value": {}}


def _invalidate() -> None:
    # The values go, not only their times: a monotonic clock starts near 0
    # on a freshly booted machine, so "at 0" can still look fresh.
    _alias_cache["value"] = None
    _guide_cache.update(at=-1e9, value=None)
    _event_cache.update(at=-1e9, value={})


def guide_facts(db: Session) -> dict:
    """{"with_guide": set of guide ids with a programme in the next 24 h,
    "types": guide id -> set of types its programmes say}. Cached 10 min."""
    now = time.monotonic()
    if _guide_cache["value"] is not None and now - _guide_cache["at"] < _GUIDE_TTL:
        return _guide_cache["value"]
    from services.epg_categories import infer_category
    utc = datetime.utcnow()
    with_guide = set()
    seen = defaultdict(Counter)
    totals = Counter()
    q = (db.query(EPGProgram.channel_id, EPGProgram.title, EPGProgram.category, EPGProgram.start, EPGProgram.stop)
         .filter(EPGProgram.stop >= utc - timedelta(hours=24))
         .yield_per(5000))
    horizon = utc + timedelta(hours=24)
    for cid, title, category, start, stop in q:
        if stop >= utc and start <= horizon:
            with_guide.add(cid)
        totals[cid] += 1
        for t in _types_from_category(category, title, infer_category):
            seen[cid][t] += 1
    types = {}
    for cid, cnt in seen.items():
        n = totals[cid]
        ts = {t for t, c in cnt.items() if c >= max(2, 0.3 * n)}
        if ts:
            types[cid] = ts
    value = {"with_guide": with_guide, "types": types}
    _guide_cache.update(at=now, value=value)
    return value


_CATEGORY_TYPES = [
    (("sport", "football", "hockey", "basketball", "baseball", "soccer", "golf", "tennis", "racing",
      "boxing", "wrestling", "mma"), "Sports"),
    (("news", "current affairs", "weather", "business", "politic"), "News"),
    (("movie", "film", "cinema"), "Movies"),
    (("kids", "children", "animation", "cartoon", "family"), "Kids"),
    (("documentary", "nature", "science", "history", "educational"), "Documentary"),
    (("music",), "Music"),
    (("cooking", "food", "home", "garden", "travel", "lifestyle", "fashion", "shopping", "reality"), "Lifestyle"),
    (("religio", "faith", "church"), "Religious"),
]
_INFERRED = {"Sports": "Sports", "News": "News", "Kids": "Kids", "Movie": "Movies"}


def _types_from_category(category, title, infer) -> set:
    out = set()
    if category:
        from services.xmltv import CATEGORY_SEP
        for c in str(category).split(CATEGORY_SEP):
            c = c.lower()
            for words, t in _CATEGORY_TYPES:
                if any(w in c for w in words):
                    out.add(t)
    else:
        inferred = infer(title, None)
        if inferred in _INFERRED:
            out.add(_INFERRED[inferred])
    return out


# ── Types from the provider's group and the channel name ─────────────────────

import re  # noqa: E402

_NAME_TYPES = [
    ("Sports", r"\bSPORTS?\b|SPORTSNET|\bTSN\d?\b|\bESPN|\bNFL\b|\bNBA\b|\bNHL\b|\bMLB\b|\bMLS\b|\bGOLF\b|\bTENNIS\b|"
               r"\bUFC\b|\bWWE\b|\bDAZN\b|\bBEIN\b|\bNESN\b|\bMSG\b|\bRACING\b|\bSOCCER\b|\bFOOTBALL\b|\bHOCKEY\b|"
               r"\bBASEBALL\b|\bBASKETBALL\b|\bOLYMPIC|\bREDZONE\b|\bBALLY\b|\bPAC 12\b|\bBIG TEN\b|\bBTN\b|"
               r"\bSEC NETWORK\b|\bACC NETWORK\b|\bFIGHT\b|\bRDS\b|\bTVA SPORTS\b|\bSPORTSMAN\b|\bOUTDOOR\b|\bF1\b|"
               r"\bCFL\b|\bWHL\b|\bCHL\b|\bNCAA|\bMILB\b|\bBILLIARD|\bSTRIKE ZONE\b|\bTUDN\b|\bPREMIER LEAGUE\b|\bLEAGUE PASS\b"),
    ("News", r"\bNEWS|\bCNN\b|\bCNBC\b|\bMSNBC\b|\bHLN\b|\bBLOOMBERG\b|\bCP24\b|\bBNN\b|\bCPAC\b|\bWEATHER\b|"
             r"\bC-?SPAN\b|\bNEWSMAX\b|\bAL JAZEERA\b|\bSKY NEWS\b|\bFOX BUSINESS\b|\bCOURT TV\b|\bNEWSNATION\b|\bCITYNEWS\b"),
    # Conservative: only channels made for children. "Teletoon at Night",
    # "Adult Swim" and the like are animation for grown-ups.
    ("Kids", r"(?<!ADULT )\bKIDS?\b|\bDISNEY (?:JR|JUNIOR|CHANNEL|XD)\b|^DISNEY$|\bNICK(?:ELODEON| JR| JUNIOR|TOONS)?\b|"
             r"\bCARTOON NETWORK\b|\bBOOMERANG\b|\bTREE ?HOUSE\b|\bYTV\b|\bTELE ?TOON\b|\bBABY ?(?:TV|FIRST)\b|"
             r"\bFAMILY (?:JR|JUNIOR|CHRGD|CHANNEL)\b|\bPBS KIDS\b|\bUNIVERSAL KIDS\b|\bTEEN ?NICK\b|\bDISCOVERY FAMILY\b|"
             r"\bMETV TOONS\b|\bDUCK ?TV\b|\bBOUNCE ?KIDS\b|\bSPROUT\b|\bLA CHAINE DISNEY\b|\bVRAK\b|\bTELE-?QUEBEC\b"),
    ("Movies", r"\bMOVIES?\b|\bCINEMA|\bCINEMAX\b|\bHBO\b|\bSTARZ\b|\bENCORE\b|\bTCM\b|\bSHOWTIME\b|\bMGM\b|"
               r"\bSUPER CHANNEL\b|\bSUPER ECRAN\b|\bCINEPOP\b|\bHOLLYWOOD SUITE\b|\bCRAVE\b|\bFXM\b|\bIFC\b|"
               r"\bSUNDANCE\b|\bEPIX\b|\bMOVIETIME\b|\bSILVER SCREEN\b|\bFLIX\b|\bMAX\b|\bCINE"),
    ("Documentary", r"\bDISCOVERY\b|\bHISTORY\b|\bNATIONAL GEOGRAPHIC\b|\bNAT ?GEO\b|\bSMITHSONIAN\b|\bANIMAL PLANET\b|"
                    r"\bSCIENCE\b|\bH2\b|\bDOCUMENTARY\b|\bCURIOSITY\b|\bBBC EARTH\b|\bINVESTIGATION\b|\bAHC\b|\bDESTINATION AMERICA\b"),
    ("Music", r"\bMUSIC\b|\bMTV|\bVH ?1\b|\bCMT\b|\bMUCH\b|\bREVOLT\b|\bAXS\b|\bFUSE\b|\bSTINGRAY\b|\bRADIO\b|\bCOACHELLA\b|\bTOMORROWLAND\b"),
    ("Lifestyle", r"\bFOOD\b|\bCOOKING\b|\bHGTV\b|\bDIY\b|\bTLC\b|\bTRAVEL\b|\bMAGNOLIA\b|\bTASTEMADE\b|\bW ?NETWORK\b|"
                  r"\bSLICE\b|\bMAKEFUL\b|\bOWN\b|\bHOME\b|\bGARDEN\b|\bFASHION\b|\bSHOP|\bQVC\b|\bHSN\b|\bLIFETIME\b|\bOXYGEN\b"),
    ("Religious", r"\bEWTN\b|\bTBN\b|\bDAYSTAR\b|\b3ABN\b|\bGOD\b|\bCHRISTIAN\b|\bCATHOLIC\b|\bSALT\b|\bGOSPEL\b|\bWORSHIP\b|\bHILLSONG\b"),
]
_NAME_TYPES = [(t, re.compile(rx)) for t, rx in _NAME_TYPES]
_GROUP_TYPES = [
    ("Sports", re.compile(r"SPORT|\bNFL\b|\bNBA\b|\bNHL\b|\bMLB\b|\bNCAA|\bMILB\b|\bCFL\b|\bWHL\b|\bTSN\b|\bDAZN\b|\bESPN|\bF1\b|\bBALLY\b")),
    ("News", re.compile(r"NEWS")),
    ("Movies", re.compile(r"CINEMA|MOVIE|FILM|\bHBO\b")),
    ("Documentary", re.compile(r"DOCUMENTAR")),
    ("Music", re.compile(r"MUSIC|MUSIQUE")),
    ("Religious", re.compile(r"RELIGIO|FAITH")),
    ("Entertainment", re.compile(r"ENTERTAINMENT|GENERAL")),
]
_NOT_KIDS = re.compile(r"\bAT NIGHT\b|\bADULT\b|\bNIGHT\b|\bXXX\b|\bAFTER DARK\b")
_LOCAL_STATION = re.compile(r"^\s*(?:CBC|CTV2?|GLOBAL|CITY ?TV|CITYTV|CITY|ABC|NBC|CBS|FOX|PBS|CW|OMNI|CHCH|MNT|"
                            r"TELEMUNDO|UNIVISION|ION)\b")
_LOCAL_NOT = re.compile(r"\bNEWS\b|SPORT|BUSINESS|WEATHER|KIDS|\bNOW\b|\bLIVE\b|SOUL|SOCCER|DEPORTES|\bNET\b|"
                        r"BRAVO|SYFY|\bE!|OXYGEN|CNBC|GOLF|TELEMUNDO \w+ NETWORK")
_LOCAL_NETWORKS = {"abc", "nbc", "cbs", "fox", "cw", "pbs", "mnt", "ion", "telemundo", "univision", "unimas",
                   "ctv", "ctv2", "global", "cbc", "city", "tva", "noovo", "omni", "ici", "metv", "chch"}
_HOME_COUNTRIES = {"US", "USA", "CA", "UK", "GB", "TV", "EN", "PRIME"}


def channel_types(name: str, group: Optional[str], event: bool = False, guide_types: Optional[set] = None,
                  fold_key: Optional[str] = None, removed: Optional[set] = None) -> list:
    """The types a channel is, without the admin's correction."""
    text = livetv_names._plain(name or "").upper()
    grp = livetv_names._plain(group or "").upper()
    out = set(guide_types or ())
    for t, rx in _NAME_TYPES:
        if rx.search(text):
            out.add(t)
    if not out:
        for t, rx in _GROUP_TYPES:
            if rx.search(grp):
                out.add(t)
    if event or livetv_events.is_event_group(group) or re.search(r"\bPPV\b|BOX OFFICE", text):
        out.add("Event/PPV")
        if event and "Music" not in out:
            out.add("Sports")   # event slots are matches and fights, unless they say otherwise
    if fold_key in _LOCAL_NETWORKS or (removed and removed & {"call", "place"} and fold_key and
                                       fold_key.rstrip("0123456789") in _LOCAL_NETWORKS):
        out.add("Local")
    bare = livetv_names._strip_prefixes(text)
    if _LOCAL_STATION.match(bare) and not _LOCAL_NOT.search(bare):
        out.add("Local")   # a local station that did not fold ("CBC YELLOWKNIFE")
    m = re.match(r"^\s*[|\[(]?\s*([A-Z]{2})(?:[ /-]([A-Z]{2}))?\s*[|\]):]", text)
    if m and (m.group(1) not in _HOME_COUNTRIES or (m.group(2) or "EN") not in ("EN", "US")):
        if not (m.group(1) in ("CA", "US") and m.group(2) is None):
            out.add("International")
    if "Kids" in out and _NOT_KIDS.search(text):
        out.discard("Kids")
    if not out - {"International", "Local", "Event/PPV"} and not event:
        out.add("Entertainment")
    return [t for t in TYPES if t in out]


# ── The pool ────────────────────────────────────────────────────────────────

def _event_for(name: str, group: Optional[str]):
    """livetv_events.parse with a 5-minute cache (the result depends on the clock)."""
    now = time.monotonic()
    if now - _event_cache["at"] > 300:
        _event_cache.update(at=now, value={})
    cache = _event_cache["value"]
    k = (name, group)
    if k not in cache:
        try:
            cache[k] = livetv_events.parse(name, group)
        except Exception as e:  # a strange name must never break the pool
            logger.debug(f"[LiveTV] Event name not read: {name!r}: {e}")
            cache[k] = None
    return cache[k]


def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_json(ev):
    if ev is None or isinstance(ev, str):
        return ev
    return {"title": ev["title"], "start": _iso(ev["start"]), "stop": _iso(ev["stop"]),
            "timed": ev["timed"], "description": ev.get("description")}


def _people_name(ch: LiveChannel, ev) -> str:
    """The name people see: the admin's own name, else the event on an
    event channel, else the provider's name cleaned ("CA EN: SPORTSNET
    ONTARIO HD" -> "Sportsnet Ontario")."""
    if (ch.custom_name or "").strip():
        return ch.custom_name.strip()
    if isinstance(ev, dict):
        return ev["title"]
    return livetv_names.clean_name(ch.name)


def fold_pool(db: Session, entries: list, channels_by_id: dict) -> list:
    """The pool with networks folded: `entries` are pool() entries in order
    (c<id> for every switched-on channel, y<id> for YouTube), `channels_by_id`
    the LiveChannel rows. A network takes the place of its first channel;
    every entry gets types, has_guide, event, variants, variant_name, check."""
    ensure_fresh(db)
    nets = _network_members(db)
    net_of = {}
    for nid, info in nets.items():
        if info["network"].excluded:
            for cid in info["all"]:
                net_of[cid] = None   # taken out of the pool
            continue
        if info["variant"] is None:
            continue
        for cid in info["all"]:
            net_of[cid] = nid
    overrides = {c.channel_id: _types(c) for c in db.query(LiveChannelCatalog)
                 .filter(LiveChannelCatalog.types.isnot(None)).all() if _types(c)}
    facts = guide_facts(db)
    with_guide, gtypes = facts["with_guide"], facts["types"]
    try:
        from services import livetv_checker
        checks = livetv_checker.results(db, networks={nid: i["variant"] for nid, i in nets.items()
                                                      if i["variant"] is not None})
    except Exception:
        checks = None

    by_number = {e["key"]: e for e in entries}
    out, placed = [], set()
    for e in entries:
        key = e["key"]
        if key[:1] != "c":
            e = dict(e)
            e.setdefault("types", ["Entertainment"])
            e.setdefault("has_guide", True)
            e.setdefault("event", None)
            e.setdefault("variants", 1)
            e.setdefault("variant_name", e["name"])
            if checks is not None:
                e.setdefault("check", checks.get(key) or {"state": "unchecked", "at": None})
            out.append(e)
            continue
        cid = int(key[1:])
        ch = channels_by_id.get(cid)
        if cid in net_of and net_of[cid] is None:
            continue
        nid = net_of.get(cid)
        if nid is not None:
            if nid in placed:
                continue
            placed.add(nid)
            info = nets[nid]
            var = info["variant"]
            vch = channels_by_id.get(var)
            ve = by_number.get(f"c{var}")
            if vch is None or ve is None:
                continue
            n = info["network"]
            gid = vch.guide_epg_id
            types = _types(n) or channel_types(
                vch.name, vch.group_title, guide_types=gtypes.get(gid), fold_key=n.fold_key,
                removed={"place"} if len(info["all"]) > 1 else None)
            entry = {
                "key": f"n{nid}", "name": n.name, "logo_url": vch.logo_url or ve.get("logo_url"),
                "xmltv_id": ve["xmltv_id"], "stream_path": f"/api/live/net/{nid}",
                "types": types, "has_guide": bool(gid and gid in with_guide), "event": None,
                "variants": len(info["members"]), "variant_name": (vch.custom_name or "").strip() or livetv_names.clean_name(vch.name),
                "provider_name": vch.name, "channel_id": var,
            }
            if checks is not None:
                entry["check"] = checks.get(f"n{nid}") or {"state": "unchecked", "at": None}
            out.append(entry)
            continue
        gid = ch.guide_epg_id if ch else None
        has_guide = bool(gid and gid in with_guide)
        provider_name = ch.name if ch else e["name"]
        ev = _event_for(ch.name, ch.group_title) if ch else None
        types = overrides.get(cid) or channel_types(
            ch.name if ch else e["name"], ch.group_title if ch else None, event=ev is not None,
            guide_types=gtypes.get(gid))
        entry = dict(e)
        entry.update(name=_people_name(ch, ev) if ch else e["name"], provider_name=provider_name,
                     channel_id=cid, types=types, has_guide=has_guide, event=_event_json(ev), variants=1,
                     variant_name=ch.guide_name if ch else e["name"])
        if checks is not None:
            entry["check"] = checks.get(key) or {"state": "unchecked", "at": None}
        out.append(entry)
    _mark_main(db, out, channels_by_id)
    try:
        _mark_new(db, out)
    except Exception as e:   # a mark must never take the channels away
        logger.warning(f"[LiveTV] New channels not marked: {e}")
    return out


NEW_DAYS = 14


def _mark_new(db: Session, entries: list, now: Optional[datetime] = None) -> None:
    """entry["new"]: in the pool for less than NEW_DAYS (live_pool_arrivals).
    The first time (no record yet) every entry is the baseline, not new."""
    now = now or datetime.utcnow()
    known = {a.key: a.added_at for a in db.query(LivePoolArrival).all()}
    fresh = [e["key"] for e in entries if e["key"] not in known]
    if fresh:
        baseline = not known
        came_from = defaultdict(set)
        if not baseline:
            for old, new in aliases(db).items():
                came_from[new].add(old)
        before = set(known)
        for k in dict.fromkeys(fresh):
            at = None if baseline or came_from.get(k, set()) & before else now
            db.add(LivePoolArrival(key=k, added_at=at))
            known[k] = at
        db.commit()
    cutoff = now - timedelta(days=NEW_DAYS)
    for e in entries:
        at = known.get(e["key"])
        e["new"] = at is not None and at >= cutoff


# French-language channels: no quick pick for an English household (setting
# `livetv_language`, default "en"); they stay in Add channels.
_FRENCH = re.compile(r"^\s*[|\[(]?\s*[A-Z]{2}[ /-]FR\b|\bFRENCH\b|\bFRANCAIS|\bICI\b|\bRDS\b|\bTVA\b|\bNOOVO\b|"
                     r"\bSUPER ECRAN\b|\bCINEPOP\b|\bADDIK\b|\bCANAL\b|\bVRAK\b|\bTELE-?QUEBEC\b|\bTV5\b|"
                     r"\bUNIS\b|\bEVASION\b|\bZESTE\b|\bFRANCE\b|\bSERIES\+|\bMOI\b|\bTELETOON FR")

# The most a quick pick ("Sports", "News", ...) adds of one type.
MAIN_PER_TYPE = 25
_NO_QUICK_PICK = {"Event/PPV", "International"}


def _mark_main(db: Session, entries: list, channels_by_id: dict) -> None:
    """main / main_types: the short list a quick pick adds for a type, so a
    new person gets a decent lineup in a few taps while every channel of the
    type stays in Add channels. Candidates: no event slot, not foreign, not
    from an event/PPV group, and a guide or a network the provider carries
    in several places (a sign it is a real, known channel). Per type the best
    MAIN_PER_TYPE: the admin's country first (its variant, e.g. "CA EN:"),
    then a guide, a working stream, the admin's region, more variants."""
    country, places = _region(db)
    place_rank = {p: len(places) - i for i, p in enumerate(places)}
    english = (get_setting(db, "livetv_language", "en") or "en").strip().lower().startswith("en")
    cands = []
    for pos, e in enumerate(entries):
        e["main"], e["main_types"] = False, []
        if e.get("event") is not None or "International" in e.get("types", ()):
            continue
        ch = channels_by_id.get(e.get("channel_id"))
        group = ch.group_title if ch else None
        if ch is not None and livetv_events.is_event_group(group):
            continue
        if (e.get("check") or {}).get("state") == "broken":
            continue
        if not (e.get("has_guide") or e.get("variants", 1) >= 2):
            continue
        pname = e.get("provider_name") or e["name"]
        if english and _FRENCH.search(livetv_names._plain(pname).upper()):
            continue
        words = livetv_names.region_words(pname)
        score = (
            1 if e.get("variants", 1) >= 2 else 0,
            1 if country and country in words else 0,
            1 if e.get("has_guide") else 0,
            1 if (e.get("check") or {}).get("state") == "works" else 0,
            max((place_rank.get(w, 0) for w in words), default=0),
            min(e.get("variants", 1), 5),
            -pos,
        )
        cands.append((score, e))
    cands.sort(key=lambda se: se[0], reverse=True)
    taken = Counter()
    for _, e in cands:
        for t in e["types"]:
            if t in _NO_QUICK_PICK:
                continue
            if t == "Local" and not e["key"].startswith("n"):
                continue
            if t == "Entertainment" and "Local" in e["types"]:
                continue   # local stations come with the Local pick
            if taken[t] < MAIN_PER_TYPE:
                taken[t] += 1
                e["main_types"].append(t)
        e["main"] = bool(e["main_types"])


# ── Events in the guide ─────────────────────────────────────────────────────

def event_programmes(db: Session, channel_numbers: list) -> list:
    """XMLTV programmes for event channels without a guide of their own:
    channel_numbers = [(LiveChannel, guide number)] as the guide lists them."""
    facts = guide_facts(db)
    with_guide = facts["with_guide"]
    out = []
    for ch, number in channel_numbers:
        gid = ch.guide_epg_id
        if gid and gid in with_guide:
            continue
        ev = _event_for(ch.name, ch.group_title)
        if not isinstance(ev, dict):
            continue
        out.append({
            "channel_id": str(number), "title": ev["title"], "sub_title": None,
            "description": ev.get("description") or ("From the channel name" if ev["timed"] else
                                                     "From the channel name; the start time is not known"),
            "start": ev["start"].astimezone(timezone.utc).replace(tzinfo=None),
            "stop": ev["stop"].astimezone(timezone.utc).replace(tzinfo=None),
            "category": "Sports" if "Music" not in channel_types(ch.name, ch.group_title, event=True) else "Music",
            "icon_url": None,
        })
    return out


# ── Admin: what the catalog holds, and changing it ───────────────────────────

class CatalogError(ValueError):
    pass


def _bump(db: Session) -> None:
    """The next ensure_fresh() refolds (the admin changed something)."""
    set_setting(db, _FP_KEY, "")
    db.commit()
    _alias_cache["value"] = None


def networks_list(db: Session, include_empty: bool = False) -> list:
    """Every network with its variants, for the admin."""
    ensure_fresh(db)
    nets = _network_members(db)
    chans = {}
    ids = [c for info in nets.values() for c in info["all"]]
    for i in range(0, len(ids), 500):
        for ch in db.query(LiveChannel).filter(LiveChannel.id.in_(ids[i:i + 500])).all():
            chans[ch.id] = ch
    try:
        from services import livetv_checker
        checks = livetv_checker.results(db, networks={})
    except Exception:
        checks = {}
    facts = guide_facts(db)
    out = []
    for nid, info in sorted(nets.items(), key=lambda kv: kv[1]["network"].name.lower()):
        n = info["network"]
        if not info["members"] and not include_empty:
            continue
        variants = []
        for cid in sorted(info["all"], key=lambda c: (not chans[c].enabled, chans[c].guide_name.lower())):
            ch = chans[cid]
            variants.append({
                "channel_id": cid, "name": ch.guide_name, "provider_name": ch.name, "group": ch.group_title,
                "enabled": bool(ch.enabled), "chosen": cid == info["variant"],
                "has_guide": bool(ch.guide_epg_id and ch.guide_epg_id in facts["with_guide"]),
                "check": checks.get(f"c{cid}") or {"state": "unchecked", "at": None},
            })
        out.append({
            "id": nid, "key": f"n{nid}", "name": n.name, "fold_key": n.fold_key,
            "variant_id": info["variant"], "chosen_by_admin": n.chosen_channel_id is not None and
            n.chosen_channel_id == info["variant"], "default_variant_id": n.auto_channel_id,
            "excluded": bool(n.excluded), "types": _types(n), "manual": n.fold_key is None,
            "variants": variants, "count": len(info["members"]),
        })
    return out


def set_variant(db: Session, nid: int, channel_id: Optional[int]) -> None:
    """The admin's variant for a network (None: back to the default)."""
    n = db.query(LiveNetwork).filter(LiveNetwork.id == nid, LiveNetwork.merged_into.is_(None)).first()
    if n is None:
        raise CatalogError("No such network")
    if channel_id is not None:
        cat = db.query(LiveChannelCatalog).filter(LiveChannelCatalog.channel_id == channel_id).first()
        if cat is None or cat.network_id != nid:
            raise CatalogError("That channel is not in this network")
    n.chosen_channel_id = channel_id
    db.commit()


def rename_network(db: Session, nid: int, name: Optional[str]) -> None:
    n = db.query(LiveNetwork).filter(LiveNetwork.id == nid).first()
    if n is None:
        raise CatalogError("No such network")
    name = " ".join((name or "").split())[:80]
    if name:
        n.name = name
        n.name_set = True
    else:
        n.name_set = False
        _bump(db)
    db.commit()


def set_excluded(db: Session, nid: int, excluded: bool) -> None:
    """Take a whole network out of the pool (or back in)."""
    n = db.query(LiveNetwork).filter(LiveNetwork.id == nid, LiveNetwork.merged_into.is_(None)).first()
    if n is None:
        raise CatalogError("No such network")
    n.excluded = bool(excluded)
    _bump(db)


def set_types(db: Session, key: str, types: Optional[list]) -> list:
    """The admin's types for a channel ("c<id>") or network ("n<id>");
    None or [] goes back to the automatic ones."""
    clean = [t for t in TYPES if t in set(types or [])]
    bad = [t for t in (types or []) if t not in TYPES]
    if bad:
        raise CatalogError(f"Unknown type: {bad[0]}")
    if key[:1] == "n" and key[1:].isdigit():
        n = db.query(LiveNetwork).filter(LiveNetwork.id == int(key[1:])).first()
        if n is None:
            raise CatalogError("No such network")
        n.types = ",".join(clean) or None
    elif key[:1] == "c" and key[1:].isdigit():
        cid = int(key[1:])
        if db.query(LiveChannel.id).filter(LiveChannel.id == cid).first() is None:
            raise CatalogError("No such channel")
        cat = db.query(LiveChannelCatalog).filter(LiveChannelCatalog.channel_id == cid).first()
        if cat is None:
            cat = LiveChannelCatalog(channel_id=cid)
            db.add(cat)
        cat.types = ",".join(clean) or None
    else:
        raise CatalogError("Unknown key")
    db.commit()
    return clean


def split(db: Session, nid: int, channel_ids: Optional[list] = None, new_name: Optional[str] = None) -> dict:
    """Undo a wrong merge. With channel_ids: those channels leave the network
    (each on its own, or together as a new network `new_name`). Without:
    the whole network is dissolved (never folded again); its key goes to
    its variant's channel."""
    ensure_fresh(db)
    n = db.query(LiveNetwork).filter(LiveNetwork.id == nid, LiveNetwork.merged_into.is_(None)).first()
    if n is None:
        raise CatalogError("No such network")
    cats = db.query(LiveChannelCatalog).filter(LiveChannelCatalog.network_id == nid).all()
    in_net = {c.channel_id: c for c in cats}
    variant = chosen_variants(db).get(nid)
    if not channel_ids:
        n.dissolved = True
        for c in cats:
            c.pinned = True
            c.network_id = None
        if variant is not None:
            _add_alias(db, f"n{nid}", f"c{variant}")
        _bump(db)
        return {"dissolved": nid, "channels": len(cats)}
    ids = [int(i) for i in channel_ids]
    missing = [i for i in ids if i not in in_net]
    if missing:
        raise CatalogError(f"Channel {missing[0]} is not in this network")
    target = None
    if new_name:
        target = LiveNetwork(name=" ".join(new_name.split())[:80], fold_key=None, name_set=True)
        db.add(target)
        db.flush()
    for i in ids:
        c = in_net[i]
        c.pinned = True
        c.network_id = target.id if target else None
    if len(ids) == len(in_net):
        n.dissolved = True
        if target is not None:
            _add_alias(db, f"n{nid}", f"n{target.id}")
    if n.chosen_channel_id in ids:
        n.chosen_channel_id = None
    _bump(db)
    return {"network": target.id if target else None, "moved": len(ids)}


def merge(db: Session, keys: list, name: Optional[str] = None) -> dict:
    """Merge pool entries that are one network: keys "n<id>" and/or "c<id>".
    The first network listed keeps its key (else a new network is made);
    the other keys become aliases of it."""
    ensure_fresh(db)
    keys = [str(k) for k in keys or []]
    if len(keys) < 2:
        raise CatalogError("Choose at least two channels to merge")
    nets, chans = [], []
    for k in keys:
        if k[:1] == "n" and k[1:].isdigit():
            n = db.query(LiveNetwork).filter(LiveNetwork.id == int(k[1:]), LiveNetwork.merged_into.is_(None)).first()
            if n is None:
                raise CatalogError(f"No such network: {k}")
            nets.append(n)
        elif k[:1] == "c" and k[1:].isdigit():
            if db.query(LiveChannel.id).filter(LiveChannel.id == int(k[1:])).first() is None:
                raise CatalogError(f"No such channel: {k}")
            chans.append(int(k[1:]))
        else:
            raise CatalogError(f"Unknown key: {k}")
    if nets:
        target = nets[0]
    else:
        target = LiveNetwork(name=name or "", fold_key=None)
        db.add(target)
        db.flush()
    if name:
        target.name = " ".join(name.split())[:80]
        target.name_set = True
    for n in nets[1:]:
        n.merged_into = target.id
        for c in db.query(LiveChannelCatalog).filter(LiveChannelCatalog.network_id == n.id).all():
            c.network_id = target.id
            c.pinned = True
        _add_alias(db, f"n{n.id}", f"n{target.id}")
    for cid in chans:
        c = db.query(LiveChannelCatalog).filter(LiveChannelCatalog.channel_id == cid).first()
        if c is None:
            c = LiveChannelCatalog(channel_id=cid)
            db.add(c)
        c.network_id = target.id
        c.pinned = True
    if not target.name:
        first = db.query(LiveChannel).filter(LiveChannel.id == chans[0]).first()
        target.name = livetv_names.display_name(livetv_names.analyse(first.name)["display"]) or first.guide_name
    if target.fold_key is None or nets:
        # Its own channels stay with it even if their names say otherwise.
        for c in db.query(LiveChannelCatalog).filter(LiveChannelCatalog.network_id == target.id).all():
            c.pinned = True
    _bump(db)
    return {"network": target.id, "key": f"n{target.id}"}


def set_channel_enabled(db: Session, channel_id: int, enabled: bool) -> None:
    """Tick / untick one channel (also a network's variant)."""
    ch = db.query(LiveChannel).filter(LiveChannel.id == channel_id).first()
    if ch is None:
        raise CatalogError("No such channel")
    ch.enabled = bool(enabled)
    ch.updated_at = datetime.utcnow()
    db.commit()

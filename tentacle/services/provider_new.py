"""New from your provider: the admin's review of Live TV groups and VOD
categories a provider added since the admin last looked.

A provider adds groups and categories all the time (nightly discovery,
services/discovery.py, and every group or category refresh). They arrive
switched off. Each one carries its review in plain columns:

  review_state      "new" (waiting for the admin), "dismissed" (the admin
                    doesn't want it; it never comes back as new, and stays
                    in the full list), "reviewed" (the admin looked at its
                    channels and added the ones they wanted). NULL = there
                    before reviews existed, or part of a provider's first
                    setup.
  first_seen_at     when Tentacle first saw it (UTC).
  reviewed_at       when the admin dismissed or reviewed it.
  looked_at         Live TV: the admin ticked it to look at its channels
                    (the stream checker tries those channels first).
  channels_read_at  Live TV: when its channels were last read from the
                    provider.

Live TV in two steps: tick the groups to look at (their channels are read
from the provider, switched off, so they can be shown folded, with the
checker's and the guide's marks), then pick channels: those join the pool
(LiveChannel.enabled) and the group is reviewed. Unpicked channels stay off.
VOD in one: a new category is added to the library (whitelisted) or
dismissed.
"""
import json
import logging
import re
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from models.database import (
    ActivityLog, LiveChannel, LiveChannelCatalog, LiveChannelGroup, LiveNetwork, Provider,
    ProviderCategory, Setting, get_setting,
)

logger = logging.getLogger(__name__)

NEW, DISMISSED, REVIEWED = "new", "dismissed", "reviewed"
_MIGRATED_KEY = "provider_new_migrated"
# A group's channels read from the provider are fresh for this long.
CHANNELS_FRESH_SECONDS = 20 * 3600

_load_lock = threading.Lock()
loading = {"running": False, "group": None, "done": 0, "total": 0}


def _now() -> datetime:
    """UTC, naive, like every other Tentacle timestamp."""
    return datetime.utcnow().replace(microsecond=0)


def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(dt) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp() if dt else 0.0


def review_of(row) -> Optional[str]:
    return row.review_state


# ── Arrival ─────────────────────────────────────────────────────────────────

def note_created(db: Session, rows: Iterable, first_setup: bool) -> None:
    """Rows (groups or categories) a refresh just created. A provider's first
    list is its setup, where the admin chooses anyway: not new."""
    ensure_migrated(db, commit=False)   # inside the refresh's transaction
    if first_setup:
        return
    now = _now()
    marked = False
    for row in rows:
        if review_of(row) is None:
            row.review_state, row.first_seen_at = NEW, now
            marked = True
    if marked:
        from services import live_updates
        live_updates.after_commit(db, "groups", admins=True)


# ── One-time start: what the old notice said was new ────────────────────────

def _names_from_activity(db: Session, event: str) -> dict:
    """name -> first date the activity feed named it new (the feed names
    the first three of each night's finds)."""
    out = {}
    rows = (db.query(ActivityLog.message, ActivityLog.created_at)
            .filter(ActivityLog.event == event).order_by(ActivityLog.id).all())
    for message, created in rows:
        _, _, names = (message or "").partition(" from provider: ")
        names = re.sub(r" and \d+ more$", "", names.strip())
        for n in names.split(", "):
            n = n.strip()
            if n and n not in out and created is not None:
                out[n] = created
    return out


def _mark_since(rows: list, named: set, dates: dict, fallback: Optional[str]) -> int:
    """`rows`: (row, name, switched on) of one provider. Every row from the
    first one the notice named on: ids only grow, so they all came after
    it. Rows switched on were chosen already. Returns how many became new."""
    rows = sorted(rows, key=lambda t: t[0].id)
    first = next((r.id for r, name, _ in rows if name in named), None)
    if first is None:
        return 0
    count, last_date = 0, None
    for r, name, on in rows:
        if r.id < first:
            continue
        last_date = dates.get(name) or last_date
        if on or review_of(r) is not None:
            continue
        r.review_state, r.first_seen_at = NEW, last_date or fallback
        count += 1
    return count


def ensure_migrated(db: Session, commit: bool = True) -> None:
    """Once: the groups and categories the old "Your providers have something
    new" notice counted become new here, with the date the activity feed
    first named them (or the one before them)."""
    if get_setting(db, _MIGRATED_KEY, ""):
        return
    try:
        notice = json.loads(get_setting(db, "new_content_notice", "") or "{}")
    except Exception:
        notice = {}
    try:
        fallback = datetime.fromisoformat(notice["detected_at"][:19])
    except (KeyError, TypeError, ValueError):
        fallback = None
    live = set(notice.get("live") or [])
    vod = set(notice.get("vod") or [])
    marked = 0
    if live:
        dates = _names_from_activity(db, "new_live_groups")
        by_provider = defaultdict(list)
        for g in db.query(LiveChannelGroup).all():
            by_provider[g.provider_id].append((g, g.name, bool(g.enabled)))
        for rows in by_provider.values():
            marked += _mark_since(rows, live, dates, fallback)
    if vod:
        dates = _names_from_activity(db, "new_categories")
        by_provider = defaultdict(list)
        for c in db.query(ProviderCategory).all():
            by_provider[(c.provider_id, c.type)].append((c, c.category_name, bool(c.whitelisted)))
        for rows in by_provider.values():
            marked += _mark_since(rows, vod, dates, fallback)
    row = db.query(Setting).filter(Setting.key == _MIGRATED_KEY).first()
    if row is None:
        db.add(Setting(key=_MIGRATED_KEY, value=_iso(_now())))
    if commit:
        db.commit()
    if marked:
        logger.info(f"[NewFromProvider] {marked} group(s)/categories from the old notice are new")


# ── Counts (the sidebar badge) ──────────────────────────────────────────────

def _live_groups(db: Session, review: Optional[set] = None) -> list:
    from routers.livetv import live_tv_providers
    pids = {p.id for p in live_tv_providers(db)}
    if not pids:
        return []
    q = db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id.in_(pids))
    if review is not None:
        q = q.filter(LiveChannelGroup.review_state.in_(list(review)))
    return q.all()


def _vod_categories(db: Session, review: Optional[set] = None) -> list:
    pids = [p.id for p in db.query(Provider).filter(Provider.active == True).all()]  # noqa: E712
    if not pids:
        return []
    q = db.query(ProviderCategory).filter(ProviderCategory.provider_id.in_(pids))
    if review is not None:
        q = q.filter(ProviderCategory.review_state.in_(list(review)))
    return q.all()


def counts(db: Session) -> dict:
    ensure_migrated(db)
    return {"live": len(_live_groups(db, {NEW})), "vod": len(_vod_categories(db, {NEW}))}


# ── Live TV: the groups ─────────────────────────────────────────────────────

def _group_channels(db: Session, groups: list) -> dict:
    """group id -> its LiveChannel rows (by provider and group name)."""
    by_key = {(g.provider_id, g.name): g.id for g in groups}
    out = defaultdict(list)
    if not groups:
        return out
    names = list({g.name for g in groups})
    for i in range(0, len(names), 500):
        for ch in db.query(LiveChannel).filter(LiveChannel.group_title.in_(names[i:i + 500])).all():
            gid = by_key.get((ch.provider_id, ch.group_title))
            if gid is not None:
                out[gid].append(ch)
    return out


def _units(db: Session, channels: list) -> list:
    """Channels as the pool would show them: one unit per network (the same
    channel in several places or qualities), else per channel. Separators
    are left out. Each unit: key, network (LiveNetwork or None), members."""
    from routers.livetv import _is_separator
    ids = [c.id for c in channels]
    net_of = {}
    for i in range(0, len(ids), 500):
        for cid, nid in (db.query(LiveChannelCatalog.channel_id, LiveChannelCatalog.network_id)
                         .filter(LiveChannelCatalog.channel_id.in_(ids[i:i + 500])).all()):
            net_of[cid] = nid
    nets = {}
    nids = {n for n in net_of.values() if n is not None}
    if nids:
        nets = {n.id: n for n in db.query(LiveNetwork).filter(LiveNetwork.id.in_(list(nids))).all()}
    units, by_key = [], {}
    for ch in sorted(channels, key=lambda c: c.id):
        if _is_separator(ch.name):
            continue
        nid = net_of.get(ch.id)
        key = f"n{nid}" if nid in nets else f"c{ch.id}"
        u = by_key.get(key)
        if u is None:
            u = by_key[key] = {"key": key, "network": nets.get(nid), "members": []}
            units.append(u)
        u["members"].append(ch)
    return units


def _state(ch) -> str:
    from services.livetv_checker import BROKEN, UNCHECKED, WORKS
    return ch.check_state if ch.check_state in (WORKS, BROKEN) else UNCHECKED


_STATE_RANK = {"works": 0, "unchecked": 1, "broken": 2}


def _best(members: list, with_guide: set):
    """The channel that stands for a unit: works, has a guide, then the
    first listed."""
    return min(members, key=lambda c: (_STATE_RANK[_state(c)], 0 if c.guide_epg_id in with_guide else 1, c.id))


def _unit_name(u: dict, best, ev) -> str:
    """The name a unit goes by: its network, the event on now, else the
    channel's own name made readable."""
    from services import livetv_names
    if u["network"] is not None:
        return u["network"].name
    if isinstance(ev, dict):
        return ev["title"]
    return (best.custom_name or "").strip() or livetv_names.clean_name(best.name)


SAMPLES = 5
_HEAD_RE = re.compile(r"^\s*[|\[]?[A-Za-z0-9&+]{1,6}\s*[:|\]]\s*")
_SLOT_RE = re.compile(r"\b(?:EVENTO?S?|PPV|SLOT)\s*\d+\s*$", re.I)


def _samples(units: list, with_guide: set) -> list:
    """A few channels to judge a group by without opening it: working ones
    with a logo first. Event slots only when an event is on (a slot's name
    says nothing)."""
    from services.livetv_catalog import _event_for
    picks = []
    for i, u in enumerate(units):
        best = _best(u["members"], with_guide)
        ev = _event_for(best.name, best.group_title)
        if ev is not None and not isinstance(ev, dict):
            continue
        guide = any(c.guide_epg_id in with_guide for c in u["members"])
        rank = (_STATE_RANK[_state(best)], 0 if u["network"] is not None else 1, 0 if guide else 1,
                0 if best.logo_url else 1, i)
        picks.append((rank, {"name": _unit_name(u, best, ev), "logo_url": best.logo_url or None,
                             "event": isinstance(ev, dict)}))
    picks.sort(key=lambda t: t[0])
    _drop_shared_head([p for _, p in picks])
    out, seen = [], set()
    for _, p in picks:
        if len(p["name"]) < 3 or _SLOT_RE.search(p["name"]) or p["name"].lower() in seen:
            continue
        seen.add(p["name"].lower())
        out.append(p)
        if len(out) == SAMPLES:
            break
    return out


def _stats(units: list, with_guide: set) -> dict:
    from services.livetv_catalog import _event_for
    works = guide = events = no_event = checked = 0
    for u in units:
        best = _best(u["members"], with_guide)
        st = _state(best)
        works += st == "works"
        checked += st != "unchecked"
        guide += any(c.guide_epg_id in with_guide for c in u["members"])
        ev = _event_for(best.name, best.group_title)
        if ev is not None:
            events += 1
            no_event += ev == "no_event"
    return {"channels": len(units), "works": works, "checked": checked, "with_guide": guide,
            "events": events, "no_event": no_event}


def _with_guide(db: Session) -> set:
    from services.livetv_catalog import guide_facts
    try:
        return guide_facts(db)["with_guide"]
    except Exception:
        return set()


def _group_json(g, provider_name: str, chans: list, units: Optional[list], with_guide: set,
                home: Optional[dict] = None) -> dict:
    out = {"id": g.id, "name": g.name, "provider_id": g.provider_id, "provider": provider_name,
           "review": g.review_state, "first_seen": _iso(g.first_seen_at), "reviewed_at": _iso(g.reviewed_at),
           "provider_count": g.channel_count or 0, "loaded": bool(g.channels_read_at or chans),
           "enabled": bool(g.enabled)}
    if home is not None:
        d = home["book"].read(g.name)
        out.update(title=d["title"], tags=d["tags"], place=d["place"], lang=d["lang"],
                   event_group=d["event"], for_you=for_you(home, d))
    if units is not None and out["loaded"]:
        out.update(_stats(units, with_guide))
        out["samples"] = _samples(units, with_guide)
        if out["events"] and out["events"] * 2 >= out["channels"]:
            out["event_group"] = True   # mostly event slots, whatever the name says
    return out


# ── Which new groups are probably for this household ───────────────────────

POOL_SHARE = 0.05   # a place with this share of the household's channels is theirs


def _book(db: Session):
    from services.livetv_groups import CodeBook
    names = [n for (n,) in db.query(LiveChannelGroup.name).all()]
    return CodeBook(names)


def household(db: Session, country: Optional[str] = None, lang: Optional[str] = None) -> dict:
    """The countries and languages this household watches: the places with
    a real share of the channels it has (by their groups), the household of
    the catalog (livetv_region, else the server's time zone, and
    livetv_language) and what the admin's browser says (`country`, `lang`).
    `book` reads group names."""
    from sqlalchemy import func
    from services.livetv_catalog import _household
    from services.livetv_groups import COUNTRIES, LANGUAGE_OF
    book = _book(db)
    rows = (db.query(LiveChannel.group_title, func.count(LiveChannel.id))
            .filter(LiveChannel.enabled == True).group_by(LiveChannel.group_title).all())  # noqa: E712
    total = sum(n for _, n in rows)
    share = defaultdict(int)
    for title, n in rows:
        place = book.read(title or "")["place"]
        if place:
            share[place["iso"] or place["code"]] += n
    countries = [k for k, n in sorted(share.items(), key=lambda kv: -kv[1]) if n >= POOL_SHARE * total]
    try:
        region, home_lang = _household(db)
    except Exception:
        region, home_lang = None, None
    for c in (region, (country or "").upper() or None):
        if c == "UK":
            c = "GB"
        if c in COUNTRIES and c not in countries:
            countries.append(c)
    langs = []
    for lg in [home_lang] + [LANGUAGE_OF.get(c) for c in countries] + [(lang or "").lower()[:2] or None]:
        if lg and lg not in langs:
            langs.append(lg)
    return {"countries": countries, "languages": langs, "book": book}


def for_you(home: dict, d: dict) -> Optional[str]:
    """"country" (a place the household watches), "language" (in a language
    it speaks) or None."""
    place = d.get("place")
    if place and (place["iso"] or place["code"]) in home["countries"]:
        return "country"
    if d.get("lang") and d["lang"] in home["languages"]:
        return "language"
    return None


def live_overview(db: Session, dismissed: bool = False, country: Optional[str] = None,
                  lang: Optional[str] = None) -> dict:
    """The new groups (and, asked for, the dismissed ones), newest first,
    each read (country, clean title, tags), with whether it is probably for
    this household, its channel counts after folding, the checker's and
    guide's marks and a few sample channels once its channels are read."""
    ensure_migrated(db)
    from services import livetv_catalog
    want = {NEW, DISMISSED} if dismissed else {NEW}
    groups = _live_groups(db, want)
    try:
        livetv_catalog.ensure_fresh(db)
    except Exception as e:
        logger.warning(f"[NewFromProvider] Catalog not refreshed: {e}")
    names = {p.id: p.name for p in db.query(Provider).all()}
    chans = _group_channels(db, groups)
    with_guide = _with_guide(db)
    home = household(db, country, lang)
    out = []
    for g in sorted(groups, key=lambda g: (g.review_state != NEW, -_ts(g.first_seen_at), g.name.lower())):
        cs = chans.get(g.id, [])
        out.append(_group_json(g, names.get(g.provider_id, ""), cs, _units(db, cs) if cs else [], with_guide, home))
    return {"groups": out, "new": sum(1 for x in out if x["review"] == NEW),
            "dismissed": len(_live_groups(db, {DISMISSED})), "loading": dict(loading),
            "household": {"countries": home["countries"], "languages": home["languages"]}}


def _groups_by_ids(db: Session, group_ids: list) -> list:
    ids = [int(i) for i in group_ids]
    if not ids:
        return []
    rows = db.query(LiveChannelGroup).filter(LiveChannelGroup.id.in_(ids)).all()
    order = {gid: i for i, gid in enumerate(ids)}
    return sorted(rows, key=lambda g: order.get(g.id, 0))


def dismiss(db: Session, group_ids: list) -> int:
    now = _now()
    rows = _groups_by_ids(db, group_ids)
    for g in rows:
        g.review_state, g.reviewed_at, g.looked_at = DISMISSED, now, None
    db.commit()
    return len(rows)


def restore(db: Session, group_ids: list) -> int:
    """A dismissed group back on the new list."""
    rows = [g for g in _groups_by_ids(db, group_ids) if review_of(g) == DISMISSED]
    for g in rows:
        g.review_state, g.reviewed_at = NEW, None
    db.commit()
    return len(rows)


# ── Live TV: reading a group's channels ─────────────────────────────────────

def _fresh(g) -> bool:
    at = g.channels_read_at
    return at is not None and (_now() - at).total_seconds() < CHANNELS_FRESH_SECONDS


def read_channels(db: Session, groups: list, force: bool = False) -> int:
    """Read these groups' channels from the provider (Xtream; an M3U
    playlist already brought every channel), stored switched off. Returns
    how many groups were read."""
    from routers.livetv import _live_extension, _upsert_channels
    from services.xtream_client import XtreamClient
    by_provider = defaultdict(list)
    for g in groups:
        if g.category_id and (force or not _fresh(g)):
            by_provider[g.provider_id].append(g)
    read = 0
    for pid, gs in by_provider.items():
        p = db.query(Provider).filter(Provider.id == pid).first()
        if p is None or (p.provider_type or "xtream") != "xtream":
            continue
        client = XtreamClient(server=p.server_url, username=p.username, password=p.password,
                              user_agent=p.user_agent or "TiviMate/4.7.0 (Linux; Android 12)")
        try:
            ext = _live_extension(db, pid)
            for g in gs:
                loading["group"] = g.name
                try:
                    streams = client.get_live_streams(category_id=g.category_id)
                except Exception as e:
                    from services.log_redaction import redact
                    logger.warning(f"[NewFromProvider] Channels of '{g.name}' not read: {redact(str(e))}")
                    continue
                _upsert_channels(pid, streams or [], {g.category_id: g.name}, client, db, extension=ext)
                g.channels_read_at = _now()
                db.commit()
                read += 1
                loading["done"] += 1
        finally:
            client.close()
    return read


def read_new_in_background() -> bool:
    """Read the channels of every new group not read lately, in a thread
    (the page shows the counts as they come). False if one is running."""
    if not _load_lock.acquire(blocking=False):
        return False

    def run():
        from models.database import SessionLocal
        db = SessionLocal()
        try:
            todo = [g for g in _live_groups(db, {NEW}) if not _fresh(g)]
            loading.update(running=True, done=0, total=len(todo), group=None)
            if todo:
                read_channels(db, todo)
                from services import livetv_catalog
                livetv_catalog.ensure_fresh(db)
        except Exception as e:
            logger.warning(f"[NewFromProvider] Reading new groups' channels failed: {e}")
        finally:
            loading.update(running=False, group=None)
            db.close()
            _load_lock.release()

    threading.Thread(target=run, name="new-groups-read", daemon=True).start()
    return True


def needs_reading(db: Session) -> bool:
    return any(not _fresh(g) and g.category_id for g in _live_groups(db, {NEW}))


# ── Live TV: step 2, the channels of the ticked groups ──────────────────────

def channels_of(db: Session, group_ids: list) -> dict:
    """The channels of these groups as the pool would show them, each with
    its marks and whether it is in the household's channels already (the
    same network from a group switched on)."""
    from services import livetv_catalog
    from services.livetv_catalog import _event_for, _event_json, channel_types
    groups = _groups_by_ids(db, group_ids)
    if not groups:
        return {"groups": [], "channels": []}
    now = _now()
    for g in groups:
        g.looked_at = now
    db.commit()
    from services import livetv_checker
    livetv_checker.forget_new_groups()
    unread = [g for g in groups if g.channels_read_at is None]
    if unread:
        read_channels(db, unread)
    livetv_catalog.ensure_fresh(db)
    names = {p.id: p.name for p in db.query(Provider).all()}
    chans = _group_channels(db, groups)
    with_guide = _with_guide(db)
    gtypes = livetv_catalog.guide_facts(db).get("types", {})
    nets_in_pool = _networks_in_pool(db)
    home = household(db)
    out_groups, out, seen = [], [], set()
    for g in groups:
        cs = chans.get(g.id, [])
        units = _units(db, cs)
        out_groups.append(_group_json(g, names.get(g.provider_id, ""), cs, units, with_guide, home))
        first = len(out)
        for u in units:
            if u["key"] in seen:
                continue   # the same network in two ticked groups: shown once
            seen.add(u["key"])
            best = _best(u["members"], with_guide)
            n = u["network"]
            ev = _event_for(best.name, best.group_title)
            gid = best.guide_epg_id
            name = _unit_name(u, best, ev)
            out.append({
                "key": u["key"], "group_id": g.id, "name": name, "provider_name": best.name,
                "logo_url": best.logo_url, "channel_ids": [c.id for c in u["members"]],
                "feeds": len(u["members"]), "check": {"state": _state(best), "at": _iso(best.check_at)},
                "has_guide": any(c.guide_epg_id in with_guide for c in u["members"]),
                "event": _event_json(ev),
                "types": channel_types(best.name, best.group_title, event=ev is not None,
                                       guide_types=gtypes.get(gid)),
                "in_pool": bool(n is not None and n.id in nets_in_pool) or any(c.enabled for c in u["members"]),
            })
        _drop_shared_head(out[first:])
    return {"groups": out_groups, "channels": out}


def _drop_shared_head(items: list) -> None:
    """"T2: Npo 1", "T2: SBS 6": a tag in front of (nearly) every channel of
    a group says nothing; the names go without it."""
    heads = defaultdict(int)
    for it in items:
        m = _HEAD_RE.match(it["name"])
        if m:
            heads[m.group(0).strip().upper()] += 1
    if not heads:
        return
    head, n = max(heads.items(), key=lambda kv: kv[1])
    if n < 2 or n < 0.8 * len(items):
        return
    for it in items:
        m = _HEAD_RE.match(it["name"])
        if m and m.group(0).strip().upper() == head and it["name"][m.end():].strip():
            it["name"] = it["name"][m.end():]


def _networks_in_pool(db: Session) -> set:
    """Networks with a channel switched on (in the household's channels)."""
    rows = (db.query(LiveChannelCatalog.network_id)
            .join(LiveChannel, LiveChannel.id == LiveChannelCatalog.channel_id)
            .filter(LiveChannel.enabled == True, LiveChannelCatalog.network_id.isnot(None))  # noqa: E712
            .distinct().all())
    return {r[0] for r in rows}


def add_to_pool(db: Session, group_ids: list, channel_ids: list) -> dict:
    """Switch on the picked channels (a network's feeds in these groups
    together) and mark the groups reviewed. A group with a channel picked
    is switched on, so the nightly channel sync keeps its channels current;
    its other channels stay off."""
    groups = _groups_by_ids(db, group_ids)
    by_key = {(g.provider_id, g.name): g for g in groups}
    picked = {int(c) for c in channel_ids}
    added = 0
    used = set()
    for i in range(0, len(picked), 500):
        chunk = list(picked)[i:i + 500]
        for ch in db.query(LiveChannel).filter(LiveChannel.id.in_(chunk)).all():
            g = by_key.get((ch.provider_id, ch.group_title))
            if g is None:
                continue   # only channels of the groups being reviewed
            used.add(g.id)
            if not ch.enabled:
                ch.enabled = True
                ch.updated_at = datetime.utcnow()
                added += 1
    now = _now()
    for g in groups:
        if g.id in used:
            g.enabled = True
        g.review_state, g.reviewed_at, g.looked_at = REVIEWED, now, None
    db.commit()
    if added:
        from routers.livetv import _pool_changed
        _pool_changed(f"{added} new channel(s) added from the provider's new groups")
    return {"added": added, "groups": len(groups)}


def unchecked_new_channel_ids(db: Session) -> list:
    """Channels of new groups without a verdict, the groups the admin ticked
    first: the stream checker tries them before checking the pool again."""
    groups = _live_groups(db, {NEW})
    if not groups:
        return []
    chans = _group_channels(db, groups)
    first, rest = [], []
    from routers.livetv import _is_separator
    for g in sorted(groups, key=lambda g: -_ts(g.looked_at)):
        target = first if g.looked_at else rest
        target.extend(c.id for c in chans.get(g.id, [])
                      if c.check_state is None and not c.enabled and not _is_separator(c.name))
    return first + rest


# ── VOD categories ──────────────────────────────────────────────────────────

def vod_overview(db: Session, dismissed: bool = False) -> dict:
    ensure_migrated(db)
    want = {NEW, DISMISSED} if dismissed else {NEW}
    names = {p.id: p.name for p in db.query(Provider).all()}
    out = []
    rows = sorted(_vod_categories(db, want),
                  key=lambda c: (c.review_state != NEW, -_ts(c.first_seen_at), (c.category_name or "").lower()))
    for c in rows:
        out.append({"id": c.id, "name": c.category_name, "type": c.type, "provider_id": c.provider_id,
                    "provider": names.get(c.provider_id, ""), "titles": c.title_count or 0,
                    "review": c.review_state, "first_seen": _iso(c.first_seen_at)})
    return {"categories": out, "new": sum(1 for x in out if x["review"] == NEW),
            "dismissed": len(_vod_categories(db, {DISMISSED}))}


def vod_decide(db: Session, category_ids: list, add: bool) -> int:
    """Add these new categories to the library (they sync tonight) or
    dismiss them."""
    ids = [int(i) for i in category_ids]
    if not ids:
        return 0
    rows = db.query(ProviderCategory).filter(ProviderCategory.id.in_(ids)).all()
    now = _now()
    for c in rows:
        if add:
            c.whitelisted = True
        c.review_state, c.reviewed_at = (REVIEWED if add else DISMISSED), now
    db.commit()
    return len(rows)


def vod_restore(db: Session, category_ids: list) -> int:
    ids = [int(i) for i in category_ids]
    rows = [c for c in db.query(ProviderCategory).filter(ProviderCategory.id.in_(ids)).all()
            if review_of(c) == DISMISSED] if ids else []
    for c in rows:
        c.review_state, c.reviewed_at = NEW, None
    db.commit()
    return len(rows)

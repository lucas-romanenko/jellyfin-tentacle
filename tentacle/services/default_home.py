"""The default home: one layout the admin picks for everyone who hasn't made
their own (Decisions 2026-10-21: never Jellyfin's stock home).

The default is stored once, in the `default_home` setting, with playlist rows
named by their ready-made playlist key ("source:Netflix:movies",
"builtin:recently_added_movies", "youtube:3"), because every person has their
own Jellyfin playlists with their own ids. Until an admin saves one, Tentacle's
own suggestion is used (`suggested_default`), so a fresh install looks right
with no setup.

A person's home config carries `"follows_default": true` while it is the
default. `apply_default` writes the default into their file with their own
playlist ids (switching on the ready-made playlists it needs), so every client
reads it exactly like a home they made themselves. Changing anything on their
own Home Screen page makes it theirs (the flag goes); "Reset to default" puts
it back. Kids get the same rows: the plugin only returns items Jellyfin lets
that person open, so their rows show only what their age limit allows.
"""
import json
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import TentacleUser, get_setting, set_setting

logger = logging.getLogger(__name__)

SETTING_KEY = "default_home"
MIGRATED_KEY = "default_home_migrated"
FOLLOWS = "follows_default"

# What the suggested default puts after Continue Watching / Next Up and the
# recently-added rows: this many of the biggest IPTV source playlists.
SUGGESTED_SOURCE_ROWS = 4

# The rows Tentacle used to seed a new person's home with (their Jellyfin home
# sections: My Media, Continue Watching, Next Up, Recently Added Media). A home
# that is still exactly that was never customised.
LEGACY_SEED_SECTIONS = ["smalllibrarytiles", "resumevideo", "nextup", "latestmedia"]

# Ready-made playlists every person can have, so a default may use them.
# A person's own lists (list:<id>) and custom playlists are theirs alone.
_SHARED_BUILTINS = {"builtin:recently_added_movies", "builtin:recently_added_tv",
                    "builtin:downloaded_movies", "builtin:downloaded_tv", "builtin:my_downloads"}

DEFAULT_HERO_OFF = {"enabled": False, "playlist_id": "", "display_name": "", "sort_by": "random",
                    "sort_order": "Descending", "require_logo": True, "require_trailer": False,
                    "trailer_audio": False, "item_count": 10}


def is_shareable_key(key: str) -> bool:
    return bool(key) and (key.startswith("source:") or key.startswith("youtube:") or key in _SHARED_BUILTINS)


def follows_default(config: dict) -> bool:
    return bool(config) and config.get(FOLLOWS) is True


def _builtin_row(section_id: str) -> dict:
    from routers.smartlists import BUILTIN_MAP
    return {"type": "builtin", "section_id": section_id,
            "display_name": BUILTIN_MAP[section_id]["display_name"]}


def _playlist_row(entry: dict) -> dict:
    return {"type": "playlist", "playlist_id": entry["key"], "display_name": entry["name"],
            "shape": "wide" if entry.get("category") == "youtube" else "poster"}


def shareable_playlists(db: Session) -> list:
    """The ready-made playlists a default can show, as [{key, name, category,
    item_count}]. "My Downloads" stands for each person's own."""
    from routers.smartlists import _compute_auto_playlists
    out = []
    for a in _compute_auto_playlists(db, user_id=None):
        if a["key"] == "builtin:my_downloads" or not is_shareable_key(a["key"]):
            continue
        out.append({"key": a["key"], "name": a["name"], "category": a["category"],
                    "item_count": a.get("item_count") or 0})
    out.append({"key": "builtin:my_downloads", "name": "My Downloads", "category": "builtin",
                "item_count": None})
    return out


def suggested_default(db: Session) -> dict:
    """Tentacle's own default (Lucas, 2026-10-21, Sections.md): Continue
    Watching with Next Up merged in, On Now (favourite channels), recently
    added movies and shows, then the biggest IPTV sources; the spotlight on
    recently added movies. Empty playlists are left out (a fresh install's
    library rows fall back to Jellyfin's Recently Added)."""
    from models.database import Movie, Series, get_recently_added_days
    entries = {e["key"]: e for e in shareable_playlists(db)}
    cutoff = datetime.utcnow() - timedelta(days=get_recently_added_days(db))
    recent_movies = db.query(func.count(Movie.id)).filter(Movie.date_added >= cutoff).scalar() or 0
    recent_tv = db.query(func.count(Series.id)).filter(Series.date_added >= cutoff).scalar() or 0

    rows = [_builtin_row("resumevideo"), _builtin_row("onnow")]
    if recent_movies:
        rows.append(_playlist_row(entries["builtin:recently_added_movies"]))
    if recent_tv:
        rows.append(_playlist_row(entries["builtin:recently_added_tv"]))
    if not recent_movies and not recent_tv:
        rows.append(_builtin_row("latestmedia"))
    sources = sorted((e for e in entries.values() if e["category"] == "source" and e["item_count"]),
                     key=lambda e: (-e["item_count"], e["name"].lower()))
    rows += [_playlist_row(e) for e in sources[:SUGGESTED_SOURCE_ROWS]]
    for i, r in enumerate(rows, 1):
        r["order"] = i

    hero_src = entries["builtin:recently_added_movies"] if recent_movies else next(
        (e for e in sources if e["key"].endswith(":movies")), None)
    hero = dict(DEFAULT_HERO_OFF)
    if hero_src:
        hero.update(enabled=True, playlist_id=hero_src["key"], display_name=hero_src["name"])
    return {"hero": hero, "rows": rows, "merge_continue_watching": True}


def saved_default(db: Session) -> dict:
    try:
        cfg = json.loads(get_setting(db, SETTING_KEY, "") or "null")
    except ValueError:
        cfg = None
    return cfg if isinstance(cfg, dict) and isinstance(cfg.get("rows"), list) else {}


def get_default(db: Session) -> dict:
    """The default home (the admin's, else Tentacle's suggestion)."""
    return saved_default(db) or suggested_default(db)


def save_default(db: Session, config: dict) -> dict:
    cfg = {"hero": config.get("hero") or dict(DEFAULT_HERO_OFF),
           "rows": [r for r in (config.get("rows") or [])
                    if r.get("type") == "builtin" or is_shareable_key(r.get("playlist_id", ""))],
           "merge_continue_watching": bool(config.get("merge_continue_watching"))}
    for i, r in enumerate(cfg["rows"], 1):
        r["order"] = i
        r.pop("unresolved_since", None)
    if cfg["hero"].get("playlist_id") and not is_shareable_key(cfg["hero"]["playlist_id"]):
        cfg["hero"] = dict(DEFAULT_HERO_OFF)
    set_setting(db, SETTING_KEY, json.dumps(cfg))
    return cfg


def clear_default(db: Session) -> None:
    """Back to Tentacle's suggestion."""
    set_setting(db, SETTING_KEY, "")


def default_from_home(db: Session, user: TentacleUser, home: dict) -> dict:
    """A person's own home as a default: their playlist rows become the
    ready-made playlists of the same name; their own lists and custom
    playlists are left out (nobody else has them)."""
    from routers.smartlists import _compute_auto_playlists
    from services.smartlists import _get_smartlists_with_playlist_ids
    key_by_name = {}
    for a in _compute_auto_playlists(db, user_id=user.id):
        if is_shareable_key(a["key"]):
            key_by_name[a["name"]] = a["key"]
    name_by_id = {s["playlist_id"]: s["name"] for s in _get_smartlists_with_playlist_ids(db, user_id=user.id)}

    def key_of(pid, name):
        return key_by_name.get(name_by_id.get(pid, name))

    rows = []
    for r in sorted(home.get("rows") or [], key=lambda r: r.get("order", 0)):
        if r.get("type") == "builtin":
            rows.append({k: v for k, v in r.items() if k != "unresolved_since"})
            continue
        key = key_of(r.get("playlist_id"), r.get("display_name"))
        if key:
            name = "My Downloads" if key == "builtin:my_downloads" else r.get("display_name")
            rows.append({**{k: v for k, v in r.items() if k not in ("unresolved_since", "sort_by", "sort_order")},
                         "playlist_id": key, "display_name": name})
    hero = dict(home.get("hero") or DEFAULT_HERO_OFF)
    if hero.get("playlist_id"):
        key = key_of(hero["playlist_id"], hero.get("display_name"))
        hero = {**hero, "playlist_id": key, "display_name": hero.get("display_name")} if key \
            else dict(DEFAULT_HERO_OFF)
    return {"hero": hero, "rows": rows, "merge_continue_watching": bool(home.get("merge_continue_watching"))}


# ── Putting the default into a person's home ─────────────────────────────────

def _needed_keys(default: dict) -> set:
    keys = {r["playlist_id"] for r in default.get("rows") or [] if r.get("type") != "builtin" and r.get("playlist_id")}
    hero = default.get("hero") or {}
    if hero.get("enabled") and hero.get("playlist_id"):
        keys.add(hero["playlist_id"])
    return {k for k in keys if is_shareable_key(k)}


def _switch_on(db: Session, user: TentacleUser, keys: set, auto: dict) -> bool:
    """Switch on the ready-made playlists the default needs. Never switches one
    off. Returns whether anything changed."""
    from models.database import AutoPlaylistToggle
    changed = False
    for key in keys:
        a = auto.get(key)
        if a is None or a["category"] == "youtube" or a.get("enabled"):
            continue
        t = db.query(AutoPlaylistToggle).filter(AutoPlaylistToggle.user_id == user.id,
                                                AutoPlaylistToggle.key == key).first()
        if t:
            t.enabled = True
        else:
            db.add(AutoPlaylistToggle(user_id=user.id, key=key, enabled=True))
        changed = True
    if changed:
        db.commit()
    return changed


def apply_default(db: Session, user: TentacleUser, build: bool = True, default: dict = None) -> dict:
    """Write the default into `user`'s home config (marked as following it).

    `build` switches on and builds the ready-made playlists it needs (Jellyfin
    work, seconds); without, rows whose playlist the person doesn't have yet
    are left out for now. Toolbar and card previews stay the person's. Returns
    the config written ({} when the person couldn't be read)."""
    from routers.smartlists import DEFAULT_TOOLBAR, _compute_auto_playlists
    from services.smartlists import (
        _get_smartlists_with_playlist_ids, _home_config_exists, _notify_jellyfin_plugin,
        _user_home_config_path, bump_playlist_version, get_home_config, home_config_lock, refresh_smartlist_playlists,
        sync_smartlists, take_over_jellyfin_home, write_home_config, write_home_json,
    )
    default = default or get_default(db)
    auto = {a["key"]: a for a in _compute_auto_playlists(db, user_id=user.id)}
    needed = _needed_keys(default)
    if build:
        mine = {s["name"] for s in _get_smartlists_with_playlist_ids(db, user_id=user.id)}
        missing = any(k in auto and auto[k]["name"] not in mine for k in needed)
        if _switch_on(db, user, needed, auto) or missing:
            sync_smartlists(db, user_id=user.id)
            refresh_smartlist_playlists(db, user_id=user.id)
            auto = {a["key"]: a for a in _compute_auto_playlists(db, user_id=user.id)}

    by_name = {}
    for s in _get_smartlists_with_playlist_ids(db, user_id=user.id):
        by_name.setdefault(s["name"], []).append(s["playlist_id"])

    def resolve(key):
        a = auto.get(key)
        ids = by_name.get(a["name"], []) if a else []
        return (ids[0], a["name"]) if len(ids) == 1 else (None, None)

    rows = []
    for r in sorted(default.get("rows") or [], key=lambda r: r.get("order", 0)):
        if r.get("type") == "builtin":
            rows.append({k: v for k, v in r.items() if k in ("type", "section_id", "display_name")})
            continue
        pid, name = resolve(r.get("playlist_id"))
        if pid:
            row = {"type": "playlist", "playlist_id": pid, "display_name": name,
                   "shape": r.get("shape") or "poster"}
            if r.get("max_items"):
                row["max_items"] = r["max_items"]
            rows.append(row)
    for i, r in enumerate(rows, 1):
        r["order"] = i

    dh = default.get("hero") or {}
    hero = dict(DEFAULT_HERO_OFF)
    if dh.get("enabled") and dh.get("playlist_id"):
        pid, name = resolve(dh["playlist_id"])
        if pid:
            hero = {**DEFAULT_HERO_OFF, **{k: v for k, v in dh.items() if k in DEFAULT_HERO_OFF},
                    "enabled": True, "playlist_id": pid, "display_name": name}

    with home_config_lock:
        existing = get_home_config(db, user_id=user.id) or {}
        if _home_config_exists(db, user.id) and not follows_default(existing):
            return existing   # their own (made meanwhile), or a file that can't be read
        config = {"hero": hero, "rows": rows,
                  "toolbar": existing.get("toolbar") or [dict(b) for b in DEFAULT_TOOLBAR],
                  "merge_continue_watching": bool(default.get("merge_continue_watching")),
                  FOLLOWS: True}
        for k in ("card_previews", "jellyfin_sections_snapshot"):
            if existing.get(k):
                config[k] = existing[k]
        changed = {k: v for k, v in existing.items() if k != "jellyfin_sections_snapshot"} != \
            {k: v for k, v in config.items() if k != "jellyfin_sections_snapshot"}
        write_home_json(_user_home_config_path(db, user.id), config)
    # Sort info, row limits and Jellyfin's own sections, as for any home
    # (it skips a person with no playlists at all: their built-in rows still
    # replace Jellyfin's own sections)
    written = write_home_config(db, user_id=user.id)
    if not written:
        written = config
        take_over_jellyfin_home(db, user.id, config)
    if changed:
        bump_playlist_version()
        _notify_jellyfin_plugin(db, user)
        logger.info(f"[Default home] {user.display_name}: {len(rows)} rows"
                    f"{', spotlight ' + hero['display_name'] if hero.get('enabled') else ''}")
    return written


def followers(db: Session) -> list:
    """People whose home is the default (or who have none yet)."""
    from services.smartlists import _home_config_exists, get_home_config
    out = []
    for u in db.query(TentacleUser).order_by(TentacleUser.id).all():
        if not _home_config_exists(db, u.id) or follows_default(get_home_config(db, user_id=u.id)):
            out.append(u)
    return out


_apply_lock = threading.Lock()
_apply_state = {"running": False, "again": False}


def apply_to_followers_soon(reason: str = "") -> None:
    """Rewrite every follower's home from the default, in the background.
    Saves made while it runs (an admin dragging rows) make it run once more
    at the end, never twice at once."""
    with _apply_lock:
        if _apply_state["running"]:
            _apply_state["again"] = True
            return
        _apply_state["running"] = True

    def _run():
        from models.database import SessionLocal
        while True:
            db = SessionLocal()
            try:
                default = get_default(db)
                people = followers(db)
                for u in people:
                    try:
                        apply_default(db, u, build=True, default=default)
                    except Exception as e:
                        db.rollback()
                        logger.warning(f"[Default home] Could not update {u.display_name}'s home: {e}")
                logger.info(f"[Default home] Applied to {len(people)} people{' (' + reason + ')' if reason else ''}")
            except Exception as e:
                logger.warning(f"[Default home] Apply failed: {e}")
            finally:
                db.close()
            with _apply_lock:
                if not _apply_state["again"]:
                    _apply_state["running"] = False
                    return
                _apply_state["again"] = False

    threading.Thread(target=_run, daemon=True, name="default-home-apply").start()


def apply_soon(user_id: int) -> None:
    """Build one person's default home in the background (their playlists)."""
    def _run():
        from models.database import SessionLocal
        db = SessionLocal()
        try:
            u = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
            if u is not None:
                apply_default(db, u, build=True)
        except Exception as e:
            logger.warning(f"[Default home] Could not build the default home for user {user_id}: {e}")
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name=f"default-home-{user_id}").start()


# ── Existing homes ───────────────────────────────────────────────────────────

def is_untouched_seed(config: dict) -> bool:
    """A home that is still the starter Tentacle used to give everyone (their
    Jellyfin home sections as rows, no spotlight, no playlist rows), never
    changed since: it gets the default. Anything else is someone's own."""
    if not config or FOLLOWS in config:
        return False
    if (config.get("hero") or {}).get("enabled"):
        return False
    rows = sorted(config.get("rows") or [], key=lambda r: r.get("order", 0))
    if not rows or any(r.get("type") != "builtin" for r in rows):
        return False
    ids = [r.get("section_id") for r in rows]
    if ids == LEGACY_SEED_SECTIONS:
        return True
    snap = config.get("jellyfin_sections_snapshot") or {}
    if snap:
        from routers.smartlists import BUILTIN_MAP, JELLYFIN_SECTION_TO_BUILTIN
        seeded = []
        for i in range(10):
            sid = JELLYFIN_SECTION_TO_BUILTIN.get(snap.get(f"homesection{i}", ""), snap.get(f"homesection{i}", ""))
            if sid and sid != "none" and sid in BUILTIN_MAP and sid not in seeded:
                seeded.append(sid)
        return ids == seeded
    return False


def migrate_existing_homes(db: Session) -> int:
    """Once per install: every home that is still the old starter follows the
    default from now on. Someone's own layout is never touched. Returns how
    many were switched."""
    from services.smartlists import _user_home_config_path, get_home_config, home_config_lock, write_home_json
    if get_setting(db, MIGRATED_KEY, "") == "1":
        return 0
    n = 0
    with home_config_lock:
        for u in db.query(TentacleUser).all():
            cfg = get_home_config(db, user_id=u.id)
            if is_untouched_seed(cfg):
                write_home_json(_user_home_config_path(db, u.id), {**cfg, FOLLOWS: True})
                logger.info(f"[Default home] {u.display_name} had the old starter home; now follows the default")
                n += 1
    set_setting(db, MIGRATED_KEY, "1")
    return n

"""Tentacle's own home rows that aren't playlists (Lucas, 2026-10-26: "no
Jellyfin built-in rows at all").

A home row is a playlist row or one of these. They keep the existing config
shape (`"type": "builtin"`, `section_id`), but Tentacle decides what each one
shows and how it looks, the same on the web and the TV app:

- Continue Watching (`resumevideo`): what you stopped part-way, plus the next
  episodes of shows you're watching when `merge_continue_watching` is on (the
  "Include next episodes" switch on the row). Poster or wide, default wide.
- On Now (`onnow`): what's on now on your favourite channels. Its own
  squarer card, no shape choice.
- Recordings (`recordings`): the household's latest Live TV recordings, in
  the picker only. Poster or wide, default poster.

Every other Jellyfin home section (Next Up on its own, My Media, the old Live
TV row, Recently Added Media, Active Recordings, Continue Listening, Continue
Reading) is gone: `normalize_rows` takes them out of every layout once.
"""
import logging

from sqlalchemy.orm import Session

from models.database import TentacleUser, get_setting, set_setting

logger = logging.getLogger(__name__)

TENTACLE_SECTIONS = [
    {"section_id": "resumevideo", "display_name": "Continue Watching", "shapes": True, "default_shape": "wide"},
    {"section_id": "onnow", "display_name": "On Now", "shapes": False},
    {"section_id": "recordings", "display_name": "Recordings", "shapes": True, "default_shape": "poster"},
]
SECTION_MAP = {s["section_id"]: s for s in TENTACLE_SECTIONS}

# Old ids that still mean one of Tentacle's rows: Jellyfin's "resume" section,
# and Next Up on its own (it is the next-episodes part of Continue Watching now).
_ALIASES = {"resume": "resumevideo", "nextup": "resumevideo"}

MIGRATED_KEY = "home_rows_tentacle_only"


def shape_of(row: dict) -> str:
    """The card shape a Tentacle row is drawn with ("" for On Now's own card)."""
    s = SECTION_MAP.get(row.get("section_id", ""))
    if not s or not s["shapes"]:
        return ""
    return row["shape"] if row.get("shape") in ("poster", "wide") else s["default_shape"]


def normalize_rows(config: dict) -> bool:
    """Bring a home config's non-playlist rows to Tentacle's own: Jellyfin's
    sections leave, Next Up on its own row becomes Continue Watching with next
    episodes on, names follow Tentacle's. Changes `config` in place; returns
    whether anything changed. Playlist rows and their order are untouched."""
    if not isinstance(config, dict):
        return False
    rows = config.get("rows")
    if not isinstance(rows, list):
        return False
    changed = False
    out, seen = [], set()
    for r in sorted(rows, key=lambda r: r.get("order", 0)):
        if r.get("type") != "builtin":
            out.append(r)
            continue
        sid = r.get("section_id", "")
        if sid == "nextup":
            # Next episodes belong to Continue Watching now
            if config.get("merge_continue_watching") is not True:
                config["merge_continue_watching"] = True
                changed = True
        new_sid = _ALIASES.get(sid, sid)
        if new_sid not in SECTION_MAP or new_sid in seen:
            changed = True
            continue
        seen.add(new_sid)
        if new_sid != sid or r.get("display_name") != SECTION_MAP[new_sid]["display_name"]:
            r = {**r, "section_id": new_sid, "display_name": SECTION_MAP[new_sid]["display_name"]}
            changed = True
        if not SECTION_MAP[new_sid]["shapes"] and "shape" in r:
            r = {k: v for k, v in r.items() if k != "shape"}
            changed = True
        out.append(r)
    if "resumevideo" in seen and "merge_continue_watching" not in config:
        # Next episodes were folded in by default since 21 Oct; say so on the file
        config["merge_continue_watching"] = True
        changed = True
    for i, r in enumerate(out, 1):
        if r.get("order") != i:
            r["order"] = i
            changed = True
    if changed or len(out) != len(rows):
        config["rows"] = out
        return True
    return False


def migrate_homes(db: Session) -> int:
    """Once per install: every saved home (and the admin's default) keeps only
    Tentacle's rows. Returns how many homes changed; their clients are told."""
    from services.smartlists import (
        _notify_jellyfin_plugin, _user_home_config_path, bump_playlist_version, get_home_config,
        home_config_lock, write_home_json,
    )
    from services import default_home
    if get_setting(db, MIGRATED_KEY, "") == "1":
        return 0
    changed_users = []
    with home_config_lock:
        for u in db.query(TentacleUser).all():
            cfg = get_home_config(db, user_id=u.id)
            if not cfg:
                continue
            before = [(r.get("section_id"), r.get("display_name")) for r in cfg.get("rows") or []
                      if r.get("type") == "builtin"]
            if normalize_rows(cfg):
                write_home_json(_user_home_config_path(db, u.id), cfg)
                changed_users.append(u)
                logger.info(f"[Home rows] {u.display_name}: Jellyfin rows {before} → Tentacle's own")
    saved = default_home.saved_default(db)
    if saved and normalize_rows(saved):
        default_home.save_default(db, saved)
        logger.info("[Home rows] The default home keeps only Tentacle's rows")
    set_setting(db, MIGRATED_KEY, "1")
    if changed_users:
        bump_playlist_version()
        for u in changed_users:
            try:
                _notify_jellyfin_plugin(db, u)
            except Exception as e:
                logger.debug(f"[Home rows] Could not tell Jellyfin about {u.display_name}'s home: {e}")
    return len(changed_users)

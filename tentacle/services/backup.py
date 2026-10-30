"""Backup & restore of Tentacle's setup (#870): what an admin set up by hand,
in one JSON file, restored by name.

Parts (PARTS), each picked on its own when backing up and when restoring:
VOD categories and Live TV channels belong to a provider and sit under it in
the file; home rows, playlists, lists and recording rules belong to the
household and its people and restore regardless of providers.

Everything is matched by name, never by id (ids are kept as a hint only): a
category by its type and name ("Movies | 4K"), a channel group by name, a
channel by its group and name (else its name alone, when that is unique), a
person by their Jellyfin id, else their name, a playlist row by the
playlist's name. So a provider that renumbered its categories, another
install, or another provider altogether lines up: a provider of the file the
target has no provider of that name for is mapped to one of the target's
(the preview counts what matches in each) or skipped; never a failure.

A restore is a preview first (`plan(..., apply=False)`: what would be added,
changed, not found, and with "replace" removed) and writes only on confirm.
The default for every part is merge: add what is missing, change what
differs, remove nothing. "Replace" for a part also takes away what the file
doesn't have (categories and channels switched off, lists, playlists and
rules deleted). Before a restore writes anything, a snapshot of the setup as
it was is saved ("before restore").

Never in the file: provider addresses and logins, API keys, tokens,
passwords (settings are not backed up at all), and any value that carries
one (a list address with a key in it is left out, and the file says so).

Automatic snapshots: daily (main.py), the last KEEP kept, as JSON in
/data/backups next to the database; "Back up now" adds one.
"""
import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from models.database import (
    AutoPlaylistToggle, ListSubscription, LiveChannel, LiveChannelGroup, LiveNetwork, Provider,
    ProviderCategory, RecordingRule, RecordingRuleChannel, RecordingRuleTerm, TagRule, TentacleUser,
    YouTubeChannel, get_setting, set_setting,
)

logger = logging.getLogger(__name__)

FORMAT = "tentacle-setup"
VERSION = 1
KEEP = 14
SNAPSHOT_PREFIX = "tentacle-setup-"
_SNAPSHOT_NAME = re.compile(r"^tentacle-setup-\d{8}-\d{6}(?:-[a-z-]+)?\.json$")

# (id, label, bound to a provider)
PARTS = [
    ("vod_categories", "VOD categories", True),
    ("live_channels", "Live TV channels", True),
    ("home_rows", "Home rows", False),
    ("playlists", "Playlists", False),
    ("lists", "Lists", False),
    ("recording_rules", "Recording rules & teams", False),
]
PART_IDS = [p[0] for p in PARTS]
PROVIDER_PARTS = {p[0] for p in PARTS if p[2]}
PEOPLE_PARTS = {"home_rows", "playlists", "lists"}

_lock = threading.Lock()


def backup_dir() -> str:
    return os.path.join(os.getenv("DATA_DIR", "/data"), "backups")


def _now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _fold(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def _norm_jf(uid) -> str:
    return str(uid or "").replace("-", "").lower()


# ── Secrets: what must never be in a file ───────────────────────────────────

def _secret_values(db: Session) -> list:
    """Every stored secret, to check exported text against: provider logins
    and every key, token and password in settings."""
    from routers.settings import NEVER_SERVED, SENSITIVE_KEYS
    vals = set()
    for p in db.query(Provider).all():
        for v in (p.password, p.username):
            if v and len(v) >= 4:
                vals.add(v)
    keys = SENSITIVE_KEYS | NEVER_SERVED
    for k in keys:
        v = get_setting(db, k, "")
        if v and len(v) >= 6:
            vals.add(v)
    return sorted(vals, key=len, reverse=True)


class _Scrub:
    def __init__(self, db: Session):
        from services.log_redaction import redact
        self._redact = redact
        self._secrets = _secret_values(db)
        self.left_out = []

    def has_secret(self, value) -> bool:
        if not isinstance(value, str) or not value:
            return False
        if any(s in value for s in self._secrets):
            return True
        return "://" in value and self._redact(value) != value

    def clean(self, value, what: str):
        """The value, or None (noted) when it carries a secret."""
        if self.has_secret(value):
            self.left_out.append(what)
            return None
        return value


# ── Export ───────────────────────────────────────────────────────────────────

def _person(u: TentacleUser) -> dict:
    return {"name": u.display_name, "jellyfin_user_id": _norm_jf(u.jellyfin_user_id)}


def _wanted_people(db: Session, people) -> list:
    users = db.query(TentacleUser).order_by(TentacleUser.id).all()
    if people is None:
        return users
    want = {_fold(p) for p in people}
    return [u for u in users if _fold(u.display_name) in want or _norm_jf(u.jellyfin_user_id) in want]


def _export_provider(db: Session, p: Provider, parts: set, scrub: _Scrub) -> dict:
    out = {"name": p.name, "type": p.provider_type, "id": p.id}
    if "vod_categories" in parts:
        cats = (db.query(ProviderCategory).filter(ProviderCategory.provider_id == p.id)
                .order_by(ProviderCategory.type, ProviderCategory.category_name).all())
        out["vod_categories"] = [
            {"name": c.category_name, "type": c.type, "whitelisted": bool(c.whitelisted),
             "source_tag": c.source_tag, "id": c.category_id}
            for c in cats if c.whitelisted or c.source_tag]
    if "live_channels" in parts:
        groups = db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == p.id) \
            .order_by(LiveChannelGroup.name).all()
        on = {g.name for g in groups if g.enabled}
        out["live_groups"] = [{"name": g.name, "enabled": True, "id": g.category_id}
                              for g in groups if g.enabled]
        chans = []
        q = db.query(LiveChannel).filter(LiveChannel.provider_id == p.id).order_by(LiveChannel.id)
        for c in q.yield_per(1000):
            custom = (c.custom_name or c.custom_logo_url or c.epg_id_override
                      or c.channel_number is not None or c.sort_order)
            # Switched on, switched off inside a group that is on, or the
            # admin's own name/number/logo/guide id: the rest is the provider's
            if not (c.enabled or c.group_title in on or custom):
                continue
            row = {"name": c.name, "group": c.group_title, "enabled": bool(c.enabled), "id": c.stream_id}
            if c.custom_name:
                row["custom_name"] = c.custom_name
            if c.channel_number is not None:
                row["number"] = c.channel_number
            if c.sort_order:
                row["sort_order"] = c.sort_order
            if c.epg_id_override:
                row["epg_id_override"] = c.epg_id_override
            logo = scrub.clean(c.custom_logo_url, f"the logo of channel {c.name} (its address has a login)")
            if logo:
                row["custom_logo_url"] = logo
            chans.append(row)
        out["live_channels"] = chans
    return out


def _home_for_file(home: dict) -> dict:
    keep = ("hero", "rows", "merge_continue_watching", "toolbar", "card_previews")
    out = {k: home[k] for k in keep if k in home}
    out["rows"] = [{k: v for k, v in r.items() if k != "unresolved_since"} for r in out.get("rows") or []]
    return out


def _pool_names(db: Session) -> dict:
    """Pool key -> the name it shows ("c12" -> "TSN 1"), for rules' channels."""
    names = {}
    try:
        from services.livetv_lineups import pool
        for e in pool(db):
            names[e["key"]] = e["name"]
    except Exception as e:
        logger.info(f"[Backup] Channel pool not read ({e}); channel names from the tables")
        db.rollback()
    return names


def _key_name(db: Session, key: str, names: dict) -> Optional[str]:
    if key in names:
        return names[key]
    base = key.split("@", 1)[0]
    try:
        n = int(base[1:])
    except ValueError:
        return None
    if base[0] == "c":
        ch = db.get(LiveChannel, n)
        return ch.guide_name if ch else None
    if base[0] == "n":
        net = db.get(LiveNetwork, n)
        return net.name if net else None
    if base[0] == "y":
        yt = db.get(YouTubeChannel, n)
        return yt.title if yt else None
    return None


def export(db: Session, parts=None, people=None, providers=None) -> dict:
    """The setup as a dict (the file's content). `parts`: ids from PARTS
    (default all); `people`: names or Jellyfin ids for the people's parts
    (default everyone); `providers`: provider names (default all)."""
    from services.smartlists import get_home_config, _home_config_exists
    from services.default_home import follows_default, saved_default
    parts = set(parts or PART_IDS) & set(PART_IDS)
    scrub = _Scrub(db)
    users = _wanted_people(db, people)
    try:
        from services.versions import current
        version = current()
    except Exception:
        version = None
    out = {"format": FORMAT, "version": VERSION, "made_at": _now(), "tentacle": version,
           "parts": [p for p in PART_IDS if p in parts],
           "secrets": "none: no provider logins or addresses, API keys, tokens or passwords",
           "people": [_person(u) for u in users]}

    if parts & PROVIDER_PARTS:
        want = {_fold(n) for n in providers} if providers else None
        out["providers"] = [_export_provider(db, p, parts, scrub)
                            for p in db.query(Provider).order_by(Provider.id).all()
                            if want is None or _fold(p.name) in want]

    if "home_rows" in parts:
        homes = []
        for u in users:
            if not _home_config_exists(db, u.id):
                continue
            home = get_home_config(db, user_id=u.id) or {}
            if follows_default(home):
                homes.append({**_person(u), "follows_default": True})
            elif home:
                homes.append({**_person(u), "follows_default": False, "home": _home_for_file(home)})
        out["home_rows"] = {"default": saved_default(db) or None, "people": homes}

    if "playlists" in parts:
        pl = []
        for u in users:
            toggles = db.query(AutoPlaylistToggle).filter(AutoPlaylistToggle.user_id == u.id) \
                .order_by(AutoPlaylistToggle.key).all()
            rules = db.query(TagRule).filter(TagRule.user_id == u.id).order_by(TagRule.id).all()
            if not toggles and not rules:
                continue
            pl.append({**_person(u),
                       "ready_made": [{"key": t.key, "enabled": bool(t.enabled)} for t in toggles],
                       "custom": [{"name": r.name, "conditions": r.conditions or [], "output_tag": r.output_tag,
                                   "active": bool(r.active), "apply_to": r.apply_to or "both"} for r in rules]})
        out["playlists"] = {"people": pl}

    if "lists" in parts:
        ls = []
        for u in users:
            rows = []
            for lst in db.query(ListSubscription).filter(ListSubscription.user_id == u.id) \
                    .order_by(ListSubscription.id).all():
                url = scrub.clean(lst.url, f"list {lst.name} (its address carries a key)")
                if not url:
                    continue
                rows.append({"name": lst.name, "type": lst.type, "url": url, "tag": lst.tag,
                             "active": bool(lst.active), "auto_add_radarr": bool(lst.auto_add_radarr),
                             "playlist": bool(lst.playlist_enabled)})
            if rows:
                ls.append({**_person(u), "lists": rows})
        out["lists"] = {"people": ls}

    if "recording_rules" in parts:
        owners = {u.id: u for u in db.query(TentacleUser).all()}
        names = _pool_names(db)
        rules = []
        for r in db.query(RecordingRule).order_by(RecordingRule.id).all():
            terms = db.query(RecordingRuleTerm).filter(RecordingRuleTerm.rule_id == r.id).all()
            chans = db.query(RecordingRuleChannel).filter(RecordingRuleChannel.rule_id == r.id).all()
            o = owners.get(r.owner_user_id)
            rules.append({
                "name": r.name, "kind": r.kind, "league": r.league, "team": r.team, "home_away": r.home_away,
                "match_all": bool(r.match_all), "search_description": bool(r.search_description),
                "min_minutes": r.min_minutes, "pre_padding_minutes": r.pre_padding_minutes,
                "post_padding_minutes": r.post_padding_minutes, "playoff_extra_minutes": r.playoff_extra_minutes,
                "pregame_next": bool(r.pregame_next), "skip_replays": bool(r.skip_replays), "mode": r.mode,
                "active": bool(r.active), "priority": r.priority,
                "owner": _person(o) if o else None,
                "terms": [t.term for t in terms if not t.exclude],
                "excludes": [t.term for t in terms if t.exclude],
                "channels": [{"key": c.pool_key, "name": _key_name(db, c.pool_key, names)} for c in chans],
            })
        out["recording_rules"] = rules

    out["left_out"] = scrub.left_out
    return out


def summary(data: dict) -> dict:
    """How much of each part a file holds (the snapshot list and the restore
    form): {part: count}, plus providers and people."""
    provs = data.get("providers") or []
    s = {
        "vod_categories": sum(sum(1 for c in p.get("vod_categories") or [] if c.get("whitelisted")) for p in provs),
        "live_channels": sum(sum(1 for c in p.get("live_channels") or [] if c.get("enabled")) for p in provs),
        "home_rows": len((data.get("home_rows") or {}).get("people") or [])
        + (1 if (data.get("home_rows") or {}).get("default") else 0),
        "playlists": sum(len(p.get("custom") or []) + sum(1 for t in p.get("ready_made") or [] if t.get("enabled"))
                         for p in (data.get("playlists") or {}).get("people") or []),
        "lists": sum(len(p.get("lists") or []) for p in (data.get("lists") or {}).get("people") or []),
        "recording_rules": len(data.get("recording_rules") or []),
    }
    return {"parts": [p for p in data.get("parts") or [] if p in PART_IDS],
            "counts": {k: v for k, v in s.items() if k in (data.get("parts") or [])},
            "providers": [p.get("name") for p in provs],
            "people": [p.get("name") for p in data.get("people") or []],
            "made_at": data.get("made_at"), "tentacle": data.get("tentacle")}


def check_file(data) -> dict:
    """A file someone uploaded: the dict, or ValueError in words."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("This isn't a Tentacle setup backup")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise ValueError("This backup comes from a newer Tentacle: update this one first")
    return data


# ── Snapshots ─────────────────────────────────────────────────────────────────

def snapshot(db: Session, reason: str = "") -> dict:
    """Save the whole setup in /data/backups and keep the last KEEP."""
    data = export(db)
    d = backup_dir()
    os.makedirs(d, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    tag = re.sub(r"[^a-z]+", "-", reason.lower()).strip("-")
    name = f"{SNAPSHOT_PREFIX}{stamp}{'-' + tag if tag else ''}.json"
    path = os.path.join(d, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    _prune()
    logger.info(f"[Backup] Setup saved: {name} ({os.path.getsize(path) // 1024} KB)")
    return {"name": name, **summary(data)}


def _prune() -> None:
    names = sorted(n for n in os.listdir(backup_dir()) if _SNAPSHOT_NAME.match(n))
    for n in names[:-KEEP]:
        try:
            os.remove(os.path.join(backup_dir(), n))
        except OSError as e:
            logger.warning(f"[Backup] Couldn't remove old snapshot {n}: {e}")


def snapshots() -> list:
    d = backup_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for n in sorted((n for n in os.listdir(d) if _SNAPSHOT_NAME.match(n)), reverse=True):
        path = os.path.join(d, n)
        try:
            with open(path, encoding="utf-8") as f:
                s = summary(json.load(f))
        except (OSError, ValueError) as e:
            s = {"problem": f"can't be read: {e}"}
        out.append({"name": n, "size": os.path.getsize(path), **s})
    return out


def read_snapshot(name: str) -> dict:
    if not _SNAPSHOT_NAME.match(name or ""):
        raise FileNotFoundError(name)
    with open(os.path.join(backup_dir(), name), encoding="utf-8") as f:
        return check_file(json.load(f))


def run_scheduled() -> None:
    """The daily snapshot (main.py)."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        snapshot(db, "daily")
    except Exception as e:
        logger.error(f"[Backup] Daily snapshot failed: {e}", exc_info=True)
    finally:
        db.close()


# ── Restore ──────────────────────────────────────────────────────────────────

def _report(label: str) -> dict:
    return {"label": label, "added": 0, "changed": 0, "unchanged": 0, "removed": 0,
            "not_found": [], "notes": [], "detail": []}


class _People:
    """People of the file -> people here: by Jellyfin id, else by name."""

    def __init__(self, db: Session):
        self.users = db.query(TentacleUser).order_by(TentacleUser.id).all()
        self.by_jf = {_norm_jf(u.jellyfin_user_id): u for u in self.users}
        self.by_name = {}
        for u in self.users:
            self.by_name.setdefault(_fold(u.display_name), []).append(u)

    def find(self, person) -> Optional[TentacleUser]:
        if not person:
            return None
        u = self.by_jf.get(_norm_jf(person.get("jellyfin_user_id")))
        if u:
            return u
        same = self.by_name.get(_fold(person.get("name")), [])
        return same[0] if len(same) == 1 else None


def _match_counts(db: Session, fp: dict, target_id: int) -> dict:
    """How much of a file provider's setup lines up with one provider here."""
    cats = {(c.type, _fold(c.category_name)) for c in
            db.query(ProviderCategory.type, ProviderCategory.category_name)
            .filter(ProviderCategory.provider_id == target_id)}
    groups = {_fold(n) for (n,) in db.query(LiveChannelGroup.name).filter(LiveChannelGroup.provider_id == target_id)}
    idx = _ChannelIndex(db, target_id)
    fc = [c for c in fp.get("vod_categories") or [] if c.get("whitelisted") or c.get("source_tag")]
    fg = fp.get("live_groups") or []
    fch = [c for c in fp.get("live_channels") or [] if c.get("enabled")]
    return {"vod_total": len(fc), "vod_matched": sum((c.get("type"), _fold(c.get("name"))) in cats for c in fc),
            "groups_total": len(fg), "groups_matched": sum(_fold(g.get("name")) in groups for g in fg),
            "channels_total": len(fch), "channels_matched": sum(idx.find(c) is not None for c in fch)}


class _ChannelIndex:
    def __init__(self, db: Session, provider_id: int):
        self.by_group_name = {}
        self.by_name = {}
        for ch in db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).yield_per(2000):
            self.by_group_name.setdefault((_fold(ch.group_title), _fold(ch.name)), ch)
            self.by_name.setdefault(_fold(ch.name), []).append(ch)

    def find(self, row) -> Optional[LiveChannel]:
        ch = self.by_group_name.get((_fold(row.get("group")), _fold(row.get("name"))))
        if ch is not None:
            return ch
        same = self.by_name.get(_fold(row.get("name")), [])
        return same[0] if len(same) == 1 else None


def provider_choices(db: Session, data: dict, mapping: Optional[dict] = None) -> list:
    """Each provider of the file: where it goes here (`target`: a provider id,
    or None = skipped), whether that was by name, and for every provider
    here how much of it matches."""
    mapping = mapping or {}
    here = db.query(Provider).order_by(Provider.id).all()
    by_name = {_fold(p.name): p for p in here}
    out = []
    for fp in data.get("providers") or []:
        name = fp.get("name") or ""
        same = by_name.get(_fold(name))
        if name in mapping:
            target = mapping[name]
            target = int(target) if target not in (None, "", "skip") else None
            if target is not None and not any(p.id == target for p in here):
                target = None
        else:
            target = same.id if same else None
        out.append({"name": name, "target": target, "same_name": same.id if same else None,
                    "candidates": [{"id": p.id, "name": p.name, **_match_counts(db, fp, p.id)} for p in here]})
    return out


def _restore_vod(db, data, choices, replace, apply) -> dict:
    rep = _report("VOD categories")
    for fp, ch in zip(data.get("providers") or [], choices):
        rows = [c for c in fp.get("vod_categories") or []]
        if ch["target"] is None:
            if rows:
                rep["notes"].append(f"{fp.get('name')}: skipped ({len(rows)} categories)")
            continue
        here = db.query(ProviderCategory).filter(ProviderCategory.provider_id == ch["target"]).all()
        index = {(c.type, _fold(c.category_name)): c for c in here}
        wanted = set()
        for row in rows:
            c = index.get((row.get("type"), _fold(row.get("name"))))
            if c is None:
                if row.get("whitelisted"):
                    rep["not_found"].append(f"{row.get('name')} ({'Series' if row.get('type') == 'series' else 'Movies'})")
                continue
            wanted.add(c.id)
            on = bool(row.get("whitelisted"))
            tag = row.get("source_tag") or None
            if bool(c.whitelisted) == on and (c.source_tag or None) == tag:
                rep["unchanged"] += 1
                continue
            if on and not c.whitelisted:
                rep["added"] += 1
            else:
                rep["changed"] += 1
            if apply:
                c.whitelisted = on
                if tag:
                    c.source_tag = tag
        if replace:
            for c in here:
                if c.whitelisted and c.id not in wanted:
                    rep["removed"] += 1
                    if apply:
                        c.whitelisted = False
    if apply:
        db.commit()
    if rep["added"] or rep["changed"] or rep["removed"]:
        rep["notes"].append("Titles follow at the next VOD sync")
    return rep


def _restore_live(db, data, choices, replace, apply) -> dict:
    rep = _report("Live TV channels")
    touched = False
    for fp, ch in zip(data.get("providers") or [], choices):
        fgroups = fp.get("live_groups") or []
        fchans = fp.get("live_channels") or []
        if ch["target"] is None:
            if fgroups or fchans:
                rep["notes"].append(f"{fp.get('name')}: skipped ({sum(1 for c in fchans if c.get('enabled'))} channels)")
            continue
        pid = ch["target"]
        groups = {_fold(g.name): g for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == pid)}
        idx = _ChannelIndex(db, pid)
        on_groups = set()
        for g in fgroups:
            grp = groups.get(_fold(g.get("name")))
            if grp is None:
                rep["not_found"].append(f"group {g.get('name')}")
                continue
            on_groups.add(grp.name)
            if not grp.enabled:
                rep["detail"].append(f"group {grp.name} on")
                if apply:
                    grp.enabled = True
                    # as the group switch does: its channels with it; the
                    # file's own choices per channel come next
                    db.query(LiveChannel).filter(LiveChannel.provider_id == pid,
                                                 LiveChannel.group_title == grp.name) \
                        .update({LiveChannel.enabled: True}, synchronize_session=False)
                    touched = True
        if apply:
            db.flush()
            db.expire_all()     # the group switch above went past the session
            idx = _ChannelIndex(db, pid)
        seen = set()
        for row in fchans:
            c = idx.find(row)
            if c is None:
                if row.get("enabled"):
                    rep["not_found"].append(row.get("custom_name") or row.get("name") or "?")
                continue
            seen.add(c.id)
            want = {"enabled": bool(row.get("enabled")), "custom_name": row.get("custom_name") or None,
                    "channel_number": row.get("number"), "sort_order": row.get("sort_order") or 0,
                    "epg_id_override": row.get("epg_id_override") or None,
                    "custom_logo_url": row.get("custom_logo_url") or None}
            # merge: the file's own names, numbers and logos; one set here
            # is cleared only when replacing
            diff = {}
            for k, v in want.items():
                cur = getattr(c, k) or 0 if k == "sort_order" else getattr(c, k)
                if cur != v and (k == "enabled" or replace or v not in (None, 0)):
                    diff[k] = v
            if not diff:
                rep["unchanged"] += 1
                continue
            if diff.get("enabled") and not c.enabled and len(diff) == 1:
                rep["added"] += 1
            else:
                rep["changed"] += 1
            if apply:
                for k, v in diff.items():
                    setattr(c, k, v)
                c.updated_at = datetime.utcnow()
                touched = True
        if replace:
            for c in db.query(LiveChannel).filter(LiveChannel.provider_id == pid, LiveChannel.enabled == True):  # noqa: E712
                if c.id not in seen and c.group_title not in on_groups:
                    rep["removed"] += 1
                    if apply:
                        c.enabled = False
                        touched = True
            for g in groups.values():
                if g.enabled and g.name not in on_groups:
                    rep["detail"].append(f"group {g.name} off")
                    if apply:
                        g.enabled = False
                        touched = True
    if apply:
        db.commit()
        if touched:
            from routers.livetv import _pool_changed
            _pool_changed("setup restored")
    return rep


def _restore_home(db, data, people: _People, wanted, replace, apply, follow_up) -> dict:
    from services.default_home import FOLLOWS, follows_default, save_default, saved_default
    from services.smartlists import (_home_config_exists, _user_home_config_path, get_home_config,
                                     home_config_lock, write_home_json)
    rep = _report("Home rows")
    part = data.get("home_rows") or {}
    if part.get("default") and wanted is None:
        if saved_default(db) == part["default"]:
            rep["unchanged"] += 1
        elif saved_default(db) and not replace:
            rep["notes"].append("The default home here is kept (pick Replace to use the file's)")
        else:
            rep["detail"].append("the default home")
            rep["changed" if saved_default(db) else "added"] += 1
            if apply:
                save_default(db, part["default"])
                follow_up["default"] = True
    for entry in part.get("people") or []:
        if wanted is not None and _fold(entry.get("name")) not in wanted:
            continue
        u = people.find(entry)
        if u is None:
            rep["not_found"].append(entry.get("name") or "?")
            continue
        exists = _home_config_exists(db, u.id)
        current = get_home_config(db, user_id=u.id) if exists else {}
        if entry.get("follows_default"):
            if exists and follows_default(current):
                rep["unchanged"] += 1
                continue
            if exists and not replace:
                rep["notes"].append(f"{u.display_name}: keeps their own home (the file has the default)")
                continue
            rep["detail"].append(f"{u.display_name}: the default home")
            rep["changed" if exists else "added"] += 1
            if apply:
                # marked now; the follow-up fills in the default's rows
                with home_config_lock:
                    write_home_json(_user_home_config_path(db, u.id),
                                    {**(current or {"rows": []}), FOLLOWS: True})
                follow_up.setdefault("to_default", set()).add(u.id)
            continue
        home = entry.get("home") or {}
        rows = [dict(r) for r in home.get("rows") or []]
        if exists and not follows_default(current) and not replace:
            # merge: their rows stay; the file's rows they don't have go after
            have = {_row_id(r) for r in current.get("rows") or []}
            extra = [r for r in rows if _row_id(r) not in have]
            if not extra:
                rep["unchanged"] += 1
                continue
            rep["detail"].append(f"{u.display_name}: {len(extra)} row{'s' if len(extra) != 1 else ''} added")
            rep["changed"] += 1
            config = dict(current)
            config["rows"] = list(current.get("rows") or []) + extra
            if not (current.get("hero") or {}).get("enabled") and (home.get("hero") or {}).get("enabled"):
                config["hero"] = home["hero"]
        else:
            config = {k: v for k, v in (current or {}).items() if k == "jellyfin_sections_snapshot"}
            config.update(home)
            config["rows"] = rows
            if _strip_rows(current) == _strip_rows(config):
                rep["unchanged"] += 1
                continue
            rep["detail"].append(f"{u.display_name}: {len(rows)} rows")
            rep["changed" if exists else "added"] += 1
        config.pop(FOLLOWS, None)
        for i, r in enumerate(sorted(config["rows"], key=lambda r: r.get("order", 0)), 1):
            r["order"] = i
        if apply:
            with home_config_lock:
                write_home_json(_user_home_config_path(db, u.id), config)
            follow_up.setdefault("users", set()).add(u.id)
    return rep


def _row_id(r: dict) -> str:
    if r.get("type") == "builtin":
        return "builtin:" + str(r.get("section_id"))
    return "playlist:" + _fold(r.get("display_name"))


def _strip_rows(cfg: dict) -> list:
    return [(_row_id(r), r.get("shape"), r.get("max_items")) for r in (cfg or {}).get("rows") or []] + \
        [((cfg or {}).get("hero") or {}).get("display_name")]


def _restore_playlists(db, data, people: _People, wanted, replace, apply, follow_up) -> dict:
    rep = _report("Playlists")
    for entry in (data.get("playlists") or {}).get("people") or []:
        if wanted is not None and _fold(entry.get("name")) not in wanted:
            continue
        u = people.find(entry)
        if u is None:
            rep["not_found"].append(entry.get("name") or "?")
            continue
        touched = False
        toggles = {t.key: t for t in db.query(AutoPlaylistToggle).filter(AutoPlaylistToggle.user_id == u.id)}
        for t in entry.get("ready_made") or []:
            key, on = t.get("key"), bool(t.get("enabled"))
            if not key:
                continue
            have = toggles.get(key)
            if have is not None and bool(have.enabled) == on:
                rep["unchanged"] += 1
                continue
            if have is None and not on:
                continue
            if not on and not replace:
                continue    # merge never switches a playlist off
            rep["added" if on else "changed"] += 1
            touched = True
            if apply:
                if have is None:
                    db.add(AutoPlaylistToggle(user_id=u.id, key=key, enabled=on))
                else:
                    have.enabled = on
        rules = {_fold(r.name): r for r in db.query(TagRule).filter(TagRule.user_id == u.id)}
        names = set()
        for r in entry.get("custom") or []:
            name = (r.get("name") or "").strip()
            if not name or not r.get("output_tag"):
                continue
            names.add(_fold(name))
            want = {"conditions": r.get("conditions") or [], "output_tag": r["output_tag"],
                    "active": bool(r.get("active", True)), "apply_to": r.get("apply_to") or "both"}
            have = rules.get(_fold(name))
            if have is None:
                rep["added"] += 1
                rep["detail"].append(f"{u.display_name}: {name}")
                touched = True
                if apply:
                    db.add(TagRule(user_id=u.id, name=name, **want))
            elif any(getattr(have, k) != v for k, v in want.items()):
                rep["changed"] += 1
                touched = True
                if apply:
                    for k, v in want.items():
                        setattr(have, k, v)
            else:
                rep["unchanged"] += 1
        if replace:
            for key, r in rules.items():
                if key not in names:
                    rep["removed"] += 1
                    touched = True
                    if apply:
                        db.delete(r)
        if touched and apply:
            follow_up.setdefault("users", set()).add(u.id)
    if apply:
        db.commit()
    return rep


def _restore_lists(db, data, people: _People, wanted, replace, apply, follow_up) -> dict:
    from services.tagger import name_key
    rep = _report("Lists")
    for entry in (data.get("lists") or {}).get("people") or []:
        if wanted is not None and _fold(entry.get("name")) not in wanted:
            continue
        u = people.find(entry)
        if u is None:
            rep["not_found"].append(entry.get("name") or "?")
            continue
        mine = db.query(ListSubscription).filter(ListSubscription.user_id == u.id).all()
        by_url = {(l.url or "").strip().rstrip("/").lower(): l for l in mine}
        others = {name_key(t) for (t,) in db.query(ListSubscription.tag).filter(ListSubscription.user_id != u.id)}
        urls = set()
        touched = False
        for row in entry.get("lists") or []:
            url = (row.get("url") or "").strip()
            if not url or not row.get("type") or not row.get("tag"):
                continue
            key = url.rstrip("/").lower()
            urls.add(key)
            want = {"name": row.get("name") or url, "tag": row["tag"], "active": bool(row.get("active", True)),
                    "auto_add_radarr": bool(row.get("auto_add_radarr")),
                    "playlist_enabled": bool(row.get("playlist"))}
            have = by_url.get(key)
            if have is None:
                if name_key(row["tag"]) in others:
                    rep["notes"].append(f"{u.display_name}: {want['name']} left out (another person's list "
                                        f"uses its tag {row['tag']})")
                    continue
                rep["added"] += 1
                rep["detail"].append(f"{u.display_name}: {want['name']}")
                touched = True
                if apply:
                    db.add(ListSubscription(user_id=u.id, type=row["type"], url=url, **want))
            elif any(getattr(have, k) != v for k, v in want.items()):
                rep["changed"] += 1
                touched = True
                if apply:
                    for k, v in want.items():
                        setattr(have, k, v)
            else:
                rep["unchanged"] += 1
        if replace:
            for key, l in by_url.items():
                if key not in urls:
                    rep["removed"] += 1
                    touched = True
                    if apply:
                        db.delete(l)
        if touched and apply:
            follow_up.setdefault("users", set()).add(u.id)
            follow_up.setdefault("lists", set()).add(u.id)
    if apply:
        db.commit()
    if rep["added"]:
        rep["notes"].append("New lists read their titles in the background")
    return rep


def _rule_key(r: dict, owner_id) -> tuple:
    kind = r.get("kind")
    if kind == "team":
        return (kind, r.get("league"), r.get("team"), owner_id)
    if kind == "series":
        return (kind, r.get("league"), owner_id)
    return (kind, _fold(r.get("name")), owner_id)


_RULE_FIELDS = ("name", "home_away", "match_all", "search_description", "min_minutes", "pre_padding_minutes",
                "post_padding_minutes", "playoff_extra_minutes", "pregame_next", "skip_replays", "mode", "active",
                "priority")


def _restore_rules(db, data, people: _People, replace, apply) -> dict:
    from services.recording_rules import leagues as LG
    rep = _report("Recording rules & teams")
    names = _pool_names(db)
    by_name = {}
    for key, name in names.items():
        by_name.setdefault(_fold(name), key)
    if not by_name:
        for c in db.query(LiveChannel).filter(LiveChannel.enabled == True):  # noqa: E712
            by_name.setdefault(_fold(c.guide_name), f"c{c.id}")
    here = db.query(RecordingRule).all()
    index = {}
    for r in here:
        index[_rule_key({"kind": r.kind, "league": r.league, "team": r.team, "name": r.name}, r.owner_user_id)] = r
    seen = set()
    changed = False
    for row in data.get("recording_rules") or []:
        kind = row.get("kind")
        if kind not in ("team", "keywords", "series"):
            continue
        if kind == "team" and LG.team(row.get("league"), row.get("team")) is None:
            rep["not_found"].append(f"{row.get('name')} (not in this Tentacle's leagues)")
            continue
        owner = people.find(row.get("owner")) if row.get("owner") else None
        if row.get("owner") and owner is None:
            rep["notes"].append(f"{row.get('name')}: {row['owner'].get('name')} isn't here; the rule is the household's")
        oid = owner.id if owner else None
        keys, missing = [], []
        for c in row.get("channels") or []:
            k = by_name.get(_fold(c.get("name"))) if c.get("name") else None
            if k is None and c.get("key") in names and not c.get("name"):
                k = c["key"]
            (keys.append(k) if k else missing.append(c.get("name") or c.get("key")))
        want = {k: row.get(k) for k in _RULE_FIELDS if k in row}
        want["name"] = str(want.get("name") or "")[:120] or "Rule"
        if want.get("mode") not in ("preview", "record"):
            want["mode"] = "preview"
        if missing:
            rep["notes"].append(f"{want['name']}: channel{'s' if len(missing) != 1 else ''} not found here: "
                                f"{', '.join(map(str, missing[:5]))}")
            if not keys:
                # limited to channels that aren't here: it would record on
                # every channel instead, so it waits in preview
                want["mode"] = "preview"
        terms = [t for t in row.get("terms") or [] if t]
        excludes = [t for t in row.get("excludes") or [] if t]
        rk = _rule_key(row, oid)
        have = index.get(rk)
        seen.add(rk)
        if have is None:
            rep["added"] += 1
            rep["detail"].append(want["name"] + (" (preview)" if want["mode"] == "preview" else ""))
            if apply:
                now = datetime.utcnow()
                r = RecordingRule(kind=kind, league=row.get("league") if kind != "keywords" else None,
                                  team=row.get("team") if kind == "team" else None, owner_user_id=oid,
                                  created_at=now, updated_at=now,
                                  **{k: v for k, v in want.items() if v is not None or k == "min_minutes"})
                db.add(r)
                db.flush()
                _set_rule_lists(db, r.id, terms, excludes, keys)
                changed = True
            continue
        old_terms = sorted((t.term, bool(t.exclude)) for t in
                           db.query(RecordingRuleTerm).filter(RecordingRuleTerm.rule_id == have.id))
        old_keys = sorted(c.pool_key for c in
                          db.query(RecordingRuleChannel).filter(RecordingRuleChannel.rule_id == have.id))
        new_terms = sorted([(t, False) for t in terms] + [(t, True) for t in excludes])
        if all(getattr(have, k) == v for k, v in want.items()) and old_terms == new_terms \
                and old_keys == sorted(set(keys)):
            rep["unchanged"] += 1
            continue
        rep["changed"] += 1
        if apply:
            for k, v in want.items():
                setattr(have, k, v)
            have.updated_at = datetime.utcnow()
            _set_rule_lists(db, have.id, terms, excludes, keys)
            changed = True
    if replace:
        for rk, r in index.items():
            if rk not in seen:
                rep["removed"] += 1
                if apply:
                    from routers.recordings import remove_rule
                    remove_rule(db, r)
                    changed = True
    if apply:
        db.commit()
        if changed:
            from routers.recordings import _changed
            _changed(db)
    return rep


def _set_rule_lists(db, rule_id, terms, excludes, keys) -> None:
    db.query(RecordingRuleTerm).filter(RecordingRuleTerm.rule_id == rule_id).delete()
    db.query(RecordingRuleChannel).filter(RecordingRuleChannel.rule_id == rule_id).delete()
    for t in dict.fromkeys(terms):
        db.add(RecordingRuleTerm(rule_id=rule_id, term=str(t)[:80], exclude=False))
    for t in dict.fromkeys(excludes):
        db.add(RecordingRuleTerm(rule_id=rule_id, term=str(t)[:80], exclude=True))
    for k in dict.fromkeys(keys):
        db.add(RecordingRuleChannel(rule_id=rule_id, pool_key=k[:64]))


def plan(db: Session, data: dict, parts=None, people=None, providers: Optional[dict] = None,
         replace=(), apply: bool = False) -> dict:
    """What restoring `data` would do (apply=False), or do it (apply=True).

    parts: the parts to restore (default: every part the file has);
    people: names of the file's people for home rows, playlists and lists
    (default everyone); providers: {file provider name: provider id here,
    or None to skip} (default: the provider of the same name, else skipped);
    replace: parts whose extras here are removed."""
    check_file(data)
    have = [p for p in data.get("parts") or [] if p in PART_IDS]
    parts = [p for p in (parts or have) if p in have]
    replace = set(replace or ()) & set(parts)
    wanted = {_fold(p) for p in people} if people is not None else None
    choices = provider_choices(db, data, providers)
    who = _People(db)
    follow_up = {}
    out = {"parts": {}, "providers": choices, "applied": apply}
    with _lock if apply else _nolock():
        if apply:
            try:
                out["snapshot"] = snapshot(db, "before restore")["name"]
            except Exception as e:
                logger.error(f"[Backup] Snapshot before restore failed: {e}")
                raise RuntimeError(f"Couldn't save the current setup first, so nothing was restored: {e}")
        for p in PART_IDS:
            if p not in parts:
                continue
            r = p in replace
            if p == "vod_categories":
                rep = _restore_vod(db, data, choices, r, apply)
            elif p == "live_channels":
                rep = _restore_live(db, data, choices, r, apply)
            elif p == "home_rows":
                rep = _restore_home(db, data, who, wanted, r, apply, follow_up)
            elif p == "playlists":
                rep = _restore_playlists(db, data, who, wanted, r, apply, follow_up)
            elif p == "lists":
                rep = _restore_lists(db, data, who, wanted, r, apply, follow_up)
            else:
                rep = _restore_rules(db, data, who, r, apply)
            rep["replace"] = r
            rep["not_found"] = list(dict.fromkeys(rep["not_found"]))
            out["parts"][p] = rep
        if not apply:
            db.rollback()
    if apply:
        logger.info("[Backup] Restored " + "; ".join(
            f"{k}: +{v['added']} ~{v['changed']} -{v['removed']} ({len(v['not_found'])} not found)"
            for k, v in out["parts"].items()))
        if follow_up:
            _follow_up_soon(follow_up)
    return out


class _nolock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _follow_up(todo: dict) -> None:
    """After a restore: lists read their titles, people's playlists are
    built in Jellyfin and their homes written (rows find their playlists by
    name), people back on the default get it, all in the background."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        users = set(todo.get("users") or ())
        if todo.get("lists"):
            try:
                from routers.lists import _get_tmdb_service, refresh_list
                from services.tmdb import get_tmdb_token
                bearer = get_tmdb_token(db)
                trakt = get_setting(db, "trakt_client_id") or ""
                tmdb = _get_tmdb_service(db)
                for lst in db.query(ListSubscription).filter(ListSubscription.user_id.in_(todo["lists"]),
                                                             ListSubscription.active == True):  # noqa: E712
                    try:
                        refresh_list(lst, db, bearer, trakt, tmdb)
                        db.commit()
                    except Exception as e:
                        db.rollback()
                        logger.warning(f"[Backup] List {lst.name} not read yet: {e}")
            except Exception as e:
                logger.warning(f"[Backup] Lists not read after the restore: {e}")
        from routers.smartlists import _run_full_resync
        for uid in sorted(users):
            _run_full_resync(uid)
        from services.default_home import apply_default, apply_to_followers_soon
        for uid in sorted(todo.get("to_default") or ()):
            u = db.get(TentacleUser, uid)
            if u is not None:
                apply_default(db, u, build=True)
        if todo.get("default"):
            apply_to_followers_soon("default home restored")
    except Exception as e:
        logger.error(f"[Backup] After the restore: {e}", exc_info=True)
    finally:
        db.close()


def _follow_up_soon(todo: dict) -> None:
    threading.Thread(target=_follow_up, args=(todo,), name="backup-restore-follow-up", daemon=True).start()

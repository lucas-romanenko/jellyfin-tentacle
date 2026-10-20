"""Tentacle sets up Live TV in Jellyfin itself: one M3U tuner for everyone on
"All channels", one per person with a list of their own, and Jellyfin shows
each person only their own tuner's channels.

How each person sees only their own (proven on Jellyfin 10.11.8, see
docs/agents/jellyfin-notes.md "Live TV per person"):
  - every channel of a tuner carries that tuner's tag (`tentacle-livetv-all`
    or `tentacle-livetv-<user id>`), set here and by the plugin the moment
    Jellyfin creates the channel;
  - every person's policy blocks every other tuner's tag (BlockedTags);
  - the plugin copies the channel's tag onto its programmes, so programme
    lists ("On now", search) are filtered in Jellyfin's database too.

One sync runs at a time, in a thread, a few seconds after the last change
(changes in a row become one sync). A sync:
  1. adds the tuners that should be there (address kept per tuner: Jellyfin
     keys every channel on it);
  2. sets everyone's blocked tags from what is set up so far;
  3. has Jellyfin read the tuners and the guide (RefreshGuide) when anything
     changed, and waits for it;
  4. tags the channels, carries favourites (and recording schedules) across
     when someone switched between "All channels" and their own list, and
     moves everyone off Tentacle's old HDHomeRun (or hand-made M3U) tuner;
  5. removes the tuners nobody uses any more, and sets the blocked tags again.
Before it first changes anything in Jellyfin it saves what was there
(tuners, everyone's favourite channels, schedules, policies) to
/data/livetv-backups/.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

import requests

from models.database import (
    LiveUserLineup, SessionLocal, get_setting, log_activity, set_setting, tentacle_address,
)
from services import livetv_lineups as L

logger = logging.getLogger(__name__)

TUNER_BASE_KEY = "livetv_tuner_base"         # the address every new tuner gets, kept once chosen
ALL_TUNER_URL_KEY = "livetv_all_tuner_url"   # the shared tuner's address, kept as first given
MIGRATED_KEY = "livetv_per_person_migrated"  # the old single tuner was moved off
STATUS_KEY = "livetv_setup_status"           # the last sync's outcome (JSON)
BACKUP_DIR = os.environ.get("TENTACLE_LIVETV_BACKUP_DIR", "/data/livetv-backups")

PLUGIN_ID = "b7e3f1a92c4d48569a1b8def23456789"
GUIDE_WAIT_SECONDS = 90 * 60
_POLL_SECONDS = 5

# Settings a Tentacle tuner takes from the HDHomeRun tuner it replaces, so
# playback behaves as it did (the admin may have tuned them in Jellyfin).
_CARRIED_TUNER_SETTINGS = (
    "AllowHWTranscoding", "AllowFmp4TranscodingContainer", "AllowStreamSharing",
    "FallbackMaxStreamingBitrate", "EnableStreamLooping", "IgnoreDts", "ReadAtNativeFramerate",
)
_NEW_TUNER_DEFAULTS = {
    "AllowHWTranscoding": True, "AllowFmp4TranscodingContainer": False, "AllowStreamSharing": True,
    "FallbackMaxStreamingBitrate": 30000000, "EnableStreamLooping": False, "IgnoreDts": True,
    "ReadAtNativeFramerate": False,
}


class Jellyfin:
    """The few Jellyfin calls a sync makes, with Tentacle's admin key."""

    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.headers = {"X-Emby-Token": key, "Content-Type": "application/json"}

    def get(self, path: str, **params):
        r = requests.get(self.url + path, headers=self.headers, params=params or None, timeout=30)
        r.raise_for_status()
        return r.json() if r.content else None

    def post(self, path: str, body=None, **params):
        r = requests.post(self.url + path, headers=self.headers, params=params or None,
                          json=body, timeout=60)
        r.raise_for_status()
        try:
            return r.json() if r.content else None
        except ValueError:
            return None

    def delete(self, path: str, **params):
        r = requests.delete(self.url + path, headers=self.headers, params=params or None, timeout=30)
        r.raise_for_status()


def jellyfin_for(db) -> Optional[Jellyfin]:
    url = (get_setting(db, "jellyfin_url", "") or "").strip()
    key = (get_setting(db, "jellyfin_api_key", "") or "").strip()
    return Jellyfin(url, key) if url and key else None


# ── Which tuners Jellyfin has ───────────────────────────────────────────────

def is_tentacle_tuner(t: dict) -> bool:
    return t.get("Type") == "m3u" and L.tag_for_tuner_url(t.get("Url") or "") is not None


def old_tuners(db, cfg: dict) -> list:
    """Tentacle's tuners from before per-person Live TV: the HDHomeRun one
    (by its device id) and an M3U tuner on /api/live/playlist.m3u."""
    device = (get_setting(db, "hdhr_device_id", "TENTACLE1") or "TENTACLE1").strip()
    out = []
    for t in cfg.get("TunerHosts") or []:
        if t.get("Type") == "hdhomerun" and (t.get("DeviceId") or "").strip() == device:
            out.append(t)
        elif t.get("Type") == "m3u" and "/api/live/playlist.m3u" in (t.get("Url") or ""):
            out.append(t)
    return out


def _base_of(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    for tail in ("/api/live/playlist.m3u", "/hdhr", "/discover.json"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    return url.rstrip("/")


def tuner_base_address(db, jf: Jellyfin, cfg: dict) -> str:
    """The address Jellyfin reaches Tentacle at, for new tuners: kept once
    chosen. First choice is the old tuner's (Jellyfin already reaches it),
    then the plugin's Tentacle address (the same thing, seen from Jellyfin),
    then Tentacle's address from Settings."""
    saved = (get_setting(db, TUNER_BASE_KEY, "") or "").strip().rstrip("/")
    if saved:
        return saved
    base = ""
    for t in old_tuners(db, cfg):
        base = _base_of(t.get("Url") or "")
        if base:
            break
    if not base:
        try:
            base = ((jf.get(f"/Plugins/{PLUGIN_ID}/Configuration") or {}).get("TentacleUrl") or "").strip().rstrip("/")
        except requests.RequestException:
            base = ""
    if not base:
        base = tentacle_address(db)
    if base and not base.lower().startswith(("http://", "https://")):
        base = "http://" + base
    if base:
        set_setting(db, TUNER_BASE_KEY, base)
        db.commit()
    return base


def all_tuner_url(db, base: str = "") -> str:
    url = (get_setting(db, ALL_TUNER_URL_KEY, "") or "").strip()
    if not url and base:
        url = base + L.ALL_TUNER_PATH
        set_setting(db, ALL_TUNER_URL_KEY, url)
        db.commit()
    return url


def tuner_url_for(db, user_id: str) -> str:
    """The tuner a person's channels are on in Jellyfin ("" if not set up)."""
    row = L.lineup_row(db, user_id)
    if row and row.synced_own_list and row.tuner_url:
        return row.tuner_url
    return all_tuner_url(db)


# ── Who sees what ───────────────────────────────────────────────────────────

def live_users(users: list) -> list:
    return [u for u in users if (u.get("Policy") or {}).get("EnableLiveTvAccess", True)]


def blocked_tags(policy_tags: list, own_tag: str, tags_in_use: set) -> list:
    """A person's BlockedTags: their own blocked tags kept, Tentacle's set to
    every tuner tag but their own."""
    keep = [t for t in (policy_tags or []) if not str(t).lower().startswith(L.TAG_PREFIX)]
    return keep + sorted(t for t in tags_in_use if t != own_tag)


def own_tag_of(db, user_id: str) -> str:
    row = L.lineup_row(db, user_id)
    return L.user_tag(user_id) if (row and row.synced_own_list and row.tuner_url) else L.ALL_TAG


def apply_policies(db, jf: Jellyfin, users: list, tags_in_use: set) -> int:
    """Set everyone's blocked tags; returns how many policies changed."""
    changed = 0
    for u in users:
        policy = u.get("Policy") or {}
        want = blocked_tags(policy.get("BlockedTags") or [], own_tag_of(db, u["Id"]), tags_in_use)
        if list(policy.get("BlockedTags") or []) == want:
            continue
        policy = {**policy, "BlockedTags": want}
        jf.post(f"/Users/{u['Id']}/Policy", policy)
        u["Policy"] = policy
        changed += 1
    return changed


# ── Channels in Jellyfin ────────────────────────────────────────────────────

_ITEM_FIELDS = "Tags,Genres,Studios,People,ProviderIds,Overview,OriginalTitle,Taglines"


def _items(jf: Jellyfin, admin_id: str, ids: list) -> list:
    """Items by id, asked without a user: an admin's own blocked tags (every
    other person's channels) would hide them otherwise."""
    out = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        res = jf.get("/Items", ids=",".join(chunk), fields=_ITEM_FIELDS,
                     enableUserData="false", enableImages="false")
        out.extend((res or {}).get("Items") or [])
    return out


def tag_channels(jf: Jellyfin, admin_id: str, item_ids: list, tag: str) -> int:
    """Give every channel of a tuner its tag (once); returns how many."""
    from services.jellyfin import _item_update_payload
    done = 0
    for item in _items(jf, admin_id, item_ids):
        tags = list(item.get("Tags") or [])
        if tag in tags:
            continue
        tags = [t for t in tags if not str(t).lower().startswith(L.TAG_PREFIX)] + [tag]
        jf.post(f"/Items/{item['Id']}", _item_update_payload(item, Tags=tags))
        done += 1
    return done


def tags_on_channels(jf: Jellyfin) -> set:
    """Tentacle's tuner tags on the channels Jellyfin has now (asked without
    a user, so nothing is hidden): a tag stays blocked while any channel
    still carries it, e.g. a removed tuner's until Jellyfin drops them."""
    res = jf.get("/Items", includeItemTypes="LiveTvChannel", recursive="true", fields="Tags",
                 enableImages="false", enableUserData="false") or {}
    return {str(t).lower() for i in res.get("Items") or [] for t in i.get("Tags") or []
            if str(t).lower().startswith(L.TAG_PREFIX)}


def favourite_ids(jf: Jellyfin, user_id: str) -> set:
    res = jf.get("/LiveTv/Channels", userId=user_id, isFavorite="true", limit=5000,
                 enableImages="false", enableUserData="false")
    return {i["Id"].replace("-", "") for i in (res or {}).get("Items") or []}


def set_favourite(jf: Jellyfin, user_id: str, item_id: str, on: bool) -> None:
    if on:
        jf.post(f"/UserFavoriteItems/{item_id}", userId=user_id)
    else:
        jf.delete(f"/UserFavoriteItems/{item_id}", userId=user_id)


def carry_favourites(jf: Jellyfin, user_id: str, from_ids: dict, to_ids: dict,
                     known_favs: Optional[set] = None) -> int:
    """Favourites a person had on one tuner's channels, set on the same
    channels of another tuner. from_ids / to_ids: item id -> pool key."""
    favs = known_favs if known_favs is not None else favourite_ids(jf, user_id)
    keys = {from_ids[i] for i in favs if i in from_ids}
    if not keys:
        return 0
    by_key = {k: i for i, k in to_ids.items()}
    carried = 0
    for key in keys:
        target = by_key.get(key)
        if target and target not in favs:
            try:
                set_favourite(jf, user_id, target, True)
                carried += 1
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 404:
                    raise
                logger.warning(f"[LiveTV] Favourite not carried: channel {target} isn't in Jellyfin")
    return carried


def _channel_id(timer: dict) -> str:
    return (timer.get("ChannelId") or "").replace("-", "")


def move_schedules(jf: Jellyfin, from_ids: dict, to_ids: dict) -> int:
    """Recording schedules on one tuner's channels, made again on the same
    channels of another tuner (then the old one is cancelled)."""
    by_key = {k: i for i, k in to_ids.items()}
    moved = 0
    for st in (jf.get("/LiveTv/SeriesTimers") or {}).get("Items") or []:
        key = from_ids.get(_channel_id(st))
        target = by_key.get(key) if key else None
        if not target:
            continue
        # The next showing of the same title (Jellyfin's searchTerm matches
        # loosely: "Hour 5" finds "Hour 2").
        progs = [p for p in (jf.get("/LiveTv/Programs", channelIds=target, hasAired="false",
                                    limit=1000) or {}).get("Items") or []
                 if (p.get("Name") or "") == (st.get("Name") or "")]
        if not progs:
            logger.warning(f"[LiveTV] Recording schedule '{st.get('Name')}': no programme of that name "
                           f"on the new channel yet, kept on the old one")
            continue
        new = jf.get("/LiveTv/Timers/Defaults", programId=progs[0]["Id"]) or {}
        for k in ("RecordAnyChannel", "RecordAnyTime", "RecordNewOnly", "Days", "KeepUpTo", "KeepUntil",
                  "PrePaddingSeconds", "PostPaddingSeconds", "IsPrePaddingRequired",
                  "IsPostPaddingRequired", "SkipEpisodesInLibrary", "Priority"):
            if k in st:
                new[k] = st[k]
        jf.post("/LiveTv/SeriesTimers", new)
        jf.delete(f"/LiveTv/SeriesTimers/{st['Id']}")
        moved += 1
    for t in (jf.get("/LiveTv/Timers", isScheduled="true") or {}).get("Items") or []:
        if t.get("SeriesTimerId"):
            continue   # made again with its series schedule
        key = from_ids.get(_channel_id(t))
        target = by_key.get(key) if key else None
        if not target:
            continue
        progs = (jf.get("/LiveTv/Programs", channelIds=target, minStartDate=t.get("StartDate"),
                        maxStartDate=t.get("StartDate"), limit=1) or {}).get("Items") or []
        if not progs:
            logger.warning(f"[LiveTV] Recording '{t.get('Name')}': not in the new channel's guide yet, kept")
            continue
        new = jf.get("/LiveTv/Timers/Defaults", programId=progs[0]["Id"]) or {}
        for k in ("PrePaddingSeconds", "PostPaddingSeconds", "IsPrePaddingRequired", "IsPostPaddingRequired",
                  "KeepUntil"):
            if k in t:
                new[k] = t[k]
        jf.post("/LiveTv/Timers", new)
        jf.delete(f"/LiveTv/Timers/{t['Id']}")
        moved += 1
    return moved


def old_tuner_item_ids(tuner: dict, channels: list) -> dict:
    """item id -> pool key for an old Tentacle tuner's channels."""
    if tuner.get("Type") == "hdhomerun":
        return {L.channel_item_id(L.hdhr_external_id(c["xmltv_id"])): c["key"] for c in channels}
    url = tuner.get("Url") or ""
    base = _base_of(url)
    return {L.m3u_channel_item_id(url, base + c["stream_path"]): c["key"] for c in channels}


def ensure_guide(jf: Jellyfin, cfg: dict, base: str) -> bool:
    """Tentacle's TV guide (XMLTV) as a guide source for every tuner, added
    when Jellyfin has none on Tentacle yet. Returns whether it was added."""
    for lp in cfg.get("ListingProviders") or []:
        if lp.get("Type") == "xmltv" and "xmltv.xml" in (lp.get("Path") or "").lower():
            return False
    jf.post("/LiveTv/ListingProviders", {
        "Type": "xmltv", "Path": base + "/api/live/xmltv.xml", "EnableAllTuners": True,
        "EnabledTuners": [],
    }, validateListings="false", validateLogin="false")
    logger.info("[LiveTV] Added Tentacle's TV guide to Jellyfin")
    return True


# ── Refreshing the guide ────────────────────────────────────────────────────

def _guide_task(jf: Jellyfin) -> Optional[dict]:
    return next((t for t in jf.get("/ScheduledTasks") or [] if t.get("Key") == "RefreshGuide"), None)


def _finished_after(task: Optional[dict], since: datetime) -> Optional[str]:
    """The status of the guide refresh's last run if it ended after `since`."""
    last = (task or {}).get("LastExecutionResult") or {}
    end = (last.get("EndTimeUtc") or "")[:19]
    try:
        ended = datetime.fromisoformat(end)
    except ValueError:
        return None
    return last.get("Status") if ended >= since else None


def refresh_and_wait(db, jf: Jellyfin) -> bool:
    """Have Jellyfin read every tuner and the guide again, and wait until a
    refresh started after now has completed. Jellyfin cancels a running
    refresh and queues a new one whenever a tuner or guide source is saved,
    so "not running" alone isn't "done"."""
    from services.jellyfin_guide import refresh_jellyfin_guide
    since = datetime.utcnow().replace(microsecond=0)
    refresh_jellyfin_guide(jf.url, jf.headers["X-Emby-Token"], then_tag=False)
    deadline = time.monotonic() + GUIDE_WAIT_SECONDS
    idle_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        task = _guide_task(jf)
        if task is None:
            return False
        if task.get("State") == "Running":
            idle_since = time.monotonic()
            continue
        if _finished_after(task, since) == "Completed":
            return True
        if time.monotonic() - idle_since > 60:
            # Cancelled and not queued again (or never started): ask once more.
            idle_since = time.monotonic()
            jf.post(f"/ScheduledTasks/Running/{task['Id']}")
    logger.warning("[LiveTV] Jellyfin's guide refresh is still running; tagging what is there")
    return False


def wait_for_guide(jf: Jellyfin) -> bool:
    """Wait while Jellyfin's guide refresh runs (up to GUIDE_WAIT_SECONDS)."""
    deadline = time.monotonic() + GUIDE_WAIT_SECONDS
    while time.monotonic() < deadline:
        task = _guide_task(jf)
        if not task or task.get("State") != "Running":
            return True
        time.sleep(_POLL_SECONDS)
    logger.warning("[LiveTV] Jellyfin's guide refresh is still running; tagging what is there")
    return False


# ── Backup ──────────────────────────────────────────────────────────────────

def backup(jf: Jellyfin, cfg: dict, users: list, reason: str) -> Optional[str]:
    """What Jellyfin had before a sync changes tuners or moves people: kept
    as JSON in /data/livetv-backups (one file per change, never removed)."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        data = {
            "reason": reason,
            "at": datetime.utcnow().isoformat() + "Z",
            "tuners": cfg.get("TunerHosts") or [],
            "listing_providers": [{k: v for k, v in lp.items() if k not in ("Password", "Username")}
                                  for lp in cfg.get("ListingProviders") or []],
            "users": [],
            "series_timers": (jf.get("/LiveTv/SeriesTimers") or {}).get("Items") or [],
            "timers": (jf.get("/LiveTv/Timers") or {}).get("Items") or [],
        }
        for u in users:
            fav = jf.get("/LiveTv/Channels", userId=u["Id"], isFavorite="true", limit=5000,
                         enableImages="false") or {}
            data["users"].append({
                "id": u["Id"], "name": u.get("Name"),
                "blocked_tags": (u.get("Policy") or {}).get("BlockedTags") or [],
                "favourite_channels": [{"id": i["Id"], "name": i.get("Name"), "number": i.get("Number")}
                                       for i in fav.get("Items") or []],
            })
        path = os.path.join(BACKUP_DIR, f"livetv-{datetime.utcnow():%Y%m%d-%H%M%S}.json")
        with open(path, "w") as f:
            json.dump(data, f, indent=1)
        return path
    except Exception as e:   # never stops the sync: Jellyfin's own config backup still exists
        logger.warning(f"[LiveTV] Could not save the Live TV backup: {e}")
        return None


# ── The sync ────────────────────────────────────────────────────────────────

status = {"running": False, "last_at": None, "last_ok": None, "last_message": "", "tuners": 0}


def _tuner_body(name: str, url: str, count: int, template: Optional[dict]) -> dict:
    body = {"Type": "m3u", "Url": url, "FriendlyName": name, "TunerCount": count,
            "ImportFavoritesOnly": False, "Source": "", "UserAgent": ""}
    for k in _CARRIED_TUNER_SETTINGS:
        body[k] = (template or {}).get(k, _NEW_TUNER_DEFAULTS[k])
    return body


def sync(db, refresh: bool = False) -> dict:
    """Bring Jellyfin's Live TV in line with everyone's choices (see the
    module docstring). Returns what it did."""
    jf = jellyfin_for(db)
    done = {"added": 0, "removed": 0, "tagged": 0, "policies": 0, "favourites": 0,
            "schedules": 0, "refreshed": False, "backup": None}
    if jf is None:
        return {**done, "skipped": "Jellyfin is not connected"}
    channels = L.pool(db)
    cfg = jf.get("/System/Configuration/livetv") or {}
    tuners = cfg.get("TunerHosts") or []
    olds = old_tuners(db, cfg)
    ours = [t for t in tuners if is_tentacle_tuner(t)]
    if not channels and not ours:
        return {**done, "skipped": "no Live TV channels"}

    users = jf.get("/Users") or []
    people = live_users(users)
    admin_id = next((u["Id"] for u in users if (u.get("Policy") or {}).get("IsAdministrator")), "")
    base = tuner_base_address(db, jf, cfg)
    if not base:
        return {**done, "skipped": "Tentacle's address is not known (Settings → Services)"}

    # What should be there
    all_url = all_tuner_url(db, base)
    count = 0
    try:
        from routers.livetv import _hdhr_tuner_count
        count = _hdhr_tuner_count(db)
    except Exception:
        count = 0
    want = {}          # url -> (name, tag, user)
    rows = {}
    for u in people:
        row = L.lineup_row(db, u["Id"])
        rows[u["Id"]] = row
        if row and row.own_list:
            if not row.tuner_url:
                row.tuner_url = base + L.user_tuner_path(u["Id"])
                db.commit()
            want[row.tuner_url] = (f"Tentacle · {u.get('Name')}", L.user_tag(u["Id"]), u)
    if any(not (rows.get(u["Id"]) and rows[u["Id"]].own_list) for u in people) or olds:
        want[all_url] = ("Tentacle · All channels", L.ALL_TAG, None)

    have = {t.get("Url"): t for t in ours}
    to_add = [url for url in want if url not in have]
    to_remove = [t for url, t in have.items() if url not in want]
    switching = [u for u in people if rows.get(u["Id"]) is not None
                 and bool(rows[u["Id"]].own_list) != bool(rows[u["Id"]].synced_own_list)]
    if to_add or to_remove or olds or switching:
        done["backup"] = backup(jf, cfg, users, "before tuner changes")

    # Favourites on the old tuner and the "All channels" one, read before
    # anything moves (the old tuner's channels disappear once it is removed).
    old_favs = {}
    if olds:
        for u in people:
            old_favs[u["Id"]] = favourite_ids(jf, u["Id"])

    template = olds[0] if olds else (ours[0] if ours else None)
    for url in to_add:
        name, tag, _ = want[url]
        jf.post("/LiveTv/TunerHosts", _tuner_body(name, url, count, template))
        done["added"] += 1
        logger.info(f"[LiveTV] Added Jellyfin tuner '{name}'")

    done["guide_added"] = ensure_guide(jf, cfg, base)

    tags_in_use = {L.tag_for_tuner_url(url) for url in list(have) + list(want)} | tags_on_channels(jf)
    tags_in_use.discard(None)
    done["policies"] += apply_policies(db, jf, people, tags_in_use)

    if to_add or olds or switching or refresh or done["guide_added"]:
        refresh_and_wait(db, jf)
        done["refreshed"] = True
    else:
        wait_for_guide(jf)   # a refresh someone else started: tag what it creates

    # Tag every tuner's channels
    entries_for = {}
    for url, (name, tag, user) in want.items():
        entries = L.lineup_for(db, user["Id"] if user else None, channels)
        entries_for[url] = entries
        if admin_id:
            done["tagged"] += tag_channels(jf, admin_id, list(L.item_ids_for(url, entries)), tag)

    # People who switched between "All channels" and their own list
    for u in switching:
        row = rows[u["Id"]]
        mine = L.item_ids_for(row.tuner_url, L.own_lineup(db, u["Id"], channels)) if row.tuner_url else {}
        everyone = L.item_ids_for(all_url, channels)
        src, dst = (everyone, mine) if row.own_list else (mine, everyone)
        if dst and admin_id and not _items(jf, admin_id, list(dst)):
            # Jellyfin hasn't made the channels yet: the next sync carries on.
            logger.warning(f"[LiveTV] {u.get('Name')}'s channels aren't in Jellyfin yet; trying again later")
            continue
        # Their favourites are read while they still see the old channels,
        # then they see the new ones: Jellyfin refuses a favourite on a
        # channel the person can't see (404).
        favs = favourite_ids(jf, u["Id"])
        row.synced_own_list = bool(row.own_list)
        db.commit()
        done["policies"] += apply_policies(db, jf, [u], tags_in_use)
        if src and dst:
            done["favourites"] += carry_favourites(jf, u["Id"], src, dst, favs)

    # Off the old single tuner: everyone's favourites and the schedules. Not
    # before the new channels are in Jellyfin: the old one stays until then.
    all_ids = L.item_ids_for(all_url, channels)
    if olds and admin_id and not _items(jf, admin_id, list(all_ids)[:100]):
        logger.warning("[LiveTV] The shared tuner's channels aren't in Jellyfin yet; "
                       "the old tuner stays until the next sync")
        olds = []
    if olds:
        for old in olds:
            from_ids = old_tuner_item_ids(old, channels)
            for u in people:
                dst = tuner_url_for(db, u["Id"])
                dst_ids = L.item_ids_for(dst, entries_for.get(dst) or [])
                done["favourites"] += carry_favourites(jf, u["Id"], from_ids, dst_ids, old_favs.get(u["Id"]))
            # Schedules have no owner in Jellyfin: they go to the shared tuner.
            done["schedules"] += move_schedules(jf, from_ids, all_ids)
            jf.delete("/LiveTv/TunerHosts", id=old["Id"])
            done["removed"] += 1
            logger.info(f"[LiveTV] Moved everyone off the old {old.get('Type')} tuner and removed it")
        set_setting(db, MIGRATED_KEY, datetime.utcnow().isoformat())
        db.commit()

    for t in to_remove:
        jf.delete("/LiveTv/TunerHosts", id=t["Id"])
        done["removed"] += 1
        logger.info(f"[LiveTV] Removed Jellyfin tuner '{t.get('FriendlyName') or t.get('Url')}'")

    # People Jellyfin no longer has
    known = {L.norm_user_id(u["Id"]) for u in users}
    for row in db.query(LiveUserLineup).all():
        if row.jellyfin_user_id not in known:
            L.forget_user(db, row.jellyfin_user_id)

    if done["removed"]:
        # Jellyfin drops a removed tuner's channels at its next guide refresh;
        # until then they stay blocked (their tag is still on them).
        refresh_and_wait(db, jf)
    tags_in_use = {L.tag_for_tuner_url(url) for url in want} | tags_on_channels(jf)
    tags_in_use.discard(None)
    done["policies"] += apply_policies(db, jf, people, tags_in_use)
    return done


# ── Running it in the background ────────────────────────────────────────────

_lock = threading.Lock()
# Off until the server starts it (main.setup_scheduler): a test that touches a
# route never starts a sync in the background.
_enabled = False
_pending = {"timer": None, "refresh": False, "reasons": []}
_pending_lock = threading.Lock()
DEBOUNCE_SECONDS = 15


def schedule_sync(reason: str, refresh: bool = False, delay: Optional[float] = None) -> None:
    """Sync soon; changes within a few seconds of each other become one sync."""
    if not _enabled:
        return
    with _pending_lock:
        _pending["refresh"] = _pending["refresh"] or refresh
        _pending["reasons"].append(reason)
        if _pending["timer"] is not None:
            _pending["timer"].cancel()
        t = threading.Timer(DEBOUNCE_SECONDS if delay is None else delay, _run_pending)
        t.daemon = True
        _pending["timer"] = t
        t.start()


def enable() -> None:
    global _enabled
    _enabled = True


def _run_pending() -> None:
    with _pending_lock:
        refresh, reasons = _pending["refresh"], _pending["reasons"]
        _pending.update(timer=None, refresh=False, reasons=[])
    run_sync(", ".join(dict.fromkeys(reasons)) or "scheduled", refresh)


def run_sync(reason: str = "scheduled", refresh: bool = False) -> dict:
    """One sync, never two at once; logs what it did and never raises."""
    with _lock:
        status["running"] = True
        db = SessionLocal()
        try:
            done = sync(db, refresh=refresh)
            status.update(last_at=datetime.utcnow().isoformat() + "Z", last_ok=True)
            if done.get("skipped"):
                status["last_message"] = done["skipped"]
            else:
                status["last_message"] = ""
                if any(done[k] for k in ("added", "removed", "favourites", "schedules")):
                    log_activity(db, "livetv_setup",
                                 f"Live TV set up in Jellyfin ({reason}): {done['added']} tuner(s) added, "
                                 f"{done['removed']} removed, {done['favourites']} favourite(s) and "
                                 f"{done['schedules']} recording schedule(s) carried over", done)
            logger.info(f"[LiveTV] Jellyfin sync ({reason}): {done}")
            return done
        except Exception as e:
            status.update(last_at=datetime.utcnow().isoformat() + "Z", last_ok=False,
                          last_message=str(e))
            logger.warning(f"[LiveTV] Jellyfin sync ({reason}) failed: {e}")
            return {"error": str(e)}
        finally:
            status["running"] = False
            try:
                set_setting(db, STATUS_KEY, json.dumps({k: status[k] for k in ("last_at", "last_ok", "last_message")}))
                db.commit()
            except Exception:
                db.rollback()
            db.close()


def last_status(db) -> dict:
    """The last sync's outcome, also after a restart."""
    out = dict(status)
    if not out.get("last_at"):
        try:
            out.update(json.loads(get_setting(db, STATUS_KEY, "") or "{}"))
        except ValueError:
            pass
    return out

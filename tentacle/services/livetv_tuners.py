"""Tentacle sets up Live TV in Jellyfin itself: one M3U tuner for the
household's lineup (services/livetv_lineups.py) and Tentacle's guide. What
is personal is each person's favourites (Jellyfin's own, per person), not
the tuner.

Kids profiles see only the kids channels (proven on Jellyfin 10.11.8, see
docs/agents/jellyfin-notes.md "Live TV per person"):
  - every lineup channel carries one tag: `tentacle-livetv-kids` on the
    household's kids channels, `tentacle-livetv-all` on the rest, set here
    after each guide refresh; the plugin gives a channel Jellyfin creates
    `-all` at once (and keeps `-kids` on one that has it);
  - a kid's policy blocks `tentacle-livetv-all` (BlockedTags);
  - the plugin copies the channel's tag onto its programmes, so programme
    lists ("On now", search) are filtered in Jellyfin's database too.

One sync runs at a time, in a thread, a few seconds after the last change
(changes in a row become one sync). A sync:
  1. adds the lineup's tuner when Jellyfin hasn't got it (address kept:
     Jellyfin keys every channel on it) and the guide source;
  2. has Jellyfin read the tuner and the guide (RefreshGuide) when anything
     changed, and waits for it;
  3. tags the channels and sets everyone's blocked tags;
  4. moves everyone onto the lineup with their favourites and the
     recording schedules: off the per-person tuners of before (v1.11-1.12's
     "My own list", Oct 2026's own tuners), off Tentacle's old HDHomeRun (or
     hand-made M3U) tuner, and off channels whose pool key changed
     (L.final_aliases: "c123" folded into "n7" has a new stream address, so
     a new Jellyfin id);
  5. removes the tuners of before once everyone is across.
Before it changes tuners or moves anything it saves what was there (tuners,
everyone's favourite channels, schedules, policies, Tentacle's lists) to
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
    LiveChannel, LiveNetwork, LiveUserLineup, SessionLocal, get_setting, log_activity, set_setting,
    tentacle_address,
)
from services import livetv_lineups as L
from services import livetv_locations as P
from services.youtube import follows as F

logger = logging.getLogger(__name__)

TUNER_BASE_KEY = "livetv_tuner_base"         # the address every new tuner gets, kept once chosen
ALL_TUNER_URL_KEY = "livetv_all_tuner_url"   # the lineup tuner's address, kept as first given
MIGRATED_KEY = "livetv_per_person_migrated"  # the old single tuner was moved off
TUNER_NAME = "Tentacle · Live TV"
STATUS_KEY = "livetv_setup_status"           # the last sync's outcome (JSON)
BACKUP_DIR = os.environ.get("TENTACLE_LIVETV_BACKUP_DIR", "/data/livetv-backups")

PLUGIN_ID = "b7e3f1a92c4d48569a1b8def23456789"
GUIDE_WAIT_SECONDS = 90 * 60
_POLL_SECONDS = 5

# Settings a Tentacle tuner takes from the HDHomeRun tuner it replaces, so
# playback behaves as it did (the admin may have tuned them in Jellyfin).
# Never "Read at native frame rate": it makes Jellyfin's ffmpeg read a channel
# no faster than it plays, so the first HLS segment of a live channel took
# 3-11 s instead of well under one (providers send their buffer at once).
_CARRIED_TUNER_SETTINGS = (
    "AllowHWTranscoding", "AllowFmp4TranscodingContainer", "AllowStreamSharing",
    "FallbackMaxStreamingBitrate", "EnableStreamLooping", "IgnoreDts",
)
# Set once the lineup tuner's "Read at native frame rate" was switched off
# (a tuner made by v1.13 or earlier carried it over); later the admin's call.
NATIVE_RATE_KEY = "livetv_tuner_native_rate_off"
_NEW_TUNER_DEFAULTS = {
    "AllowHWTranscoding": True, "AllowFmp4TranscodingContainer": False, "AllowStreamSharing": True,
    "FallbackMaxStreamingBitrate": 30000000, "EnableStreamLooping": False, "IgnoreDts": True,
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
    """The lineup tuner's address: kept once given (a v1.11-1.12 server's
    "All channels" tuner is the lineup's), else made from `base`."""
    url = (get_setting(db, ALL_TUNER_URL_KEY, "") or "").strip()
    if not url and base:
        url = base.rstrip("/") + L.ALL_TUNER_PATH
        set_setting(db, ALL_TUNER_URL_KEY, url)
        db.commit()
    return url


# ── Who sees what ───────────────────────────────────────────────────────────

def live_users(users: list) -> list:
    return [u for u in users if (u.get("Policy") or {}).get("EnableLiveTvAccess", True)]


def kids_user_ids(db) -> set:
    from models.database import TentacleUser
    return {L.norm_user_id(t.jellyfin_user_id) for t in
            db.query(TentacleUser).filter(TentacleUser.kids_level.isnot(None)).all()}


_KEEP = object()


def _ours(tag) -> bool:
    t = str(tag).lower()
    return t.startswith(L.TAG_PREFIX) or t.startswith(P.NOT_LOCAL_PREFIX)


def _hide_tag(tag) -> bool:
    """A tag that hides a channel from some people, kept on the channel next
    to its tuner tag: a place's (tentacle-notlocal-*) or a YouTube channel's
    (tentacle-youtube-<id>, services/youtube/follows.py)."""
    t = str(tag).lower()
    return t.startswith(P.NOT_LOCAL_PREFIX) or t.startswith(F.LIVE_TAG_PREFIX)


def blocked_tags(policy_tags: list, kid: bool, tags_in_use: set, place_tag=_KEEP, youtube=_KEEP) -> list:
    """A person's BlockedTags: their own blocked tags kept; Tentacle's are the
    per-person tuners of before still in Jellyfin, for a kid every channel
    that isn't a kids channel, `place_tag`: the tag of the channels for
    other places than theirs (None: no such channels; default: the one they
    have now is kept), and `youtube`: the tags of the YouTube channels they
    don't follow (default: kept as they are; always last, as
    follows.with_blocked puts them)."""
    keep = [t for t in (policy_tags or []) if not _ours(t) and not F.is_ours(t)]
    ours = {t for t in tags_in_use if t not in (L.ALL_TAG, L.KIDS_TAG)}
    if kid:
        ours.add(L.ALL_TAG)
    if place_tag is _KEEP:
        ours |= {t for t in (policy_tags or []) if str(t).lower().startswith(P.NOT_LOCAL_PREFIX)}
    elif place_tag:
        ours.add(place_tag)
    if youtube is _KEEP:
        youtube = [t for t in (policy_tags or []) if F.is_ours(t)]
    return keep + sorted(ours) + sorted(youtube)


def apply_policies(db, jf: Jellyfin, users: list, tags_in_use: set, place_tags: Optional[dict] = None) -> int:
    """Set everyone's blocked tags; returns how many policies changed.
    `place_tags`: user id -> their place's tag (or None); without it the
    place tags they have stay."""
    kids = kids_user_ids(db)
    yt_tags, yt_follows = F.all_tags(db), F.followers_by_channel(db)
    changed = 0
    for u in users:
        kid = L.norm_user_id(u["Id"]) in kids
        ptag = place_tags.get(L.norm_user_id(u["Id"])) if place_tags is not None else _KEEP
        youtube = F.blocked_for(db, u["Id"], yt_tags, yt_follows)
        policy = u.get("Policy") or {}
        want = blocked_tags(policy.get("BlockedTags") or [], kid, tags_in_use, ptag, youtube)
        if list(policy.get("BlockedTags") or []) == want:
            continue
        with F.policy_lock:
            # The policy as Jellyfin has it now, not as this sync read it (that can
            # be minutes old after a guide refresh): a kids profile or an admin
            # switch set meanwhile must not be put back.
            try:
                fresh = (jf.get(f"/Users/{u['Id']}") or {}).get("Policy") or policy
            except requests.RequestException:
                fresh = policy
            want = blocked_tags(fresh.get("BlockedTags") or [], kid, tags_in_use, ptag, youtube)
            if list(fresh.get("BlockedTags") or []) == want:
                u["Policy"] = fresh
                continue
            policy = {**fresh, "BlockedTags": want}
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


def tag_channels(jf: Jellyfin, admin_id: str, item_ids: list, tag: str, places: Optional[dict] = None) -> int:
    """Give every channel of a tuner its tag (once), and with `places` (item
    id -> the places' tags it carries, services/livetv_lineups.place_tags)
    exactly those; returns how many changed."""
    from services.jellyfin import _item_update_payload
    done = 0
    for item in _items(jf, admin_id, item_ids):
        tags = list(item.get("Tags") or [])
        mine = sorted((places or {}).get(str(item["Id"]).replace("-", ""), ()))
        have = sorted(t for t in tags if _hide_tag(t))
        if tag in tags and (places is None or have == mine):
            continue
        keep_places = have if places is None else mine
        tags = [t for t in tags if not _ours(t) and not _hide_tag(t)] + [tag] + keep_places
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


def channel_ids(jf: Jellyfin) -> set:
    """Every Live TV channel Jellyfin has now (asked without a user)."""
    res = jf.get("/Items", includeItemTypes="LiveTvChannel", recursive="true",
                 enableImages="false", enableUserData="false") or {}
    return {str(i["Id"]).replace("-", "") for i in res.get("Items") or []}


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


def scheduled_one_offs(jf: Jellyfin) -> list:
    """The one-off recordings Jellyfin has scheduled (not a series')."""
    return [t for t in (jf.get("/LiveTv/Timers", isScheduled="true") or {}).get("Items") or []
            if not t.get("SeriesTimerId")]


def move_schedules(jf: Jellyfin, from_ids: dict, to_ids: dict, one_offs: Optional[list] = None) -> int:
    """Recording schedules on one tuner's channels, made again on the same
    channels of another tuner (then the old one is cancelled).

    `one_offs`: the one-off recordings as they were before the guide
    refresh. A refresh deletes a one-off whose programme left the guide
    (Jellyfin's RefreshTimers), which is what happens to a lineup channel
    whose stream address changed; it is made again from what was read."""
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
    for t in scheduled_one_offs(jf) if one_offs is None else one_offs:
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
        try:
            jf.delete(f"/LiveTv/Timers/{t['Id']}")
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise   # 404: the refresh had dropped it already
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

def backup(jf: Jellyfin, cfg: dict, users: list, reason: str, db=None) -> Optional[str]:
    """What Jellyfin had before a sync changes tuners or moves people (and
    everyone's channel lists in Tentacle): kept as JSON in
    /data/livetv-backups (one file per change, never removed)."""
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
            "lists": {},
        }
        if db is not None:
            for row in db.query(LiveUserLineup).all():
                data["lists"][row.jellyfin_user_id] = {
                    "own_list": bool(row.own_list), "synced": row.synced_own_list,
                    "tuner_url": row.tuner_url, "keys": L.stored_keys(db, row.jellyfin_user_id)}
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
            "ImportFavoritesOnly": False, "Source": "", "UserAgent": "", "ReadAtNativeFramerate": False}
    for k in _CARRIED_TUNER_SETTINGS:
        body[k] = (template or {}).get(k, _NEW_TUNER_DEFAULTS[k])
    return body


def tuner_count(db) -> int:
    """Streams at once Jellyfin allows on the lineup's tuner: Tentacle's
    overall stream limit (every provider's "Streams at once" together), as
    the HDHomeRun device reports it. 0 when it can't be read (left as is)."""
    try:
        from routers.livetv import _hdhr_tuner_count
        return _hdhr_tuner_count(db)
    except Exception:
        logger.warning("[LiveTV] Could not work out the tuner count", exc_info=True)
        return 0


def _scheduled_channel_ids(jf: Jellyfin) -> set:
    ids = set()
    for path, params in (("/LiveTv/SeriesTimers", {}), ("/LiveTv/Timers", {"isScheduled": "true"})):
        for t in (jf.get(path, **params) or {}).get("Items") or []:
            ids.add(_channel_id(t))
    return ids


def _kids_keys(db) -> set:
    """The kids channels, and the YouTube channels a kid follows (an admin
    added them for them; other kids block them unless they follow them too)."""
    from services import family
    return set(family.kids_channels(db)) | {f"y{i}" for i in F.kids_channel_ids(db)}


def tag_lineup(jf: Jellyfin, admin_id: str, url: str, entries: list, kids: set,
               places: Optional[dict] = None) -> int:
    """Every lineup channel's tag (kids channels -kids, the rest -all), and
    the places' tags (key -> tags, L.place_tags: a channel for one place is
    hidden from people elsewhere)."""
    by_tag = {}
    by_id = {}
    for item_id, key in L.item_ids_for(url, entries).items():
        by_tag.setdefault(L.KIDS_TAG if L.base_key(key) in kids else L.ALL_TAG, []).append(item_id)
        by_id[item_id] = set((places or {}).get(key, set()))
        if key[:1] == "y" and key[1:].isdigit():
            # Only its followers see a YouTube channel (services/youtube/follows.py).
            by_id[item_id].add(F.live_tag(int(key[1:])))
    return sum(tag_channels(jf, admin_id, ids, tag, by_id) for tag, ids in by_tag.items())


PLACE_KEYS_KEY = "livetv_place_keys"   # the places' entries the tuner carried (JSON list of keys)


LOGOS_KEY = "livetv_lineup_logos"          # key -> the logo the tuner last gave Jellyfin (JSON)
SEPARATORS_KEY = "livetv_hidden_separators"  # switched-on separator rows the tuner leaves out (JSON list)


def _json_setting(db, key: str, default):
    try:
        value = json.loads(get_setting(db, key, "") or "null")
    except ValueError:
        value = None
    return default if value is None else value


def stale_logos(db, entries: list) -> set:
    """Lineup keys whose logo changed since the tuner last gave it to
    Jellyfin. Jellyfin sets a channel's image from the M3U's tvg-logo only
    while the channel has none (GuideManager), so a new logo needs the old
    image removed before the guide refresh. Before the first record: the
    channels and networks with the admin's own logo."""
    known = _json_setting(db, LOGOS_KEY, None)
    if not isinstance(known, dict):
        mine = {f"c{i}" for (i,) in db.query(LiveChannel.id).filter(LiveChannel.custom_logo_url.isnot(None))}
        mine |= {f"n{i}" for (i,) in db.query(LiveNetwork.id).filter(LiveNetwork.custom_logo_url.isnot(None))}
        return {e["key"] for e in entries if L.base_key(e["key"]) in mine}
    from services.livetv_catalog import _logo_base
    # Compared without the query: panels change their cache busters.
    return {e["key"] for e in entries
            if e["key"] in known and _logo_base(known[e["key"]] or "") != _logo_base(e.get("logo_url") or "")}


def drop_images(jf: "Jellyfin", item_ids: list) -> int:
    """Remove the channels' images; the next guide refresh sets the M3U's."""
    done = 0
    for item_id in item_ids:
        try:
            jf.delete(f"/Items/{item_id}/Images/Primary")
            done += 1
        except requests.RequestException as e:   # not in Jellyfin yet: it gets the new one
            logger.info(f"[LiveTV] Couldn't remove channel image {item_id}: {e}")
    return done


def _carried_place_keys(db) -> set:
    try:
        return {str(k) for k in json.loads(get_setting(db, PLACE_KEYS_KEY, "") or "[]")}
    except ValueError:
        return set()


def place_moves(favs: set, mine: dict, gone: dict, entries: list, place: Optional[str]) -> list:
    """Favourites someone at `place` has on another place's channel of a
    network (or on its default where their place has its own): (from item
    id, to item id) for each, to their place's channel of it. mine: item id
    -> key of the lineup; gone: item id -> key of a place's channel the
    lineup no longer carries."""
    view = {L.base_key(e["key"]): e["key"] for e in L.view_for(entries, place)}
    by_key = {k: i for i, k in mine.items()}
    out = []
    for fid in favs:
        key = mine.get(fid) or gone.get(fid)
        if key is None:
            continue
        want = view.get(L.base_key(key))
        if want is None or want == key or want not in by_key:
            continue
        out.append((fid, by_key[want]))
    return out


def move_place_favourites(jf: Jellyfin, user_id: str, moves: list, favs: set, mine: dict) -> int:
    """Star the place's channel, unstar the other (if Jellyfin still has it)."""
    done = 0
    for old, new in moves:
        try:
            if new not in favs:
                set_favourite(jf, user_id, new, True)
                favs.add(new)
                done += 1
            if old in mine:
                set_favourite(jf, user_id, old, False)
            favs.discard(old)
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
            logger.warning(f"[LiveTV] Favourite not moved to the person's place: channel {new} isn't in Jellyfin")
    return done


def sync(db, refresh: bool = False) -> dict:
    """Bring Jellyfin's Live TV in line with the lineup (see the module
    docstring). Returns what it did."""
    jf = jellyfin_for(db)
    done = {"added": 0, "removed": 0, "tagged": 0, "policies": 0, "favourites": 0,
            "place_favourites": 0, "schedules": 0, "refreshed": False, "backup": None}
    if jf is None:
        return {**done, "skipped": "Jellyfin is not connected"}
    channels = L.pool(db)
    # Where everyone watches from: networks whose variant for a place isn't
    # the default get a channel for that place, hidden from everyone else.
    place_of = P.by_user(db)
    home = P.household(db)
    places_used = {p for p in set(place_of.values()) | {home} if p}
    entries = L.lineup(db, channels, places=places_used)
    tags_of_places = L.place_tags(entries, places_used)
    place_keys = {e["key"] for e in entries if e.get("place")}
    carried_places = _carried_place_keys(db)
    cfg = jf.get("/System/Configuration/livetv") or {}
    tuners = cfg.get("TunerHosts") or []
    olds = old_tuners(db, cfg)
    ours = [t for t in tuners if is_tentacle_tuner(t)]
    if not entries and not ours:
        return {**done, "skipped": "no Live TV channels"}

    users = jf.get("/Users") or []
    people = live_users(users)
    admin_id = next((u["Id"] for u in users if (u.get("Policy") or {}).get("IsAdministrator")), "")
    base = tuner_base_address(db, jf, cfg)
    if not base:
        return {**done, "skipped": "Tentacle's address is not known (Settings → Services)"}
    url = all_tuner_url(db, base)
    have = {t.get("Url"): t for t in ours}
    tuner = have.get(url)
    personal = [t for u, t in have.items() if u != url]   # per-person tuners of before
    kids = _kids_keys(db)

    # Where favourites and schedules come from: the per-person tuners and
    # the old single tuner (every channel the pool has, under its key now
    # or the one it had), and lineup channels whose key changed.
    mine = L.item_ids_for(url, entries)
    keys = set(mine.values())
    alias = L.final_aliases(db, channels)
    sources = {}     # item id -> current key, for channels that are going
    for t in personal:
        sources.update(L.item_ids_for(t.get("Url") or "", channels))
        sources.update(L.alias_item_ids_for(t.get("Url") or "", alias))
    for old in olds:
        sources.update(old_tuner_item_ids(old, channels))
        if old.get("Type") == "m3u":
            sources.update(L.alias_item_ids_for(old.get("Url") or "", alias))
    if alias and tuner is not None:
        in_jf = channel_ids(jf)
        sources.update({i: k for i, k in L.alias_item_ids_for(url, alias, keys).items() if i in in_jf})
    sources = {i: k for i, k in sources.items() if k in keys and i not in mine}
    # Places' channels the tuner carried that it no longer does (nobody there
    # any more, or the place's variant is the default now): favourites on
    # them go to the person's own channel of the network.
    gone_places = {}
    for k in carried_places - place_keys:
        path = L.stream_path_of_key(k)
        if path:
            gone_places[L.m3u_channel_item_id(url, L.tuner_base(url) + path)] = k
    places_change = place_keys != carried_places
    by_place = bool(place_keys or carried_places)

    moving = bool(personal or olds or sources or tuner is None)
    if moving:
        done["backup"] = backup(jf, cfg, users, "before Live TV tuner changes", db)
    # Favourites, read before anything moves (Jellyfin refuses a favourite on
    # a channel the person can't see, and the old channels go at the next
    # refresh).
    pre_favs = ({u["Id"]: favourite_ids(jf, u["Id"]) for u in people}
                if (personal or olds or sources or by_place) else {})
    # One-off recordings too: the guide refresh below deletes those whose
    # channel left a tuner (a lineup channel with a new key does at once).
    pre_one_offs = scheduled_one_offs(jf) if sources else None

    count = tuner_count(db)
    if tuner is None:
        template = olds[0] if olds else (personal[0] if personal else None)
        jf.post("/LiveTv/TunerHosts", _tuner_body(TUNER_NAME, url, count, template))
        done["added"] += 1
        logger.info(f"[LiveTV] Added Jellyfin tuner '{TUNER_NAME}'")

    else:
        fixed = {}
        if not get_setting(db, NATIVE_RATE_KEY, ""):
            if tuner.get("ReadAtNativeFramerate"):
                fixed["ReadAtNativeFramerate"] = False
                done["native_rate_off"] = True
                logger.info(f"[LiveTV] Switched off 'Read at native frame rate' on Jellyfin tuner "
                            f"'{TUNER_NAME}': channels start in about a second instead of several")
            set_setting(db, NATIVE_RATE_KEY, datetime.utcnow().isoformat())
            db.commit()
        # Jellyfin refuses a channel once this many of the tuner's streams
        # play: below Tentacle's own limit, a 7th viewer was turned away
        # while the provider carried 8 (hand-set to 8 on 2026-10-22).
        if count and tuner.get("TunerCount") != count:
            fixed["TunerCount"] = count
            done["tuner_count"] = count
            logger.info(f"[LiveTV] Jellyfin tuner '{tuner.get('FriendlyName')}': {tuner.get('TunerCount')} → {count} "
                        f"streams at once (Tentacle's limit)")
        if fixed:
            jf.post("/LiveTv/TunerHosts", {**tuner, **fixed})

    done["guide_added"] = ensure_guide(jf, cfg, base)

    # Separator rows newly left out of the tuner (switched on before, or a
    # server from before they were): one refresh takes them out of Jellyfin.
    from routers.livetv import _hidden_separators
    hidden = sorted(f"c{i}" for i in _hidden_separators(db))
    new_hidden = bool(set(hidden) - set(_json_setting(db, SEPARATORS_KEY, [])))
    # A channel whose logo changed (the admin's, the provider's): its image
    # in Jellyfin goes, the refresh gives it the new one.
    relogo = stale_logos(db, entries) if tuner is not None else set()
    if relogo:
        done["logos"] = drop_images(jf, [i for i, k in mine.items() if k in relogo])

    refreshing = bool(done["added"] or refresh or done["guide_added"] or sources or places_change
                      or new_hidden or relogo)
    if admin_id and tuner is not None and refreshing:
        # Before the refresh too: the plugin gives programmes the tag their
        # channel has while it builds them, so a channel made (or no longer)
        # a kids channel carries its new tag into this refresh's programmes.
        done["tagged"] += tag_lineup(jf, admin_id, url, entries, kids, tags_of_places)
    # Kids see only the kids channels before anything else happens (a guide
    # refresh can take minutes).
    kids_ids = kids_user_ids(db)
    done["policies"] += apply_policies(db, jf, [u for u in people if L.norm_user_id(u["Id"]) in kids_ids],
                                       {L.tag_for_tuner_url(t.get("Url") or "") for t in personal} - {None})

    if refreshing:
        refresh_and_wait(db, jf)
        done["refreshed"] = True
    if done["refreshed"] or tuner is None:
        logos = {e["key"]: e.get("logo_url") or "" for e in entries}
        if logos != _json_setting(db, LOGOS_KEY, None):
            set_setting(db, LOGOS_KEY, json.dumps(logos, sort_keys=True))
        if hidden != _json_setting(db, SEPARATORS_KEY, []):
            set_setting(db, SEPARATORS_KEY, json.dumps(hidden))
        db.commit()
    if not refreshing:
        wait_for_guide(jf)   # a refresh someone else started: tag what it creates

    if admin_id:
        done["tagged"] += tag_lineup(jf, admin_id, url, entries, kids, tags_of_places)
    in_jf = bool(mine) and bool(admin_id) and bool(_items(jf, admin_id, list(mine)[:100]))
    present = channel_ids(jf) if (in_jf and place_keys) else set()
    places_ready = in_jf and all(i in present for i, k in mine.items() if k in place_keys)
    if mine and not in_jf:
        # Jellyfin hasn't made the channels yet: the next sync carries on.
        logger.warning("[LiveTV] The lineup's channels aren't in Jellyfin yet; trying again later")
        personal, olds = [], []

    # Everyone sees the lineup (kids its kids channels), and no longer the
    # tuners of before: their favourites were read above.
    tags_in_use = {L.tag_for_tuner_url(t.get("Url") or "") for t in personal} | tags_on_channels(jf)
    tags_in_use.discard(None)
    # Favourites follow each person's place (a new person starts with someone
    # else's, an admin moved them): while they still see the channels of
    # before, so before their blocked tags change.
    done["place_favourites"] = 0
    if by_place and places_ready:
        for u in people:
            favs = pre_favs.get(u["Id"])
            if not favs:
                continue
            uid = L.norm_user_id(u["Id"])
            moves = place_moves(favs, mine, gone_places, entries, place_of.get(uid) or home)
            if moves:
                done["place_favourites"] += move_place_favourites(jf, u["Id"], moves, set(favs), mine)
    if places_ready or not place_keys:
        ptags = {L.norm_user_id(u["Id"]): (P.not_local_tag(place_of.get(L.norm_user_id(u["Id"])) or home)
                                          if place_keys and (place_of.get(L.norm_user_id(u["Id"])) or home)
                                          else None) for u in people}
    else:
        ptags = None   # the places' channels aren't in Jellyfin yet: nobody's place changes
    done["policies"] += apply_policies(db, jf, people, tags_in_use, ptags)
    if places_ready or not place_keys:
        if place_keys != carried_places:
            set_setting(db, PLACE_KEYS_KEY, json.dumps(sorted(place_keys)))
            db.commit()

    if in_jf:
        for u in people:
            favs = pre_favs.get(u["Id"])
            if favs:
                done["favourites"] += carry_favourites(jf, u["Id"], sources, mine, favs)
        if sources:
            done["schedules"] += move_schedules(jf, sources, mine, pre_one_offs)

    to_remove = list(olds)
    if personal:
        stuck = _scheduled_channel_ids(jf) & {i for t in personal
                                              for i in L.item_ids_for(t.get("Url") or "", channels)}
        if stuck:
            logger.warning(f"[LiveTV] {len(stuck)} recording schedule(s) still on a per-person tuner; "
                           f"it stays until they move")
        else:
            to_remove += personal
    for t in to_remove:
        jf.delete("/LiveTv/TunerHosts", id=t["Id"])
        done["removed"] += 1
        logger.info(f"[LiveTV] Moved everyone onto the lineup and removed Jellyfin tuner "
                    f"'{t.get('FriendlyName') or t.get('Url')}'")
    if olds:
        set_setting(db, MIGRATED_KEY, datetime.utcnow().isoformat())
        db.commit()

    # People Jellyfin no longer has
    known = {L.norm_user_id(u["Id"]) for u in users}
    for row in db.query(LiveUserLineup).all():
        if row.jellyfin_user_id not in known:
            L.forget_user(db, row.jellyfin_user_id)

    if done["removed"]:
        # Jellyfin drops a removed tuner's channels at its next guide refresh;
        # until then they stay blocked (their tag is still on them).
        refresh_and_wait(db, jf)
        tags_in_use = tags_on_channels(jf)
        done["policies"] += apply_policies(db, jf, people, tags_in_use)
    elif places_change and places_ready and done["refreshed"]:
        # A place's channels are new to Jellyfin in this refresh and got their
        # places' tags after it: one more refresh gives their programmes the
        # tags (the plugin copies a channel's), so programme lists ("On now",
        # search) count only one's own place. Who sees what is right already.
        refresh_and_wait(db, jf)
    return done


# ── Running it in the background ────────────────────────────────────────────

_lock = threading.Lock()
# Off until the server starts it (main.setup_scheduler): a test that touches a
# route never starts a sync in the background.
_enabled = False
_pending = {"timer": None, "refresh": False, "reasons": []}
_pending_lock = threading.Lock()
DEBOUNCE_SECONDS = 15
LIMIT_DELAY_SECONDS = 2


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


_count_timer = {"timer": None}


def stream_limit_changed(reason: str) -> None:
    """A provider's "Streams at once" (or the default) changed: Jellyfin's
    tuner count follows within seconds, not at the hourly check. On its own,
    not as a whole sync: a sync after a tuner save runs ~40 s while Jellyfin
    refreshes its guide, and a second change waited behind it."""
    if not _enabled:
        return
    with _pending_lock:
        if _count_timer["timer"] is not None:
            _count_timer["timer"].cancel()
        t = threading.Timer(LIMIT_DELAY_SECONDS, follow_stream_limit, kwargs={"reason": reason})
        t.daemon = True
        _count_timer["timer"] = t
        t.start()


_count_lock = threading.Lock()


def follow_stream_limit(reason: str = "") -> Optional[int]:
    """Set the lineup tuner's TunerCount to Tentacle's limit when it differs
    (the rest of the tuner kept). Returns the count set, None when nothing
    changed; never raises."""
    with _count_lock:
        db = SessionLocal()
        try:
            jf = jellyfin_for(db)
            url = get_setting(db, ALL_TUNER_URL_KEY, "") or ""
            if jf is None or not url:
                return None
            cfg = jf.get("/System/Configuration/livetv") or {}
            tuner = next((t for t in cfg.get("TunerHosts") or [] if is_tentacle_tuner(t) and t.get("Url") == url), None)
            count = tuner_count(db)
            if tuner is None or not count or tuner.get("TunerCount") == count:
                return None
            jf.post("/LiveTv/TunerHosts", {**tuner, "TunerCount": count})
            logger.info(f"[LiveTV] Jellyfin tuner '{tuner.get('FriendlyName')}': {tuner.get('TunerCount')} → {count} "
                        f"streams at once (Tentacle's limit{', ' + reason if reason else ''})")
            return count
        except Exception:
            logger.warning("[LiveTV] Could not set the Jellyfin tuner's count", exc_info=True)
            return None
        finally:
            db.close()


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
            if not done.get("skipped"):
                tell_clients(db)
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


def tell_clients(db) -> bool:
    """Tell every open client that Live TV changed (channels came or went,
    someone's access or kids channels changed): the plugin sends a
    LibraryChanged with a "tentacle-livetv:<version>" marker and open guides
    read their channel list again. Favourites need nothing: Jellyfin tells
    the person's clients itself (UserDataChanged). Never raises."""
    from services import live_updates
    live_updates.publish("livetv", everyone=True)
    jf = jellyfin_for(db)
    if jf is None:
        return False
    try:
        jf.post("/Tentacle/Refresh", scope="livetv", homeVersion=int(time.time() * 1000))
        return True
    except requests.RequestException as e:
        logger.info(f"[LiveTV] Couldn't tell the clients Live TV changed: {e}")
        return False


def last_status(db) -> dict:
    """The last sync's outcome, also after a restart."""
    out = dict(status)
    if not out.get("last_at"):
        try:
            out.update(json.loads(get_setting(db, STATUS_KEY, "") or "{}"))
        except ValueError:
            pass
    return out

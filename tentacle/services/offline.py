"""Titles saved to watch without internet (the Offline page, Nov 2026).

A person saves a movie or an episode on their phone or computer from
Tentacle's Library or from Jellyfin (Download); the Offline page
(static/js/offline.js) keeps the file in the browser's own storage on
Tentacle's address and plays it from there. This module answers what it
asks first: may this person download this title, and what is it.

Jellyfin decides, as for everything a person may watch:

- the person's "Allow media downloading" (EnableContentDownloading, set on
  Server → People as "Can save offline", services/person_access.py);
- the title is one they can open (their libraries, their age limit): it is
  asked for AS them (`userId`), so Jellyfin leaves out what they may not see;
- Jellyfin's own CanDownload for the title, and a recording still being
  recorded is never offered (its file isn't finished).

The video itself is Jellyfin's Download of the title (routers/offline.py
passes it on, byte ranges and all), which for a provider's title is the
real video through Tentacle's relay (the plugin's Download hook,
StrmDownloadFilter): the same link Jellyfin's apps download from.

Answers are kept for CHECK_SECONDS per person and title: a download asks
for its file again after every interruption, and Jellyfin need not be
asked each time.
"""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import TentacleUser
from services import family as _family
from services import livetv_lineups as L
from services.family import FamilyError, dashed

logger = logging.getLogger(__name__)

CHECK_SECONDS = 60.0
_checked: dict = {}     # (jellyfin user id, item id) -> (time, item)
_lock = threading.Lock()

# Words for a person (never Jellyfin's own error text).
NOT_ALLOWED = ("Saving offline isn't turned on for you. Ask whoever runs your server to turn on "
               "“Can save offline” for you.")
NOT_FOUND = "That title isn't available to you."
STILL_RECORDING = "It's still recording. Save it once it has finished."
NOT_VIDEO = "Only movies, shows and episodes can be saved offline."
NO_EPISODES = "There are no episodes here to save."
_FIELDS = "Path,MediaSources,CanDownload,Overview,ProductionYear,RunTimeTicks"


def jellyfin(db):
    return _family.jellyfin(db)


def norm_item_id(item_id: str) -> str:
    """A Jellyfin item id as 32 hex characters, or "" when it isn't one."""
    raw = (item_id or "").replace("-", "").lower()
    return raw if len(raw) == 32 and all(c in "0123456789abcdef" for c in raw) else ""


def forget(user_id: Optional[str] = None) -> None:
    with _lock:
        if user_id is None:
            _checked.clear()
        else:
            uid = L.norm_user_id(user_id)
            for k in [k for k in _checked if k[0] == uid]:
                del _checked[k]


def can_download(db: Session, user: TentacleUser) -> bool:
    """The person's own "Can save offline" (Jellyfin's "Allow media downloading"); False when Jellyfin can't say."""
    try:
        jf_user = jellyfin(db).get(f"/Users/{dashed(user.jellyfin_user_id)}") or {}
    except (FamilyError, requests.RequestException):
        return False
    policy = jf_user.get("Policy") or {}
    return policy.get("EnableContentDownloading") is not False and not policy.get("IsDisabled")


def _item_as(db: Session, user: TentacleUser, item_id: str) -> Optional[dict]:
    """The title as Jellyfin shows it to this person; None when they can't open it."""
    res = jellyfin(db).get("/Items", ids=item_id, userId=dashed(user.jellyfin_user_id), Fields=_FIELDS,
                           EnableUserData="true", EnableImages="true") or {}
    items = res.get("Items") or []
    return items[0] if items else None


def summary(item: dict) -> dict:
    """What the Offline page keeps about a title (pure)."""
    sources = item.get("MediaSources") or []
    src = sources[0] if sources else {}
    path = (src.get("Path") or item.get("Path") or "")
    container = (src.get("Container") or "").split(",")[0].strip().lower()
    if not container or container == "strm":
        tail = path.rsplit("/", 1)[-1].split("?", 1)[0]
        ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
        container = ext if ext and ext != "strm" else "mp4"
    is_episode = item.get("Type") == "Episode"
    user_data = item.get("UserData") or {}
    return {
        "id": norm_item_id(item.get("Id") or ""),
        "kind": "episode" if is_episode else "movie",
        "name": item.get("Name") or "",
        "series": item.get("SeriesName") if is_episode else None,
        "season": item.get("ParentIndexNumber") if is_episode else None,
        "episode": item.get("IndexNumber") if is_episode else None,
        "year": item.get("ProductionYear"),
        "runtime_ticks": item.get("RunTimeTicks"),
        "overview": (item.get("Overview") or "")[:600],
        # What the Offline page saves: an MP4 (Tentacle makes one of
        # anything else, services/offline_convert.py)
        "container": container if container in ("mp4", "m4v") else "mp4",
        "source_container": container,
        # A provider's title has no size until the relay says it (Jellyfin's is the .strm's)
        "size": src.get("Size") if (src.get("Size") or 0) > (1 << 20) else None,
        "position_ticks": user_data.get("PlaybackPositionTicks") or 0,
        "played": bool(user_data.get("Played")),
    }


def check(db: Session, user: TentacleUser, item_id: str, fresh: bool = False, folders: bool = False) -> dict:
    """The title, if this person may save it; FamilyError with words for
    them otherwise. Kept CHECK_SECONDS (a resumed download asks again).
    `folders`: a show or a season is answered too (its episodes are
    checked one by one, episodes())."""
    iid = norm_item_id(item_id)
    if not iid or not user or not user.jellyfin_user_id:
        raise FamilyError(NOT_FOUND, 404)
    key = (L.norm_user_id(user.jellyfin_user_id), iid)
    now = time.monotonic()
    with _lock:
        hit = _checked.get(key)
    if hit and not fresh and now - hit[0] < CHECK_SECONDS:
        return hit[1]
    if not can_download(db, user):
        raise FamilyError(NOT_ALLOWED, 403)
    try:
        item = _item_as(db, user, iid)
    except requests.RequestException as e:
        raise _family._jf_error(e, "answer")
    if not item:
        raise FamilyError(NOT_FOUND, 404)
    if folders and item.get("Type") in ("Series", "Season"):
        return item
    if item.get("MediaType") != "Video" or item.get("IsFolder"):
        raise FamilyError(NOT_VIDEO, 400)
    if item.get("Status") == "InProgress":
        raise FamilyError(STILL_RECORDING, 409)
    if item.get("CanDownload") is False:
        raise FamilyError(NOT_ALLOWED, 403)
    with _lock:
        if len(_checked) > 2000:
            _checked.clear()
        _checked[key] = (now, item)
    return item


def episodes(db: Session, user: TentacleUser, folder: dict, season: Optional[int] = None) -> dict:
    """A show's (or one season's) episodes this person may save, in order:
    {"kind": "show"|"season", "name", "series", "season", "episodes": [summary...]}.
    Asked AS them (their libraries and age limit); episodes Jellyfin only
    knows of (missing, not yet aired), still recording or not downloadable
    are left out."""
    is_season = folder.get("Type") == "Season"
    series_id = folder.get("SeriesId") if is_season else folder.get("Id")
    params = {"userId": dashed(user.jellyfin_user_id), "Fields": _FIELDS, "EnableUserData": "true",
              "EnableImages": "false"}
    if is_season:
        params["seasonId"] = folder.get("Id")
    elif season is not None:
        params["season"] = int(season)
    try:
        res = jellyfin(db).get(f"/Shows/{series_id}/Episodes", **params) or {}
    except requests.RequestException as e:
        raise _family._jf_error(e, "answer")
    eps = []
    for ep in res.get("Items") or []:
        if (ep.get("LocationType") == "Virtual" or ep.get("IsMissing") or ep.get("Status") == "InProgress"
                or ep.get("CanDownload") is False or ep.get("MediaType") != "Video"):
            continue
        eps.append(summary(ep))
    eps.sort(key=lambda e: (e["season"] if e["season"] is not None else 999, e["episode"] or 0))
    if not eps:
        raise FamilyError(NO_EPISODES, 404)
    name = folder.get("SeriesName") if is_season else folder.get("Name")
    return {"kind": "season" if is_season or season is not None else "show", "name": name or "",
            "series": name or "", "season": folder.get("IndexNumber") if is_season else season, "episodes": eps}


def poster_of(item: dict) -> Optional[tuple]:
    """(item id, image tag) of the picture the Offline page shows (pure): an
    episode's own still, else its show's poster; a movie's poster."""
    tags = item.get("ImageTags") or {}
    if tags.get("Primary"):
        return item.get("Id"), tags["Primary"]
    if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
        return item["SeriesId"], item["SeriesPrimaryImageTag"]
    return None


def poster(db: Session, user: TentacleUser, item_id: str) -> Optional[tuple]:
    """(bytes, content type) of the title's picture, small; None without one."""
    item = check(db, user, item_id)
    which = poster_of(item)
    if which is None:
        return None
    jf = jellyfin(db)
    try:
        r = requests.get(f"{jf.url}/Items/{which[0]}/Images/Primary", headers=jf.headers,
                         params={"maxHeight": 480, "quality": 85, "tag": which[1]}, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return None
    return r.content, r.headers.get("content-type") or "image/jpeg"


def _parse_when(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        if "." in text:     # Jellyfin writes 7 decimals; Python reads 6
            head, _, tail = text.partition(".")
            digits = "".join(c for c in tail if c.isdigit())
            rest = tail[len(digits):]
            text = f"{head}.{digits[:6]}{rest}"
        when = datetime.fromisoformat(text)
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def sync_progress(db: Session, user: TentacleUser, entries: list) -> dict:
    """What was watched offline, back into Jellyfin for this person: each
    entry {id, position_ticks, played, at (ms since 1970, when it was
    watched)}. Jellyfin's own answer wins where it is newer (watched on
    the TV since). Returns {"saved": n, "newer_in_jellyfin": n, "skipped": n}."""
    jf = jellyfin(db)
    uid = dashed(user.jellyfin_user_id)
    out = {"saved": 0, "newer_in_jellyfin": 0, "skipped": 0}
    for entry in entries[:200]:
        iid = norm_item_id(str(entry.get("id") or ""))
        when = _parse_when(entry.get("at"))
        if not iid or when is None:
            out["skipped"] += 1
            continue
        try:
            item = _item_as(db, user, iid)
        except requests.RequestException:
            out["skipped"] += 1
            continue
        if not item:
            out["skipped"] += 1
            continue
        theirs = _parse_when((item.get("UserData") or {}).get("LastPlayedDate"))
        if theirs is not None and theirs >= when:
            out["newer_in_jellyfin"] += 1
            continue
        try:
            ticks = max(0, int(entry.get("position_ticks") or 0))
        except (TypeError, ValueError):
            ticks = 0
        played = bool(entry.get("played"))
        body = {"PlaybackPositionTicks": 0 if played else ticks, "Played": played,
                "LastPlayedDate": when.isoformat().replace("+00:00", "Z")}
        try:
            jf.post(f"/UserItems/{dashed(iid)}/UserData", body, userId=uid)
        except requests.RequestException as e:
            logger.info(f"[Offline] Progress for {iid} not saved in Jellyfin: {type(e).__name__}")
            out["skipped"] += 1
            continue
        out["saved"] += 1
    if out["saved"]:
        logger.info(f"[Offline] {user.display_name}: {out['saved']} title(s) watched offline saved in Jellyfin")
    return out

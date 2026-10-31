"""
Tentacle - one series per show (#889)

A show the provider streams and Sonarr downloads is ONE series in Jellyfin:
its VOD .strm files and Sonarr's downloads live in one folder, the show's VOD
folder, whatever order they arrived in. Jellyfin makes one series of one
folder; two folders (Sonarr's own under /tv, the VOD one under /vod) made two
series, each with half the episodes.

- `unify()` points Sonarr at the VOD folder of every show both hold, with
  Sonarr's own "move files" (PUT /series?moveFiles=true): on the same disk it
  is a rename, and Sonarr keeps its records straight. Run after every Sonarr
  scan (nightly, webhooks, Scan now) and after each provider sync, so it
  converges from any order of events: VOD after Sonarr, Sonarr after VOD, a
  show added in Sonarr directly, a folder renamed or moved.
- Sonarr's move overwrites a file of the same name in the target, so a show
  is moved only when nothing but tvshow.nfo (Tentacle writes both) would be
  overwritten, and never when Sonarr's folder holds Tentacle's own stream
  files. Nothing is deleted.
- In one folder an episode can exist twice: a .strm and a download. Jellyfin
  lists both, so `settle_episode_copies()` parks the stream copy while the
  episode is downloaded ("<name>.strm.parked": Jellyfin and Sonarr ignore it)
  and restores it when the download goes. Download first, as the duplicates
  default; the stream copy is never deleted.
- Users' watched state, resume points and favourites are on the old items:
  saved before the move and merged onto the new ones by path once Jellyfin
  has them (services.duplicates pending user data).
"""

import logging
import os
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PARKED_SUFFIX = ".parked"
STRM_SUFFIX = ".strm"

# One unify at a time: the Sonarr scan and a provider sync can both finish
# at once, and two moves of one show must never be sent.
_lock = threading.Lock()

# How long one move may take before the run goes on (the next run finishes
# the bookkeeping). Same-disk moves are renames, done in a second.
MOVE_WAIT_SECONDS = 90
MOVE_POLL_SECONDS = 2

ONE_SERIES = "one_series"   # Duplicate.resolved_by of a show put into one folder


# ── Files ──────────────────────────────────────────────────────────────────

def is_parked(path) -> bool:
    return str(path).lower().endswith(STRM_SUFFIX + PARKED_SUFFIX)


def parked_path(strm_file: Path) -> Path:
    return strm_file.with_name(strm_file.name + PARKED_SUFFIX)


def stream_file_of(strm_file: Path) -> Optional[Path]:
    """The stream copy of this episode as it is on disk: the .strm, or its
    parked twin, or None."""
    if strm_file.exists():
        return strm_file
    try:
        parked = parked_path(strm_file)
        return parked if parked.exists() else None
    except OSError:   # the name only just fits: there can't be a parked copy
        return None


def is_stream_name(name: str) -> bool:
    """A .strm, parked or not: the episode exists as a stream."""
    n = name.lower()
    return n.endswith(STRM_SUFFIX) or n.endswith(STRM_SUFFIX + PARKED_SUFFIX)


def _episode_keys(name: str) -> set:
    from services.duplicates import _episode_keys as keys
    return keys(name)


def _is_download(f: Path) -> bool:
    from services.media_files import MEDIA_SUFFIXES
    return f.suffix.lower() in MEDIA_SUFFIXES


def episode_sources(show_dir) -> dict:
    """{"SxxEyy": {"stream": bool, "download": bool}} for the files in one
    show folder. Unreadable folder: {}."""
    out = {}
    root = Path(show_dir) if show_dir else None
    if root is None:
        return out
    try:
        files = [f for f in root.rglob("*") if f.is_file()]
    except OSError as e:
        logger.warning(f"Could not read {root}: {e}")
        return out
    for f in files:
        kind = "stream" if is_stream_name(f.name) else "download" if _is_download(f) else None
        if kind:
            for key in _episode_keys(f.name):
                out.setdefault(key, {"stream": False, "download": False})[kind] = True
    return out


def settle_episode_copies(show_dir) -> dict:
    """Download first, in one show folder: the .strm of an episode that is
    also downloaded is parked, a parked one whose download is gone comes back.
    Returns {"parked": [(parked file, download file)], "restored": n}. Renames
    only; nothing is deleted. A folder that can't be read changes nothing."""
    out = {"parked": [], "restored": 0}
    root = Path(show_dir) if show_dir else None
    if root is None or not root.is_dir():
        return out
    try:
        files = [f for f in root.rglob("*") if f.is_file()]
    except OSError as e:
        logger.warning(f"Could not read {root}: {e}")
        return out
    downloads = {}
    for f in files:
        if not is_stream_name(f.name) and _is_download(f):
            for key in _episode_keys(f.name):
                downloads.setdefault(key, f)
    for f in files:
        keys = _episode_keys(f.name)
        if not keys:
            continue
        downloaded = keys <= downloads.keys()
        try:
            if f.name.lower().endswith(STRM_SUFFIX) and downloaded:
                target = parked_path(f)
                os.replace(f, target)    # a parked copy of the same name is ours: the newer address wins
                out["parked"].append((target, downloads[min(keys)]))
            elif is_parked(f) and not downloaded:
                target = f.with_name(f.name[:-len(PARKED_SUFFIX)])
                if target.exists():      # written again meanwhile: that one plays
                    continue
                f.rename(target)
                out["restored"] += 1
        except OSError as e:
            logger.warning(f"Could not settle {f}: {e}")
    if out["parked"] or out["restored"]:
        logger.info(f"[One series] {root.name}: {len(out['parked'])} stream cop(ies) parked (downloaded), "
                    f"{out['restored']} back (download gone)")
    return out


# ── Where Sonarr's copy belongs ─────────────────────────────────────────────

def _norm(path: Optional[str]) -> str:
    return unicodedata.normalize("NFC", (path or "").replace("\\", "/").rstrip("/"))


def _base(path: Optional[str]) -> str:
    return _norm(path).rsplit("/", 1)[-1]


def vod_root_folder(folders: list) -> Optional[str]:
    """Sonarr's root folder on the VOD shows directory (found by "vod" in its path)."""
    return next((rf["path"].rstrip("/") for rf in folders or [] if "vod" in (rf.get("path") or "").lower()), None)


def sonarr_path_for(strm_path: str, folders: list, layout: str, current_path: Optional[str] = None) -> Optional[str]:
    """Sonarr's path of a show's one folder: the VOD folder under Sonarr's VOD
    root folder (vod_root layout), or the VOD folder's name under Sonarr's own
    root (shared_library: Jellyfin merges same-named folders of one library).
    None when it can't be had (no VOD root folder in Sonarr)."""
    name = _base(strm_path)
    if not name:
        return None
    if layout == "shared_library":
        root = current_path and _norm(current_path).rsplit("/", 1)[0]
        return f"{root}/{name}" if root else None
    root = vod_root_folder(folders)
    return f"{root}/{name}" if root else None


def _local_folder(sonarr_path: str, folders: list) -> Optional[Path]:
    """Tentacle's own path of a Sonarr folder: under the VOD mount when it is
    in Sonarr's VOD root folder, else under the downloads mount."""
    from services import media_files
    from services.sync import VOD_SERIES_ROOT
    path = _norm(sonarr_path)
    for rf in folders or []:
        root = _norm(rf.get("path"))
        if root and path.startswith(root + "/"):
            local_root = VOD_SERIES_ROOT if "vod" in root.lower() else media_files.SONARR_MEDIA_ROOT
            return Path(str(local_root) + path[len(root):])
    if media_files.is_vod_arr_path(path):
        return None
    return media_files.local_arr_folder(path, media_files.SONARR_MEDIA_ROOT, "/data/shows")


def _blockers(src: Path, dst: Path) -> list:
    """Why Sonarr's folder can't be moved into the VOD folder as it is: a file
    Sonarr's move would overwrite (it does, silently), or Tentacle's own
    stream files (they'd show episodes twice). tvshow.nfo is Tentacle's on
    both sides and is written again after the move."""
    reasons = []
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src)
        if is_stream_name(f.name):
            reasons.append(f"Sonarr's folder holds a stream file ({rel})")
        elif (dst / rel).exists() and str(rel) != "tvshow.nfo":
            reasons.append(f"{rel} is in both folders")
        if len(reasons) >= 3:
            break
    return reasons


def _downloads_already_in(sonarr, show: dict, vod_dir: Path) -> bool:
    """Sonarr's folder is gone and every download Sonarr lists is in the VOD
    folder already, by its path inside the show folder (the folder was renamed
    or moved by hand): Sonarr only needs the new path. False when Sonarr's
    files can't be read."""
    from services.duplicates import is_downloaded_file
    try:
        files = sonarr.get_episode_files(show["id"])
    except Exception as e:
        logger.warning(f"[One series] could not read Sonarr's files of '{show.get('title')}': {e}")
        return False
    rels = [(f.get("relativePath") or "").replace("\\", "/") for f in files if is_downloaded_file(f.get("path"))]
    return bool(rels) and all(r and (vod_dir / r).is_file() for r in rels)


# ── Users' data ───────────────────────────────────────────────────────────

def _jellyfin(db):
    from models.database import get_setting
    url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return None
    from services.jellyfin import JellyfinService
    return JellyfinService(url, key)


def _show_items(jf, tmdb_id: int, vod_name: str, arr_name: str) -> tuple:
    """(the VOD show's Jellyfin item, the download's), by folder name: the
    VOD one is the one holding .strm episodes when both names are equal."""
    from services.duplicates import _items_for_tmdb, _path, _episodes, is_downloaded_file
    shows = _items_for_tmdb(jf, "Series", tmdb_id)
    vod = [s for s in shows if _base(_path(s)) == vod_name]
    arr = [s for s in shows if _base(_path(s)) == arr_name]
    if vod_name == arr_name and len(vod) > 1:
        with_strm = [s for s in vod if any(not is_downloaded_file(_path(e)) for e in _episodes(jf, s["Id"]))]
        vod = with_strm[:1]
        arr = [s for s in arr if s not in vod]
    else:
        arr = [s for s in arr if s not in vod]
    return (vod[0] if vod else None), (arr[0] if arr else None)


def _someone_watching(jf, shows: list) -> bool:
    """Is anyone playing an episode of these shows? Jellyfin opens a
    download by its path for every range it sends, so moving the file under
    a playing episode cuts it off. Sessions unreadable: yes (try later)."""
    from services.duplicates import _JF_TIMEOUT, _path
    ids = {s["Id"].replace("-", "") for s in shows}
    roots = [_path(s).rstrip("/") + "/" for s in shows]
    try:
        r = jf.session.get(f"{jf.url}/Sessions", params={"activeWithinSeconds": 960}, timeout=_JF_TIMEOUT)
        r.raise_for_status()
        sessions = r.json() or []
    except Exception as e:
        logger.warning(f"[One series] could not read Jellyfin's sessions: {e}")
        return True
    for s in sessions:
        item = s.get("NowPlayingItem") or {}
        if (item.get("SeriesId") or "").replace("-", "") in ids or any(
                (item.get("Path") or "").replace("\\", "/").startswith(r) for r in roots):
            return True
    return False


def _save_user_data(db, jf, row, vod_item, arr_item, moved_rel: list, downloaded_keys: dict) -> list:
    """Users' data on the copy Sonarr's move removes: the download's episodes
    (by the path each file gets in the VOD folder) and the .strm episodes the
    move parks (onto the download of that episode). The download show's own
    data (a favourite) is merged onto the VOD show at once. Returns pending
    entries for services.duplicates.apply_pending_user_data. Raises when
    Jellyfin can't be read: then nothing is moved."""
    from services.duplicates import (_episodes, _path, _user_data, _has_user_data, merged_user_data,
                                     _USER_DATA_FIELDS, _JF_TIMEOUT)
    if arr_item is None:
        return []
    users = jf.get_user_ids()
    if users is None:
        raise RuntimeError("could not list Jellyfin's users")
    arr_root = _path(arr_item).rstrip("/")
    arr_eps = [e for e in _episodes(jf, arr_item["Id"]) if _path(e).startswith(arr_root + "/")]
    if vod_item is None:
        if any(_has_user_data(_user_data(jf, u, e["Id"])) for u in users for e in arr_eps + [arr_item]):
            try:
                jf.trigger_library_scan()
            except Exception:
                pass
            raise RuntimeError("the VOD show isn't in Jellyfin yet and users have watched the download; "
                               "waiting for Jellyfin's scan")
        return []
    vod_root = _path(vod_item).rstrip("/")
    moved = {rel for rel in moved_rel}
    new_path = {}   # removed item id -> (removed path, kept path)
    by_episode = {}
    for e in arr_eps:
        rel = _path(e)[len(arr_root) + 1:]
        if rel in moved:
            new_path[e["Id"]] = (_path(e), f"{vod_root}/{rel}")
            for key in _episode_keys(Path(rel).name):
                by_episode.setdefault(key, f"{vod_root}/{rel}")
    # The .strm episodes the move will park: their data goes to the download.
    for e in _episodes(jf, vod_item["Id"]):
        p = _path(e)
        if not p.lower().endswith(STRM_SUFFIX):
            continue
        keys = _episode_keys(Path(p).name)
        if keys and keys <= downloaded_keys.keys():
            kept = by_episode.get(min(keys))
            if kept:
                new_path[e["Id"]] = (p, kept)
    now = datetime.now(timezone.utc).isoformat()
    pending = {}
    for user_id in users:
        for item_id, (removed, kept) in new_path.items():
            d = _user_data(jf, user_id, item_id)
            if _has_user_data(d):
                entry = pending.setdefault((removed, kept), {"removed_path": removed, "kept_path": kept,
                                                             "users": {}, "saved_at": now})
                entry["users"][user_id] = {k: d[k] for k in _USER_DATA_FIELDS if d.get(k)}
        change = merged_user_data(_user_data(jf, user_id, arr_item["Id"]), _user_data(jf, user_id, vod_item["Id"]))
        if change:
            r = jf.session.post(f"{jf.url}/UserItems/{vod_item['Id']}/UserData", params={"userId": user_id},
                                json=change, timeout=_JF_TIMEOUT)
            r.raise_for_status()
    return list(pending.values())


def _record(db, row, sources: list, why: str, pending: list):
    """The show's duplicate record, resolved as one series (both copies kept,
    in one folder); it carries the saved user data. Commits."""
    from models.database import Duplicate
    dups = db.query(Duplicate).filter(Duplicate.tmdb_id == row.tmdb_id, Duplicate.media_type == "series").all()
    dup = next((d for d in dups if d.resolution == "pending"), None) or next(
        (d for d in dups if d.resolved_by == ONE_SERIES), None)
    if dup is None:
        dup = Duplicate(tmdb_id=row.tmdb_id, media_type="series", sources=sources)
        db.add(dup)
    for d in dups:   # pending twins (#504) go the same way
        if d is not dup and d.resolution == "pending":
            d.resolution, d.resolved_at, d.resolved_by, d.resolved_why = "keep_both", datetime.utcnow(), ONE_SERIES, why
    dup.resolution, dup.resolved_at, dup.resolved_by, dup.resolved_why = "keep_both", datetime.utcnow(), ONE_SERIES, why
    if pending:
        fresh = {(p["removed_path"], p["kept_path"]) for p in pending}
        dup.pending_user_data = [p for p in dup.pending_user_data or []
                                 if (p["removed_path"], p["kept_path"]) not in fresh] + pending
    db.commit()
    return dup


# ── Moving Sonarr's copy ─────────────────────────────────────────────────

def _wait_for_move(sonarr, series_id: int, target: str, src: Optional[Path]) -> bool:
    deadline = time.monotonic() + MOVE_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            r = sonarr.session.get(f"{sonarr.url}/api/v3/series/{series_id}", timeout=15)
            r.raise_for_status()
            path_ok = _norm(r.json().get("path")) == _norm(target)
        except Exception:
            path_ok = False
        if path_ok and (src is None or not _holds_download(src)):
            return True
        time.sleep(MOVE_POLL_SECONDS)
    return False


def _holds_download(folder: Path) -> bool:
    try:
        return folder.exists() and any(_is_download(f) for f in folder.rglob("*"))
    except OSError:
        return True


def _move(sonarr, show: dict, target: str) -> None:
    """Sonarr's own Edit → path with "move files". Raises on failure."""
    r = sonarr.session.get(f"{sonarr.url}/api/v3/series/{show['id']}", timeout=15)
    r.raise_for_status()
    body = r.json()
    body["path"] = target
    r = sonarr.session.put(f"{sonarr.url}/api/v3/series/{show['id']}", params={"moveFiles": "true"},
                           json=body, timeout=30)
    r.raise_for_status()


def _after_move(db, row, target: str, vod_dir: Path) -> dict:
    """The row follows its download into the VOD folder; the episodes both
    copies have play the download; Tentacle's tvshow.nfo there gets the row's
    tags (Sonarr's move may have put the download's copy over it)."""
    from services.nfo import update_nfo_tags
    from services.tagger import tentacle_owned_tags
    row.sonarr_path = target
    row.nfo_path = str(vod_dir / "tvshow.nfo")
    row.jellyfin_item_id = None   # the download show's item, gone with its folder; re-matched on use
    row.date_updated = datetime.utcnow()
    settled = settle_episode_copies(vod_dir)
    nfo = vod_dir / "tvshow.nfo"
    if nfo.exists() and row.tags:
        try:
            update_nfo_tags(nfo, list(row.tags), tentacle_owned_tags(db))
        except Exception as e:
            logger.warning(f"[One series] could not write the tags into {nfo}: {e}")
    return settled


def _tell_jellyfin(jf, paths_gone: list, paths_changed: list) -> None:
    if jf is None:
        return
    try:
        if paths_gone:
            jf.notify_media_updated(paths_gone, "Deleted")
        if paths_changed:
            jf.notify_media_updated(paths_changed, "Modified")
    except Exception as e:   # its own scan finds it later
        logger.warning(f"[One series] could not tell Jellyfin about the move: {e}")


def unify(db, sonarr=None, all_series: Optional[list] = None) -> dict:
    """Put every show the provider streams and Sonarr holds into one folder.
    Returns counts: checked, moved, pointed (Sonarr's path set, nothing on
    disk yet), parked/restored stream copies, blocked (with reasons, also
    logged), waiting (move still running). Never raises for one show."""
    stats = {"checked": 0, "moved": 0, "pointed": 0, "parked": 0, "restored": 0,
             "blocked": [], "waiting": 0, "dismissed": 0}
    if not _lock.acquire(blocking=False):
        stats["skipped"] = "running"
        return stats
    try:
        return _unify(db, sonarr, all_series, stats)
    finally:
        _lock.release()


def _unify(db, sonarr, all_series, stats) -> dict:
    from models.database import Series, get_setting, log_activity
    if sonarr is None:
        url, key = get_setting(db, "sonarr_url"), get_setting(db, "sonarr_api_key")
        if not (url and key):
            return stats
        from services.sonarr import SonarrService
        sonarr = SonarrService(url, key)
    if all_series is None:
        all_series = sonarr.get_all_series()
    if not all_series:
        return stats
    by_tmdb = {s.get("tmdbId"): s for s in all_series if s.get("tmdbId")}
    rows = [r for r in db.query(Series).filter(Series.source.like("provider_%"), Series.strm_path.isnot(None)).all()
            if r.tmdb_id in by_tmdb and not r.strm_disabled]
    if not rows:
        return stats
    try:
        folders = sonarr.get_root_folders()
    except Exception as e:
        folders = None
        logger.warning(f"[One series] Sonarr's root folders: {e}")
    if not folders:
        logger.warning("[One series] Sonarr's root folders couldn't be read; nothing checked")
        return stats
    layout = get_setting(db, "hybrid_series_layout", "vod_root")
    for row in rows:
        stats["checked"] += 1
        try:
            _unify_one(db, sonarr, by_tmdb[row.tmdb_id], row, folders, layout, stats)
        except Exception as e:
            db.rollback()
            logger.error(f"[One series] '{row.title}' (tmdb:{row.tmdb_id}): {e}")
            stats["blocked"].append(f"{row.title}: {e}")
    if stats["moved"]:
        log_activity(db, "one_series", f"{stats['moved']} show(s) put into one series in Jellyfin "
                                       f"(the downloads moved next to the streams)")
        from services.duplicates import start_pending_user_data_worker
        start_pending_user_data_worker()
    if stats["blocked"]:
        logger.warning(f"[One series] {len(stats['blocked'])} show(s) stay in two folders for now: "
                       f"{'; '.join(stats['blocked'][:5])}")
    return stats


def _unify_one(db, sonarr, show, row, folders, layout, stats) -> None:
    vod_dir = Path(row.strm_path)
    target = sonarr_path_for(row.strm_path, folders, layout, show.get("path"))
    if target is None:
        stats["blocked"].append(f"{row.title}: Sonarr has no root folder on the VOD shows directory")
        return
    current = show.get("path") or ""
    if _norm(current) == _norm(target):
        if row.sonarr_path != current:
            row.sonarr_path = current
            db.commit()
        settled = settle_episode_copies(vod_dir)
        stats["parked"] += len(settled["parked"])
        stats["restored"] += settled["restored"]
        if settled["parked"]:
            _carry_parked(db, row, settled["parked"])
        _dismiss_pending(db, row, stats)
        return
    from services.sync import _holds_own_file
    if not vod_dir.is_dir() or not _holds_own_file(vod_dir):
        stats["blocked"].append(f"{row.title}: its VOD folder holds no stream file")
        return
    src = _local_folder(current, folders)
    has_files = (show.get("statistics") or {}).get("episodeFileCount", 0) > 0
    if src is None or not src.is_dir():
        if has_files and not _downloads_already_in(sonarr, show, vod_dir):
            stats["blocked"].append(f"{row.title}: Tentacle can't see Sonarr's folder {current} to check it")
            return
        src = None   # nothing of Sonarr's left there: Sonarr only learns the new path
    else:
        reasons = _blockers(src, vod_dir)
        if reasons:
            stats["blocked"].append(f"{row.title}: {', '.join(reasons)}")
            return
    moved_rel, downloaded = [], {}
    if src is not None:
        for f in src.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(src))
                moved_rel.append(rel)
                if _is_download(f):
                    for key in _episode_keys(f.name):
                        downloaded.setdefault(key, rel)
    from services.duplicates import _path
    jf = _jellyfin(db)
    vod_item = arr_item = None
    pending = []
    if jf is not None and src is not None:
        vod_item, arr_item = _show_items(jf, row.tmdb_id, _base(row.strm_path), _base(current))
        if _someone_watching(jf, [i for i in (vod_item, arr_item) if i]):
            stats["blocked"].append(f"{row.title}: someone is watching it; moved once they stop")
            return
        pending = _save_user_data(db, jf, row, vod_item, arr_item, moved_rel, downloaded)
    why = ("stream and download in one folder: " + _base(row.strm_path))
    _record(db, row, [{"source": "sonarr", "path": current}, {"source": row.source, "path": row.strm_path}],
            why, pending)
    logger.info(f"[One series] '{row.title}' (tmdb:{row.tmdb_id}): Sonarr {current} -> {target} "
                f"({len(moved_rel)} file(s), {len(pending)} watched-state record(s) saved)")
    _move(sonarr, show, target)
    if not _wait_for_move(sonarr, show["id"], target, src):
        stats["waiting"] += 1
        logger.warning(f"[One series] '{row.title}': Sonarr is still moving it; the next run finishes it")
        return
    settled = _after_move(db, row, target, vod_dir)
    db.commit()
    stats["parked"] += len(settled["parked"])
    if src is None:
        stats["pointed"] += 1
        return
    stats["moved"] += 1
    gone = [_path(arr_item)] if arr_item else []
    changed = [_path(vod_item)] if vod_item else []
    _tell_jellyfin(jf, gone, changed)
    if jf is not None and not vod_item:
        try:
            jf.trigger_library_scan()
        except Exception:
            pass


def _carry_parked(db, row, parked: list) -> None:
    """A download that landed in the show's folder: the stream copy's
    watched state goes to the download's item once Jellyfin has it."""
    jf = _jellyfin(db)
    if jf is None:
        return
    from services.duplicates import (_episodes, _path, _user_data, _has_user_data, _USER_DATA_FIELDS,
                                     start_pending_user_data_worker)
    try:
        vod_item, _ = _show_items(jf, row.tmdb_id, _base(row.strm_path), "")
        if vod_item is None:
            return
        root = _path(vod_item).rstrip("/")
        local = Path(row.strm_path)
        by_path = {_path(e): e for e in _episodes(jf, vod_item["Id"])}
        users = jf.get_user_ids() or []
        now = datetime.now(timezone.utc).isoformat()
        pending = []
        for parked_file, download in parked:
            strm = parked_file.with_name(parked_file.name[:-len(PARKED_SUFFIX)])
            removed = f"{root}/{strm.relative_to(local).as_posix()}"
            kept = f"{root}/{download.relative_to(local).as_posix()}"
            item = by_path.get(removed)
            if item is None:
                continue
            saved = {}
            for u in users:
                d = _user_data(jf, u, item["Id"])
                if _has_user_data(d):
                    saved[u] = {k: d[k] for k in _USER_DATA_FIELDS if d.get(k)}
            if saved:
                pending.append({"removed_path": removed, "kept_path": kept, "users": saved, "saved_at": now})
        if pending:
            _record(db, row, [{"source": "sonarr", "path": row.sonarr_path},
                              {"source": row.source, "path": row.strm_path}],
                    "stream and download in one folder: " + _base(row.strm_path), pending)
            jf.notify_media_updated([p["removed_path"] for p in pending], "Deleted")
            start_pending_user_data_worker()
    except Exception as e:
        logger.warning(f"[One series] '{row.title}': could not carry the stream copies' watched state: {e}")


def _dismiss_pending(db, row, stats) -> None:
    """A show already in one folder is no duplicate: a pending record left
    from before (the Sonarr scan's, a VOD sync's) is resolved as one series."""
    from models.database import Duplicate
    pending = db.query(Duplicate).filter(Duplicate.tmdb_id == row.tmdb_id, Duplicate.media_type == "series",
                                         Duplicate.resolution == "pending").all()
    for d in pending:
        d.resolution, d.resolved_at, d.resolved_by = "keep_both", datetime.utcnow(), ONE_SERIES
        d.resolved_why = "stream and download in one folder: " + _base(row.strm_path)
        stats["dismissed"] += 1
    if pending:
        db.commit()

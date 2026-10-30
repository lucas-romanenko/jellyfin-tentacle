"""Library folders Radarr and Sonarr don't know about (#871).

A folder in the movie or show library (Tentacle's /media/movies and
/media/shows) that no Radarr movie or Sonarr series uses: a manual copy, an
import from an old server, a show re-added by hand. Jellyfin plays it, but no
*arr upgrades it, finds its missing episodes or knows it is owned, so
Tentacle may offer it for download again.

`scan()` lists them (read-only): each folder with a real video file in it
(`.strm`-only folders are Tentacle's own) and not used by Radarr/Sonarr (by
folder name, as local_arr_folder finds downloads, #649), matched to TMDB by
the ids in its NFO, else the ids in its name ("{tmdb-603}", "[imdbid-tt..]",
"{tvdb-..}"), else its title and year, with how sure the match is.

`add()` tells Radarr/Sonarr to use the folder as it is: the household's
default quality profile, the folder as the title's path, no search, so
nothing is downloaded, moved or renamed (services/media_requests). Nothing
here writes to the library.

The list is kept in memory (`state()`); a scan runs in a thread, started by
the page when the list is older than STALE_SECONDS, by "Check again", and
by the nightly run when "add confident matches automatically" is on
(`untracked_auto_add`, off by default; `untracked_monitored` picks
monitored or not, off by default).
"""
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from services import media_files
from services.media_files import MEDIA_SUFFIXES, arr_parent, arr_path_parts
from services.tmdb import _similarity

logger = logging.getLogger(__name__)

# Auto-add and the "Add all" button take matches at least this sure.
SURE = 95
# The page rescans when it opens on a list older than this.
STALE_SECONDS = 6 * 3600
# Show folders are deeper (Season 1/...); nothing sits further down than this.
_MAX_DEPTH = {"movie": 2, "series": 4}
_SKIP_DIRS = {"@eaDir", "#recycle", "#snapshot", "lost+found", ".Trash", "$RECYCLE.BIN",
              "System Volume Information"}
_NAMES = {"movie": "Radarr", "series": "Sonarr"}

_lock = threading.Lock()
_state = {"running": False, "scanned_at": None, "items": [], "problems": [], "progress": None}


# ── Reading a folder ──────────────────────────────────────────────────────

def _media_files(folder: Path, kind: str) -> list:
    """The real video files under a folder (never .strm), to _MAX_DEPTH."""
    found = []

    def walk(d: Path, depth: int):
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in _SKIP_DIRS:
                continue
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    if depth < _MAX_DEPTH[kind]:
                        walk(e, depth + 1)
                elif e.suffix.lower() in MEDIA_SUFFIXES and e.is_file():
                    found.append(e)
            except OSError:
                continue

    walk(folder, 1)
    return found


_ID_IN_NAME = [
    ("tmdb", re.compile(r"[\[{(]\s*tmdb(?:id)?[-=: ]\s*(\d+)\s*[\]})]", re.I)),
    ("imdb", re.compile(r"[\[{(]\s*imdb(?:id)?[-=: ]\s*(tt\d+)\s*[\]})]", re.I)),
    ("tvdb", re.compile(r"[\[{(]\s*tvdb(?:id)?[-=: ]\s*(\d+)\s*[\]})]", re.I)),
]
_ANY_TAG = re.compile(r"[\[{(][^\]})]*[\]})]")
_YEAR = re.compile(r"^(.*?)[\s._-]*[\[(]?((?:19|20)\d{2})[\])]?(?:[\s._-].*)?$")


def ids_in_name(name: str) -> dict:
    out = {}
    for kind, rx in _ID_IN_NAME:
        m = rx.search(name or "")
        if m:
            out[kind] = m.group(1)
    return out


def title_and_year(name: str) -> tuple:
    """("Heat", "1995") from "Heat (1995) {tmdb-949}"; the year may be None."""
    year_tag = re.search(r"\(((?:19|20)\d{2})\)", name or "")
    bare = _ANY_TAG.sub(" ", name or "")
    if year_tag:
        title, year = bare.replace(year_tag.group(0), " "), year_tag.group(1)
    else:
        m = _YEAR.match(bare.strip())
        title, year = (m.group(1), m.group(2)) if m and m.group(1).strip() else (bare, None)
    title = re.sub(r"[._]+", " ", title)
    return " ".join(title.split()).strip(" -"), year


def _nfo_ids(folder: Path, kind: str) -> dict:
    """ids, title and year from the folder's NFO (Kodi/Jellyfin/Radarr style)."""
    if kind == "series":
        candidates = [folder / "tvshow.nfo"]
    else:
        try:
            candidates = sorted((p for p in folder.iterdir() if p.suffix.lower() == ".nfo" and p.is_file()),
                                key=lambda p: p.name.lower() != "movie.nfo")
        except OSError:
            candidates = []
    for nfo in candidates:
        try:
            if not nfo.is_file() or nfo.stat().st_size > 2_000_000:
                continue
            root = ET.fromstring(nfo.read_bytes())
        except (OSError, ET.ParseError, ValueError):
            continue
        if root.tag not in ("movie", "tvshow"):
            continue
        out = {}
        for el in root.iter("uniqueid"):
            t = (el.get("type") or "").lower()
            if t in ("tmdb", "imdb", "tvdb") and (el.text or "").strip():
                out.setdefault(t, el.text.strip())
        for tag, t in (("tmdbid", "tmdb"), ("imdbid", "imdb"), ("imdb_id", "imdb"), ("tvdbid", "tvdb")):
            el = root.find(tag)
            if el is not None and (el.text or "").strip():
                out.setdefault(t, el.text.strip())
        el = root.find("id")
        if el is not None and re.fullmatch(r"tt\d+", (el.text or "").strip()):
            out.setdefault("imdb", el.text.strip())
        for tag in ("title", "year"):
            el = root.find(tag)
            if el is not None and (el.text or "").strip():
                out[tag] = el.text.strip()
        if out.get("tmdb") and not out["tmdb"].isdigit():
            out.pop("tmdb")
        if out.get("tvdb") and not out["tvdb"].isdigit():
            out.pop("tvdb")
        return out
    return {}


# ── Matching ──────────────────────────────────────────────────────────────

def confidence(title: str, year: Optional[str], cand_title: str, cand_year: Optional[str]) -> int:
    """How sure (0–99) a title search hit is the folder's title. 100 is kept
    for an id the folder itself carries."""
    score = _similarity(title, cand_title) * 100
    if year and cand_year:
        apart = abs(int(year) - int(cand_year))
        score -= 0 if apart == 0 else 8 if apart == 1 else 40
    elif not year:
        score -= 12   # no year in the folder name: a namesake can't be told apart
    else:
        score -= 20   # TMDB has no date for it
    return max(0, min(99, round(score)))


def _cand(kind: str, r: dict) -> dict:
    date = r.get("release_date" if kind == "movie" else "first_air_date") or ""
    return {"tmdb_id": r.get("id"), "title": r.get("title" if kind == "movie" else "name") or "",
            "year": date[:4] or None, "poster_path": r.get("poster_path"),
            "votes": r.get("vote_count") or 0}


def search_candidates(tmdb, kind: str, title: str, year: Optional[str]) -> list:
    """TMDB's hits for a title (and year), best first, each with `confidence`.
    Raises LookupError when TMDB could not be asked."""
    if not title:
        return []
    key = f"untracked_search:{kind}:{title.lower()}:{year}"
    cached = tmdb._cache_get(key, ttl_seconds=7 * 86400)
    if cached is not None:
        return cached
    endpoint, year_param = ("search/movie", "year") if kind == "movie" else ("search/tv", "first_air_date_year")
    seen, cands = set(), []
    for params in ([{"query": title, year_param: year}, {"query": title}] if year else [{"query": title}]):
        tmdb._tl.failed = False
        data = tmdb._request(endpoint, params)
        if tmdb._lookup_failed():
            raise LookupError("TMDB could not be asked")
        for r in ((data or {}).get("results") or [])[:10]:
            c = _cand(kind, r)
            if c["tmdb_id"] and c["tmdb_id"] not in seen:
                seen.add(c["tmdb_id"])
                c["confidence"] = confidence(title, year, c["title"], c["year"])
                cands.append(c)
    cands.sort(key=lambda c: (-c["confidence"], -c["votes"]))
    cands = cands[:6]
    tmdb._cache_set(key, cands, ttl_seconds=7 * 86400)
    return cands


def _details(tmdb, kind: str, tmdb_id) -> Optional[dict]:
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return None
    d = tmdb.get_movie_details(tmdb_id) if kind == "movie" else tmdb.get_series_details(tmdb_id)
    if not d:
        return None
    return {"tmdb_id": d.get("tmdb_id"), "title": d.get("title") or "", "year": d.get("year"),
            "poster_path": d.get("poster_path")}


def match_folder(tmdb, kind: str, name: str, nfo: dict) -> dict:
    """{match, how, confidence, candidates, ambiguous} for one folder."""
    named = ids_in_name(name)
    for source, ids in (("its NFO file", nfo), ("its folder name", named)):
        hit = None
        if ids.get("tmdb"):
            hit = _details(tmdb, kind, ids["tmdb"])
        if not hit and ids.get("imdb"):
            found = tmdb.find_by_imdb_id(ids["imdb"])
            if found and found.get("media_type") == kind:
                hit = {"tmdb_id": found.get("tmdb_id"), "title": found.get("title") or "",
                       "year": found.get("year"), "poster_path": found.get("poster_path")}
        if not hit and kind == "series" and ids.get("tvdb"):
            tmdb_id = tmdb.find_by_tvdb_id(int(ids["tvdb"]))
            hit = _details(tmdb, kind, tmdb_id) if tmdb_id else None
            if hit:
                hit["tvdb_id"] = int(ids["tvdb"])
        if hit:
            if kind == "series" and ids.get("tvdb") and "tvdb_id" not in hit:
                hit["tvdb_id"] = int(ids["tvdb"])
            return {"match": hit, "how": f"the id in {source}", "confidence": 100,
                    "candidates": [dict(hit, confidence=100)], "ambiguous": False}

    title, year = (nfo.get("title"), (nfo.get("year") or "")[:4] or None) if nfo.get("title") else title_and_year(name)
    if year and not year.isdigit():
        year = None
    cands = search_candidates(tmdb, kind, title, year)
    if not cands:
        return {"match": None, "how": "no match on TMDB", "confidence": 0, "candidates": [],
                "ambiguous": True, "title": title, "year": year}
    best = cands[0]
    # Two hits about as good (a remake the same year, a show title with no
    # year): the person picks.
    ambiguous = best["confidence"] < 80 or (len(cands) > 1 and cands[1]["confidence"] >= best["confidence"] - 5)
    sure = best["confidence"]
    if ambiguous:
        sure = min(sure, 79)
    return {"match": {k: best[k] for k in ("tmdb_id", "title", "year", "poster_path")},
            "how": "title and year" if year else "title only (no year in the folder name)",
            "confidence": sure, "candidates": cands, "ambiguous": ambiguous, "title": title, "year": year}


# ── What Radarr/Sonarr have ───────────────────────────────────────────────

def _arr_titles(url: str, key: str, kind: str) -> list:
    endpoint = "movie" if kind == "movie" else "series"
    r = requests.get(f"{url.rstrip('/')}/api/v3/{endpoint}", headers={"X-Api-Key": key}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError(f"{_NAMES[kind]} did not answer with a list")
    return data


def _arr_roots(url: str, key: str) -> list:
    r = requests.get(f"{url.rstrip('/')}/api/v3/rootfolder", headers={"X-Api-Key": key}, timeout=30)
    r.raise_for_status()
    return [f["path"] for f in r.json() if isinstance(f, dict) and f.get("path")]


def arr_root_for(titles: list, local_names: set, roots: list, configured: str) -> Optional[str]:
    """Radarr/Sonarr's own path of Tentacle's library root: the folder its
    titles that Tentacle sees by name sit in (most of them), else the default
    root folder in Tentacle's settings, else its first root that isn't VOD."""
    counts = {}
    for t in titles:
        parts = arr_path_parts(t.get("path"))
        if parts and parts[-1].lower() in local_names:
            parent = arr_parent(t.get("path"))
            if parent and "vod" not in parent.lower():
                counts[parent] = counts.get(parent, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    if configured:
        return configured.rstrip("/\\")
    non_vod = [r for r in roots if "vod" not in r.lower()] or roots
    return non_vod[0].rstrip("/\\") if non_vod else None


def arr_path(arr_root: str, folder_name: str) -> str:
    sep = "\\" if "\\" in arr_root and "/" not in arr_root else "/"
    return f"{arr_root.rstrip(sep)}{sep}{folder_name}"


def _local_root(kind: str) -> Path:
    return media_files.RADARR_MEDIA_ROOT if kind == "movie" else media_files.SONARR_MEDIA_ROOT


def _local_folders(root: Path) -> list:
    try:
        return sorted((d for d in root.iterdir()
                       if d.is_dir() and not d.is_symlink() and not d.name.startswith(".")
                       and d.name not in _SKIP_DIRS), key=lambda d: d.name.lower())
    except OSError:
        return []


# ── The scan ──────────────────────────────────────────────────────────────

def _scan_kind(db, kind: str, tmdb, problems: list, progress: dict) -> list:
    from models.database import get_setting
    service = "radarr" if kind == "movie" else "sonarr"
    name = _NAMES[kind]
    url, key = get_setting(db, f"{service}_url"), get_setting(db, f"{service}_api_key")
    if not url or not key:
        return []
    root = _local_root(kind)
    if not root.is_dir():
        problems.append(f"Tentacle can't see the {'movie' if kind == 'movie' else 'show'} folder ({root}), "
                        f"so it couldn't look for folders {name} doesn't know about.")
        return []
    try:
        titles = _arr_titles(url, key, kind)
        roots = _arr_roots(url, key)
    except Exception as e:
        logger.warning(f"[Untracked] Could not read {name}: {e}")
        problems.append(f"Couldn't read {name}'s list, so its folders weren't checked. Try again in a moment.")
        return []

    folders = _local_folders(root)
    local_names = {d.name.lower() for d in folders}
    known_names = {arr_path_parts(t.get("path"))[-1].lower() for t in titles if arr_path_parts(t.get("path"))}
    by_tmdb = {t.get("tmdbId"): t for t in titles if t.get("tmdbId")}
    by_tvdb = {t.get("tvdbId"): t for t in titles if t.get("tvdbId")}
    configured = (get_setting(db, f"{service}_root_folder") or "").strip()
    a_root = arr_root_for(titles, local_names, roots, configured)

    todo = []
    for d in folders:
        if d.name.lower() in known_names:
            continue
        files = _media_files(d, kind)
        if not files:
            continue   # empty, or .strm only (Tentacle's own VOD)
        todo.append((d, files))
    progress["total"] += len(todo)

    items = []
    for d, files in todo:
        size = 0
        for f in files:
            try:
                size += f.stat().st_size
            except OSError:
                pass
        item = {"kind": kind, "folder": d.name, "local_path": str(d), "size": size,
                "files": len(files), "arr_path": arr_path(a_root, d.name) if a_root else None}
        try:
            item.update(match_folder(tmdb, kind, d.name, _nfo_ids(d, kind)) if tmdb else
                        {"match": None, "how": "TMDB isn't set up", "confidence": 0, "candidates": [],
                         "ambiguous": True})
        except LookupError:
            item.update({"match": None, "how": "TMDB didn't answer; check again later", "confidence": 0,
                         "candidates": [], "ambiguous": True})
        m = item.get("match")
        other = None
        if m:
            other = by_tmdb.get(m.get("tmdb_id")) or (by_tvdb.get(m.get("tvdb_id")) if m.get("tvdb_id") else None)
        if other:
            # The title is in Radarr/Sonarr already, in another folder: a second copy, not addable.
            item["elsewhere"] = other.get("path")
        items.append(item)
        progress["done"] += 1
    return items


def _tmdb(db):
    from services.tmdb import TMDBService, get_tmdb_token
    from models.database import get_setting
    token = get_tmdb_token(db)
    return TMDBService(token, get_setting(db, "data_dir", "/data")) if token else None


def scan(db) -> dict:
    """List the folders now (blocking) and keep the list. Returns state()."""
    with _lock:
        if _state["running"] and _state.get("_thread") is not threading.current_thread():
            return state()
        _state["running"] = True
        _state["progress"] = {"done": 0, "total": 0}
    started = time.monotonic()
    problems, items = [], []
    try:
        tmdb = _tmdb(db)
        for kind in ("movie", "series"):
            items += _scan_kind(db, kind, tmdb, problems, _state["progress"])
    except Exception as e:
        logger.error(f"[Untracked] scan failed: {e}", exc_info=True)
        problems.append(f"The check stopped: {e}")
    finally:
        with _lock:
            if not problems or items or not _state["items"]:
                _state["items"] = items
            _state["problems"] = problems
            _state["scanned_at"] = datetime.utcnow()
            _state["running"] = False
            _state["progress"] = None
            _state.pop("_thread", None)
    logger.info(f"[Untracked] {len(items)} folder(s) Radarr/Sonarr don't know about "
                f"({time.monotonic() - started:.1f}s)")
    return state()


def scan_in_background() -> bool:
    """Start a scan in a thread unless one runs. True when one was started."""
    def run():
        from models.database import SessionLocal
        db = SessionLocal()
        try:
            scan(db)
        finally:
            db.close()

    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["progress"] = {"done": 0, "total": 0}
        t = threading.Thread(target=run, daemon=True, name="untracked-scan")
        _state["_thread"] = t
    t.start()
    return True


def stale() -> bool:
    at = _state["scanned_at"]
    return at is None or (datetime.utcnow() - at).total_seconds() > STALE_SECONDS


def addable(item: dict) -> bool:
    return bool(item.get("match") and not item.get("elsewhere") and item.get("arr_path"))


def state() -> dict:
    items = [{k: v for k, v in i.items() if not k.startswith("_")} for i in _state["items"]]
    return {
        "running": _state["running"],
        "progress": dict(_state["progress"]) if _state["progress"] else None,
        "scanned_at": _state["scanned_at"].isoformat() + "Z" if _state["scanned_at"] else None,
        "items": items,
        "count": len(items),
        "sure": sum(1 for i in items if addable(i) and not i.get("ambiguous") and i["confidence"] >= SURE),
        "sure_at": SURE,
        "problems": list(_state["problems"]),
    }


def find(kind: str, folder: str) -> Optional[dict]:
    return next((i for i in _state["items"] if i["kind"] == kind and i["folder"] == folder), None)


# ── Adding ────────────────────────────────────────────────────────────────

def add(db, kind: str, folder: str, *, tmdb_id: Optional[int] = None, monitored: bool = False,
        via: str = "library-untracked") -> tuple:
    """Add one listed folder to Radarr/Sonarr as it is. Returns (outcome, message).

    `tmdb_id` is the person's pick (Pick); else the folder's match. Only a
    folder from the current list, still on disk, is sent. Raises
    media_requests.RequestRefused when the add can't be made as configured.
    """
    from services import media_requests
    from services.arr_add import ADDED, EXISTS
    item = find(kind, folder)
    if not item:
        return "failed", "That folder isn't on the list any more. Check again."
    local = Path(item["local_path"])
    if not local.is_dir() or local.parent != _local_root(kind):
        return "failed", f"{folder} is no longer in the library folder."
    if item.get("elsewhere") and not tmdb_id:
        return "failed", f"{_NAMES[kind]} already has this title in another folder ({item['elsewhere']})."
    if not item.get("arr_path"):
        return "failed", f"Tentacle couldn't tell where {_NAMES[kind]} keeps its library. Check its root folders."
    match = item.get("match") or {}
    chosen = int(tmdb_id) if tmdb_id else match.get("tmdb_id")
    if not chosen:
        return "failed", "Pick the title first."
    if kind == "movie":
        outcome, reason = media_requests.add_existing_movie(
            db, chosen, item["arr_path"], arr_parent(item["arr_path"]) or item["arr_path"],
            monitored=monitored, via=via)
    else:
        tvdb = match.get("tvdb_id") if chosen == match.get("tmdb_id") else None
        outcome, reason = media_requests.add_existing_series(
            db, item["arr_path"], tmdb_id=chosen, tvdb_id=tvdb, monitored=monitored, via=via)
    if outcome in (ADDED, EXISTS):
        with _lock:
            _state["items"] = [i for i in _state["items"] if i is not item]
    return outcome, reason


def add_sure(db, *, monitored: bool, via: str) -> dict:
    """Add every listed folder whose match is at least SURE and not in doubt."""
    from services.arr_add import AddReport
    report = AddReport()
    for item in [i for i in _state["items"] if addable(i) and not i.get("ambiguous") and i["confidence"] >= SURE]:
        outcome, reason = add(db, item["kind"], item["folder"], monitored=monitored, via=via)
        report.record(outcome, reason)
    return report.as_response()


def nightly(db) -> Optional[dict]:
    """After the nightly Radarr/Sonarr scans: when auto-add is on, list the
    folders again and add the sure ones. Off by default."""
    from models.database import get_setting, log_activity
    if (get_setting(db, "untracked_auto_add", "false") or "").lower() != "true":
        return None
    scan(db)
    monitored = (get_setting(db, "untracked_monitored", "false") or "").lower() == "true"
    result = add_sure(db, monitored=monitored, via="nightly-untracked")
    if result["added"]:
        log_activity(db, "radarr_scan", f"Added {result['added']} library folder(s) to Radarr/Sonarr "
                                        f"as they are (no search, nothing moved)")
    return result

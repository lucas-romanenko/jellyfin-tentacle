"""
Tentacle - The automatic duplicates rule (#872)

Off unless the admin turns it on (Health > Duplicates). Every RUN_MINUTES it
decides each pending duplicate (a provider .strm and a real download of the
same title) and applies the decision through the page's own Keep Downloaded /
Keep VOD (routers/duplicates._resolve): the same checks (#800: a show is
never left without an episode only one copy has), the same carry-over of
users' watched state, the same tombstone, the same Deletion Log (reason
"duplicates-rule", with why).

Modes (setting `duplicates_rule`):
  off       nothing happens (the default, also after an upgrade)
  download  download first (the default when it is turned on): the download
            stays and the .strm goes. It is yours and doesn't depend on the
            provider.
  stream    stream first: the .strm stays and the download is deleted through
            Radarr/Sonarr (their recycle bin, when they have one set). The
            download stays (Keep Downloaded) when the title is on the "prefer
            download" list, its stream is known bad (stream health), or it
            came from a protected indexer. It is left for a person while the
            provider isn't answering or hasn't been checked, while the torrent
            is still in qBittorrent (seeding), while nobody has watched the
            download yet (a better copy is not removed before it is watched),
            or when the download has episodes the stream doesn't.

"The stream plays" is judged only from what Tentacle already knows: stream
health's known-bad list, provider health's state, the .strm on disk. The
rule never opens a provider stream.

Safety cap (`duplicates_rule_cap`, default DEFAULT_CAP): when a run would
change more titles than that (a bulk change in Radarr/Sonarr turns many
titles back into pairs at once), it changes nothing, keeps the dry-run list
and waits until the admin applies it or raises the cap.

The last run's summary (counts, every decision and why) is the setting
`duplicates_rule_last`, JSON, shown on the Duplicates card.
"""

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from models.database import (
    Duplicate, Movie, Series, StreamHealth, get_setting, set_setting, log_activity,
)
from services.duplicates import (
    is_downloaded_file, episodes_only_in, vod_copy_on_disk,
)

logger = logging.getLogger(__name__)

MODES = ("off", "download", "stream")
SETTING_MODE = "duplicates_rule"
SETTING_CAP = "duplicates_rule_cap"
SETTING_INDEXERS = "duplicates_rule_indexers"
SETTING_QBIT_TAG = "duplicates_rule_qbit_tag"
SETTING_LAST = "duplicates_rule_last"
DEFAULT_CAP = 10
RUN_MINUTES = 15
MAX_LISTED = 300           # decisions kept in the summary

KEEP_DOWNLOAD, KEEP_STREAM, SKIP = "keep_download", "keep_stream", "skip"
RESOLUTION = {KEEP_DOWNLOAD: "keep_radarr", KEEP_STREAM: "keep_vod"}
LOG_REASON = "duplicates-rule"

_run_lock = threading.Lock()


def mode(db) -> str:
    m = (get_setting(db, SETTING_MODE, "off") or "off").strip()
    return m if m in MODES else "off"


def cap(db) -> int:
    try:
        n = int(get_setting(db, SETTING_CAP, "") or DEFAULT_CAP)
    except (TypeError, ValueError):
        return DEFAULT_CAP
    return max(n, 0)


def protected_indexers(db) -> set:
    raw = get_setting(db, SETTING_INDEXERS, "") or ""
    return {p.strip().casefold() for p in raw.replace("\n", ",").split(",") if p.strip()}


def last_run(db) -> Optional[dict]:
    try:
        data = json.loads(get_setting(db, SETTING_LAST, "") or "null")
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _episodes(eps: list) -> str:
    return f"{_plural(len(eps), 'episode')} (first: {eps[0]})"


class _Facts:
    """What one run reads, each thing once: Radarr/Sonarr's titles, files and
    history, stream health, provider health, qBittorrent, Jellyfin's played
    items. Read-only. Anything that can't be read makes the titles that need
    it "not sure", never a change."""

    def __init__(self, db):
        self.db = db
        self._arr = {}
        self._titles = {}
        self._files = {}
        self._history = {}
        self._qbit = None
        self._qbit_error = None
        self._played = None
        self._played_error = None
        bad = {}
        for e in db.query(StreamHealth).all():
            key = (e.media_type, e.tmdb_id)
            if key not in bad or (e.first_failed_at and e.first_failed_at < bad[key]):
                bad[key] = e.first_failed_at
        self.bad = bad
        from services.provider_health import load_state
        self.providers = load_state(db)
        self.indexers = protected_indexers(db)
        self.qbit_tag = (get_setting(db, SETTING_QBIT_TAG, "") or "").strip()

    def arr(self, media_type: str):
        """The Radarr/Sonarr service, or None when it isn't configured."""
        if media_type not in self._arr:
            self._arr[media_type] = _arr_service(self.db, media_type)
        return self._arr[media_type]

    def title(self, media_type: str, tmdb_id: int) -> Optional[dict]:
        """The arr's title for this TMDB id; raises when it can't be read."""
        if media_type not in self._titles:
            svc = self.arr(media_type)
            rows = (svc.get_all_movies(raise_errors=True) if media_type == "movie"
                    else svc.get_all_series(raise_errors=True)) or []
            self._titles[media_type] = {t.get("tmdbId"): t for t in rows if t.get("tmdbId")}
        return self._titles[media_type].get(tmdb_id)

    def files(self, media_type: str, arr_id: int) -> list:
        key = (media_type, arr_id)
        if key not in self._files:
            svc = self.arr(media_type)
            self._files[key] = (svc.get_movie_files(arr_id) if media_type == "movie"
                                else svc.get_episode_files(arr_id)) or []
        return self._files[key]

    def history(self, media_type: str, arr_id: int) -> list:
        key = (media_type, arr_id)
        if key not in self._history:
            self._history[key] = self.arr(media_type).get_history(arr_id) or []
        return self._history[key]

    def seeding_hashes(self) -> Optional[set]:
        """Hashes of the torrents qBittorrent still has (with the tag, when
        one is set). None when qBittorrent isn't connected; raises when it
        can't be read."""
        if self._qbit is None and self._qbit_error is None:
            from services import qbittorrent
            try:
                self._qbit = qbittorrent.torrent_hashes(self.db, self.qbit_tag)
                if self._qbit is None:
                    self._qbit_error = "not connected"
            except Exception as e:
                logger.warning(f"[Duplicates rule] could not read qBittorrent: {e}")
                self._qbit_error = "unreadable"
        if self._qbit_error == "unreadable":
            raise RuntimeError("qBittorrent couldn't be read")
        return self._qbit

    def played_names(self) -> set:
        """File names (case-folded) of every movie and episode any Jellyfin
        user has played; raises when Jellyfin can't be read."""
        if self._played is None and self._played_error is None:
            try:
                self._played = self._read_played()
            except Exception as e:
                logger.warning(f"[Duplicates rule] could not read what was watched in Jellyfin: {e}")
                self._played_error = str(e) or "unreadable"
        if self._played is None:
            raise RuntimeError(self._played_error)
        return self._played

    def _read_played(self) -> set:
        url, key = get_setting(self.db, "jellyfin_url", ""), get_setting(self.db, "jellyfin_api_key", "")
        if not (url and key):
            raise RuntimeError("Jellyfin isn't set up")
        from services.jellyfin import JellyfinService
        from services.duplicates import _jf_paged
        jf = JellyfinService(url, key)
        users = jf.get_user_ids()
        if users is None:
            raise RuntimeError("could not list Jellyfin's users")
        names = set()
        for uid in users:
            for item in _jf_paged(jf, {"userId": uid, "IsPlayed": "true", "Recursive": "true",
                                       "IncludeItemTypes": "Movie,Episode", "Fields": "Path",
                                       "EnableImages": "false", "EnableUserData": "false"}):
                names.add(_name(item.get("Path")))
        names.discard("")
        return names


def _name(path: Optional[str]) -> str:
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _provider_id(dup, record) -> Optional[int]:
    src = record.source if record is not None and (record.source or "").startswith("provider_") else None
    src = src or next((s.get("source") for s in dup.sources or []
                       if (s.get("source") or "").startswith("provider_")), None)
    try:
        return int(src.split("_", 1)[1]) if src else None
    except (ValueError, IndexError):
        return None


def _grabs(history: list) -> list:
    """The grabs whose download was imported (the files on disk now came
    from one of them); all grabs when none matches (an older history)."""
    grabs = [h for h in history if (h.get("eventType") or "").lower() == "grabbed"]
    imported = {h.get("downloadId") for h in history
                if (h.get("eventType") or "").lower() == "downloadfolderimported" and h.get("downloadId")}
    matched = [g for g in grabs if g.get("downloadId") in imported]
    return matched or grabs


def _is_torrent(grab: dict) -> bool:
    data = grab.get("data") or {}
    return str(data.get("protocol") or "").lower() in ("2", "torrent") or bool(data.get("torrentInfoHash"))


def _hash(grab: dict) -> str:
    data = grab.get("data") or {}
    return (data.get("torrentInfoHash") or grab.get("downloadId") or "").lower()


def decide(db, dup, rule: str, facts: _Facts) -> dict:
    """What the rule does with one pending duplicate: {action, why}, where
    action is keep_download / keep_stream / skip. Reads only."""
    model = Movie if dup.media_type == "movie" else Series
    record = db.query(model).filter(model.tmdb_id == dup.tmdb_id).first()
    out = {"dup_id": dup.id, "tmdb_id": dup.tmdb_id, "media_type": dup.media_type,
           "title": (record.title if record is not None else None) or f"tmdb:{dup.tmdb_id}"}

    def result(action, why):
        return {**out, "action": action, "why": why}

    is_series = dup.media_type != "movie"
    arr_name = "Sonarr" if is_series else "Radarr"
    if not is_series and not any((s.get("source") or "") == "radarr" for s in dup.sources or []):
        return result(SKIP, "both copies are streams: no download to keep")
    if facts.arr(dup.media_type) is None:
        return result(SKIP, f"{arr_name} isn't set up")
    try:
        title = facts.title(dup.media_type, dup.tmdb_id)
        files = facts.files(dup.media_type, title["id"]) if title else []
    except Exception as e:
        logger.warning(f"[Duplicates rule] tmdb:{dup.tmdb_id}: could not read {arr_name}: {e}")
        return result(SKIP, f"couldn't read {arr_name}")
    if not title:
        return result(SKIP, f"{arr_name} doesn't list it")
    real = [f for f in files if is_downloaded_file(f.get("path"))]
    if not real:
        return result(SKIP, f"nothing downloaded: {arr_name} has no real file of it")
    only_stream, only_download = episodes_only_in(files, dup) if is_series else ([], [])

    def keep_download(why):
        # Keep Downloaded deletes every .strm of the show: never while an
        # episode exists only as a stream (#800).
        if only_stream:
            return result(SKIP, f"{why}, but {_episodes(only_stream)} exist only as streams")
        return result(KEEP_DOWNLOAD, why)

    if rule == "download":
        return keep_download("download first")

    # Stream first.
    if record is not None and record.prefer_download:
        return keep_download("on the prefer-download list")
    if (dup.media_type, dup.tmdb_id) in facts.bad:
        since = facts.bad[(dup.media_type, dup.tmdb_id)]
        return keep_download("stream known bad" + (f" since {since:%Y-%m-%d}" if since else ""))
    if not vod_copy_on_disk(dup, record):
        return keep_download("no stream copy on disk")
    pid = _provider_id(dup, record)
    health = facts.providers.get(str(pid)) if pid is not None else None
    state = (health or {}).get("state")
    if state == "failing":
        return result(SKIP, "not sure: the provider isn't answering")
    if state != "ok":
        return result(SKIP, "not sure: the provider hasn't been checked yet")
    try:
        grabs = _grabs(facts.history(dup.media_type, title["id"]))
    except Exception as e:
        logger.warning(f"[Duplicates rule] tmdb:{dup.tmdb_id}: could not read {arr_name}'s history: {e}")
        return result(SKIP, f"couldn't read {arr_name}'s history")
    protected = next((g for g in grabs
                      if ((g.get("data") or {}).get("indexer") or "").casefold() in facts.indexers), None)
    if protected:
        return keep_download(f"from the protected indexer {(protected.get('data') or {}).get('indexer')}")
    torrents = [g for g in grabs if _is_torrent(g)]
    if torrents:
        try:
            hashes = facts.seeding_hashes()
        except Exception:
            return result(SKIP, "not sure: qBittorrent couldn't be read to see whether it still seeds")
        if hashes is None:
            return result(SKIP, "not sure: a torrent, and qBittorrent isn't connected to tell whether it still seeds")
        if any(_hash(g) in hashes for g in torrents):
            return result(SKIP, "still seeding in qBittorrent")
    if only_download:
        return result(SKIP, f"{_episodes(only_download)} exist only as downloads")
    try:
        played = facts.played_names()
    except Exception:
        return result(SKIP, "not sure: Jellyfin couldn't say whether the download was watched")
    unwatched = [f for f in real if _name(f.get("path")) not in played]
    if unwatched:
        return result(SKIP, "the download hasn't been watched yet" if not is_series
                      else f"{_plural(len(unwatched), 'downloaded episode')} not watched yet")
    return result(KEEP_STREAM, "stream first")


def _pending(db) -> list:
    """Pending duplicates, one per title (twins are resolved with it)."""
    seen, out = set(), []
    for d in db.query(Duplicate).filter(Duplicate.resolution == "pending").order_by(Duplicate.id).all():
        key = (d.media_type, d.tmdb_id)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def preview(db, rule: Optional[str] = None) -> dict:
    """What the rule would do now (rule = a mode; default the saved one, and
    "download" while it is off). Changes nothing."""
    rule = rule if rule in ("download", "stream") else (mode(db) if mode(db) != "off" else "download")
    facts = _Facts(db)
    decisions = [decide(db, d, rule, facts) for d in _pending(db)]
    return _summary(rule, decisions, "dry-run", cap(db))


def _summary(rule: str, decisions: list, state: str, limit: int, note: str = "") -> dict:
    counts = {KEEP_DOWNLOAD: 0, KEEP_STREAM: 0, SKIP: 0, "failed": 0}
    for d in decisions:
        counts["failed" if d.get("failed") else d["action"]] += 1
    reasons = {}
    for d in decisions:
        if d["action"] == SKIP:
            key = re.sub(r"\d+", "N", re.sub(r" \(first: [^)]*\)", "", d["why"]))
            reasons[key] = reasons.get(key, 0) + 1
    return {"at": datetime.now(timezone.utc).isoformat(), "mode": rule, "state": state, "cap": limit,
            "pending": len(decisions), "changes": counts[KEEP_DOWNLOAD] + counts[KEEP_STREAM],
            "counts": counts, "skip_reasons": reasons, "note": note,
            "items": sorted(decisions, key=lambda d: (d["action"] == SKIP, d["title"].casefold()))[:MAX_LISTED]}


def run(db, confirm: Optional[int] = None) -> dict:
    """One run of the rule in its saved mode. Over the cap it changes nothing
    and keeps the list ("held") unless `confirm` (the number of changes the
    admin was shown) covers what it would change now. Returns the summary."""
    rule = mode(db)
    if rule == "off":
        return {"state": "off"}
    if not _run_lock.acquire(blocking=False):
        return {"state": "busy"}
    try:
        return _run(db, rule, confirm)
    finally:
        _run_lock.release()


def _run(db, rule: str, confirm: Optional[int]) -> dict:
    from fastapi import HTTPException
    from routers.duplicates import _resolve_lock, _current_resolution, _resolve
    limit = cap(db)
    facts = _Facts(db)
    decisions = [decide(db, d, rule, facts) for d in _pending(db)]
    changes = [d for d in decisions if d["action"] != SKIP]
    if len(changes) > limit and not (confirm is not None and len(changes) <= confirm):
        before = last_run(db) or {}
        summary = _summary(rule, decisions, "held", limit,
                           f"{_plural(len(changes), 'title')} would change, more than the cap of {limit}: "
                           f"nothing was changed. Apply them, or raise the cap.")
        if before.get("state") != "held" or before.get("changes") != len(changes):
            log_activity(db, "duplicates_rule", f"Duplicates rule waiting: {summary['note']}")
            logger.warning(f"[Duplicates rule] {summary['note']}")
        _save(db, summary)
        return summary
    for d in changes:
        with _resolve_lock:
            if _current_resolution(db, d["dup_id"]) != "pending":
                d.update(action=SKIP, why="resolved meanwhile")
                continue
            dup = db.query(Duplicate).filter(Duplicate.id == d["dup_id"]).first()
            try:
                _resolve(dup, RESOLUTION[d["action"]], db, reason=LOG_REASON, why=f"duplicates rule: {d['why']}")
            except HTTPException as e:
                db.rollback()
                d.update(failed=True, why=f"{d['why']}; not done: {e.detail}")
            except Exception as e:
                db.rollback()
                logger.exception(f"[Duplicates rule] tmdb:{d['tmdb_id']}: failed")
                d.update(failed=True, why=f"{d['why']}; not done: {e}")
    summary = _summary(rule, decisions, "done", limit)
    done = summary["counts"][KEEP_DOWNLOAD] + summary["counts"][KEEP_STREAM]
    if done or summary["counts"]["failed"]:
        log_activity(db, "duplicates_rule",
                     f"Duplicates rule ({'download' if rule == 'download' else 'stream'} first): "
                     f"kept {summary['counts'][KEEP_DOWNLOAD]} download(s), {summary['counts'][KEEP_STREAM]} "
                     f"stream(s), left {summary['counts'][SKIP]}, {summary['counts']['failed']} failed")
        _changed()
    _save(db, summary)
    return summary


def _save(db, summary: dict) -> None:
    set_setting(db, SETTING_LAST, json.dumps(summary, default=str))
    db.commit()


def _changed() -> None:
    try:
        from services import live_updates
        live_updates.publish("health", admins=True)
    except Exception:
        pass


def run_scheduled() -> None:
    """The RUN_MINUTES job: nothing at all while the rule is off."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        if mode(db) != "off":
            run(db)
    except Exception:
        logger.exception("[Duplicates rule] run failed")
    finally:
        db.close()


def _arr_service(db, media_type: str):
    prefix = "radarr" if media_type == "movie" else "sonarr"
    url, key = get_setting(db, f"{prefix}_url"), get_setting(db, f"{prefix}_api_key")
    if not (url and key):
        return None
    if media_type == "movie":
        from services.radarr import RadarrService
        return RadarrService(url, key)
    from services.sonarr import SonarrService
    return SonarrService(url, key)


def recycle_bins(db) -> dict:
    """{"Radarr": True/False/None, "Sonarr": ...}: whether each has a recycle
    bin (stream first deletes downloads through them); None = unknown."""
    out = {}
    for media_type, name in (("movie", "Radarr"), ("series", "Sonarr")):
        svc = _arr_service(db, media_type)
        if svc is None:
            continue
        try:
            out[name] = bool(svc.get_recycle_bin())
        except Exception:
            out[name] = None
    return out

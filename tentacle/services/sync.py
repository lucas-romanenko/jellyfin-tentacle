"""
Tentacle - Sync Engine
Core sync logic for VOD content.
Replaces xtream_to_jellyfin.py as a proper service.
"""

import os
import re
import zlib
import shutil
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple
import requests

from sqlalchemy.orm import Session

from models.database import (
    Provider, ProviderCategory, Movie, Series,
    SyncRun, CategorySnapshot, Duplicate, get_setting, log_deletion
)
from services.tmdb import TMDBService
from services.nfo import write_movie_nfo, write_series_nfo, make_folder_name, vod_folder_name
from services.cleaner import clean_title
from services.m3u_parser import episode_from_title, container_from_url
from services.duplicates import delete_vod_files, convert_record_to_downloaded
from services.media_files import delete_movie_files, delete_series_files
from services.tagger import compute_tags, get_list_tags_for_tmdb_id, apply_tag_rules
from services.exceptions import ProviderConnectionError, SyncCancelledError, SyncError, TMDBConnectionError

logger = logging.getLogger(__name__)

MIN_DISK_SPACE_MB = 500
WARN_DISK_SPACE_MB = 2000
DISK_CHECK_INTERVAL = 50  # Check every N items

# Sentinel offset for synthetic (no-TMDB-match) negative tmdb_ids.
# Each provider gets a 1-billion-wide block so a large Xtream stream_id can
# never collide with another provider's range (modern stream_ids can exceed
# the old 10M window). Keep this large enough that stream_id never overflows
# into the next provider's block.
NEGATIVE_ID_BLOCK = 1_000_000_000


# Ownership for created VOD files/dirs (linuxserver-style PUID/PGID).
# Tentacle runs as root; on a hybrid show (VOD .strm + Sonarr downloads in one
# folder) whichever container creates a season folder FIRST owns it. When
# Tentacle wins that race — a new season hits VOD before Sonarr grabs anything —
# the folder is root-owned and Sonarr can no longer write into it. Setting
# PUID/PGID to Sonarr's user hands ownership over at creation time. Unset =
# current behavior (root-owned, fine for pure-VOD setups).
VOD_PUID = os.environ.get("PUID")
VOD_PGID = os.environ.get("PGID")


def chown_path(path) -> None:
    """Best-effort chown of a created VOD path to PUID/PGID. No-op when unset."""
    if VOD_PUID is None:
        return
    try:
        os.chown(path, int(VOD_PUID), int(VOD_PGID or VOD_PUID))
    except (OSError, ValueError) as e:
        logger.debug(f"chown_path failed for {path}: {e}")


_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov", ".wmv"}


def repair_hybrid_ownership(db) -> list:
    """Nightly repair for the ownership race on EXISTING hybrid shows (Series
    rows with sonarr_path set): chown the show dir, its season dirs, and any
    real video files not owned by PUID. Narrow on purpose — only hybrid shows
    are ever written to by Sonarr, so the huge pure-VOD catalog is never
    walked. No-op when PUID is unset."""
    if VOD_PUID is None:
        return []
    from models.database import Series as _Series
    uid = int(VOD_PUID)
    fixed = []
    hybrids = db.query(_Series).filter(_Series.sonarr_path.isnot(None),
                                       _Series.strm_path.isnot(None)).all()
    for s in hybrids:
        show_dir = Path(s.strm_path)
        if not show_dir.is_dir():
            continue
        targets = [show_dir] + [d for d in show_dir.iterdir() if d.is_dir()]
        for d in targets:
            try:
                changed = False
                if d.stat().st_uid != uid:
                    chown_path(d)
                    changed = True
                if d.is_dir():
                    for f in d.iterdir():
                        if f.suffix.lower() in _VIDEO_EXTS and f.is_file() and f.stat().st_uid != uid:
                            chown_path(f)
                            changed = True
                if changed:
                    fixed.append(str(d.relative_to(show_dir.parent)))
            except OSError as e:
                logger.warning(f"[Ownership repair] could not fix {d}: {e}")
    if fixed:
        logger.info(f"[Ownership repair] fixed ownership on: {fixed}")
    return fixed


def check_disk_space(path: Path) -> int:
    """Check available disk space in MB. Returns available MB."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free // (1024 * 1024)
    except OSError:
        return -1  # Can't check, skip


def _check_disk_before_sync(path: Path):
    """Raise SyncError if disk space is critically low before sync starts."""
    available_mb = check_disk_space(path)
    if available_mb == -1:
        return
    if available_mb < MIN_DISK_SPACE_MB:
        raise SyncError(
            f"Insufficient disk space: only {available_mb} MB available on {path}. "
            f"Free up space before syncing."
        )
    if available_mb < WARN_DISK_SPACE_MB:
        logger.warning(f"Low disk space: {available_mb} MB available on {path}")


def _check_disk_during_sync(path: Path):
    """Raise SyncCancelledError if disk space drops critically low during sync."""
    available_mb = check_disk_space(path)
    if available_mb == -1:
        return
    if available_mb < MIN_DISK_SPACE_MB:
        raise SyncCancelledError(f"Sync stopped: disk full ({available_mb} MB remaining on {path})")
    if available_mb < WARN_DISK_SPACE_MB:
        logger.warning(f"Low disk space: {available_mb} MB available on {path}")

XTREAM_HEADERS = {"User-Agent": "TiviMate/4.7.0 (Linux; Android 12)"}


# ── Xtream API ─────────────────────────────────────────────────────────────

class XtreamClient:
    def __init__(self, provider: Provider):
        self.base = f"{provider.server_url.rstrip('/')}/player_api.php?username={provider.username}&password={provider.password}"
        self.server = provider.server_url.rstrip('/')
        self.username = provider.username
        self.password = provider.password
        self.session = requests.Session()
        self.session.headers.update(XTREAM_HEADERS)
        # requests ignores a `timeout` attribute on a Session; it has to be
        # passed per call. Without it a stalled panel hung the sync for ever.
        self.timeout = 30
        # A services.provider_activity.JobPause when the sync must stand
        # aside for live TV / a recording. It is called at CATEGORY
        # boundaries, never between calls inside one, because a category's
        # writes are committed once at its end: pausing with writes pending
        # would hold the SQLite lock for as long as the pause lasts, and every
        # other write in Tentacle (webhooks, settings) would fail meanwhile.
        self.job_pause = None
        self.provider_id = getattr(provider, "id", None)
        # A services.vod_tokens.Links when VOD is served through Tentacle
        # (setting `vod_via_tentacle_enabled`): the .strm files then point
        # at Tentacle's /api/vod route instead of at the provider.
        self.vod_links = None

    def _get(self, action: str, extra: str = "") -> list:
        try:
            r = self.session.get(f"{self.base}&action={action}{extra}", timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except requests.ConnectionError as e:
            raise ProviderConnectionError(self.username, str(e))

    def get_vod_streams(self, category_id: str) -> list:
        return self._get(f"get_vod_streams&category_id={category_id}")

    def get_series_list(self, category_id: str) -> list:
        return self._get(f"get_series&category_id={category_id}")

    def get_series_info(self, series_id: str) -> dict:
        r = self.session.get(f"{self.base}&action=get_series_info&series_id={series_id}",
                             timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def movie_stream_url(self, stream_id, container="mp4") -> str:
        if self.vod_links is not None:
            return self.vod_links.movie(stream_id, container)
        return f"{self.server}/movie/{self.username}/{self.password}/{stream_id}.{container}"

    def episode_stream_url(self, episode_id, container="mp4") -> str:
        if self.vod_links is not None:
            return self.vod_links.episode(episode_id, container)
        return f"{self.server}/series/{self.username}/{self.password}/{episode_id}.{container}"


def vod_links_for(db: Session, provider: Provider):
    """The Links the sync client writes .strm files with, or None for direct
    provider URLs. Needs the setting on, an Xtream provider, and Tentacle's
    own address as clients reach it (the one the YouTube feature already
    works out and keeps)."""
    from routers.vod import vod_enabled
    from services import vod_tokens
    if not vod_enabled(db) or (provider.provider_type or "xtream") != "xtream":
        return None
    # `vod_base_url` first: the address every PLAYER reaches Tentacle at on
    # the LAN. The YouTube address is the fallback; where that is a public
    # name behind a tunnel or an access gate, every film would go out and
    # back in through it, so a LAN address here is the right choice.
    base = (get_setting(db, "vod_base_url", "") or "").strip().rstrip("/")
    if not base:
        from services.youtube.sync import base_url
        base = base_url(db)
    if not base:
        logger.warning("[Sync] VOD through Tentacle is on but Tentacle's address is not known "
                       "(set vod_base_url, or Settings → YouTube → Tentacle address); writing direct provider URLs")
        return None
    return vod_tokens.Links(base, vod_tokens.token_secret(db), provider.id)


WAITING_FOR_LIVE_TV = "Waiting for live TV / a recording to finish before continuing"


def _pause_between_categories(client, db: Session, progress_callback=None, phase: str = "",
                              category: str = "", stats: dict = None) -> None:
    """Stand aside for live TV / a recording, with nothing left uncommitted.
    A sync started from the dashboard says on screen why it is not moving."""
    pause = getattr(client, "job_pause", None)
    if pause is None:
        return
    db.commit()
    if progress_callback and pause.would_wait():
        progress_callback(phase, category, stats or {}, item_title=WAITING_FOR_LIVE_TV, item_pos=0, item_total=0)
    cancel_check = getattr(pause, "cancel_check", None)
    if not pause() and cancel_check and cancel_check():
        # Cancelled while waiting: stop here, not after another category's
        # provider calls and TMDB lookups.
        raise SyncCancelledError("Sync cancelled while waiting for live TV to finish")


# The provider stream a .strm plays, as (kind, id): a direct Xtream URL, the
# same wrapped by a resume proxy (URL-encoded in a query parameter), or
# Tentacle's own /api/vod address. None for anything else (a hand-made file).
_DIRECT_STREAM_RE = re.compile(r"/(movie|series)/[^/]+/[^/]+/(\d+)\.[A-Za-z0-9]+")


def _stream_ref(url_text: str, unwrap: bool = False):
    """`unwrap` also looks inside a URL-encoded query parameter (a resume
    proxy wrapping the provider URL) -- only when migrating such files TO
    Tentacle's own route; with VOD through Tentacle off, a hand-made proxy
    file is somebody's deliberate setup and is left alone."""
    from urllib.parse import unquote
    from services import vod_tokens
    via_tentacle = vod_tokens.stream_id_in_url(url_text)
    if via_tentacle:
        return via_tentacle
    for candidate in ((url_text, unquote(url_text)) if unwrap else (url_text,)):
        m = _DIRECT_STREAM_RE.search(candidate or "")
        if m:
            return m.group(1), int(m.group(2))
    return None


def _strm_needs_rewrite(strm_file: Path, expected: str, client) -> bool:
    """An existing .strm is rewritten only when it plays the SAME provider
    stream as the sync would write today, in a different form: switching to
    or from Tentacle's VOD address, new credentials or host, or a resume
    proxy wrapping the provider URL. A file that plays a different stream is
    left alone -- a provider that lists one title in two categories offers
    it twice, and rewriting to whichever listing came last would flip the
    file every night (and change which copy plays). A file that points
    somewhere else entirely was set up by hand and is left alone too."""
    try:
        current = strm_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    if not current or current == expected:
        return False
    from services import vod_tokens
    current_ref = _stream_ref(current, unwrap=vod_tokens.is_vod_url(expected))
    return current_ref is not None and current_ref == _stream_ref(expected)


# ── M3U provider support ─────────────────────────────────────────────────
# An M3U playlist has no categories/streams API — it's a flat list. M3UClient
# parses it once and exposes the SAME interface as XtreamClient so the entire
# VOD sync pipeline (_sync_movies / _sync_series) works unchanged. Movies vs
# series are classified by URL path (/movie/ vs /series/) and SxxExx markers in
# the title; the M3U group-title is used as the category.

# Stable positive id from a string, kept < NEGATIVE_ID_BLOCK so the synthetic
# negative tmdb_id math (require_tmdb=False) can't overflow into other providers.
def _m3u_id(s: str) -> int:
    return zlib.crc32(s.encode("utf-8", "ignore")) % NEGATIVE_ID_BLOCK


class M3UClient:
    """XtreamClient-compatible adapter backed by a parsed M3U playlist."""

    def __init__(self, provider: Provider):
        self.provider = provider
        self._loaded = False
        self._movies: dict = {}        # group-title -> [stream dict]
        self._series: dict = {}        # group-title -> { show -> { season(str) -> [ep dict] } }
        self._series_info: dict = {}   # series_id -> {"episodes": {season: [ep dict]}}
        self._urls: dict = {}          # stream_id / ep_id -> direct url

    def _load(self):
        if self._loaded:
            return
        ua = self.provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)"
        try:
            if self.provider.provider_type == "m3u_file":
                from services.m3u_parser import parse_m3u_from_file
                entries = parse_m3u_from_file(self.provider.m3u_url)
            else:
                from services.m3u_parser import parse_m3u_from_url
                entries = parse_m3u_from_url(self.provider.m3u_url, user_agent=ua)
        except Exception as e:
            raise ProviderConnectionError(self.provider.name, str(e))

        for e in entries:
            url = (e.get("stream_url") or "").strip()
            name = (e.get("name") or "").strip()
            if not url or not name:
                continue
            group = (e.get("group_title") or "").strip() or "Uncategorized"
            low = url.lower()
            container = container_from_url(url)
            ep = episode_from_title(name)
            is_series = ("/series/" in low) or (ep is not None and "/movie/" not in low)
            if is_series:
                if not ep:
                    continue  # series-ish but no parseable SxxExx — can't place it
                show, season, epnum = ep
                if not show:
                    show = name
                ep_id = _m3u_id(url)
                self._urls[ep_id] = url
                seasons = self._series.setdefault(group, {}).setdefault(show, {})
                seasons.setdefault(str(season), []).append(
                    {"id": ep_id, "episode_num": epnum, "container_extension": container}
                )
            else:
                sid = _m3u_id(url)
                self._urls[sid] = url
                self._movies.setdefault(group, []).append({
                    "stream_id": sid, "name": name, "category_id": group,
                    "container_extension": container, "stream_icon": e.get("logo_url") or "",
                })

        for group, shows in self._series.items():
            for show, seasons in shows.items():
                self._series_info[_m3u_id(f"{group}|{show}")] = {"episodes": seasons}
        self._loaded = True

    # ── Category discovery (fetch-categories) ──
    def get_vod_categories(self) -> list:
        self._load()
        return [{"category_id": g, "category_name": g} for g in sorted(self._movies)]

    def get_series_categories(self) -> list:
        self._load()
        return [{"category_id": g, "category_name": g} for g in sorted(self._series)]

    def vod_counts(self) -> dict:
        self._load()
        return {g: len(v) for g, v in self._movies.items()}

    def series_counts(self) -> dict:
        self._load()
        return {g: len(shows) for g, shows in self._series.items()}

    # ── XtreamClient-compatible interface used by _sync_movies / _sync_series ──
    def get_vod_streams(self, category_id: str) -> list:
        self._load()
        return list(self._movies.get(category_id, []))

    def get_series_list(self, category_id: str) -> list:
        self._load()
        shows = self._series.get(category_id, {})
        return [
            {"series_id": _m3u_id(f"{category_id}|{show}"), "name": show,
             "category_id": category_id, "cover": ""}
            for show in sorted(shows)
        ]

    def get_series_info(self, series_id) -> dict:
        self._load()
        try:
            return self._series_info.get(int(series_id), {"episodes": {}})
        except (TypeError, ValueError):
            return {"episodes": {}}

    def movie_stream_url(self, stream_id, container="mp4") -> str:
        try:
            return self._urls.get(int(stream_id), "")
        except (TypeError, ValueError):
            return ""

    def episode_stream_url(self, episode_id, container="mp4") -> str:
        try:
            return self._urls.get(int(episode_id), "")
        except (TypeError, ValueError):
            return ""


def _unhidden(name: str) -> str:
    return name.lstrip(". ")


def unhide_vod_paths(db: Session) -> int:
    """Move VOD titles written under a dot-prefixed name to a visible one.

    Jellyfin never scans a path matching `**/.*`, so these titles were on disk
    but missing from the library. Renames the folder and every file in it
    whose name starts with the old stem, then updates the stored paths. Skips
    a title when the target name is already taken rather than merging folders.
    Returns the number of titles moved.
    """
    moved = 0
    for Model in (Movie, Series):
        rows = db.query(Model).filter(Model.source.like("provider_%"),
                                      Model.strm_path.isnot(None)).all()
        for row in rows:
            strm = Path(row.strm_path)
            folder = strm if Model is Series else strm.parent
            if not folder.name.startswith("."):
                continue
            new_folder = folder.with_name(_unhidden(folder.name))
            if not _unhidden(folder.name) or new_folder.exists() or not folder.is_dir():
                continue
            old_stem = folder.name
            folder.rename(new_folder)
            for f in list(new_folder.rglob("*")):
                if f.is_file() and f.name.startswith(old_stem):
                    f.rename(f.with_name(_unhidden(old_stem) + f.name[len(old_stem):]))
            def _fix(p):
                if not p:
                    return p
                p = Path(p)
                rel = p.relative_to(folder) if p != folder else None
                if rel is None:
                    return str(new_folder)
                parts = list(rel.parts)
                if parts and parts[-1].startswith(old_stem):
                    parts[-1] = _unhidden(old_stem) + parts[-1][len(old_stem):]
                return str(new_folder.joinpath(*parts))
            row.strm_path = _fix(row.strm_path)
            row.nfo_path = _fix(row.nfo_path) if row.nfo_path and Path(row.nfo_path).is_relative_to(folder) else row.nfo_path
            row.jellyfin_item_id = None
            moved += 1
            logger.info(f"Moved hidden VOD folder so Jellyfin can see it: {folder.name} -> {new_folder.name}")
    if moved:
        db.commit()
    return moved


def make_provider_client(provider: Provider):
    """Return the right VOD client for a provider based on provider_type."""
    if (provider.provider_type or "xtream") in ("m3u_url", "m3u_file"):
        return M3UClient(provider)
    return XtreamClient(provider)


# ── Tag Merging ──────────────────────────────────────────────────────────

def _merge_source_tag(tmdb_id: int, media_type: str, source_tag: str, provider_id: int, db: Session):
    """
    When a movie/series already exists but appears in another category,
    merge the new category's source tag into the existing record's tags.
    Also updates the NFO file if it exists.
    """
    if not source_tag:
        return

    type_label = "Movies" if media_type == "movie" else "TV"
    new_tag = f"{source_tag} {type_label}"

    Model = Movie if media_type == "movie" else Series
    record = db.query(Model).filter(
        Model.tmdb_id == tmdb_id,
        Model.provider_id == provider_id
    ).first()

    if not record:
        return

    tags = record.tags or []
    if new_tag in tags:
        return

    tags.append(new_tag)
    record.tags = list(tags)  # Force SQLAlchemy to detect mutation
    record.date_updated = datetime.utcnow()

    # Update NFO file with new tags
    nfo_path = record.nfo_path
    if nfo_path:
        try:
            from pathlib import Path
            nfo_file = Path(nfo_path)
            if nfo_file.exists():
                content = nfo_file.read_text(encoding='utf-8')
                if f"<tag>{new_tag}</tag>" not in content:
                    # Insert new tag before closing </movie> or </tvshow>
                    for closing_tag in ['</movie>', '</tvshow>']:
                        if closing_tag in content:
                            content = content.replace(
                                closing_tag,
                                f"    <tag>{new_tag}</tag>\n{closing_tag}"
                            )
                            nfo_file.write_text(content, encoding='utf-8')
                            break
        except Exception:
            pass  # NFO update is best-effort


def _write_episode_strms(client: "XtreamClient", episodes: dict, show_dir: Path, folder_name: str) -> int:
    """Write .strm files for any episodes that don't already exist on disk.

    Returns the number of NEW episode files written. Safe to call on an
    existing series to back-fill newly-added seasons/episodes (idempotent —
    existing .strm files are left untouched).
    """
    ep_count = 0
    for season_num, eps in episodes.items():
        if isinstance(eps, list) and eps and isinstance(eps[0], list):
            eps = eps[0]
        if not isinstance(eps, list):
            continue

        try:
            season_int = int(season_num)
        except (TypeError, ValueError):
            continue

        season_dir = show_dir / f"Season {season_int:02d}"
        season_dir.mkdir(parents=True, exist_ok=True)
        chown_path(season_dir)

        for ep in eps:
            if not isinstance(ep, dict):
                continue
            ep_id = ep.get("id")
            ep_num = ep.get("episode_num", 0)
            container = ep.get("container_extension", "mp4")
            try:
                ep_filename = f"{folder_name} S{season_int:02d}E{int(ep_num):02d}"
            except (TypeError, ValueError):
                continue
            strm_file = season_dir / f"{ep_filename}.strm"
            expected = client.episode_stream_url(ep_id, container)
            if not strm_file.exists():
                strm_file.write_text(expected, encoding='utf-8')
                chown_path(strm_file)
                ep_count += 1
            elif _strm_needs_rewrite(strm_file, expected, client):
                strm_file.write_text(expected, encoding='utf-8')
                chown_path(strm_file)
                logger.info(f"[Sync] Rewrote {strm_file.name}: stream address changed")
    return ep_count


def _repair_movie_strm(client, stream: dict, tmdb_id: int, provider: Provider, db: Session) -> bool:
    """Rewrite an existing movie's .strm when it has gone missing from disk.

    Series already self-heal via _backfill_series_episodes; movies did not, so a
    deleted .strm was never restored — which also meant re-enabling the .strm
    opt-out on a movie did nothing, despite the UI saying the files would be
    kept up to date. Opted-out titles are skipped, since not writing their files
    is the point. Best-effort: any error is swallowed so one bad title can't
    break the category batch.
    """
    record = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if not record or record.provider_id != provider.id or record.strm_disabled:
        return False
    if not record.strm_path:
        return False
    try:
        strm = Path(record.strm_path)
        expected = client.movie_stream_url(stream.get("stream_id"), stream.get("container_extension", "mp4"))
        if strm.exists():
            if _strm_needs_rewrite(strm, expected, client):
                strm.write_text(expected, encoding="utf-8")
                chown_path(strm)
                logger.info(f"[Sync] Rewrote {strm.name}: stream address changed")
            return False
        strm.parent.mkdir(parents=True, exist_ok=True)
        chown_path(strm.parent)
        strm.write_text(expected, encoding="utf-8")
        chown_path(strm)
        # The NFO goes with it when the whole folder was lost (or an opt-out
        # deleted both) — without it Jellyfin has to guess the match again.
        nfo = strm.with_suffix(".nfo")
        if not nfo.exists():
            write_movie_nfo(nfo, {
                "tmdb_id": record.tmdb_id, "title": record.title, "year": record.year,
                "overview": record.overview, "runtime": record.runtime, "rating": record.rating,
                "genres": record.genres or [], "poster_path": record.poster_path,
                "backdrop_path": record.backdrop_path,
            }, record.tags or [])
            chown_path(nfo)
        record.date_updated = datetime.utcnow()
        logger.info(f"[Sync] Restored missing .strm for existing movie '{record.title}'")
        return True
    except Exception as e:
        logger.debug(f"[Sync] .strm repair failed for tmdb_id={tmdb_id}: {e}")
        return False


def _backfill_series_episodes(
    client: "XtreamClient",
    series: dict,
    tmdb_id: int,
    provider: Provider,
    db: Session,
) -> int:
    """For an EXISTING VOD series owned by this provider, fetch series info and
    write any newly-added season/episode .strm files. Returns count of new files.

    No-ops for series not owned by this provider or without a known folder.
    Best-effort: any provider/IO error is swallowed so a single bad series
    doesn't break the category batch.
    """
    record = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
    if not record or record.provider_id != provider.id:
        return 0
    if record.strm_disabled:
        # The user switched this title to downloaded copies. Regenerating its
        # .strm files every night would put two sources in the same folder.
        logger.debug(f"[Sync] Skipping .strm repair for '{record.title}' (strm management disabled)")
        return 0
    show_dir_str = record.strm_path
    if not show_dir_str:
        return 0
    show_dir = Path(show_dir_str)
    recreate = not show_dir.exists()
    if recreate:
        # The whole show folder is gone (failed disk, restore, an opt-out with
        # "delete files" switched back on). Returning here left the row to the
        # VOD sweep, which deleted it, and the next sync re-imported the title as
        # new. Rebuild it instead — but only when the library root is there: a
        # missing or empty mount point means storage is unavailable.
        root = show_dir.parent
        if not root.is_dir() or not any(root.iterdir()):
            return 0

    try:
        series_info = client.get_series_info(series.get("series_id"))
        episodes = series_info.get("episodes", {})
        if isinstance(episodes, list):
            episodes = {"1": episodes}
        if not episodes:
            return 0
        if recreate:
            show_dir.mkdir(parents=True, exist_ok=True)
            chown_path(show_dir)
            nfo = show_dir / "tvshow.nfo"
            write_series_nfo(nfo, {
                "tmdb_id": record.tmdb_id, "title": record.title, "year": record.year,
                "overview": record.overview, "genres": record.genres or [],
                "rating": record.rating, "status": record.status,
                "poster_path": record.poster_path, "backdrop_path": record.backdrop_path,
            }, record.tags or [])
            chown_path(nfo)
            logger.info(f"[Sync] Restored missing folder for existing series '{record.title}'")
        folder_name = show_dir.name
        new_eps = _write_episode_strms(client, episodes, show_dir, folder_name)
        if new_eps:
            record.date_updated = datetime.utcnow()
            logger.info(f"[Sync] Back-filled {new_eps} new episode(s) for existing series '{record.title}'")
        return new_eps
    except Exception as e:
        logger.debug(f"[Sync] Episode back-fill failed for tmdb_id={tmdb_id}: {e}")
        return 0


# ── Duplicate Detection ───────────────────────────────────────────────────

def check_and_record_duplicate(
    tmdb_id: int,
    media_type: str,
    source: str,
    path: str,
    provider: Provider,
    db: Session
) -> bool:
    """
    Check if this TMDB ID already exists from another source.
    Records duplicate if found. Returns True if a higher-priority source
    already owns this content (caller should skip creating files).
    """
    if media_type == "movie":
        existing = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    else:
        existing = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()

    dup = db.query(Duplicate).filter(
        Duplicate.tmdb_id == tmdb_id,
        Duplicate.media_type == media_type
    ).first()

    if not existing:
        # Tombstone: this duplicate was resolved as keep-downloaded — the
        # provider copy was deliberately removed, so never re-import it as
        # a "new" title just because the provider still offers it.
        if dup and dup.resolution == "keep_radarr":
            return True
        return False

    # Enforce a past keep-downloaded resolution: the provider copy must not
    # own this title. Also self-heals rows that were re-imported by older
    # versions, where resolving deleted the row instead of converting it.
    if dup and dup.resolution == "keep_radarr" and existing.source and existing.source.startswith("provider_"):
        if existing.strm_path:
            delete_vod_files(existing.strm_path)
        convert_record_to_downloaded(existing, media_type)
        logger.info(f"[Sync] Enforced keep-downloaded resolution for tmdb:{tmdb_id} — provider copy suppressed")
        return True

    new_source = {"source": source, "path": path}

    if dup:
        sources = dup.sources or []
        if not any(s["source"] == source for s in sources):
            sources.append(new_source)
            dup.sources = sources
    else:
        db.add(Duplicate(
            tmdb_id=tmdb_id,
            media_type=media_type,
            sources=[
                {"source": existing.source, "path": existing.strm_path or getattr(existing, 'radarr_path', None) or getattr(existing, 'sonarr_path', None) or ""},
                new_source
            ],
            resolution="pending"
        ))

    # Downloaded content always takes priority -- Sonarr's as much as Radarr's.
    # A "sonarr" row used to fall through to `return False` below, which tells
    # the caller to insert a second row for a UNIQUE tmdb_id.
    if existing.source in ("radarr", "sonarr"):
        return True

    # If existing is from another provider, decide which wins by priority.
    # tmdb_id is globally unique, so we must NEVER let the caller insert a
    # competing row — that would raise an IntegrityError and lose the whole
    # per-category batch. Either skip (existing wins) or update the existing
    # row in place to point at the higher-priority provider (new wins).
    if existing.provider_id and existing.provider_id != provider.id:
        existing_provider = db.query(Provider).filter(Provider.id == existing.provider_id).first()
        if existing_provider and existing_provider.priority <= provider.priority:
            return True  # Existing provider has equal or higher priority, skip

        # New provider is higher priority (lower number) — take over the
        # existing row instead of inserting a duplicate tmdb_id.
        existing.provider_id = provider.id
        existing.source = source
        existing.date_updated = datetime.utcnow()
        return True

    # Same provider (e.g. re-sync / appears in a second category of the same
    # provider): the existing row already belongs to us — don't insert again.
    if existing.provider_id == provider.id:
        return True

    # A row exists for this tmdb_id, whoever owns it. tmdb_id is unique, so
    # telling the caller to insert can only ever raise IntegrityError.
    return True


# A category must return nothing this many syncs in a row before we believe it
# genuinely emptied. Zeroing the count after a single empty response meant a
# two-night provider outage was accepted as real on the second night.
EMPTY_CATEGORY_STRIKES = 3


def _category_went_empty(db: Session, cat: ProviderCategory, returned: int) -> bool:
    """True when a category returned nothing but is known to hold titles.

    An HTTP 200 carrying an empty list is indistinguishable from a category
    that genuinely emptied, so the previous title count is the only signal we
    have — and it is kept at its last non-zero value the whole time, so the
    signal doesn't erase itself. Empty responses are counted instead, and only
    after EMPTY_CATEGORY_STRIKES in a row is the category believed and normal
    pruning allowed to resume.
    """
    if returned:
        if cat.consecutive_empty_syncs:
            cat.consecutive_empty_syncs = 0
            db.commit()
        return False

    if not cat.title_count:
        return False  # never held anything — nothing to protect

    cat.consecutive_empty_syncs = (cat.consecutive_empty_syncs or 0) + 1
    db.add(CategorySnapshot(category_id=cat.id, title_count=0, new_count=0))
    db.commit()

    if cat.consecutive_empty_syncs >= EMPTY_CATEGORY_STRIKES:
        logger.warning(
            f"Category '{cat.category_name}' has returned 0 titles "
            f"{cat.consecutive_empty_syncs} syncs in a row (it held {cat.title_count}) "
            f"— treating it as genuinely empty from now on"
        )
        return False

    return True


# A single run may never delete more than this share of a provider's rows.
# A genuine catalogue removal trickles; a provider outage arrives all at once.
PRUNE_MAX_FRACTION = 0.05
PRUNE_MIN_ALLOWANCE = 50
# A removal the cap refused is no longer "an outage" once every sync has agreed
# for this long. It is then processed gradually, at most the allowance per run,
# instead of being refused (and leaving dead .strm files in Jellyfin) forever.
PRUNE_BLOCKED_GRACE = timedelta(days=7)


def _clear_provider_marks(db: Session, provider: Provider, Model, seen_ids: set) -> None:
    """Clear provider_missing_since on every row the provider served this sync.

    Runs even when the prune itself is skipped because a fetch failed: a title
    that was served is positive evidence, and a stale mark must never count as
    the first strike of a later, unrelated absence.
    Filtered in Python: the marked set is small, and an IN clause over a
    26k-id seen set would blow SQLite's bound-variable limit.
    """
    for record in db.query(Model).filter(
        Model.provider_id == provider.id,
        Model.provider_missing_since.isnot(None),
    ).all():
        if record.tmdb_id in seen_ids:
            record.provider_missing_since = None


def _prune_removed_content(db: Session, provider: Provider, media_type: str, seen_ids: set) -> int:
    """Delete DB rows (and their .strm/.nfo files) for content this provider
    used to offer but didn't return during the latest successful sync.

    Two guards make a transient provider response harmless:

    * **Two strikes.** A title missing for the first time is only marked
      (``provider_missing_since``); it is deleted on the next run that still doesn't
      see it. Anything that reappears has the mark cleared.
    * **Blast radius.** A run refuses to delete more than 5% of the provider's
      rows (floor 50) and logs loudly instead, so a partial outage that slips
      past the per-category guard still can't wipe the library.

    Strictly scoped to rows owned by this provider. Returns count removed.
    """
    Model = Movie if media_type == "movie" else Series
    now = datetime.utcnow()

    # Anything the provider served again is healthy — clear our own mark only.
    _clear_provider_marks(db, provider, Model, seen_ids)

    # Diff in Python rather than with a NOT IN over the whole seen set — that
    # set runs to tens of thousands of ids on a large provider, past SQLite's
    # bound-variable limit.
    stale_pks = [
        pk for pk, tmdb_id in db.query(Model.id, Model.tmdb_id).filter(
            Model.provider_id == provider.id
        ).all()
        if tmdb_id not in seen_ids
    ]
    if not stale_pks:
        db.commit()
        return 0
    stale = []
    for i in range(0, len(stale_pks), 500):
        stale.extend(db.query(Model).filter(Model.id.in_(stale_pks[i:i + 500])).all())

    # First sighting of an absence is recorded, not acted on. Only this guard's
    # own mark counts as a first strike — a file that went missing on disk says
    # nothing about whether the provider still lists the title.
    confirmed = [r for r in stale if r.provider_missing_since is not None]
    newly_missing = [r for r in stale if r.provider_missing_since is None]
    for record in newly_missing:
        record.provider_missing_since = now
    if newly_missing:
        logger.info(
            f"[Sync] {len(newly_missing)} {media_type}(s) missing from provider "
            f"{provider.name} for the first time — marked, will be removed if "
            f"they are still gone next sync"
        )

    if not confirmed:
        db.commit()
        return 0

    total_rows = db.query(Model).filter(Model.provider_id == provider.id).count()
    allowance = max(PRUNE_MIN_ALLOWANCE, int(total_rows * PRUNE_MAX_FRACTION))
    if len(confirmed) > allowance:
        settled = sorted(
            (r for r in confirmed if now - r.provider_missing_since >= PRUNE_BLOCKED_GRACE),
            key=lambda r: r.provider_missing_since,
        )
        if not settled:
            logger.error(
                f"[Sync] REFUSING to prune {len(confirmed)} {media_type}(s) from provider "
                f"{provider.name}: that exceeds the safety limit of {allowance} "
                f"({int(PRUNE_MAX_FRACTION * 100)}% of {total_rows} rows). This looks like a "
                f"provider outage rather than a catalogue change — nothing was deleted. "
                f"If they are still missing after {PRUNE_BLOCKED_GRACE.days} days they will "
                f"be removed gradually, at most {allowance} per sync."
            )
            db.commit()
            log_deletion(
                db, kind="sync-prune-blocked", name=provider.name, media_type=media_type,
                reason="safety-limit",
                detail=f"{len(confirmed)} {media_type}(s) were absent from two consecutive syncs "
                       f"but exceed the {allowance}-row limit; nothing deleted",
            )
            return 0
        logger.warning(
            f"[Sync] {len(settled)} {media_type}(s) from provider {provider.name} have been "
            f"missing from every sync for over {PRUNE_BLOCKED_GRACE.days} days — removing "
            f"{min(len(settled), allowance)} this sync (safety limit {allowance})"
        )
        confirmed = settled[:allowance]

    removed = 0
    for record in confirmed:
        if media_type == "movie":
            # strm_path points at the .strm file itself.
            delete_movie_files(record.strm_path)
        else:
            # strm_path points at the show directory. Only Tentacle's own
            # .strm/.nfo files are removed — merged setups share this folder
            # with Sonarr downloads.
            delete_series_files(record.strm_path)

        # Clean up any duplicate records referencing this content
        db.query(Duplicate).filter(
            Duplicate.tmdb_id == record.tmdb_id,
            Duplicate.media_type == media_type,
        ).delete(synchronize_session=False)

        db.delete(record)
        removed += 1

    db.commit()
    if removed:
        log_deletion(
            db, kind="sync-prune", name=provider.name, media_type=media_type,
            reason="removed-upstream",
            detail=f"{removed} {media_type}(s) absent from two consecutive syncs",
        )
    return removed



# ── Orphaned VOD record sweep ──────────────────────────────────────────────

VOD_MOVIES_ROOT = Path("/media/vod/movies")
VOD_SERIES_ROOT = Path("/media/vod/shows")


def _sweep_one_type(db: Session, Model, media_type: str, root: Path, now: datetime):
    """Sweep one media type. Returns (removed_count, removed_titles)."""
    rows = db.query(Model).filter(
        Model.source.like("provider_%"),
        Model.strm_path.isnot(None),
        # Titles the user opted out of .strm management are expected to have no
        # .strm on disk — that is the whole point. Sweeping them would delete
        # the row and the next sync would re-import the title and rewrite the
        # file, silently undoing the opt-out.
        Model.strm_disabled.isnot(True),
    ).all()
    if not rows:
        return 0, []

    # A mount that is missing or empty means the storage is unavailable, not
    # that every title was deleted. mergerfs/NFS/SMB/rclone all report plain
    # "not found" for every path while a branch is out, which raises nothing.
    if not root.is_dir() or not any(root.iterdir()):
        logger.error(
            f"[VOD sweep] {root} is missing or empty — storage looks unavailable. "
            f"Skipping the {media_type} sweep rather than deleting "
            f"{len(rows)} record(s)."
        )
        return 0, []

    missing = [r for r in rows if not Path(r.strm_path).exists()]
    missing_ids = {r.id for r in missing}

    # Files that came back clear this guard's own mark.
    for r in rows:
        if r.id not in missing_ids and r.file_missing_since is not None:
            r.file_missing_since = None

    # Two strikes: a title must be missing on two separate sweeps to be deleted,
    # so a transient outage never destroys records. Only a mark this guard set
    # counts — the prune's mark means the provider dropped the title, which says
    # nothing about whether its file is on disk.
    confirmed = [r for r in missing if r.file_missing_since is not None]
    for r in missing:
        if r.file_missing_since is None:
            r.file_missing_since = now
    if len(missing) != len(confirmed):
        logger.info(
            f"[VOD sweep] {len(missing) - len(confirmed)} {media_type}(s) missing "
            f"for the first time — marked, will be removed if still missing next sweep"
        )
    if not confirmed:
        return 0, []

    allowance = max(PRUNE_MIN_ALLOWANCE, int(len(rows) * PRUNE_MAX_FRACTION))
    if len(confirmed) > allowance:
        logger.error(
            f"[VOD sweep] REFUSING to remove {len(confirmed)} {media_type} record(s): "
            f"that exceeds the safety limit of {allowance} "
            f"({int(PRUNE_MAX_FRACTION * 100)}% of {len(rows)}). Files disappearing "
            f"this fast points at a storage problem, not at content removal — "
            f"nothing was deleted."
        )
        log_deletion(
            db, kind="vod-sweep-blocked", name=f"{len(confirmed)} {media_type} record(s)",
            media_type=media_type, reason="safety-limit",
            detail=f"{len(confirmed)} record(s) had missing files on two consecutive "
                   f"sweeps but exceed the {allowance}-record limit; nothing deleted",
        )
        return 0, []

    titles = []
    for r in confirmed:
        logger.info(f"[VOD sweep] Removing orphaned {media_type}: {r.title} (missing: {r.strm_path})")
        titles.append(r.title)
        db.delete(r)
    return len(confirmed), titles


def sweep_orphaned_vod_records(db: Session) -> int:
    """Remove provider-owned Movie/Series rows whose .strm files are gone.

    Guarded three ways, because the cascade downstream of a wrong answer here
    is severe (the record loss takes auto-playlist toggles, SmartLists and home
    rows with it): the media root is probed first, a record must be missing on
    two separate sweeps, and no single sweep may remove more than 5% of the
    rows for that media type.
    """
    now = datetime.utcnow()
    removed = 0
    swept_titles = []
    for Model, media_type, root in (
        (Movie, "movie", VOD_MOVIES_ROOT),
        (Series, "series", VOD_SERIES_ROOT),
    ):
        count, titles = _sweep_one_type(db, Model, media_type, root, now)
        removed += count
        swept_titles.extend(titles)
    db.commit()

    if removed:
        log_deletion(
            db, kind="vod-sweep", name=f"{removed} VOD record(s)", reason="auto",
            detail="DB records removed — .strm files missing on disk over two sweeps: "
                   + ", ".join(swept_titles[:20]) + ("…" if len(swept_titles) > 20 else ""),
        )
        logger.info(f"VOD sweep: removed {removed} orphaned record(s)")
    return removed


# ── Main Sync Functions ────────────────────────────────────────────────────

def sync_provider(
    provider: Provider,
    sync_type: str,  # "full" | "movies" | "series"
    db: Session,
    progress_callback=None,
    cancel_check=None,
    pause=None,
) -> SyncRun:
    """
    Main entry point for syncing a provider.
    Creates and returns a SyncRun record.
    cancel_check: callable that returns True if sync should be cancelled.
    pause: a services.provider_activity.JobPause shared with the caller's
    other provider work, so one wait budget covers the whole run.
    """
    # Load settings
    from services.tmdb import get_tmdb_token
    bearer_token = get_tmdb_token(db)
    data_dir = get_setting(db, "data_dir", "/data")
    vod_movies_path = Path("/media/vod/movies")
    vod_series_path = Path("/media/vod/shows")
    match_threshold = float(get_setting(db, "tmdb_match_threshold", "0.7"))
    recently_added_days = int(get_setting(db, "recently_added_days", "30"))
    require_tmdb = provider.require_tmdb_match if provider.require_tmdb_match is not None else True

    # Create sync run record
    run = SyncRun(
        provider_id=provider.id,
        status="running",
        sync_type=sync_type,
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    logger.info(f"Starting {sync_type} sync for provider: {provider.name} (run #{run.id})")

    try:
        try:
            unhide_vod_paths(db)
        except Exception as e:
            logger.warning(f"Could not move hidden VOD folders: {e}")

        # Pre-sync disk space check
        if sync_type in ("full", "movies"):
            _check_disk_before_sync(vod_movies_path)
        if sync_type in ("full", "series"):
            _check_disk_before_sync(vod_series_path)

        tmdb = TMDBService(bearer_token, data_dir, match_threshold)
        client = make_provider_client(provider)
        # The sync stands aside for live TV / a recording at every category
        # boundary (services.provider_activity), so a recording that starts
        # mid-sync is not competed with either. One budget for the whole run.
        from services.provider_activity import JobPause
        client.job_pause = pause if pause is not None else JobPause(db, "the provider sync", cancel_check)
        client.job_pause.cancel_check = cancel_check
        client.vod_links = vod_links_for(db, provider)
        # Before the first provider call, with the run already visible (so a
        # waiting sync can be seen and cancelled from the dashboard).
        _pause_between_categories(client, db, progress_callback,
                                  "series" if sync_type == "series" else "movies", "", {})

        category_stats = {}
        new_movies_feed = []
        new_series_feed = []

        m_cleanup = None
        s_cleanup = None

        if sync_type in ("full", "movies"):
            m_stats, m_feed, m_cat_stats, m_cleanup = _sync_movies(
                provider, client, tmdb, db,
                vod_movies_path, recently_added_days,
                progress_callback, cancel_check, require_tmdb
            )
            run.movies_new = m_stats["new"]
            run.movies_existing = m_stats["existing"]
            run.movies_failed = m_stats["failed"]
            run.movies_skipped = m_stats["skipped"]
            new_movies_feed = m_feed
            category_stats.update(m_cat_stats)

        if sync_type in ("full", "series"):
            s_stats, s_feed, s_cat_stats, s_cleanup = _sync_series(
                provider, client, tmdb, db,
                vod_series_path, recently_added_days,
                progress_callback, cancel_check, require_tmdb
            )
            run.series_new = s_stats["new"]
            run.series_existing = s_stats["existing"]
            run.series_failed = s_stats["failed"]
            run.series_skipped = s_stats["skipped"]
            new_series_feed = s_feed
            category_stats.update(s_cat_stats)

        # Prune content the provider has dropped upstream. Only when the sync
        # fetched everything cleanly AND found at least some content (an empty
        # seen set with fetch_ok almost certainly means a transient/empty
        # response — never wipe the whole provider on that basis).
        removed = 0
        for media_type, Model, cleanup in (("movie", Movie, m_cleanup), ("series", Series, s_cleanup)):
            if not (cleanup and cleanup.get("seen_ids")):
                continue
            if cleanup.get("fetch_ok"):
                removed += _prune_removed_content(db, provider, media_type, cleanup["seen_ids"])
            else:
                # Incomplete picture: prune nothing, but what WAS served still
                # clears its mark (see _clear_provider_marks).
                _clear_provider_marks(db, provider, Model, cleanup["seen_ids"])
                db.commit()
        if removed:
            logger.info(f"Sync pruned {removed} item(s) removed upstream by provider {provider.name}")

        run.status = "completed"
        run.category_stats = category_stats
        run.new_movies = new_movies_feed[:50]  # Keep last 50 for feed
        run.new_series = new_series_feed[:50]
        run.completed_at = datetime.utcnow()
        run.duration_seconds = int((run.completed_at - run.started_at).total_seconds())

        db.commit()
        logger.info(f"Sync complete in {run.duration_seconds}s")

    except SyncCancelledError as e:
        msg = str(e) or "Cancelled by user"
        logger.info(f"Sync cancelled: {msg}")
        run.status = "cancelled"
        run.error_message = msg
        run.completed_at = datetime.utcnow()
        run.duration_seconds = int((run.completed_at - run.started_at).total_seconds())
        db.commit()
    except SyncError as e:
        logger.error(f"Sync error: {e}")
        run.status = "failed"
        run.error_message = str(e)
        run.completed_at = datetime.utcnow()
        db.commit()
    except ProviderConnectionError as e:
        logger.error(f"Sync failed — provider unreachable: {e}")
        run.status = "failed"
        run.error_message = str(e)
        run.completed_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        logger.error(f"Sync failed: {e}", exc_info=True)
        run.status = "failed"
        run.error_message = str(e)
        run.completed_at = datetime.utcnow()
        db.commit()

    return run


def _sync_movies(
    provider: Provider,
    client: XtreamClient,
    tmdb: TMDBService,
    db: Session,
    output_dir: Path,
    recently_added_days: int,
    progress_callback=None,
    cancel_check=None,
    require_tmdb: bool = True,
) -> Tuple[dict, list, dict]:
    """Sync all whitelisted movie categories for a provider"""

    whitelisted_cats = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider.id,
        ProviderCategory.type == "movie",
        ProviderCategory.whitelisted == True
    ).all()

    logger.info(f"Movies: {len(whitelisted_cats)} whitelisted categories")

    stats = {"new": 0, "existing": 0, "failed": 0, "skipped": 0}
    feed = []
    category_stats = {}

    # Track TMDB IDs seen this run to dedupe across categories
    seen_tmdb_ids = set()
    # Comprehensive set of every tmdb_id this provider still offers (new OR
    # existing). Used after a successful run to delete rows for content the
    # provider has dropped upstream.
    seen_ids_all = set()
    # True only if every whitelisted category was fetched successfully. If any
    # fetch failed, the seen set is incomplete and we must NOT prune.
    fetch_ok = True

    # Load existing TMDB IDs from this provider to avoid re-processing
    existing_provider_tmdb_ids = {
        m.tmdb_id for m in db.query(Movie.tmdb_id).filter(
            Movie.provider_id == provider.id
        ).all()
    }

    # Build title→tmdb_id lookup so we can skip TMDB API for known items
    known_titles = {
        (m.title.lower(), m.year): m.tmdb_id
        for m in db.query(Movie.title, Movie.year, Movie.tmdb_id).filter(
            Movie.provider_id == provider.id
        ).all()
    }

    # Streams an admin reported as mislabelled ("Wrong movie"): never imported
    # again, whatever the provider calls them and whichever category they're in.
    from services.wrong_match import blocked_keys, is_blocked, override_keys, override_for
    blocked = blocked_keys(db, provider.id, "movie")
    blocked_skips = 0
    # Streams an admin re-matched ("this stream is really film X"): the label is
    # wrong, so the override is used instead of matching it.
    overrides = override_keys(db, provider.id, "movie")

    output_dir.mkdir(parents=True, exist_ok=True)

    for cat in whitelisted_cats:
        cat_new = 0
        cat_existing = 0
        cat_failed = 0
        cat_skipped = 0

        _pause_between_categories(client, db, progress_callback, "movies", cat.category_name, stats)
        logger.info(f"Processing category: {cat.category_name}")

        # Notify UI immediately so user sees which category is loading
        if progress_callback:
            progress_callback("movies", cat.category_name, stats,
                              item_title="Loading streams...", item_pos=0, item_total=0)

        try:
            streams = client.get_vod_streams(cat.category_id)
        except Exception as e:
            logger.error(f"Failed to fetch streams for {cat.category_name}: {e}")
            fetch_ok = False  # a failed fetch means our "seen" set is incomplete
            continue

        _prev_count = cat.title_count
        if _category_went_empty(db, cat, len(streams)):
            logger.warning(
                f"Category '{cat.category_name}' returned 0 titles but held "
                f"{_prev_count} last time — treating as a failed fetch, not as "
                f"an emptied category. Nothing will be pruned from this sync."
            )
            fetch_ok = False
            continue

        total_in_cat = len(streams)

        # Phase 1: Clean titles and split into known vs needs-TMDB
        cleaned = []
        for stream in streams:
            if blocked and is_blocked(
                    blocked, stream.get("stream_id"),
                    client.movie_stream_url(stream.get("stream_id"), stream.get("container_extension", "mp4"))):
                blocked_skips += 1
                continue
            raw_name = stream.get("name", "")
            clean_name, year = clean_title(raw_name)
            cleaned.append((stream, raw_name, clean_name, year))

        # Phase 2: Batch TMDB lookups for items that need it
        # Pre-resolve: check which items we can skip entirely
        needs_tmdb = []  # (index, clean_name, year)
        tmdb_results = {}  # index → metadata

        for idx, (stream, raw_name, clean_name, year) in enumerate(cleaned):
            if not clean_name:
                continue
            if overrides and override_for(overrides, stream.get("stream_id"), client.movie_stream_url(
                    stream.get("stream_id"), stream.get("container_extension", "mp4"))) is not None:
                continue  # re-matched by an admin — no lookup of the (wrong) label

            # Check if we already know this title → skip TMDB API call
            lookup_key = (clean_name.lower(), year)
            if lookup_key in known_titles:
                tmdb_id = known_titles[lookup_key]
                if tmdb_id in existing_provider_tmdb_ids or tmdb_id in seen_tmdb_ids:
                    continue  # Will be counted as existing in phase 3
            needs_tmdb.append((idx, clean_name, year))

        # Parallel TMDB lookups for items that actually need it
        if needs_tmdb:
            logger.info(f"  {cat.category_name}: {len(needs_tmdb)} items need TMDB lookup (skipping {total_in_cat - len(needs_tmdb)} known)")

            def _tmdb_lookup(args):
                idx, name, yr = args
                try:
                    return idx, tmdb.search_movie(name, yr, strict=True), False
                except Exception:
                    return idx, None, True

            lookup_failed = 0
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(_tmdb_lookup, item): item for item in needs_tmdb}
                for future in as_completed(futures):
                    idx, metadata, failed = future.result()
                    if failed:
                        lookup_failed += 1
                    elif metadata:
                        tmdb_results[idx] = metadata
            if lookup_failed:
                # A stream whose lookup errored is neither matched nor "seen", so
                # the seen set is incomplete — exactly like a failed category fetch.
                logger.warning(
                    f"  {cat.category_name}: {lookup_failed} TMDB lookup(s) failed — "
                    f"nothing will be pruned from this sync"
                )
                fetch_ok = False

        # Phase 3: Process all items sequentially (DB writes, file creation, progress)
        items_since_disk_check = 0
        for item_idx, (stream, raw_name, clean_name, year) in enumerate(cleaned, 1):
            if cancel_check and cancel_check():
                raise SyncCancelledError()

            items_since_disk_check += 1
            if items_since_disk_check >= DISK_CHECK_INTERVAL:
                _check_disk_during_sync(output_dir)
                items_since_disk_check = 0

            # Notify progress for every item
            if progress_callback:
                progress_callback("movies", cat.category_name, stats,
                                  item_title=clean_name or raw_name, item_pos=item_idx, item_total=total_in_cat)

            if not clean_name:
                cat_skipped += 1
                stats["skipped"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            # Try title-based skip first (no TMDB needed)
            lookup_key = (clean_name.lower(), year)
            known_id = known_titles.get(lookup_key)
            idx = item_idx - 1  # 0-based index into cleaned

            # Get metadata: from parallel batch or title-based lookup
            metadata = tmdb_results.get(idx)
            override_id = override_for(overrides, stream.get("stream_id"), client.movie_stream_url(
                stream.get("stream_id"), stream.get("container_extension", "mp4"))) if overrides else None
            if override_id is not None:
                # Already in the library under the fixed id → the existing path
                # below merges tags; otherwise fetch the right film's metadata.
                if override_id in existing_provider_tmdb_ids or override_id in seen_tmdb_ids:
                    metadata = {"tmdb_id": override_id}
                else:
                    tmdb_down = False
                    try:
                        metadata = tmdb.get_movie_details(override_id)
                        failed = getattr(tmdb, "_lookup_failed", None)
                        tmdb_down = bool(not metadata and callable(failed) and failed())  # 429/5xx
                    except TMDBConnectionError:
                        metadata, tmdb_down = None, True
                    if not metadata:
                        # The right film's details didn't come. Skip the stream
                        # for tonight (never import it under its wrong label,
                        # never fail the sync). Only an unreachable TMDB makes
                        # the seen set doubtful enough to hold back pruning: a
                        # plain "no such film" (TMDB removed or merged the id)
                        # comes back every night and would stop this
                        # provider's pruning for good.
                        if tmdb_down:
                            fetch_ok = False
                        else:
                            logger.warning(f"[Sync] Stream {stream.get('stream_id')} is fixed to TMDB {override_id}, "
                                           f"which TMDB no longer has; skipped (fix it again to a film TMDB knows)")
                        cat_skipped += 1
                        stats["skipped"] += 1
                        continue
                known_id = None
            if not metadata and known_id:
                # Known title but not in batch — it's existing, merge tags
                if known_id in existing_provider_tmdb_ids or known_id in seen_tmdb_ids:
                    _merge_source_tag(known_id, "movie", cat.source_tag, provider.id, db)
                    seen_ids_all.add(known_id)
                    # Existing VOD movie — restore its .strm if it vanished from disk
                    _repair_movie_strm(client, stream, known_id, provider, db)
                    cat_existing += 1
                    stats["existing"] += 1
                    if item_idx % 10 == 0 or item_idx == total_in_cat:
                        logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                    continue

            if not metadata:
                if require_tmdb:
                    cat_skipped += 1
                    stats["skipped"] += 1
                    if item_idx % 10 == 0 or item_idx == total_in_cat:
                        logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                    continue
                # No TMDB match but require_tmdb is off — use provider title
                stream_id = int(stream.get("stream_id", 0))
                metadata = {
                    "tmdb_id": -(provider.id * NEGATIVE_ID_BLOCK + stream_id),
                    "title": clean_name,
                    "year": year,
                    "overview": None,
                    "runtime": None,
                    "rating": None,
                    "genres": [],
                    "poster_path": stream.get("stream_icon"),
                    "backdrop_path": None,
                }

            tmdb_id = metadata["tmdb_id"]
            seen_ids_all.add(tmdb_id)

            # Skip if already seen this run or in library from this provider — merge tags
            if tmdb_id in seen_tmdb_ids or tmdb_id in existing_provider_tmdb_ids:
                _merge_source_tag(tmdb_id, "movie", cat.source_tag, provider.id, db)
                # Existing VOD movie — restore its .strm if it vanished from disk
                _repair_movie_strm(client, stream, tmdb_id, provider, db)
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            seen_tmdb_ids.add(tmdb_id)

            # Also check DB directly in case of prior partial sync — merge tags
            if db.query(Movie).filter(Movie.tmdb_id == tmdb_id, Movie.provider_id == provider.id).first():
                existing_provider_tmdb_ids.add(tmdb_id)
                _merge_source_tag(tmdb_id, "movie", cat.source_tag, provider.id, db)
                # Existing VOD movie — restore its .strm if it vanished from disk
                _repair_movie_strm(client, stream, tmdb_id, provider, db)
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            # Compute file path early so duplicate record has it
            title = metadata["title"]
            year_str = metadata.get("year")
            folder_name = vod_folder_name(title, year_str)
            movie_dir = output_dir / folder_name
            strm_file = movie_dir / f"{folder_name}.strm"
            nfo_file = movie_dir / f"{folder_name}.nfo"

            # Check if exists from another provider (duplicate)
            if check_and_record_duplicate(tmdb_id, "movie", f"provider_{provider.id}", str(strm_file), provider, db):
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            # Compute tags
            now = datetime.utcnow()
            list_tags = get_list_tags_for_tmdb_id(tmdb_id, "movie", db)
            tags = compute_tags(cat.source_tag, now, list_tags, recently_added_days, media_type="movie")

            # Apply tag rules
            metadata["tags"] = tags
            rule_tags = apply_tag_rules(metadata, "movie", f"provider_{provider.id}", cat.source_tag, db)
            for rt in rule_tags:
                if rt not in tags:
                    tags.append(rt)

            try:
                movie_dir.mkdir(parents=True, exist_ok=True)
                chown_path(movie_dir)

                # Write strm
                stream_url = client.movie_stream_url(
                    stream.get("stream_id"),
                    stream.get("container_extension", "mp4")
                )
                strm_file.write_text(stream_url, encoding='utf-8')
                chown_path(strm_file)

                # Write full NFO with all metadata
                write_movie_nfo(nfo_file, metadata, tags)
                chown_path(nfo_file)

                # Record in DB (batched — committed per category)
                movie_record = Movie(
                    tmdb_id=tmdb_id,
                    title=title,
                    year=year_str,
                    overview=metadata.get("overview"),
                    runtime=metadata.get("runtime"),
                    rating=metadata.get("rating"),
                    genres=metadata.get("genres", []),
                    poster_path=metadata.get("poster_path"),
                    backdrop_path=metadata.get("backdrop_path"),
                    source=f"provider_{provider.id}",
                    provider_id=provider.id,
                    strm_path=str(strm_file),
                    nfo_path=str(nfo_file),
                    source_tag=cat.source_tag,
                    tags=tags,
                    date_added=now,
                )
                db.add(movie_record)

                existing_provider_tmdb_ids.add(tmdb_id)
                known_titles[lookup_key] = tmdb_id
                cat_new += 1
                stats["new"] += 1

                # Add to feed
                if len(feed) < 100:
                    feed.append({
                        "tmdb_id": tmdb_id,
                        "title": title,
                        "year": year_str,
                        "poster": metadata.get("poster_path"),
                        "tags": tags,
                        "type": "movie",
                        "added_at": now.isoformat(),
                    })

                logger.debug(f"+ {folder_name} [{', '.join(tags)}]")

            except Exception as e:
                logger.error(f"Failed to create files for {title}: {e}")
                cat_failed += 1
                stats["failed"] += 1

            # Periodic progress log — every 10 items or last item
            if item_idx % 10 == 0 or item_idx == total_in_cat:
                logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")

        # Commit all new movies for this category at once
        cat.title_count = cat_new + cat_existing
        cat.last_sync_matched = cat_new + cat_existing
        cat.last_sync_skipped = cat_skipped
        snapshot = CategorySnapshot(
            category_id=cat.id,
            title_count=cat_new + cat_existing,
            new_count=cat_new,
        )
        db.add(snapshot)
        db.commit()

        category_stats[cat.category_name] = {
            "new": cat_new,
            "existing": cat_existing,
            "failed": cat_failed,
            "skipped": cat_skipped,
            "total": cat_new + cat_existing,
        }

        logger.info(
            f"  {cat.category_name}: +{cat_new} new, "
            f"{cat_existing} existing, {cat_skipped} skipped"
        )

    logger.info(
        f"Movies complete: {stats['new']} new, {stats['existing']} existing, "
        f"{stats['skipped']} skipped (no TMDB), {stats['failed']} failed"
    )

    if blocked_skips:
        logger.info(f"[Sync] Skipped {blocked_skips} blocked (mislabelled) stream(s) from {provider.name}")
    return stats, feed, category_stats, {"seen_ids": seen_ids_all, "fetch_ok": fetch_ok}


def _sync_series(
    provider: Provider,
    client: XtreamClient,
    tmdb: TMDBService,
    db: Session,
    output_dir: Path,
    recently_added_days: int,
    progress_callback=None,
    cancel_check=None,
    require_tmdb: bool = True,
) -> Tuple[dict, list, dict]:
    """Sync all whitelisted series categories for a provider"""

    whitelisted_cats = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider.id,
        ProviderCategory.type == "series",
        ProviderCategory.whitelisted == True
    ).all()

    logger.info(f"Series: {len(whitelisted_cats)} whitelisted categories")

    stats = {"new": 0, "existing": 0, "failed": 0, "skipped": 0}
    feed = []
    category_stats = {}

    seen_tmdb_ids = set()
    # Comprehensive set of every tmdb_id this provider still offers (new OR
    # existing), and whether all categories fetched cleanly — see _sync_movies.
    seen_ids_all = set()
    fetch_ok = True
    existing_provider_tmdb_ids = {
        s.tmdb_id for s in db.query(Series.tmdb_id).filter(
            Series.provider_id == provider.id
        ).all()
    }

    # Build title→tmdb_id lookup so we can skip TMDB API for known items
    known_titles = {
        (s.title.lower(), s.year): s.tmdb_id
        for s in db.query(Series.title, Series.year, Series.tmdb_id).filter(
            Series.provider_id == provider.id
        ).all()
    }

    output_dir.mkdir(parents=True, exist_ok=True)

    for cat in whitelisted_cats:
        cat_new = 0
        cat_existing = 0
        cat_skipped = 0
        cat_failed = 0

        _pause_between_categories(client, db, progress_callback, "series", cat.category_name, stats)
        logger.info(f"Processing category: {cat.category_name}")

        # Notify UI immediately so user sees which category is loading
        if progress_callback:
            progress_callback("series", cat.category_name, stats,
                              item_title="Loading streams...", item_pos=0, item_total=0)

        try:
            series_list = client.get_series_list(cat.category_id)
        except Exception as e:
            logger.error(f"Failed to fetch series for {cat.category_name}: {e}")
            fetch_ok = False  # a failed fetch means our "seen" set is incomplete
            continue

        _prev_count = cat.title_count
        if _category_went_empty(db, cat, len(series_list)):
            logger.warning(
                f"Category '{cat.category_name}' returned 0 series but held "
                f"{_prev_count} last time — treating as a failed fetch, not as "
                f"an emptied category. Nothing will be pruned from this sync."
            )
            fetch_ok = False
            continue

        total_in_cat = len(series_list)

        # Phase 1: Clean titles and split into known vs needs-TMDB
        cleaned = []
        for series in series_list:
            raw_name = series.get("name", "")
            clean_name, year = clean_title(raw_name)
            cleaned.append((series, raw_name, clean_name, year))

        # Phase 2: Batch TMDB lookups for items that need it
        needs_tmdb = []
        tmdb_results = {}

        for idx, (series, raw_name, clean_name, year) in enumerate(cleaned):
            if not clean_name:
                continue
            lookup_key = (clean_name.lower(), year)
            if lookup_key in known_titles:
                tmdb_id = known_titles[lookup_key]
                if tmdb_id in existing_provider_tmdb_ids or tmdb_id in seen_tmdb_ids:
                    continue
            needs_tmdb.append((idx, clean_name, year))

        if needs_tmdb:
            logger.info(f"  {cat.category_name}: {len(needs_tmdb)} items need TMDB lookup (skipping {total_in_cat - len(needs_tmdb)} known)")

            def _tmdb_lookup(args):
                idx, name, yr = args
                try:
                    return idx, tmdb.search_series(name, yr, strict=True), False
                except Exception:
                    return idx, None, True

            lookup_failed = 0
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(_tmdb_lookup, item): item for item in needs_tmdb}
                for future in as_completed(futures):
                    idx, metadata, failed = future.result()
                    if failed:
                        lookup_failed += 1
                    elif metadata:
                        tmdb_results[idx] = metadata
            if lookup_failed:
                # A stream whose lookup errored is neither matched nor "seen", so
                # the seen set is incomplete — exactly like a failed category fetch.
                logger.warning(
                    f"  {cat.category_name}: {lookup_failed} TMDB lookup(s) failed — "
                    f"nothing will be pruned from this sync"
                )
                fetch_ok = False

        # Phase 3: Process all items sequentially (DB writes, file creation, progress)
        items_since_disk_check = 0
        for item_idx, (series, raw_name, clean_name, year) in enumerate(cleaned, 1):
            if cancel_check and cancel_check():
                raise SyncCancelledError()

            items_since_disk_check += 1
            if items_since_disk_check >= DISK_CHECK_INTERVAL:
                _check_disk_during_sync(output_dir)
                items_since_disk_check = 0

            # Notify progress for every item
            if progress_callback:
                progress_callback("series", cat.category_name, stats,
                                  item_title=clean_name or raw_name, item_pos=item_idx, item_total=total_in_cat)

            if not clean_name:
                cat_skipped += 1
                stats["skipped"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            lookup_key = (clean_name.lower(), year)
            known_id = known_titles.get(lookup_key)
            idx = item_idx - 1

            metadata = tmdb_results.get(idx)
            if not metadata and known_id:
                if known_id in existing_provider_tmdb_ids or known_id in seen_tmdb_ids:
                    _merge_source_tag(known_id, "series", cat.source_tag, provider.id, db)
                    seen_ids_all.add(known_id)
                    # Existing VOD series — back-fill any new seasons/episodes
                    _backfill_series_episodes(client, series, known_id, provider, db)
                    cat_existing += 1
                    stats["existing"] += 1
                    if item_idx % 10 == 0 or item_idx == total_in_cat:
                        logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                    continue

            if not metadata:
                if require_tmdb:
                    cat_skipped += 1
                    stats["skipped"] += 1
                    if item_idx % 10 == 0 or item_idx == total_in_cat:
                        logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                    continue
                # No TMDB match but require_tmdb is off — use provider title
                series_id = int(series.get("series_id", 0))
                metadata = {
                    "tmdb_id": -(provider.id * NEGATIVE_ID_BLOCK + series_id),
                    "title": clean_name,
                    "year": year,
                    "overview": None,
                    "genres": [],
                    "poster_path": series.get("cover"),
                    "backdrop_path": None,
                    "status": None,
                }

            tmdb_id = metadata["tmdb_id"]
            seen_ids_all.add(tmdb_id)

            if tmdb_id in seen_tmdb_ids or tmdb_id in existing_provider_tmdb_ids:
                _merge_source_tag(tmdb_id, "series", cat.source_tag, provider.id, db)
                # Existing VOD series — back-fill any new seasons/episodes
                _backfill_series_episodes(client, series, tmdb_id, provider, db)
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            seen_tmdb_ids.add(tmdb_id)

            # Also check DB directly in case of prior partial sync — merge tags
            if db.query(Series).filter(Series.tmdb_id == tmdb_id, Series.provider_id == provider.id).first():
                existing_provider_tmdb_ids.add(tmdb_id)
                _merge_source_tag(tmdb_id, "series", cat.source_tag, provider.id, db)
                # Existing VOD series — back-fill any new seasons/episodes
                _backfill_series_episodes(client, series, tmdb_id, provider, db)
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            # Compute file path early so duplicate record has it
            title = metadata["title"]
            year_str = metadata.get("year")
            folder_name = vod_folder_name(title, year_str)
            show_dir = output_dir / folder_name

            if check_and_record_duplicate(tmdb_id, "series", f"provider_{provider.id}", str(show_dir), provider, db):
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            now = datetime.utcnow()
            list_tags = get_list_tags_for_tmdb_id(tmdb_id, "series", db)
            tags = compute_tags(cat.source_tag, now, list_tags, recently_added_days, media_type="series")

            # Apply tag rules
            metadata["tags"] = tags
            rule_tags = apply_tag_rules(metadata, "series", f"provider_{provider.id}", cat.source_tag, db)
            for rt in rule_tags:
                if rt not in tags:
                    tags.append(rt)

            try:
                # Get episode info
                series_info = client.get_series_info(series.get("series_id"))
                episodes = series_info.get("episodes", {})
                if isinstance(episodes, list):
                    episodes = {"1": episodes}
                if not episodes:
                    cat_skipped += 1
                    stats["skipped"] += 1
                    if item_idx % 10 == 0 or item_idx == total_in_cat:
                        logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                    continue

                show_dir.mkdir(parents=True, exist_ok=True)
                chown_path(show_dir)

                # Write show NFO
                nfo_file = show_dir / "tvshow.nfo"
                write_series_nfo(nfo_file, metadata, tags)
                chown_path(nfo_file)

                # Write episode strm files
                ep_count = _write_episode_strms(client, episodes, show_dir, folder_name)

                # Record in DB
                series_record = Series(
                    tmdb_id=tmdb_id,
                    title=title,
                    year=year_str,
                    overview=metadata.get("overview"),
                    genres=metadata.get("genres", []),
                    poster_path=metadata.get("poster_path"),
                    backdrop_path=metadata.get("backdrop_path"),
                    status=metadata.get("status"),
                    source=f"provider_{provider.id}",
                    provider_id=provider.id,
                    strm_path=str(show_dir),
                    nfo_path=str(nfo_file),
                    source_tag=cat.source_tag,
                    tags=tags,
                    date_added=now,
                )
                db.add(series_record)

                existing_provider_tmdb_ids.add(tmdb_id)
                known_titles[lookup_key] = tmdb_id
                cat_new += 1
                stats["new"] += 1

                if len(feed) < 100:
                    feed.append({
                        "tmdb_id": tmdb_id,
                        "title": title,
                        "year": year_str,
                        "poster": metadata.get("poster_path"),
                        "tags": tags,
                        "type": "series",
                        "episodes": ep_count,
                        "added_at": now.isoformat(),
                    })

                logger.debug(f"+ {folder_name} ({ep_count} eps) [{', '.join(tags)}]")

            except Exception as e:
                logger.error(f"Failed to create series {title}: {e}")
                cat_failed += 1
                stats["failed"] += 1

            # Periodic progress log — every 10 items or last item
            if item_idx % 10 == 0 or item_idx == total_in_cat:
                logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")

        # Commit all new series for this category at once
        cat.title_count = cat_new + cat_existing
        cat.last_sync_matched = cat_new + cat_existing
        cat.last_sync_skipped = cat_skipped
        snapshot = CategorySnapshot(
            category_id=cat.id,
            title_count=cat_new + cat_existing,
            new_count=cat_new,
        )
        db.add(snapshot)
        db.commit()

        category_stats[cat.category_name] = {
            "new": cat_new,
            "existing": cat_existing,
            "failed": cat_failed,
            "skipped": cat_skipped,
            "total": cat_new + cat_existing,
        }

        logger.info(
            f"  {cat.category_name}: +{cat_new} new, "
            f"{cat_existing} existing, {cat_skipped} skipped"
        )

    logger.info(
        f"Series complete: {stats['new']} new, {stats['existing']} existing, "
        f"{stats['skipped']} skipped, {stats['failed']} failed"
    )

    return stats, feed, category_stats, {"seen_ids": seen_ids_all, "fetch_ok": fetch_ok}

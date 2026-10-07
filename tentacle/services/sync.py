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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple
import requests

from sqlalchemy.orm import Session

from models.database import (
    Provider, ProviderCategory, Movie, Series,
    SyncRun, CategorySnapshot, Duplicate, get_setting, set_setting, log_deletion
)
from services.tmdb import TMDBService
from services.nfo import write_movie_nfo, write_series_nfo, make_folder_name, vod_folder_name, fit_file_stem
from services.cleaner import clean_title
from services.m3u_parser import episode_from_title, container_from_url
from services.duplicates import delete_vod_files, convert_record_to_downloaded
from services.media_files import delete_movie_files, delete_series_files, MEDIA_SUFFIXES
from services.tagger import compute_tags, get_list_tags_for_tmdb_id, apply_tag_rules
from services.exceptions import (ProviderConnectionError, ProviderDataError, SyncCancelledError, SyncError,
                                 TMDBConnectionError)
from services.xtream_client import quote_cred

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
# a created path takes its parent folder's owner (see chown_path).
VOD_PUID = os.environ.get("PUID")
VOD_PGID = os.environ.get("PGID")


def chown_path(path) -> None:
    """Best-effort chown of a path Tentacle just created.

    To PUID/PGID when set. Without them, a path root created takes the owner
    of the folder it was created in (#239): library roots belong to the media
    user Sonarr/Radarr run as, and a root-owned season folder made Sonarr's
    imports into it fail with "permission denied" weeks later. Nothing
    changes under a root-owned folder, or when Tentacle doesn't run as root.
    The chown never follows a symlink (a link is skipped; one swapped in
    between the check and the chown changes only the link itself), so what a
    link points at is never re-owned. A real folder reached through a
    symlinked show folder is library content and is handed over as usual.
    """
    try:
        if os.path.islink(path):
            return
        if VOD_PUID is not None:
            os.chown(path, int(VOD_PUID), int(VOD_PGID or VOD_PUID), follow_symlinks=False)
            return
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        parent = os.stat(os.path.dirname(os.path.abspath(str(path))))
        if (parent.st_uid, parent.st_gid) == (0, 0):
            return
        if (os.lstat(path).st_uid, os.lstat(path).st_gid) == (0, 0):
            os.chown(path, parent.st_uid, parent.st_gid, follow_symlinks=False)
    except (OSError, ValueError) as e:
        logger.debug(f"chown_path failed for {path}: {e}")


_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov", ".wmv"}


def repair_hybrid_ownership(db) -> list:
    """Nightly repair for the ownership race on EXISTING hybrid shows (Series
    rows with sonarr_path set): chown the show dir, its season dirs, and any
    real video files not owned by PUID. Narrow on purpose — only hybrid shows
    are ever written to by Sonarr, so the huge pure-VOD catalog is never
    walked. It skips a symlinked show folder and symlinked season folders (the
    video files behind them are not re-owned; season folders reached through
    a symlinked show folder are still handed over when the sync writes into
    them). No-op when PUID is unset."""
    if VOD_PUID is None:
        return []
    from models.database import Series as _Series
    uid = int(VOD_PUID)
    fixed = []
    hybrids = db.query(_Series).filter(_Series.sonarr_path.isnot(None),
                                       _Series.strm_path.isnot(None)).all()
    for s in hybrids:
        show_dir = Path(s.strm_path)
        if not show_dir.is_dir() or show_dir.is_symlink():
            continue
        targets = [show_dir] + [d for d in show_dir.iterdir() if d.is_dir() and not d.is_symlink()]
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
        self.base = (f"{provider.server_url.rstrip('/')}/player_api.php"
                     f"?username={quote_cred(provider.username)}&password={quote_cred(provider.password)}")
        self.server = provider.server_url.rstrip('/')
        self.username = provider.username
        self.password = provider.password
        self.session = requests.Session()
        self.session.headers.update(XTREAM_HEADERS)
        # requests ignores a `timeout` attribute on a Session; it has to be
        # passed per call. Without it a stalled panel hung the sync for ever.
        # (connect, read): a large category can take a slow panel well over
        # 30 s before its first byte; a stalled one must still not hang the
        # nightly sync for ever.
        self.timeout = (15, 180)
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
            try:
                data = r.json()
            except ValueError:
                # A login/Cloudflare page or a cut-off body, with status 200 (#267)
                body = (getattr(r, "text", "") or "").lstrip()[:200].lower()
                if body.startswith("<") or "html" in body:
                    raise ProviderDataError("the provider returned a web page instead of data "
                                            "(check the server URL and the account)")
                raise ProviderDataError("the provider's answer was not valid data")
            if isinstance(data, dict):
                user_info = data.get("user_info")
                if isinstance(user_info, dict) and not user_info.get("auth", 1):
                    # The panel refused the login (a wrong or expired account) and
                    # answers every action like this. Read as [] it looked like an
                    # emptied category: after EMPTY_CATEGORY_STRIKES nights the run
                    # was "completed" with no message again (#267).
                    raise ProviderDataError("the provider refused the login: check the account and its expiry")
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
        return f"{self.server}/movie/{quote_cred(self.username)}/{quote_cred(self.password)}/{stream_id}.{container}"

    def episode_stream_url(self, episode_id, container="mp4") -> str:
        if self.vod_links is not None:
            return self.vod_links.episode(episode_id, container)
        return f"{self.server}/series/{quote_cred(self.username)}/{quote_cred(self.password)}/{episode_id}.{container}"


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


# One provider's VOD sync at a time (#446). A category's new rows are
# committed once, at its end, so a second provider's sync running alongside
# could not see them: both wrote the same "Title (Year).strm" (the last write
# won) and both added the row, and the second commit failed on
# UNIQUE(tmdb_id), leaving its files with no row. A sync now waits for the
# one running and then finds its rows (a duplicate, a priority takeover), as
# if the two had run one after the other.
_vod_sync_lock = threading.Lock()
_vod_sync_holder = None   # name of the provider whose sync holds the lock
VOD_SYNC_POLL_SECONDS = 1.0


def _wait_for_vod_sync_turn(provider: Provider, run: SyncRun, db: Session, progress_callback=None,
                            cancel_check=None, phase: str = "movies") -> None:
    """Take _vod_sync_lock; meanwhile say on screen whose sync it waits for,
    stay cancellable, and book the wait to the run so the status route does
    not call it stuck."""
    global _vod_sync_holder
    if not _vod_sync_lock.acquire(blocking=False):
        db.commit()   # nothing open while waiting; the queries after it see the other sync's rows
        from services.provider_activity import booked_wait
        shown = None
        with booked_wait(run.id):
            while True:
                holder = _vod_sync_holder or "another provider"
                if holder != shown:
                    shown = holder
                    logger.info(f"Sync of {provider.name} waits for {holder}'s sync to finish")
                    if progress_callback:
                        progress_callback(phase, "", {}, item_title=f"Waiting for {holder}'s sync to finish",
                                          item_pos=0, item_total=0)
                if _vod_sync_lock.acquire(timeout=VOD_SYNC_POLL_SECONDS):
                    break
                if cancel_check and cancel_check():
                    raise SyncCancelledError("Sync cancelled while waiting for another provider's sync to finish")
    _vod_sync_holder = provider.name


def _release_vod_sync_turn() -> None:
    global _vod_sync_holder
    _vod_sync_holder = None
    _vod_sync_lock.release()


def _direct_stream_res(prefix: str = ""):
    """A direct Xtream stream URL: scheme://host[:port]<prefix>/movie|series/<u>/<p>/<id>.<ext>,
    where <prefix> is the path of the provider's own server_url (usually
    empty). Anything else -- a proxy that puts the provider's path under
    its own prefix -- is not one. The second pattern finds such a URL
    carried inside another one (a resume proxy's `?d=`)."""
    p = re.escape(prefix.rstrip("/"))
    return (re.compile(rf"(?i)^https?://([^/:?#]+)(?::\d+)?{p}/(movie|series)/[^/?#]+/[^/?#]+/(\d+)\.[a-z0-9]+$"),
            re.compile(rf"(?i)https?://([^/:?#&]+)(?::\d+)?{p}/(movie|series)/[^/?#&]+/[^/?#&]+/(\d+)\.[a-z0-9]+"))


def _direct_ref(url_text: str, embedded: bool = False, prefix: str = ""):
    """(host, kind, id) of a direct Xtream URL; with `embedded`, also of one
    carried URL-encoded inside another URL."""
    from urllib.parse import unquote
    direct, carried = _direct_stream_res(prefix)
    m = direct.match(url_text or "")
    if m is None and embedded:
        m = carried.search(unquote(url_text or "")) if "%2F" in (url_text or "").upper() else None
    return (m.group(1).lower(), m.group(2).lower(), int(m.group(3))) if m else None


def _strm_is_blank(strm_file: Path) -> bool:
    """An existing .strm with no address in it (0 bytes or whitespace): what a
    write cut short leaves (#283). Rewritten for any provider type, M3U too."""
    try:
        return strm_file.stat().st_size < 64 and not strm_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False


def _write_strm(strm_file: Path, url: str) -> None:
    """Write a .strm through a hidden temp file and a rename, so a write cut
    short (disk full, the container stopped, a power loss) leaves the old file
    whole instead of an empty one nothing repaired (#283). An existing file's
    owner and mode carry over; the caller still runs chown_path."""
    import uuid
    # A short name: the .strm's own can already be at the 255-byte limit
    tmp = strm_file.with_name(f".tentacle-{uuid.uuid4().hex[:12]}.tmp")
    try:
        tmp.write_text(url, encoding="utf-8")
        try:
            st = os.stat(strm_file)
        except OSError:
            st = None
        if st is not None:
            try:
                os.chmod(tmp, st.st_mode & 0o7777)
                os.chown(tmp, st.st_uid, st.st_gid, follow_symlinks=False)
            except OSError:
                pass  # not ours to give away (no root): the file is still written
        os.replace(tmp, strm_file)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _login_encoded(url_text: str, client) -> str:
    """A direct link of this provider written before the login was
    URL-encoded, in the form the sync writes now. A '/' or '?' in the
    password made it unplayable, and unreadable to the checks below, so it
    was never rewritten. A login that needs no encoding is left as is."""
    raw = f"/{client.username}/{client.password}/"
    encoded = f"/{quote_cred(client.username)}/{quote_cred(client.password)}/"
    if raw == encoded:
        return url_text
    for kind in ("movie", "series"):
        url_text = url_text.replace(f"/{kind}{raw}", f"/{kind}{encoded}", 1)
    return url_text


def _strm_needs_rewrite(strm_file: Path, expected: str, client) -> bool:
    """An existing .strm is rewritten only when it plays the SAME stream of
    THIS Xtream provider as the sync would write today, in a different form:
      - switching to or from Tentacle's VOD route (vod_via_tentacle_enabled),
        including a file that wraps the provider URL in a resume proxy
        (URL-encoded), when moving TO Tentacle's route;
      - the provider's own URL with changed credentials, scheme, port or
        container extension.
    Everything else is left alone: another stream id (a title listed twice
    would otherwise flip every night), another host, a proxy of somebody's
    own, and M3U providers (their playlist URLs may rotate tokens or hosts
    on every fetch)."""
    if not isinstance(client, XtreamClient):
        return False
    try:
        current = strm_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    if not current:
        return True   # 0 bytes / blank: a write cut short, never a link of anyone's (#283)
    if current == expected:
        return False
    current = _login_encoded(current, client)
    from urllib.parse import urlparse
    from services import vod_tokens
    provider_host = (urlparse(client.server).hostname or "").lower()
    prefix = urlparse(client.server).path or ""
    if not provider_host:
        return False

    def ours(url_text):
        """(kind, id) of a Tentacle VOD link for THIS provider, else None."""
        m = vod_tokens._TOKEN_URL.search(url_text or "")
        if m is None or int(m.group(2)) != getattr(client, "provider_id", None):
            return None
        return m.group(1), int(m.group(3))
    want = ours(expected)
    if want is not None:                                   # moving TO Tentacle's route
        have = ours(current)
        if have is not None:
            return have == want                            # new secret / address
        ref = _direct_ref(current, embedded=True, prefix=prefix)
        return ref is not None and ref == (provider_host, want[0], want[1])
    exp = _direct_ref(expected, prefix=prefix)
    if exp is None:
        return False
    have = ours(current)
    if have is not None:                                   # moving back to direct
        return have == (exp[1], exp[2])
    ref = _direct_ref(current, prefix=prefix)
    return ref is not None and ref == exp and exp[0] == provider_host


# A direct Xtream stream URL, for its host and username.
_XTREAM_ACCOUNT_RE = re.compile(
    r"(?i)^https?://([^/:?#]+)(?::\d+)?(?:/[^?#]*?)?/(?:movie|series)/([^/?#]+)/[^/?#]+/\d+\.[a-z0-9]+$")


def _note_other_providers(client, provider: Provider, db: Session) -> None:
    """Tell the client which provider ids and Xtream accounts are someone
    else's, for _strm_plays_other_provider."""
    from urllib.parse import urlparse
    own = ((urlparse(provider.server_url or "").hostname or "").lower(), provider.username or "")
    ids, accounts = set(), set()
    for other in db.query(Provider).filter(Provider.id != provider.id).all():
        ids.add(other.id)
        account = ((urlparse(other.server_url or "").hostname or "").lower(), other.username or "")
        if account[0] and account[1] and account != own:
            accounts.add(account)
    client.other_providers = {"ids": ids, "accounts": accounts}


def _strm_plays_other_provider(strm_file: Path, client) -> bool:
    """True only on positive evidence that an existing .strm plays ANOTHER
    configured provider: a Tentacle VOD link naming another provider id, or a
    direct Xtream URL on another provider's host and account.

    A title a higher-priority provider takes over keeps its row but its file
    still played the lower-priority provider, and _strm_needs_rewrite never
    rewrites a file that plays a different stream: when the backup provider
    expired, the title stopped playing (#154). This heals those files, the
    ones taken over before this fix included. A hand-made or proxy URL, or
    one of this provider's own, is left alone."""
    others = getattr(client, "other_providers", None)
    if not others:
        return False
    try:
        current = strm_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    from services import vod_tokens
    m = vod_tokens._TOKEN_URL.search(current)
    if m:
        return int(m.group(2)) in others["ids"]
    m = _XTREAM_ACCOUNT_RE.match(current)
    from urllib.parse import unquote
    return bool(m) and (m.group(1).lower(), unquote(m.group(2))) in others["accounts"]


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

    # A copy: appending to the loaded list changed the value SQLAlchemy
    # compares against too, so the assignment below looked like no change and
    # the tag was never written for a title already in the DB.
    tags = list(record.tags or [])
    if new_tag in tags:
        return

    tags.append(new_tag)
    record.tags = tags
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


def _our_episode_id(strm_file: Path, client) -> Optional[int]:
    """The episode id this .strm plays when it is an episode of THIS Xtream
    provider, else None. Not for M3U (its ids are made from the URL) or a
    link that is not ours."""
    if not isinstance(client, XtreamClient):
        return None
    try:
        current = strm_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    from urllib.parse import urlparse
    from services import vod_tokens
    m = vod_tokens._TOKEN_URL.search(current)
    if m:
        ours = m.group(1) == "series" and int(m.group(2)) == getattr(client, "provider_id", None)
        return int(m.group(3)) if ours else None
    host = (urlparse(client.server).hostname or "").lower()
    ref = _direct_ref(current, embedded=True, prefix=urlparse(client.server).path or "")
    return ref[2] if ref is not None and ref[0] == host and ref[1] == "series" else None


class _EpisodeSlots:
    """The episodes each listing of a show offers at each SxxEyy file, so a
    file playing an id listed there by none of them can be repointed (#263):
    the provider replaced the upload under a new id, or renumbered it (the
    old id now sits at another number). The file is keyed by its SxxEyy, so
    the episode listed there now is the one to play.

    A provider can list one show under several series ids (an EN and a DE
    category) that match one show folder. Judged per listing, each one took
    the other's ids for delisted and the files flipped twice a night (#376),
    so the series sync collects every listing and settles once, after a
    complete fetch. A file whose id is listed at its number by any listing
    stays as it is."""

    def __init__(self):
        self.offers = {}      # strm path -> [(episode id, stream url), ...] in listing order
        self.unsure = set()   # show folders with a listing we couldn't read

    def offer(self, strm_file: Path, ep_id: int, url: str) -> None:
        self.offers.setdefault(strm_file, []).append((ep_id, url))

    def unknown(self, show_dir: Path) -> None:
        self.unsure.add(show_dir)

    def settle(self, client) -> int:
        """Repoint the files that play an id no listing offers at their number."""
        rewritten = 0
        for strm_file, offered in self.offers.items():
            if strm_file.parent.parent in self.unsure or not strm_file.exists() or _strm_is_blank(strm_file):
                continue
            current = _our_episode_id(strm_file, client)
            if current is None or current in {ep_id for ep_id, _ in offered}:
                continue
            _write_strm(strm_file, offered[0][1])
            chown_path(strm_file)
            rewritten += 1
            logger.info(f"[Sync] Rewrote {strm_file.name}: its old episode id is no longer listed at this number")
        return rewritten


def _write_episode_strms(client: "XtreamClient", episodes: dict, show_dir: Path, folder_name: str,
                         slots: Optional[_EpisodeSlots] = None) -> int:
    """Write .strm files for any episodes that don't already exist on disk.

    Returns the number of NEW episode files written. Safe to call on an
    existing series to back-fill newly-added seasons/episodes (idempotent).
    An existing file is rewritten when it plays the same stream in another
    form, plays another provider (a takeover, #154), or plays an id no longer
    listed at its number (#263, see _EpisodeSlots). That last one is left to
    the caller's `slots` when given: the series sync settles it once all
    listings of the show are read.
    """
    ep_count = 0
    own = slots is None
    if own:
        slots = _EpisodeSlots()
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
                code = f" S{season_int:02d}E{int(ep_num):02d}"
            except (TypeError, ValueError):
                continue
            # Byte-safe: a show folder that fits can still overflow once the
            # episode code and ".strm" are added. Unchanged when it fits.
            ep_filename = fit_file_stem(folder_name, code, len(".strm"))
            strm_file = season_dir / f"{ep_filename}.strm"
            expected = client.episode_stream_url(ep_id, container)
            if not strm_file.exists():
                _write_strm(strm_file, expected)
                chown_path(strm_file)
                ep_count += 1
            elif _strm_is_blank(strm_file):
                _write_strm(strm_file, expected)
                chown_path(strm_file)
                logger.info(f"[Sync] Rewrote {strm_file.name}: it was empty")
            elif _strm_needs_rewrite(strm_file, expected, client) or _strm_plays_other_provider(strm_file, client):
                _write_strm(strm_file, expected)
                chown_path(strm_file)
                logger.info(f"[Sync] Rewrote {strm_file.name}: stream address changed")
            try:
                slots.offer(strm_file, int(ep_id), expected)
            except (TypeError, ValueError):
                slots.unknown(show_dir)   # an id we can't read: can't tell what is gone
    if own:
        slots.settle(client)
    return ep_count


def _repair_movie_strm(client, stream: dict, tmdb_id: int, provider: Provider, db: Session,
                       restore: bool = True) -> bool:
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
            if _strm_is_blank(strm):
                if restore:   # only the row's own stream may fill it (#185 E25), as for a missing one
                    _write_strm(strm, expected)
                    chown_path(strm)
                    logger.info(f"[Sync] Rewrote {strm.name}: it was empty")
            elif _strm_needs_rewrite(strm, expected, client) or _strm_plays_other_provider(strm, client):
                _write_strm(strm, expected)
                chown_path(strm)
                logger.info(f"[Sync] Rewrote {strm.name}: stream address changed")
            return False
        if not restore:
            # #185 (E25): with two films of one title, only the row's own stream
            # may restore its file -- another listing could be its namesake.
            return False
        if _vod_root_unavailable(strm.parent.parent):
            # Storage unavailable, as for a show folder (#439).
            return False
        strm.parent.mkdir(parents=True, exist_ok=True)
        chown_path(strm.parent)
        _write_strm(strm, expected)
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
    slots: Optional[_EpisodeSlots] = None,
) -> int:
    """For an EXISTING VOD series owned by this provider, fetch series info and
    write any newly-added season/episode .strm files. Returns count of new files.

    No-ops for series not owned by this provider or without a known folder.
    Best-effort: any provider/IO error is swallowed so a single bad series
    doesn't break the category batch. `slots`: see _write_episode_strms.
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
        if _vod_root_unavailable(show_dir.parent):
            return 0

    try:
        series_info = client.get_series_info(series.get("series_id"))
        episodes = series_info.get("episodes", {})
        if isinstance(episodes, list):
            episodes = {"1": episodes}
        if not episodes:
            if slots is not None:
                slots.unknown(show_dir)
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
        new_eps = _write_episode_strms(client, episodes, show_dir, folder_name, slots)
        if new_eps:
            record.date_updated = datetime.utcnow()
            logger.info(f"[Sync] Back-filled {new_eps} new episode(s) for existing series '{record.title}'")
        return new_eps
    except Exception as e:
        if slots is not None:
            slots.unknown(show_dir)   # this listing's episodes are unknown: repoint nothing
        logger.debug(f"[Sync] Episode back-fill failed for tmdb_id={tmdb_id}: {e}")
        return 0


# ── Duplicate Detection ───────────────────────────────────────────────────

# What check_and_record_duplicate answers when a higher-priority provider has
# just taken a title over: truthy (the caller still creates no new row), and
# the caller must point the title's files at the new provider (#154).
TAKEOVER = "takeover"


def _take_over_files(db: Session, media_type: str, tmdb_id: int, client, item: dict,
                     provider: Provider, slots: Optional[_EpisodeSlots] = None) -> None:
    """Point a title a higher-priority provider just took over at that provider:
    the same repairs every later sync runs (see _strm_plays_other_provider),
    so paths and Jellyfin items stay. Tags are left alone: the old provider
    still offers the title, and this provider's tag is merged in on its next
    pass like any title's (#154)."""
    if media_type == "movie":
        _repair_movie_strm(client, item, tmdb_id, provider, db)
    else:
        _backfill_series_episodes(client, item, tmdb_id, provider, db, slots)
    logger.info(f"[Sync] tmdb:{tmdb_id} now plays from {provider.name} (higher priority)")


def _claim_vod_name(db: Session, media_type: str, output_dir: Path, title: str, year: Optional[str],
                    tmdb_id: int) -> str:
    """The folder name for a new VOD title, clear of other titles' files.

    Two different titles can map to one folder: namesakes with the same
    year, or names sanitize_filename reduces to the same string. The second
    import overwrote the first one's .strm and NFO, and pruning either
    deleted both (#155). When another row owns the path, this title gets a
    " [tmdbid-N]" folder, which Jellyfin also reads as the TMDB id."""
    Model = Movie if media_type == "movie" else Series

    def target(name):
        return output_dir / name / f"{name}.strm" if media_type == "movie" else output_dir / name

    name = vod_folder_name(title, year)
    taken = db.query(Model).filter(Model.strm_path == str(target(name)), Model.tmdb_id != tmdb_id).first()
    if taken is None:
        return name
    tag = f" [tmdbid-{tmdb_id}]" if tmdb_id > 0 else f" [id-{abs(tmdb_id)}]"
    claimed = vod_folder_name(title, year, tag=tag)
    logger.info(f"[Sync] '{name}' already holds '{taken.title}' (tmdb:{taken.tmdb_id}); "
                f"writing tmdb:{tmdb_id} to '{claimed}'")
    return claimed

# ── Namesakes: two films with the same title and year (#185) ─────────────
# Name matching cannot tell two such films apart: the same name and year give
# the same TMDB search result. The only thing that can is the TMDB id many
# Xtream panels attach to each stream ("tmdb"). It is used for that tie-break
# only: it must name a film with exactly the title and year the name found,
# and a stream never leaves the film whose .strm it already plays.

_ASCII_ID_RE = re.compile(r"[0-9]+")


def _provider_tmdb_hint(stream: dict):
    """The TMDB id the provider attached to this stream, or None. Only plain
    ASCII digits count: str.isdigit() also accepts "²" (which int() rejects,
    failing the whole provider sync) and other scripts' digits."""
    for key in ("tmdb", "tmdb_id"):
        try:
            raw = stream.get(key)
            if isinstance(raw, bool):
                continue
            text = str(raw or "").strip()
            if _ASCII_ID_RE.fullmatch(text) and 0 < int(text) < 2 ** 31:
                return int(text)
        except Exception:
            continue
    return None


class _MovieIndex:
    """What the namesake checks need to know about movie rows, loaded once per
    sync and kept up to date as titles are imported. A query per new import
    (on the unindexed strm_path column) made a first import of a large
    catalogue twice as slow."""

    def __init__(self, db: Session, provider: Provider):
        self.provider = provider
        self.strm_owner = {}  # strm_path -> tmdb_id, every row
        self.own_strm = {}    # tmdb_id -> strm_path, this provider's rows
        self.own_meta = {}    # tmdb_id -> {"title", "year"}, this provider's rows
        self.title_count = {}  # (normalised title, year) -> rows with it, any source
        # Accounts of the OTHER configured providers, as (host, username), and
        # their ids: positive evidence that a .strm plays another provider's
        # stream. Anything else a file of ours points at is this provider at
        # an earlier address (a host change), never "someone else" (D3).
        self.our_pair = _account_of(provider)
        self.other_pairs = set()
        self.other_ids = set()
        for other in db.query(Provider).filter(Provider.id != provider.id).all():
            self.other_ids.add(other.id)
            pair = _account_of(other)
            if pair and pair != self.our_pair:
                self.other_pairs.add(pair)
        # folder NAME -> tmdb_ids of Radarr downloads in a folder of that name.
        # Names, not paths: Radarr sees /data/movies/X while Tentacle writes
        # /media/vod/movies/X, and in the merged layout both are one folder.
        self.radarr_folders = {}
        self.shared = set()
        for tid, strm, pid, radarr, title, year in db.query(
                Movie.tmdb_id, Movie.strm_path, Movie.provider_id, Movie.radarr_path,
                Movie.title, Movie.year).all():
            if strm:
                if strm in self.strm_owner and self.strm_owner[strm] != tid:
                    self.shared.add(strm)  # two rows, one file (#155, from before)
                self.strm_owner[strm] = tid
            if radarr:
                self.radarr_folders.setdefault(Path(radarr).parent.name, set()).add(tid)
            key = _title_key(title, year)
            self.title_count[key] = self.title_count.get(key, 0) + 1
            if pid == provider.id:
                self.own_strm[tid] = strm
                self.own_meta[tid] = {"title": title, "year": year}

    def add(self, tmdb_id: int, strm_path: str, title=None, year=None):
        self.strm_owner[strm_path] = tmdb_id
        self.own_strm[tmdb_id] = strm_path
        self.own_meta[tmdb_id] = {"title": title, "year": year}
        key = _title_key(title, year)
        self.title_count[key] = self.title_count.get(key, 0) + 1

    def is_other_provider(self, origin) -> bool:
        if origin is None:
            return False
        if origin[0] == "pid":
            return origin[1] != self.provider.id
        return (origin[1], origin[2]) in self.other_pairs

    def only_one_with_title(self, tmdb_id: int) -> bool:
        meta = self.own_meta.get(tmdb_id)
        return not meta or self.title_count.get(_title_key(meta["title"], meta["year"]), 0) <= 1


_NS_STREAM_RE = re.compile(r"/(movie|series)/[^/?#&]+/[^/?#&]+/(\d+)\.[A-Za-z0-9]+")
_NS_CARRIED_RE = re.compile(r"(?i)https?://[^\s?#&]+/(?:movie|series)/[^/?#&]+/[^/?#&]+/\d+\.[a-z0-9]+")


def _title_key(title, year):
    return (re.sub(r"\W+", "", str(title or "").casefold()), str(year or ""))


def _account_of(provider: Provider):
    """(host, username) of a provider's Xtream account, or None (as _note_other_providers)."""
    from urllib.parse import urlparse
    host = (urlparse(provider.server_url or "").hostname or "").lower()
    return (host, provider.username or "") if host else None


def _play_ref(url_text: str, unwrap: bool = False):
    """(kind, stream number) a .strm plays: Tentacle's VOD address, a direct
    Xtream URL, or (with `unwrap`) one carried inside a resume proxy's URL."""
    from urllib.parse import unquote
    from services import vod_tokens
    via = vod_tokens.stream_id_in_url(url_text or "")
    if via:
        return via
    for candidate in ((url_text, unquote(url_text or "")) if unwrap else (url_text,)):
        m = _NS_STREAM_RE.search(candidate or "")
        if m:
            return m.group(1), int(m.group(2))
    return None


def _stream_origin(url_text: str, unwrap: bool = False):
    """Whose stream a URL plays: ("pid", provider id) for Tentacle's own VOD
    address, ("host", host, username) for a direct Xtream URL, None if unknown."""
    from urllib.parse import unquote
    from services import vod_tokens
    m = vod_tokens._TOKEN_URL.search(url_text or "")
    if m:
        return ("pid", int(m.group(2)))
    m = _XTREAM_ACCOUNT_RE.match(url_text or "")
    if m is None and unwrap:
        carried = _NS_CARRIED_RE.search(unquote(url_text or ""))
        m = _XTREAM_ACCOUNT_RE.match(carried.group(0)) if carried else None
    return ("host", m.group(1).lower(), unquote(m.group(2))) if m else None


def _movie_row_plays_stream(client, stream: dict, strm_path, index: "_MovieIndex" = None):
    """True / False when this .strm (a row's) does / does not play `stream`;
    None when that cannot be told (no row, no file, a hand-made URL).
    Stream numbers are only unique per provider, so with `index` a file playing
    the same number counts as another stream only on positive evidence that it
    is another configured provider's: its Tentacle VOD token names another
    provider, or its (host, username) is another provider's account. An older
    host of this provider is still this provider (#185 D3, E14/E27)."""
    if not strm_path:
        return None
    try:
        current = Path(strm_path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    expected = client.movie_stream_url(stream.get("stream_id"), stream.get("container_extension", "mp4"))
    mine = _play_ref(expected)
    theirs = _play_ref(current, unwrap=True)
    if mine is None or theirs is None:
        return None
    if mine != theirs:
        return False
    if index is None:
        return True
    return not index.is_other_provider(_stream_origin(current, unwrap=True))


def _same_film_name(a: dict, b: dict) -> bool:
    def norm(s):
        return re.sub(r"\W+", "", str(s or "").casefold())
    return bool(norm(a.get("title"))) and norm(a.get("title")) == norm(b.get("title")) \
        and str(a.get("year") or "") == str(b.get("year") or "")


def _known_title_id(ids, stream: dict, owns, namesake_of=None):
    """The already-imported film a stream is, from the title->ids map, or None
    when it needs a real lookup. `owns(id)` is _movie_row_plays_stream for it;
    `namesake_of(hint, id)` says whether TMDB names the hinted film with the
    same title and year as row `id`.

    One film with this title and a stream that carries no hint (or the same id)
    is the normal case, and stays a map lookup with no file read. Otherwise a
    stream keeps the film its .strm already plays, a hint picks among namesakes
    already imported, a hint that is not a namesake of the one known film is
    ignored (no name search), and a stream the map cannot place gets a real
    lookup."""
    if not ids:
        return None
    hint = _provider_tmdb_hint(stream)
    if hint is not None and not any(i > 0 for i in ids):
        hint = None  # provider-only titles (synthetic ids): the hint means nothing here
    if len(ids) == 1 and (hint is None or hint == ids[0]):
        return ids[0]
    verdicts = {i: owns(i) for i in ids}
    for i in ids:
        if verdicts[i] is True:
            return i
    if hint is not None and hint in ids:
        return hint
    if len(ids) == 1 and verdicts[ids[0]] is None:
        return ids[0]  # cannot tell: as before
    if len(ids) == 1 and hint is not None and namesake_of is not None and not namesake_of(hint, ids[0]):
        return ids[0]  # another listing of the known film with a wrong id: as before
    return None


def _namesake_claim(details, client, stream: dict, metadata: dict, index: _MovieIndex):
    """#185 D1: the hinted film's metadata when this stream's provider id names a
    DIFFERENT film with exactly the title and year the name found (a namesake
    claim), else None. A stream whose .strm already plays under the found film
    (or where that cannot be told) stays with it: no claim."""
    hint = _provider_tmdb_hint(stream)
    found = metadata.get("tmdb_id")
    if hint is None or not isinstance(found, int) or found <= 0 or hint == found:
        return None
    if found in index.own_strm and \
            _movie_row_plays_stream(client, stream, index.own_strm.get(found), index) is not False:
        return None
    other = details(hint)
    if not other or other.get("tmdb_id") != hint or not _same_film_name(other, metadata):
        return None
    return other


_NFO_TMDB_RE = re.compile(r"<tmdbid>\s*(-?\d+)\s*</tmdbid>|<uniqueid[^>]*type=\"tmdb\"[^>]*>\s*(-?\d+)\s*<", re.I)


def _nfo_tmdb_ids(folder: Path, names) -> set:
    """TMDB ids named by these NFOs in `folder` (missing/unreadable ones skipped)."""
    ids = set()
    for n in names:
        try:
            text = (folder / n).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for a, b in _NFO_TMDB_RE.findall(text):
            ids.add(int(a or b))
    return ids


def _folder_owned_elsewhere(index: _MovieIndex, folder: Path, tmdb_id: int, claim: bool = False) -> bool:
    """#185 additions to _claim_vod_name (which already moves a new title out
    of a folder another row's .strm is in): True when another film owns the
    folder without a .strm row there --
    - a Radarr download of another film in a folder of that NAME (the merged
      Radarr/VOD layout, where Radarr writes no NFO by default; names, since
      Radarr sees /data/movies/X and Tentacle /media/vod/movies/X);
    - an NFO there naming another TMDB id;
    - for a namesake claim only: a video there with no NFO naming this film
      and no Radarr row of this film in it (an unknown owner). A plain import
      writes next to such a video, as it always did.
    A re-import of the same film (its own NFO still on disk) keeps its folder."""
    radarr_ids = index.radarr_folders.get(folder.name, set())
    if radarr_ids - {tmdb_id}:
        return True
    if not folder.is_dir():
        return False
    try:
        entries = [f for f in folder.iterdir()]
    except OSError:
        return True  # cannot look inside: do not write into it
    nfo_ids = _nfo_tmdb_ids(folder, [f.name for f in entries if f.suffix.lower() == ".nfo"])
    if nfo_ids - {tmdb_id}:
        return True
    if not claim:
        return False
    has_video = any(f.suffix.lower() in MEDIA_SUFFIXES for f in entries)
    return has_video and tmdb_id not in nfo_ids and tmdb_id not in radarr_ids


def _record_duplicate_only(tmdb_id: int, source: str, path: str, db: Session) -> None:
    """Note that `source` also offers `tmdb_id`, which another source owns --
    the duplicate record of check_and_record_duplicate, without its #154
    takeover. For a film found through a provider id (#185 E26): a mislabelled
    stream must never move a row to a provider that does not really have it."""
    existing = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if not existing:
        return
    dup = db.query(Duplicate).filter(Duplicate.tmdb_id == tmdb_id, Duplicate.media_type == "movie").first()
    new_source = {"source": source, "path": path}
    if dup:
        sources = dup.sources or []
        if not any(s["source"] == source for s in sources):
            dup.sources = sources + [new_source]
    elif existing.source != source:
        db.add(Duplicate(
            tmdb_id=tmdb_id, media_type="movie",
            sources=[{"source": existing.source,
                      "path": existing.strm_path or existing.radarr_path or ""}, new_source],
            resolution="pending"))


def _stream_num(stream: dict) -> int:
    try:
        return int(stream.get("stream_id") or 0)
    except (TypeError, ValueError):
        return 0


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
            if media_type == "movie":
                delete_vod_files(existing.strm_path)
            else:
                # A series' strm_path is its show folder, which the movie
                # helper ignores — the episodes' .strm files stayed on disk
                # with nothing tracking them once the row was converted.
                from services.media_files import delete_series_files
                delete_series_files(existing.strm_path)
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
        # existing row instead of inserting a duplicate tmdb_id. The caller
        # points the files at it (see TAKEOVER).
        existing.provider_id = provider.id
        existing.source = source
        existing.date_updated = datetime.utcnow()
        return TAKEOVER

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


# A TMDB lookup that FAILED (429/5xx/timeout/unreachable/refused key) is not
# "TMDB has no such title" (#377). With "Require TMDB match" off, such a title
# used to be imported under a provider-only id, and from then on the known-title
# map skipped its lookup for good. Now it is left for the next sync, like a
# title with the setting on, for up to LOOKUP_RETRY_DAYS from its first failed
# lookup; after that it is imported without a match as before, so an install
# that can never reach TMDB still gets its titles. The first-failure times live
# in one setting per provider and type, rewritten at the end of each sync with
# only the titles that failed again (matched or vanished titles drop out).
LOOKUP_RETRY_DAYS = 3


def _save_failed_lookups(db: Session, failed_lookups: "_FailedLookups", kind: str) -> None:
    if failed_lookups.deferred:
        logger.info(f"[Sync] {failed_lookups.deferred} {kind} title(s) whose TMDB lookup failed are left for the "
                    f"next sync instead of being imported without a match (after {LOOKUP_RETRY_DAYS} days of "
                    f"failed lookups they are imported anyway)")
    try:
        failed_lookups.save(db)
    except Exception as e:
        db.rollback()
        logger.warning(f"[Sync] Could not save the failed TMDB lookups: {e}")


class _FailedLookups:
    def __init__(self, db: Session, provider_id: int, media_type: str):
        import json
        self.key = f"tmdb_failed_lookups:{provider_id}:{media_type}"
        try:
            prior = json.loads(get_setting(db, self.key) or "{}")
        except ValueError:
            prior = {}
        self.prior = prior if isinstance(prior, dict) else {}
        self.tonight = {}
        self.deferred = 0

    def defer(self, lookup_key, now: datetime) -> bool:
        """True: skip the title tonight and look it up again next sync."""
        name, year = lookup_key
        k = f"{name}|{year or ''}"
        first = self.tonight.get(k) or self.prior.get(k)
        try:
            first = datetime.fromisoformat(first) if first else now
            # A clock set back since: never later than now (no longer wait)
            first = min(first.replace(tzinfo=None), now)
        except (TypeError, ValueError, AttributeError):
            first = now
        if now - first >= timedelta(days=LOOKUP_RETRY_DAYS):
            return False
        self.tonight[k] = first.isoformat()
        self.deferred += 1
        return True

    def save(self, db: Session) -> None:
        import json
        from models.database import Setting
        if self.tonight:
            set_setting(db, self.key, json.dumps(self.tonight, sort_keys=True))
        elif self.prior or db.query(Setting).filter(Setting.key == self.key).first() is not None:
            db.query(Setting).filter(Setting.key == self.key).delete()
            db.commit()


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
        # Two titles that were written to one folder before #155 share its
        # files: removing one row must not delete what the other plays.
        shared = record.strm_path and db.query(Model).filter(
            Model.strm_path == record.strm_path, Model.id != record.id).first()
        if shared:
            logger.info(f"[Sync] Keeping the files of '{record.title}': '{shared.title}' still uses them")
        elif media_type == "movie":
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


def _vod_root_unavailable(root: Path) -> bool:
    """A library root that is missing or empty: the storage is not mounted.

    mergerfs/NFS/SMB/rclone all report plain "not found" for every path while
    a branch is out, and Docker shows a share that isn't mounted as the bare,
    empty mount point."""
    return not root.is_dir() or not any(root.iterdir())


def _swept_rows(db: Session, Model):
    """The VOD rows the sweep checks against the disk (and the sync must not
    lose by writing onto an unmounted root)."""
    return db.query(Model).filter(
        Model.source.like("provider_%"),
        Model.strm_path.isnot(None),
        # Titles the user opted out of .strm management are expected to have no
        # .strm on disk — that is the whole point. Sweeping them would delete
        # the row and the next sync would re-import the title and rewrite the
        # file, silently undoing the opt-out.
        Model.strm_disabled.isnot(True),
    )


def _check_vod_root_before_sync(db: Session, Model, root: Path, what: str):
    """Raise SyncError when the library root is missing or empty while titles
    are recorded in it (#439). Writing there put the provider's titles on the
    container's own disk, hidden again once the share was mounted, and the
    root was no longer empty, so the VOD sweep deleted every title the sync
    had not written back. A new install has no rows, so it syncs."""
    if not _vod_root_unavailable(root):
        return
    count = _swept_rows(db, Model).count()
    if count:
        raise SyncError(
            f"{root} is missing or empty, but {count} {what} are recorded there: the storage "
            f"looks unmounted, so nothing was synced. Mount it and sync again. If the folder "
            f"really is empty now (a new disk), put any file in it and sync again."
        )


def _sweep_one_type(db: Session, Model, media_type: str, root: Path, now: datetime):
    """Sweep one media type. Returns (removed_count, removed_titles)."""
    rows = _swept_rows(db, Model).all()
    if not rows:
        return 0, []

    # A mount that is missing or empty means the storage is unavailable, not
    # that every title was deleted.
    if _vod_root_unavailable(root):
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

def _provider_error_reason(e: Exception) -> str:
    """A short reason, for the run and Activity, why a category fetch failed
    (#267). Provider URLs carry the account's login: never quote one."""
    if isinstance(e, ProviderDataError):
        return str(e)
    if isinstance(e, requests.HTTPError):
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status is None:
            m = re.match(r"\s*(\d{3})\b", str(e))
            status = m.group(1) if m else None
        return f"the provider answered HTTP {status}" if status else "the provider answered with an error"
    if isinstance(e, requests.Timeout):
        return "the provider did not answer in time"
    if isinstance(e, (ProviderConnectionError, requests.ConnectionError)):
        return "the provider could not be reached"
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return re.sub(r"https?://\S+", "<provider URL>", text)[:200]


def _unread_summary(unread: list, total: int) -> str:
    """"N of M categories could not be read (reason)", with the first reason."""
    return f"{len(unread)} of {total} categor{'y' if total == 1 else 'ies'} could not be read ({unread[0][1]})"


def _finish_run(db: Session, run: SyncRun, status: str, message: str) -> SyncRun:
    """Record how a sync run ended after an error (#270).

    Committed as before when the session can still write (what the sync did
    up to the error is kept). When the error was a failed flush ("database is
    locked", a constraint) the session refuses every statement until it is
    rolled back: committing the run's end on it raised PendingRollbackError and
    the row stayed "running" for good (the nightly skipped the provider every
    night; "Sync now" said a sync was running). Then roll back (only the
    uncommitted work is lost; each category commits its own) and record it,
    or record it with a fresh session if even that fails."""
    text = (message or "").strip()
    message = text.splitlines()[0][:1000] if text else status.capitalize()
    from sqlalchemy import inspect as sa_inspect
    run_id = (sa_inspect(run).identity or (None,))[0]   # no load: the session may refuse one

    def _stamp(row):
        row.status = status
        row.error_message = message
        row.completed_at = datetime.utcnow()
        if row.started_at:
            row.duration_seconds = int((row.completed_at - row.started_at).total_seconds())

    for attempt in ("as is", "after a rollback"):
        try:
            if attempt != "as is":
                db.rollback()
                run = db.merge(run)
            _stamp(run)
            db.commit()
            return run
        except Exception as e:
            logger.warning(f"Could not record the end of sync run #{run_id} ({attempt}): {e}")
            try:
                db.rollback()
            except Exception:
                pass
    from sqlalchemy.orm import sessionmaker
    fresh = sessionmaker(bind=db.get_bind())()
    try:
        row = fresh.get(SyncRun, run_id)
        if row is not None:
            _stamp(row)
            fresh.commit()
            fresh.refresh(row)
            fresh.expunge(row)
            return row
    except Exception as e:
        logger.error(f"Could not record the end of sync run #{run_id}: {e}")
    finally:
        fresh.close()
    return run


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

    turn = False
    try:
        _wait_for_vod_sync_turn(provider, run, db, progress_callback, cancel_check,
                                "series" if sync_type == "series" else "movies")
        turn = True
        try:
            unhide_vod_paths(db)
        except Exception as e:
            logger.warning(f"Could not move hidden VOD folders: {e}")

        # Pre-sync disk space check
        if sync_type in ("full", "movies"):
            _check_vod_root_before_sync(db, Movie, vod_movies_path, "films")
            _check_disk_before_sync(vod_movies_path)
        if sync_type in ("full", "series"):
            _check_vod_root_before_sync(db, Series, vod_series_path, "shows")
            _check_disk_before_sync(vod_series_path)

        tmdb = TMDBService(bearer_token, data_dir, match_threshold)
        client = make_provider_client(provider)
        # The sync stands aside for live TV / a recording at every category
        # boundary (services.provider_activity), so a recording that starts
        # mid-sync is not competed with either. One budget for the whole run.
        from services.provider_activity import JobPause
        client.job_pause = pause if pause is not None else JobPause(db, "the provider sync", cancel_check)
        client.job_pause.cancel_check = cancel_check
        client.job_pause.run_id = run.id    # protected waits are booked to this run (routers.sync)
        client.vod_links = vod_links_for(db, provider)
        _note_other_providers(client, provider, db)
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

        # Categories the provider could not be read for (#267): all of them is a
        # failed run, some a completed one with a warning. Pruning already
        # skipped them (fetch_ok).
        unread = [u for c in (m_cleanup, s_cleanup) if c for u in c.get("unread", ())]
        total_cats = sum(c.get("categories", 0) for c in (m_cleanup, s_cleanup) if c)
        run.status = "completed"
        run.error_message = None
        if unread:
            summary = _unread_summary(unread, total_cats)
            logger.warning(f"Sync of {provider.name}: {summary}")
            if len(unread) >= total_cats:
                run.status = "failed"
                run.error_message = (f"The provider could not be read for any of its {total_cats} "
                                     f"categor{'y' if total_cats == 1 else 'ies'} ({unread[0][1]})")
            else:
                run.error_message = summary
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
        run = _finish_run(db, run, "cancelled", msg)
    except SyncError as e:
        logger.error(f"Sync error: {e}")
        run = _finish_run(db, run, "failed", str(e))
    except ProviderConnectionError as e:
        logger.error(f"Sync failed — provider unreachable: {e}")
        run = _finish_run(db, run, "failed", str(e))
    except Exception as e:
        logger.error(f"Sync failed: {e}", exc_info=True)
        run = _finish_run(db, run, "failed", str(e))
    finally:
        if turn:
            _release_vod_sync_turn()

    return run


def _place_relisted_movies(db: Session, client, provider: Provider, index: "_MovieIndex", stats: dict,
                           seen_ids_all: set, unmatched: list, met_streams: dict, listed_refs: set,
                           fetch_ok: bool, may_restore) -> None:
    """After every category of a movie sync, two repairs by what each film's
    .strm plays (one read of each file of ours; the sync reads them anyway).

    #262: a stream no label placed (no TMDB match) that a film's .strm already
    plays is that film, relabelled by the provider (a year or name change):
    it keeps it, counted as existing and seen -- a stream never leaves the
    film whose .strm plays it (#185). Before, the film was pruned after two
    syncs although its stream was still listed.

    #263: a film whose .strm plays a stream of this provider that is no longer
    listed anywhere (a complete fetch only), met this sync under another
    stream id, is pointed at that stream in place (same path: the Jellyfin
    item and its user data stay). While both ids are listed nothing flips,
    as before. Not for M3U (its ids are made from the URL), an opted-out
    film, a file two rows share, or a stream another film's .strm plays."""
    xtream = isinstance(client, XtreamClient)
    if not unmatched and not (fetch_ok and xtream and met_streams):
        return
    plays = {}    # (kind, number) -> tmdb_id of the film of ours whose .strm plays it
    row_ref = {}  # tmdb_id -> what its .strm plays
    for tid, path in index.own_strm.items():
        if not path or path in index.shared:
            continue
        try:
            current = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        ref = _play_ref(current, unwrap=True)
        if ref is None or index.is_other_provider(_stream_origin(current, unwrap=True)):
            continue
        row_ref[tid] = ref
        plays[ref] = tid if plays.get(ref, tid) == tid else None   # two rows: nobody's

    def ref_of(stream):
        return _play_ref(client.movie_stream_url(stream.get("stream_id"), stream.get("container_extension", "mp4")))

    kept = 0
    for stream, tag in unmatched:
        tid = plays.get(ref_of(stream))
        if tid is None:
            continue
        logger.info(f"[Sync] '{stream.get('name')}' (stream {stream.get('stream_id')}) is still "
                    f"'{(index.own_meta.get(tid) or {}).get('title')}' (TMDB {tid}): relabelled by the provider")
        _merge_source_tag(tid, "movie", tag, provider.id, db)
        seen_ids_all.add(tid)
        stats["skipped"] -= 1
        stats["existing"] += 1
        kept += 1

    repointed = 0
    if fetch_ok and xtream:
        for tid, streams in met_streams.items():
            have = row_ref.get(tid)
            if have is None or have in listed_refs or tid not in index.own_strm:
                continue
            target = next((st for st in streams
                           if plays.get(ref_of(st), tid) == tid and ref_of(st) != have and may_restore(st, tid)),
                          None)
            if target is None:
                continue
            record = db.query(Movie).filter(Movie.tmdb_id == tid, Movie.provider_id == provider.id).first()
            if record is None or record.strm_disabled or not record.strm_path:
                continue
            strm = Path(record.strm_path)
            expected = client.movie_stream_url(target.get("stream_id"), target.get("container_extension", "mp4"))
            try:
                _write_strm(strm, expected)
                chown_path(strm)
            except OSError as e:
                logger.warning(f"[Sync] Could not point {strm.name} at its new stream: {e}")
                continue
            plays[ref_of(target)] = tid
            record.date_updated = datetime.utcnow()
            repointed += 1
            logger.info(f"[Sync] {strm.name}: stream {have[1]} is no longer listed; "
                        f"now plays stream {target.get('stream_id')}")
    if kept or repointed:
        db.commit()
        logger.info(f"[Sync] {provider.name}: {kept} relabelled film(s) kept, "
                    f"{repointed} .strm file(s) moved to a re-listed stream")


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
    failed_lookups = _FailedLookups(db, provider.id, "movie")
    lookup_now = datetime.utcnow()

    stats = {"new": 0, "existing": 0, "failed": 0, "skipped": 0}
    feed = []
    category_stats = {}

    # Track TMDB IDs seen this run to dedupe across categories
    seen_tmdb_ids = set()
    dup_ids = set()  # films another source owns, met this run (duplicate recorded)
    # Comprehensive set of every tmdb_id this provider still offers (new OR
    # existing). Used after a successful run to delete rows for content the
    # provider has dropped upstream.
    seen_ids_all = set()
    # True only if every whitelisted category was fetched successfully. If any
    # fetch failed, the seen set is incomplete and we must NOT prune.
    fetch_ok = True
    # Categories that could not be read, as (name, reason): reported on the run (#267)
    unread = []

    # Load existing TMDB IDs from this provider to avoid re-processing
    existing_provider_tmdb_ids = {
        m.tmdb_id for m in db.query(Movie.tmdb_id).filter(
            Movie.provider_id == provider.id
        ).all()
    }

    # Build title→tmdb_ids lookup so we can skip TMDB API for known items.
    # A list: two different films can share a title and year (#185).
    known_titles = {}
    for m in db.query(Movie.title, Movie.year, Movie.tmdb_id).filter(
            Movie.provider_id == provider.id).all():
        known_titles.setdefault((m.title.lower(), m.year), []).append(m.tmdb_id)

    index = _MovieIndex(db, provider)

    details_memo = {}

    def _details(i):
        """TMDB details, once per id per sync (TMDBService also caches them)."""
        if i not in details_memo:
            try:
                details_memo[i] = tmdb.get_movie_details(i)
            except Exception:
                details_memo[i] = None
        return details_memo[i]

    def _namesake_of(hint, row_id):
        other = _details(hint)
        row = index.own_meta.get(row_id)
        return bool(other and row and other.get("tmdb_id") == hint and _same_film_name(other, row))

    def _may_restore(stream, tmdb_id, guessed=False):
        """#185 E25: a missing .strm is restored only from the row's own stream:
        its provider id names the row, or the row is the only film with its
        title and year (any source) -- then there is no namesake to confuse it
        with, exactly as in 755ea67."""
        if index.own_strm.get(tmdb_id) in index.shared:
            return False  # a #155 legacy file two rows share: never rewritten (E18)
        if tmdb_id < 0:  # a provider-only title: its id is made from its own stream
            return tmdb_id == -(provider.id * NEGATIVE_ID_BLOCK + _stream_num(stream))
        return _provider_tmdb_hint(stream) == tmdb_id or index.only_one_with_title(tmdb_id)

    # #185 D6: a row created by its namesake's stream plays the wrong film. It
    # is only logged (once per row per sync): a panel that swaps two namesakes'
    # ids shows exactly the same signals, and rewriting would cross a right row.
    hinted_streams = {}   # (title key, provider id) -> first stream carrying it
    swap_suspects = {}    # row id -> (stream its .strm plays, that stream's id)
    swap_logged = set()

    def _log_suspected_swap(lookup_key, r, a, b, b_hint):
        if r in swap_logged or b_hint == r:
            return
        strm = index.own_strm.get(r)
        if not strm or strm in index.shared:
            return
        row, mine, theirs = index.own_meta.get(r), _details(r), _details(b_hint)
        if not (row and mine and theirs and mine.get("tmdb_id") == r and theirs.get("tmdb_id") == b_hint
                and _same_film_name(mine, row) and _same_film_name(theirs, row)):
            return
        if _nfo_tmdb_ids(Path(strm).parent, [Path(strm).with_suffix(".nfo").name]) != {r}:
            return
        swap_logged.add(r)
        logger.info(f"[Sync] Suspected swapped namesake ids: row TMDB {r} plays stream {b.get('stream_id')} "
                    f"(provider id {b_hint}); stream {a.get('stream_id')} has provider id {r}. "
                    f"Not changed: fix it with Wrong movie.")

    # #185 D2: namesake claims (a stream whose provider id names a namesake of
    # the film its name found), decided once every category has been seen.
    claims = []
    # Streams this sync did not place on a row of ours (another source's film,
    # or no match), by title: once a claim adds a film with that title, they
    # are placed the way the next sync's title map will place them (S4).
    unplaced = {}
    # #262/#263: what the provider lists this sync, by what a .strm would play;
    # streams with no match at all; the streams each existing film was met with.
    listed_refs = set()
    unmatched = []        # (stream, source tag)
    met_streams = {}      # tmdb_id -> [stream]

    def _import_movie(stream, metadata, source_tag, lookup_key, claim=False):
        """Write a new film's .strm/.nfo and add its row (committed with the
        category). "new", "duplicate" (another source owns it: nothing
        written) or "failed". A `claim` (a film found through a provider id)
        records a duplicate only and never takes a row over (#185 E26)."""
        tmdb_id = metadata["tmdb_id"]
        # Compute file path early so duplicate record has it
        title = metadata["title"]
        year_str = metadata.get("year")
        folder_name = _claim_vod_name(db, "movie", output_dir, title, year_str, tmdb_id)
        if folder_name == vod_folder_name(title, year_str) and \
                _folder_owned_elsewhere(index, output_dir / folder_name, tmdb_id, claim):
            # Another film owns that folder without a .strm row there (#185)
            folder_name = vod_folder_name(title, year_str, tag=f" [tmdbid-{tmdb_id}]" if tmdb_id > 0
                                          else f" [id-{abs(tmdb_id)}]")
        movie_dir = output_dir / folder_name
        strm_file = movie_dir / f"{folder_name}.strm"
        nfo_file = movie_dir / f"{folder_name}.nfo"

        # Check if exists from another provider (duplicate)
        if claim and db.query(Movie.id).filter(Movie.tmdb_id == tmdb_id).first():
            _record_duplicate_only(tmdb_id, f"provider_{provider.id}", str(strm_file), db)
            return "duplicate"
        dup_answer = check_and_record_duplicate(tmdb_id, "movie", f"provider_{provider.id}", str(strm_file),
                                                provider, db)
        if dup_answer == TAKEOVER:
            _take_over_files(db, "movie", tmdb_id, client, stream, provider)
        if dup_answer:
            return "duplicate"

        # Compute tags
        now = datetime.utcnow()
        list_tags = get_list_tags_for_tmdb_id(tmdb_id, "movie", db)
        tags = compute_tags(source_tag, now, list_tags, recently_added_days, media_type="movie")

        # Apply tag rules
        metadata["tags"] = tags
        rule_tags = apply_tag_rules(metadata, "movie", f"provider_{provider.id}", source_tag, db)
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
            _write_strm(strm_file, stream_url)
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
                source_tag=source_tag,
                tags=tags,
                date_added=now,
            )
            db.add(movie_record)

            existing_provider_tmdb_ids.add(tmdb_id)
            known_titles.setdefault(lookup_key, []).append(tmdb_id)
            index.add(tmdb_id, str(strm_file), title, year_str)

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
            return "new"
        except Exception as e:
            logger.error(f"Failed to create files for {title}: {e}")
            return "failed"

    def _known_id(lookup_key, stream):
        return _known_title_id(
            known_titles.get(lookup_key), stream,
            lambda i: _movie_row_plays_stream(client, stream, index.own_strm.get(i), index),
            _namesake_of)

    def _in_library(i):
        return i in existing_provider_tmdb_ids or i in seen_tmdb_ids

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
            unread.append((cat.category_name, _provider_error_reason(e)))
            continue

        _prev_count = cat.title_count
        # Some TMDB lookup in this category failed tonight (see the count below).
        cat_lookup_failed = False
        if _category_went_empty(db, cat, len(streams)):
            logger.warning(
                f"Category '{cat.category_name}' returned 0 titles but held "
                f"{_prev_count} last time — treating as a failed fetch, not as "
                f"an emptied category. Nothing will be pruned from this sync."
            )
            fetch_ok = False
            unread.append((cat.category_name, f"it returned no titles (it held {_prev_count} last time)"))
            continue

        total_in_cat = len(streams)
        for listed in streams:
            ref = _play_ref(client.movie_stream_url(listed.get("stream_id"),
                                                    listed.get("container_extension", "mp4")))
            if ref:
                listed_refs.add(ref)

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
        failed_idx = set()  # indices whose lookup failed (not "no match", #377)
        known_by_idx = {}  # index → tmdb_id of an already-imported film (no lookup)

        for idx, (stream, raw_name, clean_name, year) in enumerate(cleaned):
            if not clean_name:
                continue
            if overrides and override_for(overrides, stream.get("stream_id"), client.movie_stream_url(
                    stream.get("stream_id"), stream.get("container_extension", "mp4"))) is not None:
                continue  # re-matched by an admin — no lookup of the (wrong) label

            # Check if we already know this title → skip TMDB API call
            lookup_key = (clean_name.lower(), year)
            tmdb_id = _known_id(lookup_key, stream)
            if tmdb_id is not None and _in_library(tmdb_id):
                known_by_idx[idx] = tmdb_id
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
                        failed_idx.add(idx)
                    elif metadata:
                        tmdb_results[idx] = metadata
            if lookup_failed:
                cat_lookup_failed = True
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
            idx = item_idx - 1  # 0-based index into cleaned

            # Get metadata: from parallel batch or title-based lookup
            metadata = tmdb_results.get(idx)
            known_id = None
            override_hit = overrides and override_for(overrides, stream.get("stream_id"), client.movie_stream_url(
                stream.get("stream_id"), stream.get("container_extension", "mp4"))) is not None
            if metadata and not override_hit:
                # Same name and year as another film: a claim, decided at the end (#185 D2)
                other = _namesake_claim(_details, client, stream, metadata, index)
                if other is not None:
                    claims.append({"stream": stream, "tag": cat.source_tag, "key": lookup_key,
                                   "x": metadata["tmdb_id"], "h": other})
                    cat_existing += 1
                    stats["existing"] += 1
                    continue
            guessed = False
            if not metadata and not override_hit:
                known_id = known_by_idx.get(idx)
                if known_id is None and known_titles.get(lookup_key):
                    # No lookup result: as before, the title map places it
                    known_id = _known_id(lookup_key, stream)
                    if known_id is None:
                        known_id = known_titles[lookup_key][-1]
                        guessed = True
            override_id = override_for(overrides, stream.get("stream_id"), client.movie_stream_url(
                stream.get("stream_id"), stream.get("container_extension", "mp4"))) if overrides else None
            if override_id is not None:
                # Already in the library under the fixed id → the existing path
                # below merges tags; otherwise fetch the right film's metadata.
                if override_id in existing_provider_tmdb_ids or override_id in seen_tmdb_ids:
                    metadata = {"tmdb_id": override_id}
                else:
                    tmdb_down = False
                    tl = getattr(tmdb, "_tl", None)
                    if tl is not None:
                        tl.failed = False  # get_movie_details doesn't reset it; a stale flag isn't this lookup's
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
                            cat_lookup_failed = True
                        else:
                            logger.warning(f"[Sync] Stream {stream.get('stream_id')} is fixed to TMDB {override_id}, "
                                           f"which TMDB no longer has; skipped (fix it again to a film TMDB knows)")
                        cat_skipped += 1
                        stats["skipped"] += 1
                        continue
                known_id = None
            hint = _provider_tmdb_hint(stream) if (not metadata and known_id and override_id is None
                                                   and known_id > 0 and _in_library(known_id)) else None
            if hint is not None:
                if hint == known_id:
                    hinted_streams.setdefault((lookup_key, hint), stream)
                    if known_id in swap_suspects:
                        b, b_hint = swap_suspects[known_id]
                        _log_suspected_swap(lookup_key, known_id, stream, b, b_hint)
                elif _movie_row_plays_stream(client, stream, index.own_strm.get(known_id), index) is True:
                    swap_suspects.setdefault(known_id, (stream, hint))
                    a = hinted_streams.get((lookup_key, known_id))
                    if a is not None:
                        _log_suspected_swap(lookup_key, known_id, a, stream, hint)
            if not metadata and known_id:
                # Known title but not in batch — it's existing, merge tags
                if known_id in existing_provider_tmdb_ids or known_id in seen_tmdb_ids:
                    _merge_source_tag(known_id, "movie", cat.source_tag, provider.id, db)
                    seen_ids_all.add(known_id)
                    # Existing VOD movie — restore its .strm if it vanished from disk
                    _repair_movie_strm(client, stream, known_id, provider, db,
                                       restore=_may_restore(stream, known_id, guessed))
                    met_streams.setdefault(known_id, []).append(stream)
                    cat_existing += 1
                    stats["existing"] += 1
                    if item_idx % 10 == 0 or item_idx == total_in_cat:
                        logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                    continue

            if not metadata:
                if require_tmdb or (idx in failed_idx and failed_lookups.defer(lookup_key, lookup_now)):
                    unplaced.setdefault(lookup_key, []).append((stream, cat.source_tag))
                    unmatched.append((stream, cat.source_tag))
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
            if tmdb_id in seen_tmdb_ids or tmdb_id in existing_provider_tmdb_ids or tmdb_id in dup_ids:
                if tmdb_id not in existing_provider_tmdb_ids:
                    unplaced.setdefault(lookup_key, []).append((stream, cat.source_tag))
                _merge_source_tag(tmdb_id, "movie", cat.source_tag, provider.id, db)
                # Existing VOD movie — restore its .strm if it vanished from disk
                _repair_movie_strm(client, stream, tmdb_id, provider, db,
                                   restore=_may_restore(stream, tmdb_id))
                met_streams.setdefault(tmdb_id, []).append(stream)
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            # Also check DB directly in case of prior partial sync — merge tags
            if db.query(Movie).filter(Movie.tmdb_id == tmdb_id, Movie.provider_id == provider.id).first():
                existing_provider_tmdb_ids.add(tmdb_id)
                _merge_source_tag(tmdb_id, "movie", cat.source_tag, provider.id, db)
                # Existing VOD movie — restore its .strm if it vanished from disk
                _repair_movie_strm(client, stream, tmdb_id, provider, db,
                                   restore=_may_restore(stream, tmdb_id))
                met_streams.setdefault(tmdb_id, []).append(stream)
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            result = _import_movie(stream, metadata, cat.source_tag, lookup_key)
            # "Seen" only once it is ours: a film another source owns must not
            # count as this provider's (#185 review cause A).
            if result == "new":
                seen_tmdb_ids.add(tmdb_id)
                cat_new += 1
                stats["new"] += 1
            elif result == "duplicate":
                dup_ids.add(tmdb_id)
                unplaced.setdefault(lookup_key, []).append((stream, cat.source_tag))
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue
            else:
                cat_failed += 1
                stats["failed"] += 1

            # Periodic progress log — every 10 items or last item
            if item_idx % 10 == 0 or item_idx == total_in_cat:
                logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")

        # Commit all new movies for this category at once
        # The count is also the guard's memory (_category_went_empty): a category
        # at 0 is not protected from an empty provider answer. On a night some
        # lookups failed, what matched undercounts what the category holds — a
        # TMDB outage could take it to 0, and two empty answers later every title
        # in it was pruned (#25). Keep the higher count then; the snapshot below
        # records what was really matched.
        cat.title_count = max(_prev_count or 0, cat_new + cat_existing) if cat_lookup_failed \
            else cat_new + cat_existing
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

    # #185 D2: namesake claims, decided once every category has been seen,
    # from the state at sync start plus this sync's plain matches -- never from
    # the order streams came in.
    if claims:
        held = set(seen_ids_all)  # films a plain (non-claim) stream resolved to this sync
        start_rows = set(existing_provider_tmdb_ids)
        for c in sorted(claims, key=lambda c: (c["h"]["tmdb_id"], _stream_num(c["stream"]))):
            h, x, stream = c["h"]["tmdb_id"], c["x"], c["stream"]
            logger.debug(f"[Sync] claim: stream {stream.get('stream_id')} names TMDB {h} (name: {x}); "
                         f"ours {h in existing_provider_tmdb_ids}/{x in start_rows}, held {x in held}, "
                         f"fetch_ok {fetch_ok}")
            if x in start_rows and x not in held:
                # Whatever the claim becomes, a row that existed at the start and
                # has no stream of its own this sync keeps counting this one:
                # a claim never leaves a row to the prune (S1).
                seen_ids_all.add(x)
            if h in existing_provider_tmdb_ids:
                # Another listing of a film we already have (or just imported)
                _merge_source_tag(h, "movie", c["tag"], provider.id, db)
                seen_ids_all.add(h)
                _repair_movie_strm(client, stream, h, provider, db, restore=True)
                continue
            other_owner = db.query(Movie.id).filter(Movie.tmdb_id == h).first()
            if other_owner:
                # Radarr or another provider has it: a duplicate record, never a takeover (E26)
                _record_duplicate_only(h, f"provider_{provider.id}", "", db)
                continue
            x_other = db.query(Movie.id).filter(Movie.tmdb_id == x, Movie.provider_id != provider.id).first()
            if fetch_ok and (x not in start_rows or x in held or x_other):
                logger.info(f"[Sync] '{stream.get('name')}' is TMDB {h}, not TMDB {x}: "
                            f"two films named '{c['h'].get('title')} ({c['h'].get('year')})'")
                result = _import_movie(stream, c["h"], c["tag"], c["key"], claim=True)
                seen_ids_all.add(h)
                if result == "new":
                    seen_tmdb_ids.add(h)
                    stats["existing"] -= 1
                    stats["new"] += 1
                continue
            # Undecided (the name's film is ours and has no stream of its own
            # this sync, or part of the catalogue was not seen): x is kept from
            # the prune (above) and nothing else changes.
        # A film a claim just added: streams of the same title that found no row
        # of ours are placed now as the title map will place them next sync.
        for key in {c["key"] for c in claims}:
            for stream, tag in unplaced.get(key, ()):
                kid = _known_id(key, stream)
                if kid is not None and kid in existing_provider_tmdb_ids:
                    _merge_source_tag(kid, "movie", tag, provider.id, db)
                    seen_ids_all.add(kid)
        db.commit()

    _place_relisted_movies(db, client, provider, index, stats, seen_ids_all, unmatched,
                           met_streams, listed_refs, fetch_ok, _may_restore)

    logger.info(
        f"Movies complete: {stats['new']} new, {stats['existing']} existing, "
        f"{stats['skipped']} skipped (no TMDB), {stats['failed']} failed"
    )

    if blocked_skips:
        logger.info(f"[Sync] Skipped {blocked_skips} blocked (mislabelled) stream(s) from {provider.name}")
    _save_failed_lookups(db, failed_lookups, "movie")
    return stats, feed, category_stats, {"seen_ids": seen_ids_all, "fetch_ok": fetch_ok,
                                          "categories": len(whitelisted_cats), "unread": unread}


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
    unread = []
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
    failed_lookups = _FailedLookups(db, provider.id, "series")
    lookup_now = datetime.utcnow()
    # What every listing offers at each episode file; settled after the loop (#376)
    slots = _EpisodeSlots()

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
            unread.append((cat.category_name, _provider_error_reason(e)))
            continue

        _prev_count = cat.title_count
        # Some TMDB lookup in this category failed tonight (see the count below).
        cat_lookup_failed = False
        if _category_went_empty(db, cat, len(series_list)):
            logger.warning(
                f"Category '{cat.category_name}' returned 0 series but held "
                f"{_prev_count} last time — treating as a failed fetch, not as "
                f"an emptied category. Nothing will be pruned from this sync."
            )
            fetch_ok = False
            unread.append((cat.category_name, f"it returned no series (it held {_prev_count} last time)"))
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
        failed_idx = set()  # indices whose lookup failed (not "no match", #377)

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
                        failed_idx.add(idx)
                    elif metadata:
                        tmdb_results[idx] = metadata
            if lookup_failed:
                cat_lookup_failed = True
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
                    _backfill_series_episodes(client, series, known_id, provider, db, slots)
                    cat_existing += 1
                    stats["existing"] += 1
                    if item_idx % 10 == 0 or item_idx == total_in_cat:
                        logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                    continue

            if not metadata:
                if require_tmdb or (idx in failed_idx and failed_lookups.defer(lookup_key, lookup_now)):
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
                _backfill_series_episodes(client, series, tmdb_id, provider, db, slots)
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
                _backfill_series_episodes(client, series, tmdb_id, provider, db, slots)
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            # Compute file path early so duplicate record has it
            title = metadata["title"]
            year_str = metadata.get("year")
            folder_name = _claim_vod_name(db, "series", output_dir, title, year_str, tmdb_id)
            show_dir = output_dir / folder_name

            dup_answer = check_and_record_duplicate(tmdb_id, "series", f"provider_{provider.id}", str(show_dir),
                                                    provider, db)
            if dup_answer == TAKEOVER:
                _take_over_files(db, "series", tmdb_id, client, series, provider, slots)
            if dup_answer:
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
                ep_count = _write_episode_strms(client, episodes, show_dir, folder_name, slots)

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
        # The count is also the guard's memory (_category_went_empty): a category
        # at 0 is not protected from an empty provider answer. On a night some
        # lookups failed, what matched undercounts what the category holds — a
        # TMDB outage could take it to 0, and two empty answers later every title
        # in it was pruned (#25). Keep the higher count then; the snapshot below
        # records what was really matched.
        cat.title_count = max(_prev_count or 0, cat_new + cat_existing) if cat_lookup_failed \
            else cat_new + cat_existing
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

    # A listing in an unread category may be the one an episode file plays:
    # repoint episodes only when every listing was read, as for films (#263).
    if fetch_ok:
        slots.settle(client)

    logger.info(
        f"Series complete: {stats['new']} new, {stats['existing']} existing, "
        f"{stats['skipped']} skipped, {stats['failed']} failed"
    )

    _save_failed_lookups(db, failed_lookups, "series")
    return stats, feed, category_stats, {"seen_ids": seen_ids_all, "fetch_ok": fetch_ok,
                                          "categories": len(whitelisted_cats), "unread": unread}

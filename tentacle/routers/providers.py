"""
Tentacle - Providers Router
Handles provider management, testing, category browsing, and preview
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import requests
import re
import logging

logger = logging.getLogger(__name__)

from models.database import (
    get_db, Provider, ProviderCategory, CategorySnapshot, Setting,
    Movie, Series, Duplicate, SyncRun, LiveChannel, LiveChannelGroup, EPGProgram,
    BlockedStream, MatchOverride,
)
from routers.auth import require_admin
from services.media_files import delete_movie_files, delete_series_files
from services.xtream_client import quote_cred

router = APIRouter(prefix="/api/providers", tags=["providers"], dependencies=[Depends(require_admin)])

HEADERS = {"User-Agent": "TiviMate/4.7.0 (Linux; Android 12)"}


def _api_base(provider) -> str:
    """The provider's player_api.php URL with its login, percent-encoded (#529)."""
    return (f"{provider.server_url.rstrip('/')}/player_api.php"
            f"?username={quote_cred(provider.username)}&password={quote_cred(provider.password)}")

FOREIGN_PREFIXES = {
    'AF', 'AL', 'AR', 'BE', 'BG', 'BN', 'BR', 'CN', 'CZ', 'DE', 'DK',
    'ES', 'EX', 'FI', 'FR', 'GR', 'HU', 'IL', 'IN', 'IR', 'IT', 'JP',
    'KR', 'KU', 'LA', 'MT', 'NL', 'NO', 'PH', 'PK', 'PL', 'PT', 'QC',
    'RO', 'RU', 'SE', 'SO', 'TH', 'TR', 'UA', 'VN'
}

# Auto-detect source tag from category name
SOURCE_TAG_MAP = {
    "NETFLIX": "Netflix",
    "AMAZON": "Amazon Prime",
    "APPLE+": "Apple TV+",
    "DISNEY+": "Disney+",
    "HBO": "HBO",
    "HULU": "Hulu",
    "PARAMOUNT+": "Paramount+",
    "PARAMOUNT PICTURES": "Paramount",
    "PEACOCK": "Peacock",
    "SHOWTIME": "Showtime",
    "DISCOVERY+": "Discovery+",
    "MARVEL": "Marvel",
    "JAMES BOND": "James Bond",
    "DREAMWORKS": "DreamWorks",
    "PIXAR": "Pixar",
    "UNIVERSAL": "Universal",
    "NICKELODEON": "Nickelodeon",
    "IMDB TOP": "IMDb Top 250",
}


class ProviderCreate(BaseModel):
    name: str
    provider_type: Optional[str] = "xtream"  # xtream | m3u_url | m3u_file
    server_url: Optional[str] = ""            # Xtream only
    username: Optional[str] = ""              # Xtream only
    password: Optional[str] = ""              # Xtream only
    m3u_url: Optional[str] = None             # M3U URL or local file path
    epg_url: Optional[str] = None
    user_agent: Optional[str] = None
    priority: Optional[int] = 1
    require_tmdb_match: Optional[bool] = True


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    server_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    m3u_url: Optional[str] = None
    epg_url: Optional[str] = None
    user_agent: Optional[str] = None
    priority: Optional[int] = None
    active: Optional[bool] = None
    # "Use for Live TV" on the provider card. Off takes its channels out of
    # the tuner (the lineup lists enabled channels only).
    live_tv_enabled: Optional[bool] = None
    require_tmdb_match: Optional[bool] = None
    # Live TV, per provider: streams at once (0 = the default again) and what
    # is added to its channel numbers
    live_max_streams: Optional[int] = None
    channel_number_offset: Optional[int] = None


class AddressesAdd(BaseModel):
    text: str


class CategoryUpdate(BaseModel):
    category_ids: List[str]
    whitelisted: bool


def has_foreign_prefix(name: str) -> bool:
    match = re.match(r'^([A-Z]{2})(?:/[A-Z]{2})?\s*-\s*', name.upper())
    if match:
        return match.group(1) in FOREIGN_PREFIXES
    return False


def detect_source_tag(category_name: str) -> Optional[str]:
    upper = category_name.upper()
    for keyword, tag in SOURCE_TAG_MAP.items():
        if keyword in upper:
            return tag
    return None


COUNTRY_PREFIXES = [
    'GERMANY', 'GERMAN', 'FRANCE', 'FRENCH', 'NORDIC', 'NORWAY', 'NORWEGIAN',
    'SWEDEN', 'SWEDISH', 'DENMARK', 'DANISH', 'FINLAND', 'FINNISH',
    'TURKEY', 'TURKISH', 'TURKSIH', 'PT/BR', 'PORTUGAL', 'PORTUGUESE', 'BRAZIL',
    'NETHERLANDS', 'DUTCH', 'BELGIUM', 'ITALY', 'ITALIAN', 'SPAIN', 'SPANISH',
    'GREECE', 'GREEK', 'INDIA', 'HINDI', 'PERSIAN', 'IRAN', 'PAKISTAN',
    'AFRICA', 'SOUTH AFRICA', 'MALTA', 'ROMANIA', 'RUSSIA', 'RUSSIAN', 'RUSSAIN', 'POLAND', 'POLISH',
    'CZECH', 'HUNGARY', 'HUNGARIAN', 'UKRAINE', 'UKRAINIAN', 'ARABIC',
    'ARAB', 'ISRAEL', 'HEBREW', 'JAPAN', 'JAPANESE', 'KOREA', 'KOREAN',
    'CHINA', 'CHINESE', 'THAILAND', 'THAI', 'VIETNAM', 'PHILIPPINES',
    'INDONESIA', 'MALAYSIA', 'PHILIPPINES', 'ASIA', 'LATIN', 'MEXICO',
    'MEXICAN', 'COLOMBIA', 'ARGENTINA', 'CHILE', 'VENEZUELA', 'PERU',
    'SCANDINAVIA', 'SCANDINAVIAN', 'BALKANS', 'VIAPLAY', 'VIDEOLAND',
    'CRUNCHYROLL', 'CHRISTIAN', 'AFRICA',
]

def is_likely_english(name: str) -> bool:
    if has_foreign_prefix(name):
        return False
    if not all(ord(c) < 128 for c in name):
        return False
    upper = name.upper()
    # Exclude anything starting with a country/region name
    for prefix in COUNTRY_PREFIXES:
        if upper.startswith(prefix):
            return False
    english_signals = [
        "EN -", "EN-", "NETFLIX", "AMAZON", "APPLE+", "DISNEY+", "HBO",
        "MOVIES", "SERIES", "COMEDY", "ACTION", "HORROR", "THRILLER",
        "DRAMA", "DOCUMENTARY", "MARVEL", "PARAMOUNT", "UNIVERSAL",
        "IMDB", "4K", "BLURAY", "NEW RELEASE", "DISCOVERY", "PEACOCK",
        "HULU", "SHOWTIME", "MAX ", "HBO MAX",
    ]
    return any(s in upper for s in english_signals)


def _category_list(data) -> list:
    """A category answer as a list. A panel that refuses the login (wrong or
    expired account) answers every action with {"user_info": {"auth": 0}};
    walked as a list it failed on its keys with a 500, after which the
    dashboard showed "Internal server error" or no categories at all."""
    if isinstance(data, list):
        return data
    if not data:
        return []
    user_info = data.get("user_info") if isinstance(data, dict) else None
    if isinstance(user_info, dict) and not user_info.get("auth", 1):
        raise ValueError("the provider refused the login: check the account and its expiry")
    raise ValueError("the provider did not answer with a category list")


def fetch_provider_categories(provider: Provider, read: dict = None, counts: bool = True, gap: float = 0.0):
    """Fetch VOD/series categories + counts. Xtream via the player API; M3U by
    parsing the playlist and using group-titles as categories (`read` is told
    how many entries the playlist had).

    Xtream: `counts=False` leaves the counts out (two of the biggest answers
    a panel gives: every film and every series); `gap` is how many seconds
    apart its requests go (the nightly discovery's, services/discovery)."""
    if (provider.provider_type or "xtream") in ("m3u_url", "m3u_file"):
        from services.sync import M3UClient
        c = M3UClient(provider)
        vod_cats, series_cats = c.get_vod_categories(), c.get_series_categories()
        if read is not None:
            read["entries"] = len(c.playlist.entries) if c.playlist else 0
        return vod_cats, series_cats, c.vod_counts(), c.series_counts()

    from services.xtream_client import note_api_request, pause
    base = _api_base(provider)
    session = requests.Session()
    session.headers.update(HEADERS)

    note_api_request()
    vod_cats = _category_list(session.get(f"{base}&action=get_vod_categories", timeout=15).json())
    pause(gap)
    note_api_request()
    series_cats = _category_list(session.get(f"{base}&action=get_series_categories", timeout=15).json())
    if not counts:
        return vod_cats, series_cats, {}, {}
    pause(gap)
    vod_counts, series_counts = fetch_category_counts(provider, session=session, gap=gap)
    return vod_cats, series_cats, vod_counts, series_counts


def fetch_category_counts(provider: Provider, session=None, gap: float = 0.0):
    """Titles per VOD and series category of an Xtream provider: the whole
    catalogue, twice (every film, then every series), `gap` seconds apart.
    Empty dicts for what could not be read."""
    from services.xtream_client import note_api_request, pause
    base = _api_base(provider)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
    vod_counts = {}
    series_counts = {}
    try:
        note_api_request()
        vod_streams = session.get(f"{base}&action=get_vod_streams", timeout=60).json()
        if isinstance(vod_streams, list):
            for s in vod_streams:
                cid = str(s.get("category_id", ""))
                vod_counts[cid] = vod_counts.get(cid, 0) + 1
    except Exception:
        pass
    pause(gap)
    try:
        note_api_request()
        all_series = session.get(f"{base}&action=get_series", timeout=60).json()
        if isinstance(all_series, list):
            for s in all_series:
                cid = str(s.get("category_id", ""))
                series_counts[cid] = series_counts.get(cid, 0) + 1
    except Exception:
        pass
    return vod_counts, series_counts


def test_provider_connection(provider: Provider, db: Session = None):
    """Test connection and get account info. For M3U, read the playlist: what
    it offers (channels, films, episodes), and with `db` what it says about
    the provider (services/m3u_source.py)."""
    if (provider.provider_type or "xtream") in ("m3u_url", "m3u_file"):
        from services.sync import M3UClient
        c = M3UClient(provider, db)
        c._load()  # raises ProviderConnectionError if unreachable/unparseable
        n_movies = sum(len(v) for v in c._movies.values())
        n_series = sum(len(s) for s in c._series.values())
        if n_movies == 0 and n_series == 0 and c.live_count == 0:
            raise Exception("The playlist was read, but it lists no channels, films or episodes")
        return {"user_info": {"auth": 1}, "_m3u": {"has_vod": n_movies > 0, "has_series": n_series > 0,
                                                   "has_live": c.live_count > 0}}

    url = _api_base(provider)
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("user_info", {}).get("auth") == 0:
        raise Exception("Authentication failed")
    return data


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
def list_providers(db: Session = Depends(get_db)):
    from models.database import SyncRun
    providers = db.query(Provider).order_by(Provider.priority).all()

    # Get last completed sync per provider
    last_syncs = {}
    for p in providers:
        last_run = db.query(SyncRun).filter(
            SyncRun.provider_id == p.id,
            SyncRun.status == "completed"
        ).order_by(SyncRun.completed_at.desc()).first()
        if last_run and last_run.completed_at:
            last_syncs[p.id] = last_run.completed_at.isoformat()

    return [
        {
            "id": p.id,
            "name": p.name,
            "provider_type": p.provider_type or "xtream",
            "server_url": p.server_url,
            "m3u_url": p.m3u_url,
            "epg_url": p.epg_url,
            "username": p.username,
            "active": p.active,
            "priority": p.priority,
            "status": p.status,
            "last_tested": p.last_tested,
            "last_synced": last_syncs.get(p.id),
            "expiry": p.expiry,
            "max_connections": p.max_connections,
            "created_at": p.created_at,
            "has_live": p.has_live,
            "has_vod": p.has_vod,
            "has_series": p.has_series,
            "live_tv_enabled": p.live_tv_enabled,
            "require_tmdb_match": p.require_tmdb_match if p.require_tmdb_match is not None else True,
            "address_count": len(p.addresses) or (1 if p.server_url else 0),
            "live_max_streams": p.live_max_streams,
            "channel_number_offset": p.channel_number_offset,
        }
        for p in providers
    ]


def _check_m3u_address(ptype: str, address: str) -> None:
    """A playlist link must be a web address, and a playlist file one
    Tentacle can read inside its container: said when it is saved, not after."""
    address = (address or "").strip()
    if ptype == "m3u_file":
        from services.m3u_parser import m3u_file_problem
        problem = m3u_file_problem(address)
        if problem:
            raise HTTPException(400, problem)
    elif not address.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "A playlist link starts with http:// or https://. "
                                 "For a file on the server choose \"Playlist file on the server\".")


@router.post("")
def create_provider(body: ProviderCreate, db: Session = Depends(get_db)):
    ptype = body.provider_type or "xtream"
    if ptype in ("m3u_url", "m3u_file"):
        if not body.m3u_url:
            raise HTTPException(400, "An M3U URL (or file path) is required for M3U providers")
        _check_m3u_address(ptype, body.m3u_url)
    elif not (body.server_url and body.username and body.password):
        raise HTTPException(400, "Server URL, username and password are required for Xtream providers")

    provider = Provider(
        name=body.name,
        provider_type=ptype,
        server_url=body.server_url or "",
        username=body.username or "",
        password=body.password or "",
        m3u_url=body.m3u_url,
        epg_url=body.epg_url,
        priority=body.priority,
        require_tmdb_match=body.require_tmdb_match,
        active=True,
        status="untested",
    )
    if body.user_agent:
        provider.user_agent = body.user_agent
    from services import livetv_tuners
    count_before = livetv_tuners.tuner_count(db)
    db.add(provider)
    from services import provider_addresses
    provider_addresses.ensure_current(db, provider)
    db.commit()
    db.refresh(provider)
    _follow_stream_limit(db, count_before)
    return {"id": provider.id, "success": True}


def _stop_live_tv(db: Session, provider_id: int) -> None:
    """Live TV switched off for a provider: its channels leave the tuner.

    The lineup, the M3U and the guide list enabled channels whatever their
    provider's switch says, so the switch alone would leave every channel in
    Jellyfin. Its groups and channels are switched off instead; turning Live
    TV on again starts from an empty choice of groups."""
    from models.database import LiveChannel, LiveChannelGroup
    db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).update(
        {LiveChannel.enabled: False}, synchronize_session=False)
    db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id).update(
        {LiveChannelGroup.enabled: False}, synchronize_session=False)


def _follow_stream_limit(db: Session, before: int) -> None:
    """Jellyfin's tuner count follows Tentacle's stream limit within seconds
    of a change (services/livetv_tuners.py)."""
    from services import livetv_tuners
    if livetv_tuners.tuner_count(db) != before:
        livetv_tuners.stream_limit_changed("streams at once changed")


@router.put("/{provider_id}")
def update_provider(provider_id: int, body: ProviderUpdate, db: Session = Depends(get_db)):
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")
    from routers.livetv import _xtream_login, rewrite_xtream_channel_urls
    from services import livetv_tuners
    before = _xtream_login(p)
    count_before = livetv_tuners.tuner_count(db)
    m3u_before = p.m3u_url
    if body.name is not None:
        p.name = body.name
    if body.provider_type is not None:
        p.provider_type = body.provider_type
    if body.server_url is not None:
        p.server_url = body.server_url
        p.status = "untested"
    if body.username is not None:
        p.username = body.username
    if body.password is not None:
        p.password = body.password
    if body.m3u_url is not None:
        if (p.provider_type or "xtream") in ("m3u_url", "m3u_file") and body.m3u_url != p.m3u_url:
            _check_m3u_address(p.provider_type, body.m3u_url)
        p.m3u_url = body.m3u_url
        p.status = "untested"
    if body.epg_url is not None:
        p.epg_url = body.epg_url
    if body.user_agent is not None:
        p.user_agent = body.user_agent
    if body.priority is not None:
        p.priority = body.priority
    if body.active is not None:
        p.active = body.active
    if body.live_tv_enabled is not None:
        if p.live_tv_enabled and not body.live_tv_enabled:
            _stop_live_tv(db, p.id)
        p.live_tv_enabled = body.live_tv_enabled
    if body.require_tmdb_match is not None:
        p.require_tmdb_match = body.require_tmdb_match
    # A new server or login reaches the Live TV channels now, not at the
    # next channel sync (#469)
    rewrite_xtream_channel_urls(p, before, db)
    # ...and the films and episodes already in the library (#142), recorded in
    # this commit so a restart before the rewrite ran loses nothing (#149)
    from services import provider_relink, provider_addresses
    relink = _xtream_login(p) != before
    if provider_relink.address_changed(p, before, m3u_before):
        provider_relink.record_pending(db, p, before)
    if provider_addresses.normalize(p.server_url) != provider_addresses.normalize(before[0]):
        # A new address typed in is a switch like "Use this one" (#704)
        provider_addresses.ensure_current(db, p)
        if before[0] and (p.provider_type or "xtream") == "xtream":
            provider_addresses._start_probation(db, p)
    if body.live_max_streams is not None:
        p.live_max_streams = body.live_max_streams if body.live_max_streams > 0 else None
    if body.channel_number_offset is not None:
        p.channel_number_offset = max(0, body.channel_number_offset)
    db.commit()
    _follow_stream_limit(db, count_before)
    if relink:
        provider_relink.start_relink(p.id, before)
    return {"success": True}


@router.delete("/{provider_id}")
def delete_provider(provider_id: int, db: Session = Depends(get_db)):
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")
    from services import livetv_tuners
    count_before = livetv_tuners.tuner_count(db)

    # A sync writes each new title's files before its rows, and commits the rows
    # once per category: a delete under it misses them, the sync's commit then
    # fails on the deleted category, and the files stay with no row for good
    # (#450). Take the provider's sync slot like "Sync now" and the nightly do,
    # and hold it until the rows are gone so neither starts a sync meanwhile.
    from routers.sync import _running_syncs, _sync_lock
    with _sync_lock:
        if provider_id in _running_syncs or db.query(SyncRun).filter(
                SyncRun.provider_id == provider_id, SyncRun.status == "running").first():
            raise HTTPException(409, "A sync of this provider is running — cancel it (or let it "
                                     "finish), then delete the provider")
        _running_syncs[provider_id] = True
    try:
        # ── Delete VOD files from disk ──────────────────────────────────────────
        deleted_files = 0

        # Movies: strm_path points to .strm file, delete file + .nfo + parent folder
        movies = db.query(Movie).filter(Movie.provider_id == provider_id).all()
        for m in movies:
            deleted_files += delete_movie_files(m.strm_path)

        # Series: strm_path points to the show directory. Only the .strm/.nfo files
        # Tentacle wrote are removed — merged setups share this folder with Sonarr's
        # downloads, and a recursive delete would destroy those too.
        series = db.query(Series).filter(Series.provider_id == provider_id).all()
        for s in series:
            deleted_files += delete_series_files(s.strm_path)

        logger.info(f"Deleted {deleted_files} VOD files from disk for provider {p.name}")

        # ── Cascade-delete all DB records ────────────────────────────────────────
        # Remove duplicates that reference movies from this provider
        provider_tmdb_ids = [m.tmdb_id for m in movies]
        if provider_tmdb_ids:
            db.query(Duplicate).filter(Duplicate.tmdb_id.in_(provider_tmdb_ids)).delete(synchronize_session=False)

        deleted_movies = len(movies)
        deleted_series = len(series)
        db.query(Movie).filter(Movie.provider_id == provider_id).delete()
        db.query(Series).filter(Series.provider_id == provider_id).delete()
        # Delete category snapshots via category IDs, then categories
        cat_ids = [c.id for c in db.query(ProviderCategory.id).filter(ProviderCategory.provider_id == provider_id).all()]
        if cat_ids:
            db.query(CategorySnapshot).filter(CategorySnapshot.category_id.in_(cat_ids)).delete(synchronize_session=False)
        db.query(ProviderCategory).filter(ProviderCategory.provider_id == provider_id).delete()
        db.query(SyncRun).filter(SyncRun.provider_id == provider_id).delete()
        # Delete EPG programs for channels belonging to this provider, then channels/groups
        channel_epg_ids = list({
            gid for ch in db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id)
            for gid in (ch.guide_epg_id, ch.epg_channel_id) if gid
        })
        if channel_epg_ids:
            db.query(EPGProgram).filter(EPGProgram.channel_id.in_(channel_epg_ids)).delete(synchronize_session=False)
        db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).delete()
        db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id).delete()
        # State keyed by the provider's id: SQLite gives the next provider the
        # same id, which would inherit it (#625). All in this one commit.
        from routers.livetv import PROVIDER_SETTING_KEYS
        from services import provider_health, provider_relink
        db.query(Setting).filter(Setting.key.in_([k.format(provider_id) for k in PROVIDER_SETTING_KEYS])) \
            .delete(synchronize_session=False)
        db.query(BlockedStream).filter(BlockedStream.provider_id == provider_id).delete()
        db.query(MatchOverride).filter(MatchOverride.provider_id == provider_id).delete()
        provider_relink.forget(db, provider_id)
        provider_health.forget(db, provider_id)

        provider_name = p.name
        db.delete(p)
        db.commit()
    finally:
        _running_syncs.pop(provider_id, None)
    _follow_stream_limit(db, count_before)

    from models.database import log_deletion
    log_deletion(db, kind="provider-cascade", name=provider_name, reason="manual",
                 detail=f"Provider deleted — {deleted_movies} movie(s), {deleted_series} series, "
                        f"{deleted_files} file(s) removed from disk, plus categories/sync history/Live TV data/"
                        "blocked streams/match fixes")

    # ── Rebuild playlists and home config ────────────────────────────────────
    # Source-tag playlists (e.g. "Netflix Movies") may now be orphaned if all
    # content came from this provider. sync_smartlists removes orphaned playlists
    # from Jellyfin and disk, refresh repopulates remaining ones, and
    # write_home_config drops stale rows/hero references.
    try:
        from services.smartlists import sync_smartlists, refresh_smartlist_playlists, write_home_config
        from models.database import TentacleUser as _TU
        logger.info(f"Rebuilding playlists after deleting provider {provider_name}...")
        sync_smartlists(db)  # loops all users
        refresh_smartlist_playlists(db)  # loops all users
        for _u in db.query(_TU).all():
            write_home_config(db, user_id=_u.id)
        logger.info(f"Playlist rebuild complete after provider deletion")
    except Exception as e:
        logger.warning(f"Playlist rebuild after provider delete failed (non-fatal): {e}")

    return {
        "success": True,
        "deleted_movies": deleted_movies,
        "deleted_series": deleted_series,
        "deleted_files": deleted_files,
    }


def _keep_account_info(db: Session, p: Provider, data: dict) -> None:
    """Expiry, connection limit and output formats from a player_api answer."""
    info = data.get("user_info") or {}
    exp_ts = info.get("exp_date")
    if exp_ts:
        try:
            p.expiry = datetime.fromtimestamp(int(exp_ts))
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    try:
        p.max_connections = int(info.get("max_connections", 1))
    except (TypeError, ValueError):
        p.max_connections = 1
    if (p.provider_type or "xtream") != "xtream":
        from routers.livetv import _remember_output_formats
        _remember_output_formats(db, p.id, data)


@router.post("/{provider_id}/test")
def test_provider(provider_id: int, db: Session = Depends(get_db)):
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "Testing the provider")
    try:
        first_test = p.last_tested is None
        data = test_provider_connection(p, db)
        info = data.get("user_info", {})
        p.status = "ok"
        p.last_tested = datetime.utcnow()

        # M3U provider: what it offers comes from the playlist; the account's
        # details from its panel when its links are a panel's and it answers.
        if "_m3u" in data:
            from services import m3u_source
            had_live = bool(p.has_live)
            p.has_vod = data["_m3u"].get("has_vod", False)
            p.has_series = data["_m3u"].get("has_series", False)
            p.has_live = data["_m3u"].get("has_live", False)
            if p.has_live and not had_live:
                p.live_tv_enabled = True      # as for Xtream, below
            if first_test and not (p.has_vod or p.has_series):
                p.active = False              # a channels-only playlist: no Movies & Series switch
            account = m3u_source.account_info(p)
            ainfo = account.get("user_info") or {}
            if ainfo:
                _keep_account_info(db, p, account)
            db.commit()
            return {
                "success": True, "status": ainfo.get("status") or "Active",
                "expiry": p.expiry, "max_connections": p.max_connections if ainfo else None,
                "active_connections": ainfo.get("active_cons"),
                "has_live": p.has_live, "has_vod": p.has_vod, "has_series": p.has_series,
                "panel": bool(p.server_url),
            }

        _keep_account_info(db, p, data)

        # Probe capabilities
        had_live = bool(p.has_live)
        base = _api_base(p)
        session = requests.Session()
        session.headers.update(HEADERS)
        for attr, action in [("has_vod", "get_vod_categories"), ("has_series", "get_series_categories"), ("has_live", "get_live_categories")]:
            try:
                cats = session.get(f"{base}&action={action}", timeout=10).json()
                setattr(p, attr, bool(cats and len(cats) > 0))
            except Exception:
                setattr(p, attr, False)

        # Live TV comes on by itself the first time live channels are found.
        # A later test leaves the provider card's "Use for Live TV" switch as
        # the admin set it (a re-test used to switch it back on).
        if p.has_live and not had_live:
            p.live_tv_enabled = True

        db.commit()
        return {
            "success": True,
            "status": info.get("status"),
            "expiry": p.expiry,
            "max_connections": p.max_connections,
            "active_connections": info.get("active_cons"),
            "has_live": p.has_live,
            "has_vod": p.has_vod,
            "has_series": p.has_series,
        }
    except Exception as e:
        p.status = "error"
        p.last_tested = datetime.utcnow()
        db.commit()
        # requests' error text carries the player_api.php?username=&password=
        # URL; the Live TV provider test already redacts the same text.
        from services.log_redaction import redact
        raise HTTPException(400, redact(str(e)))


@router.post("/{provider_id}/fetch-categories")
def fetch_categories(provider_id: int, db: Session = Depends(get_db)):
    """Fetch categories from provider and store in DB"""
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "Fetching the provider's categories")

    try:
        read = {}
        if (p.provider_type or "xtream") in ("m3u_url", "m3u_file"):
            vod_cats, series_cats, vod_counts, series_counts = fetch_provider_categories(p, read)
        else:
            vod_cats, series_cats, vod_counts, series_counts = fetch_provider_categories(p)
        # An M3U playlist that listed anything at all (a live-only one lists no films)
        m3u_read_entries = bool(read.get("entries"))
    except Exception as e:
        from services.log_redaction import redact  # the error text carries the login URL
        raise HTTPException(400, f"Failed to fetch categories: {redact(str(e))}")

    # Get existing categories for this provider
    existing = {
        (c.category_id, c.type): c
        for c in db.query(ProviderCategory).filter(
            ProviderCategory.provider_id == provider_id
        ).all()
    }

    new_count = 0
    created = []
    for cat_type, cats, counts in [("movie", vod_cats, vod_counts), ("series", series_cats, series_counts)]:
        # Not a single title of this type in the provider's answer is a blip, not
        # every category emptying at once. A category's count is also the sync's
        # memory that it held titles; writing 0 into every one disarmed the
        # guard against empty answers everywhere, and the next two empty answers
        # pruned whole categories (#25). Existing counts stay; new ones are 0.
        blip = not any(counts.values())
        for cat in cats:
            cid = str(cat["category_id"])
            cname = cat["category_name"]
            key = (cid, cat_type)
            count = counts.get(cid, 0)

            if key in existing:
                existing[key].category_name = cname
                if not (blip and existing[key].title_count):
                    existing[key].title_count = count
                existing[key].last_seen = datetime.utcnow()
            else:
                new_cat = ProviderCategory(
                    provider_id=provider_id,
                    category_id=cid,
                    category_name=cname,
                    type=cat_type,
                    whitelisted=False,
                    source_tag=detect_source_tag(cname),
                    title_count=count,
                    last_seen=datetime.utcnow(),
                )
                db.add(new_cat)
                created.append(new_cat)
                new_count += 1

    removed = 0
    if (p.provider_type or "xtream") in ("m3u_url", "m3u_file") and m3u_read_entries:
        # A playlist's categories are its group titles. Groups it no longer
        # lists as films or series (its live channels' groups, which an older
        # build offered as film categories) go, unless chosen.
        listed = {(str(c["category_id"]), "movie") for c in vod_cats} | \
                 {(str(c["category_id"]), "series") for c in series_cats}
        from models.database import CategorySnapshot
        for key, row in existing.items():
            if key not in listed and not row.whitelisted:
                db.query(CategorySnapshot).filter(CategorySnapshot.category_id == row.id).delete(
                    synchronize_session=False)
                db.delete(row)
                removed += 1

    if created:
        from services import provider_new
        provider_new.note_created(db, created, first_setup=not existing)
    db.commit()
    return {"success": True, "new_categories": new_count, "removed_categories": removed}


@router.get("/{provider_id}/categories")
def get_categories(
    provider_id: int,
    type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get stored categories for a provider"""
    query = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider_id
    )
    if type:
        query = query.filter(ProviderCategory.type == type)

    cats = query.order_by(ProviderCategory.category_name).all()

    return [
        {
            "id": c.id,
            "category_id": c.category_id,
            "name": c.category_name,
            "type": c.type,
            "whitelisted": c.whitelisted,
            "source_tag": c.source_tag,
            "title_count": c.title_count,
            "last_sync_matched": c.last_sync_matched,
            "last_sync_skipped": c.last_sync_skipped,
            "is_foreign": has_foreign_prefix(c.category_name),
            "is_likely_english": is_likely_english(c.category_name),
        }
        for c in cats
    ]


@router.post("/{provider_id}/categories/update")
def update_categories(
    provider_id: int,
    body: CategoryUpdate,
    db: Session = Depends(get_db)
):
    """Bulk update category whitelist status"""
    cats = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider_id,
        ProviderCategory.id.in_(body.category_ids)
    ).all()

    for cat in cats:
        cat.whitelisted = body.whitelisted

    db.commit()
    return {"success": True, "updated": len(cats)}


@router.post("/{provider_id}/categories/{category_id}/tag")
def update_category_tag(
    provider_id: int,
    category_id: int,
    tag: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Update source tag for a category"""
    cat = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider_id,
        ProviderCategory.id == category_id
    ).first()
    if not cat:
        raise HTTPException(404, "Category not found")
    cat.source_tag = tag
    db.commit()
    return {"success": True}


@router.get("/{provider_id}/preview")
def preview_sync(provider_id: int, db: Session = Depends(get_db)):
    """
    Preview what a sync would bring in.
    Returns stats without actually syncing.
    """
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")

    whitelisted = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider_id,
        ProviderCategory.whitelisted == True
    ).all()

    if not whitelisted:
        return {
            "whitelisted_categories": 0,
            "estimated_movies": 0,
            "estimated_series": 0,
            "message": "No categories whitelisted yet"
        }

    # Get stream counts per category
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "A sync preview")
    base = _api_base(p)
    session = requests.Session()
    session.headers.update(HEADERS)

    movie_cats = [c for c in whitelisted if c.type == "movie"]
    series_cats = [c for c in whitelisted if c.type == "series"]

    # Sample first few categories to estimate
    sample_size = min(5, len(movie_cats))
    movie_count = 0
    sample_categories = []

    for cat in movie_cats[:sample_size]:
        try:
            r = session.get(
                f"{base}&action=get_vod_streams&category_id={cat.category_id}",
                timeout=15
            )
            count = len(r.json())
            movie_count += count
            sample_categories.append({
                "name": cat.category_name,
                "count": count,
                "tag": cat.source_tag
            })
        except:
            pass

    # Extrapolate if we only sampled
    if len(movie_cats) > sample_size:
        avg = movie_count / sample_size if sample_size else 0
        estimated_movies = int(avg * len(movie_cats))
    else:
        estimated_movies = movie_count

    return {
        "whitelisted_categories": len(whitelisted),
        "movie_categories": len(movie_cats),
        "series_categories": len(series_cats),
        "estimated_movies": estimated_movies,
        "sample_categories": sample_categories,
        "note": "Estimates based on category sampling. Actual count after TMDB matching may differ."
    }


# ─── Several addresses (services/provider_addresses.py) ──────────────────────

def _provider_or_404(db: Session, provider_id: int) -> Provider:
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")
    return p


def _addresses_view(db: Session, p: Provider) -> dict:
    from services import provider_addresses as pa
    rows = pa.ranked(db, p)
    db.commit()
    return {
        "xtream": (p.provider_type or "xtream") == "xtream",
        "addresses": rows,
        "test": pa.progress(p.id),
    }


@router.get("/{provider_id}/addresses")
def list_addresses(provider_id: int, db: Session = Depends(get_db)):
    return _addresses_view(db, _provider_or_404(db, provider_id))


@router.post("/{provider_id}/addresses")
def add_addresses(provider_id: int, body: AddressesAdd, db: Session = Depends(get_db)):
    """Paste the provider's message: every address in it is added as a backup."""
    from services import provider_addresses as pa
    p = _provider_or_404(db, provider_id)
    if (p.provider_type or "xtream") != "xtream":
        raise HTTPException(400, "An M3U playlist has one address: edit the provider to change it")
    result = pa.add_from_text(db, p, body.text or "")
    if not result["added"] and not result["known"]:
        raise HTTPException(400, "No address found in that text")
    return {**result, **_addresses_view(db, p)}


@router.delete("/{provider_id}/addresses/{address_id}")
def remove_address(provider_id: int, address_id: int, db: Session = Depends(get_db)):
    from services import provider_addresses as pa
    p = _provider_or_404(db, provider_id)
    try:
        pa.remove(db, p, address_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _addresses_view(db, p)


@router.post("/{provider_id}/addresses/test")
def test_addresses(provider_id: int, db: Session = Depends(get_db)):
    """Test every address, one at a time, in the background; GET the list for
    how far it is. 409 while a test or the hourly check runs."""
    from services import provider_addresses as pa
    from services import provider_health
    p = _provider_or_404(db, provider_id)
    if (p.provider_type or "xtream") != "xtream":
        raise HTTPException(400, "Only an Xtream provider has addresses to test")
    from services.provider_activity import refuse_while_recording
    refuse_while_recording(db, "Testing the provider's addresses")
    if provider_health.check_running():
        raise HTTPException(409, "The hourly provider check is running (one connection at a time): "
                                 "try again in a few minutes")
    if not pa.start_test_all(p.id):
        raise HTTPException(409, "A test is already running: wait for it to finish")
    return _addresses_view(db, p)


@router.post("/{provider_id}/addresses/{address_id}/use")
def use_address(provider_id: int, address_id: int, db: Session = Depends(get_db)):
    """"Use this one" on the card, or Switch on the Health banner: the
    provider moves to this address. Nothing is imported again or deleted
    (provider_addresses.switch_to). Tentacle never switches by itself."""
    from services import provider_addresses as pa
    from services import provider_health
    p = _provider_or_404(db, provider_id)
    a = next((a for a in p.addresses if a.id == address_id), None)
    if a is None:
        raise HTTPException(404, "No such address")
    result = pa.switch_to(db, p, a.url, "chosen by hand")
    if result.get("switched"):
        provider_health.address_switched(db, p.id)
    return {**result, **_addresses_view(db, p)}


@router.post("/{provider_id}/addresses/{address_id}/keep")
def keep_address(provider_id: int, address_id: int, db: Session = Depends(get_db)):
    """Keep on the Health banner: the address in use stays, and this one is
    not offered again until the address in use fails another time. 409 when
    the provider isn't failing (nothing to keep)."""
    from services import provider_health
    p = _provider_or_404(db, provider_id)
    a = next((a for a in p.addresses if a.id == address_id), None)
    if a is None:
        raise HTTPException(404, "No such address")
    if not provider_health.keep_address(db, p.id, a.url):
        raise HTTPException(409, "The address in use isn't failing: there is nothing to keep")
    return _addresses_view(db, p)

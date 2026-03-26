"""
Tentacle - Sync Engine
Core sync logic for VOD content.
Replaces xtream_to_jellyfin.py as a proper service.
"""

import shutil
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Tuple
import requests

from sqlalchemy.orm import Session

from models.database import (
    Provider, ProviderCategory, Movie, Series,
    SyncRun, CategorySnapshot, Duplicate, get_setting
)
from services.tmdb import TMDBService
from services.nfo import write_movie_nfo, write_series_nfo, make_folder_name
from services.cleaner import clean_title
from services.tagger import compute_tags, get_list_tags_for_tmdb_id, apply_tag_rules
from services.exceptions import ProviderConnectionError, SyncCancelledError, SyncError

logger = logging.getLogger(__name__)

MIN_DISK_SPACE_MB = 500
WARN_DISK_SPACE_MB = 2000
DISK_CHECK_INTERVAL = 50  # Check every N items


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
        self.session.timeout = 30

    def _get(self, action: str, extra: str = "") -> list:
        try:
            r = self.session.get(f"{self.base}&action={action}{extra}")
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
        r = self.session.get(f"{self.base}&action=get_series_info&series_id={series_id}")
        r.raise_for_status()
        return r.json()

    def movie_stream_url(self, stream_id, container="mp4") -> str:
        return f"{self.server}/movie/{self.username}/{self.password}/{stream_id}.{container}"

    def episode_stream_url(self, episode_id, container="mp4") -> str:
        return f"{self.server}/series/{self.username}/{self.password}/{episode_id}.{container}"


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

    if not existing:
        return False

    # Record as duplicate
    dup = db.query(Duplicate).filter(
        Duplicate.tmdb_id == tmdb_id,
        Duplicate.media_type == media_type
    ).first()

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
                {"source": existing.source, "path": existing.strm_path or existing.radarr_path or ""},
                new_source
            ],
            resolution="pending"
        ))

    # Radarr (downloaded) content always takes priority
    if existing.source == "radarr":
        return True

    # If existing is from another provider, check priority
    if existing.provider_id and existing.provider_id != provider.id:
        existing_provider = db.query(Provider).filter(Provider.id == existing.provider_id).first()
        if existing_provider and existing_provider.priority <= provider.priority:
            return True  # Existing provider has equal or higher priority, skip

    return False


# ── Main Sync Functions ────────────────────────────────────────────────────

def sync_provider(
    provider: Provider,
    sync_type: str,  # "full" | "movies" | "series"
    db: Session,
    progress_callback=None,
    cancel_check=None,
) -> SyncRun:
    """
    Main entry point for syncing a provider.
    Creates and returns a SyncRun record.
    cancel_check: callable that returns True if sync should be cancelled.
    """
    # Load settings
    bearer_token = get_setting(db, "tmdb_bearer_token")
    data_dir = get_setting(db, "data_dir", "/data")
    vod_movies_path = Path(get_setting(db, "vod_movies_path", "/mnt/media/vod/Movies"))
    vod_series_path = Path(get_setting(db, "vod_series_path", "/mnt/media/vod/Series"))
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
        # Pre-sync disk space check
        if sync_type in ("full", "movies"):
            _check_disk_before_sync(vod_movies_path)
        if sync_type in ("full", "series"):
            _check_disk_before_sync(vod_series_path)

        tmdb = TMDBService(bearer_token, data_dir, match_threshold)
        client = XtreamClient(provider)

        category_stats = {}
        new_movies_feed = []
        new_series_feed = []

        if sync_type in ("full", "movies"):
            m_stats, m_feed, m_cat_stats = _sync_movies(
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
            s_stats, s_feed, s_cat_stats = _sync_series(
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

    output_dir.mkdir(parents=True, exist_ok=True)

    for cat in whitelisted_cats:
        cat_new = 0
        cat_existing = 0
        cat_failed = 0
        cat_skipped = 0

        logger.info(f"Processing category: {cat.category_name}")

        # Notify UI immediately so user sees which category is loading
        if progress_callback:
            progress_callback("movies", cat.category_name, stats,
                              item_title="Loading streams...", item_pos=0, item_total=0)

        try:
            streams = client.get_vod_streams(cat.category_id)
        except Exception as e:
            logger.error(f"Failed to fetch streams for {cat.category_name}: {e}")
            continue

        total_in_cat = len(streams)

        # Phase 1: Clean titles and split into known vs needs-TMDB
        cleaned = []
        for stream in streams:
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
                    return idx, tmdb.search_movie(name, yr)
                except Exception:
                    return idx, None

            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(_tmdb_lookup, item): item for item in needs_tmdb}
                for future in as_completed(futures):
                    idx, metadata = future.result()
                    if metadata:
                        tmdb_results[idx] = metadata

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
            if not metadata and known_id:
                # Known title but not in batch — it's existing, skip
                if known_id in existing_provider_tmdb_ids or known_id in seen_tmdb_ids:
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
                    "tmdb_id": -(provider.id * 10_000_000 + stream_id),
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

            # Skip if already seen this run (cross-category dedup)
            if tmdb_id in seen_tmdb_ids:
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            seen_tmdb_ids.add(tmdb_id)

            # Skip if already in library from this provider
            if tmdb_id in existing_provider_tmdb_ids:
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            # Compute file path early so duplicate record has it
            title = metadata["title"]
            year_str = metadata.get("year")
            folder_name = make_folder_name(title, year_str)
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

                # Write strm
                stream_url = client.movie_stream_url(
                    stream.get("stream_id"),
                    stream.get("container_extension", "mp4")
                )
                strm_file.write_text(stream_url, encoding='utf-8')

                # Write full NFO with all metadata
                write_movie_nfo(nfo_file, metadata, tags)

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

    return stats, feed, category_stats


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

        logger.info(f"Processing category: {cat.category_name}")

        # Notify UI immediately so user sees which category is loading
        if progress_callback:
            progress_callback("series", cat.category_name, stats,
                              item_title="Loading streams...", item_pos=0, item_total=0)

        try:
            series_list = client.get_series_list(cat.category_id)
        except Exception as e:
            logger.error(f"Failed to fetch series for {cat.category_name}: {e}")
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
                    return idx, tmdb.search_series(name, yr)
                except Exception:
                    return idx, None

            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(_tmdb_lookup, item): item for item in needs_tmdb}
                for future in as_completed(futures):
                    idx, metadata = future.result()
                    if metadata:
                        tmdb_results[idx] = metadata

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
                    "tmdb_id": -(provider.id * 10_000_000 + series_id),
                    "title": clean_name,
                    "year": year,
                    "overview": None,
                    "genres": [],
                    "poster_path": series.get("cover"),
                    "backdrop_path": None,
                    "status": None,
                }

            tmdb_id = metadata["tmdb_id"]

            if tmdb_id in seen_tmdb_ids:
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            seen_tmdb_ids.add(tmdb_id)

            if tmdb_id in existing_provider_tmdb_ids:
                cat_existing += 1
                stats["existing"] += 1
                if item_idx % 10 == 0 or item_idx == total_in_cat:
                    logger.info(f"  {cat.category_name} ({item_idx}/{total_in_cat}) — {cat_new} new, {cat_existing} existing")
                continue

            # Compute file path early so duplicate record has it
            title = metadata["title"]
            year_str = metadata.get("year")
            folder_name = make_folder_name(title, year_str)
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

                # Write show NFO
                nfo_file = show_dir / "tvshow.nfo"
                write_series_nfo(nfo_file, metadata, tags)

                # Write episode strm files
                ep_count = 0
                for season_num, eps in episodes.items():
                    if isinstance(eps, list) and eps and isinstance(eps[0], list):
                        eps = eps[0]
                    if not isinstance(eps, list):
                        continue

                    season_dir = show_dir / f"Season {int(season_num):02d}"
                    season_dir.mkdir(parents=True, exist_ok=True)

                    for ep in eps:
                        if not isinstance(ep, dict):
                            continue
                        ep_id = ep.get("id")
                        ep_num = ep.get("episode_num", 0)
                        container = ep.get("container_extension", "mp4")
                        ep_filename = f"{folder_name} S{int(season_num):02d}E{int(ep_num):02d}"
                        strm_file = season_dir / f"{ep_filename}.strm"
                        if not strm_file.exists():
                            strm_file.write_text(
                                client.episode_stream_url(ep_id, container),
                                encoding='utf-8'
                            )
                            ep_count += 1

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

    return stats, feed, category_stats

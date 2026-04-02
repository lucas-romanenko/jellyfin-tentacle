"""
Tentacle - Sonarr Scanner Service
Scans Sonarr library, records downloaded series in DB,
and writes NFO files with tags for Jellyfin.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import requests
from sqlalchemy.orm import Session

from models.database import Series, Duplicate, DownloadRequest, TentacleUser, get_setting

DOWNLOADED_TV_TAG = "Downloaded TV"
RECENTLY_ADDED_TV_TAG = "Recently Added TV"
from services.tmdb import TMDBService
from services.nfo import write_series_nfo
from services.tagger import apply_tag_rules, get_list_tags_for_tmdb_id, detect_source_tag_from_studios
from services.exceptions import SonarrConnectionError
from services.logstream import emit_library_event

logger = logging.getLogger(__name__)

DOWNLOADED_TV_TAG = "Downloaded TV"


class SonarrService:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key})

    def test(self) -> Optional[dict]:
        try:
            r = self.session.get(f"{self.url}/api/v3/system/status", timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.ConnectionError as e:
            raise SonarrConnectionError(f"Cannot reach Sonarr at {self.url}: {e}")
        except Exception as e:
            logger.error(f"Sonarr connection failed: {e}")
            return None

    def get_all_series(self) -> list:
        try:
            r = self.session.get(f"{self.url}/api/v3/series", timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch Sonarr series: {e}")
            return []

    def get_series_by_tvdb(self, tvdb_id: int) -> Optional[dict]:
        series = self.get_all_series()
        return next((s for s in series if s.get("tvdbId") == tvdb_id), None)

    def get_quality_profiles(self) -> list:
        try:
            r = self.session.get(f"{self.url}/api/v3/qualityprofile", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch quality profiles: {e}")
            return []

    def lookup_by_tmdb(self, tmdb_id: int) -> Optional[dict]:
        try:
            r = self.session.get(
                f"{self.url}/api/v3/series/lookup",
                params={"term": f"tmdb:{tmdb_id}"},
                timeout=15,
            )
            r.raise_for_status()
            results = r.json()
            return results[0] if results else None
        except Exception as e:
            logger.error(f"Sonarr lookup failed for tmdb:{tmdb_id}: {e}")
            return None

    def add_series(self, tmdb_id: int, quality_profile_id: int, root_folder: str,
                   monitor: str = "all", season_folder: bool = True) -> Optional[dict]:
        lookup = self.lookup_by_tmdb(tmdb_id)
        if not lookup:
            logger.error(f"Sonarr: no lookup result for tmdb:{tmdb_id}")
            return None
        payload = lookup
        payload["qualityProfileId"] = quality_profile_id
        payload["rootFolderPath"] = root_folder
        payload["seasonFolder"] = season_folder
        payload["monitored"] = True
        payload["addOptions"] = {
            "monitor": monitor,
            "searchForMissingEpisodes": True,
        }
        try:
            r = self.session.post(
                f"{self.url}/api/v3/series",
                json=payload,
                timeout=15,
            )
            if r.status_code < 400:
                series_data = r.json()
                # For partial monitor options, unmonitor the series after the initial
                # search so Sonarr doesn't keep grabbing future episodes.
                # "all" and "future" want ongoing monitoring; everything else is a
                # one-time grab (pilot, first/last season, none).
                if monitor not in ("all", "future"):
                    self._unmonitor_series(series_data["id"])
                return series_data
            logger.error(f"Sonarr rejected tmdb:{tmdb_id} — HTTP {r.status_code}: {r.text}")
            return None
        except Exception as e:
            logger.error(f"Failed to add tmdb:{tmdb_id} to Sonarr: {e}")
            return None

    def _unmonitor_series(self, series_id: int):
        """Set series monitored=false so Sonarr stops watching for new episodes."""
        try:
            r = self.session.get(f"{self.url}/api/v3/series/{series_id}", timeout=10)
            if r.status_code >= 400:
                logger.warning(f"Sonarr: failed to fetch series {series_id} for unmonitor")
                return
            series = r.json()
            series["monitored"] = False
            r = self.session.put(
                f"{self.url}/api/v3/series/{series_id}",
                json=series,
                timeout=10,
            )
            if r.status_code < 400:
                logger.info(f"Sonarr: unmonitored series {series_id} ({series.get('title', '?')})")
            else:
                logger.warning(f"Sonarr: failed to unmonitor series {series_id} — HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"Sonarr: failed to unmonitor series {series_id}: {e}")

    def get_root_folders(self) -> list:
        try:
            r = self.session.get(f"{self.url}/api/v3/rootfolder", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch Sonarr root folders: {e}")
            return []


def scan_sonarr_library(db: Session) -> dict:
    """
    Scan Sonarr library and:
    1. Record downloaded series in Tentacle DB with full TMDB metadata
    2. Write NFO files with tags for Jellyfin to read
    3. Detect duplicates with VOD content
    """
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")

    if not sonarr_url or not sonarr_key:
        return {"error": "Sonarr not configured", "scanned": 0, "new": 0, "nfo_written": 0}

    sonarr = SonarrService(sonarr_url, sonarr_key)

    from services.tmdb import get_tmdb_token
    bearer_token = get_tmdb_token(db)
    data_dir = get_setting(db, "data_dir", "/data")
    tmdb = TMDBService(bearer_token, data_dir) if bearer_token else None

    all_series = sonarr.get_all_series()
    if not all_series:
        return {"error": "No series found in Sonarr", "scanned": 0, "new": 0, "nfo_written": 0}

    # Filter to series that have at least one downloaded episode
    downloaded = [
        s for s in all_series
        if s.get("statistics", {}).get("episodeFileCount", 0) > 0
    ]
    logger.info(f"Sonarr scan: {len(downloaded)} series with downloaded episodes")

    stats = {"scanned": len(downloaded), "new": 0, "updated": 0, "nfo_written": 0, "duplicates": 0, "enriched": 0}

    # Pre-load ALL existing series by tmdb_id to handle VOD overlap (UNIQUE constraint)
    all_series_by_tmdb = {
        row.tmdb_id: row for row in db.query(Series).all()
    }
    existing_dup_tmdb_ids = {
        row.tmdb_id for row in db.query(Duplicate.tmdb_id).filter(
            Duplicate.media_type == "series"
        ).all()
    }

    # Collect series that need NFO writing
    series_needing_nfo = []

    for show in downloaded:
        tmdb_id = show.get("tmdbId") or 0
        title = show.get("title", "")
        year = str(show.get("year", "")) if show.get("year") else None
        series_path = show.get("path", "")

        # If no TMDB ID from Sonarr, try to look it up via TMDB
        if not tmdb_id and tmdb and title:
            details = tmdb.search_series(title, year)
            if details:
                tmdb_id = details.get("tmdb_id", 0)

        if not tmdb_id:
            logger.debug(f"Sonarr: skipping '{title}' — no TMDB ID")
            continue

        existing = all_series_by_tmdb.get(tmdb_id)
        details = None

        if existing:
            changed = False
            if series_path and existing.sonarr_path != series_path:
                existing.sonarr_path = series_path
                changed = True
            # If this was a VOD-only row, create a duplicate record
            if existing.source and existing.source.startswith("provider_") and tmdb_id not in existing_dup_tmdb_ids:
                db.add(Duplicate(
                    tmdb_id=tmdb_id,
                    media_type="series",
                    sources=[
                        {"source": "sonarr", "path": series_path},
                        {"source": existing.source, "path": existing.strm_path or ""},
                    ],
                    resolution="pending"
                ))
                existing_dup_tmdb_ids.add(tmdb_id)
                stats["duplicates"] += 1
            # Backfill TMDB metadata if missing
            if tmdb and not existing.poster_path:
                details = tmdb.get_series_details(tmdb_id)
                if details:
                    existing.overview = details.get("overview") or existing.overview
                    existing.rating = details.get("rating") or existing.rating
                    existing.genres = details.get("genres") or existing.genres
                    existing.poster_path = details.get("poster_path") or existing.poster_path
                    existing.backdrop_path = details.get("backdrop_path") or existing.backdrop_path
                    existing.status = details.get("status") or existing.status
                    changed = True
                    stats["enriched"] += 1
            # Detect streaming service from TMDB studios
            if tmdb and not existing.source_tag:
                if not details:
                    details = tmdb.get_series_details(tmdb_id)
                if details:
                    detected = detect_source_tag_from_studios(details.get("studios") or [])
                    if detected:
                        existing.source_tag = detected
                        changed = True
            if changed:
                existing.date_updated = datetime.utcnow()
                stats["updated"] += 1

            series_needing_nfo.append((tmdb_id, existing))
        else:
            new_series = Series(
                tmdb_id=tmdb_id,
                title=title,
                year=year,
                source="sonarr",
                sonarr_path=series_path,
                tags=[],
                date_added=datetime.utcnow(),
            )
            # Fetch full TMDB metadata for new series
            if tmdb:
                details = tmdb.get_series_details(tmdb_id)
                if details:
                    new_series.title = details.get("title") or title
                    new_series.year = details.get("year") or year
                    new_series.overview = details.get("overview")
                    new_series.rating = details.get("rating")
                    new_series.genres = details.get("genres") or []
                    new_series.poster_path = details.get("poster_path")
                    new_series.backdrop_path = details.get("backdrop_path")
                    new_series.status = details.get("status")
                    # Detect streaming service from production companies
                    detected = detect_source_tag_from_studios(details.get("studios") or [])
                    if detected:
                        new_series.source_tag = detected
                    stats["enriched"] += 1
            db.add(new_series)
            all_series_by_tmdb[tmdb_id] = new_series
            stats["new"] += 1

            emit_library_event("series_added", {
                "tmdb_id": tmdb_id,
                "title": new_series.title,
                "year": new_series.year,
                "poster_path": new_series.poster_path,
                "source": "sonarr",
                "source_tag": new_series.source_tag,
                "tags": new_series.tags or [],
                "media_type": "series",
                "in_library": True,
            })

            series_needing_nfo.append((tmdb_id, new_series))

    # Remove series no longer in Sonarr
    sonarr_tmdb_ids = set()
    for s in downloaded:
        tid = s.get("tmdbId") or 0
        if tid:
            sonarr_tmdb_ids.add(tid)
    removed = 0
    for series in db.query(Series).filter(Series.source == "sonarr").all():
        if series.tmdb_id not in sonarr_tmdb_ids:
            emit_library_event("series_removed", {
                "tmdb_id": series.tmdb_id,
                "title": series.title,
                "media_type": "series",
            })
            db.delete(series)
            removed += 1
    if removed:
        logger.info(f"Sonarr scan: removed {removed} series no longer in Sonarr")
    stats["removed"] = removed

    # Single commit for all DB changes
    db.commit()

    # Compute tags and write NFO files for all downloaded series
    for tmdb_id, db_series in series_needing_nfo:
        try:
            # Build tag list: built-in + source tag + rule tags + list tags + user attribution
            tags = [DOWNLOADED_TV_TAG]

            # Recently added (within rolling window)
            recently_added_days = int(get_setting(db, "recently_added_days", "30") or "30")
            cutoff = datetime.utcnow() - timedelta(days=recently_added_days)
            if db_series.date_added and db_series.date_added >= cutoff:
                tags.append(RECENTLY_ADDED_TV_TAG)

            if db_series.source_tag:
                tags.append(db_series.source_tag)

            metadata = {
                "genres": db_series.genres or [],
                "rating": db_series.rating or 0,
                "year": db_series.year,
                "runtime": 0,
                "tags": tags,
            }
            rule_tags = apply_tag_rules(metadata, "series", "sonarr", db_series.source_tag, db)
            for rt in rule_tags:
                if rt not in tags:
                    tags.append(rt)

            list_tags = get_list_tags_for_tmdb_id(tmdb_id, "series", db)
            for lt in list_tags:
                if lt not in tags:
                    tags.append(lt)

            # Attribution: tag with the user who requested the download
            dl_req = db.query(DownloadRequest).filter(
                DownloadRequest.tmdb_id == tmdb_id,
                DownloadRequest.media_type == "series",
            ).first()
            if dl_req:
                req_user = db.query(TentacleUser).filter(TentacleUser.id == dl_req.user_id).first()
                if req_user:
                    user_tag = f"{req_user.display_name}'s Downloads"
                    if user_tag not in tags:
                        tags.append(user_tag)

            # Update tags on DB record
            db_series.tags = tags

            # Write NFO if series folder exists on disk
            if not db_series.sonarr_path:
                continue
            # Remap Sonarr's container path to Tentacle's mount
            local_path = db_series.sonarr_path.replace("/data/shows", "/media/shows", 1)
            series_folder = Path(local_path)
            if not series_folder.exists():
                continue

            # Build NFO metadata from DB record
            nfo_metadata = {
                "title": db_series.title,
                "tmdb_id": tmdb_id,
                "year": db_series.year,
                "overview": db_series.overview,
                "rating": db_series.rating,
                "genres": db_series.genres or [],
                "poster_path": db_series.poster_path,
                "backdrop_path": db_series.backdrop_path,
                "status": db_series.status,
            }

            # tvshow.nfo goes in the series root folder
            nfo_path = series_folder / "tvshow.nfo"
            if write_series_nfo(nfo_path, nfo_metadata, tags):
                db_series.nfo_path = str(nfo_path)
                stats["nfo_written"] += 1

        except Exception as e:
            logger.debug(f"NFO/tag processing failed for {db_series.title}: {e}")

    db.commit()

    # Trigger Jellyfin library scan so it picks up new NFOs
    jellyfin_url = get_setting(db, "jellyfin_url")
    jellyfin_key = get_setting(db, "jellyfin_api_key")
    jellyfin_uid = get_setting(db, "jellyfin_user_id", "")
    if jellyfin_url and jellyfin_key:
        from services.jellyfin import JellyfinService
        jf = JellyfinService(jellyfin_url, jellyfin_key, jellyfin_uid)

        if stats["nfo_written"] > 0 or stats["new"] > 0:
            try:
                jf.trigger_library_scan()
                logger.info("Triggered Jellyfin library scan after Sonarr NFO updates")
            except Exception as e:
                logger.warning(f"Failed to trigger Jellyfin scan: {e}")

        # Push tags to Jellyfin via API for all downloaded series.
        # NFO tags are ignored by Jellyfin for real video files — API is the only way.
        try:
            jf_lookup, jf_title_lookup = jf.get_tmdb_lookup_with_fallback("Series")
            tags_pushed = 0
            tags_failed = 0
            for tmdb_id, db_series in all_series_by_tmdb.items():
                if not db_series.tags:
                    continue
                jf_item = jf_lookup.get(tmdb_id)
                if not jf_item and db_series.title:
                    norm_title = jf._normalize_title(db_series.title)
                    year_str = str(db_series.year or "")
                    jf_item = jf_title_lookup.get((norm_title, year_str))
                    if not jf_item:
                        jf_item = jf_title_lookup.get((norm_title, ""))
                if jf_item:
                    existing_tags = set(jf_item.get("Tags", []))
                    desired_tags = set(db_series.tags)
                    if not desired_tags.issubset(existing_tags):
                        merged = list(existing_tags | desired_tags)
                        if jf.set_item_tags(jf_item["Id"], merged):
                            tags_pushed += 1
                        else:
                            tags_failed += 1
                    # Refresh metadata for items missing poster/info
                    if not jf_item.get("ImageTags", {}).get("Primary"):
                        if jf.refresh_item_metadata(jf_item["Id"]):
                            logger.info(f"Triggered metadata refresh for '{db_series.title}' (missing poster)")
                else:
                    tags_failed += 1
            stats["jf_tags_pushed"] = tags_pushed
            stats["jf_tags_failed"] = tags_failed
            logger.info(
                f"Jellyfin tag sync (series): {tags_pushed} pushed, {tags_failed} failed, "
                f"{len(all_series_by_tmdb)} total series checked"
            )
        except Exception as e:
            logger.warning(f"Jellyfin tag push failed (series): {e}")

    logger.info(
        f"Sonarr scan complete: {stats['new']} new, {stats['updated']} updated, "
        f"{stats['enriched']} enriched from TMDB, "
        f"{stats['nfo_written']} NFOs written, {stats['duplicates']} duplicates found"
    )
    return stats

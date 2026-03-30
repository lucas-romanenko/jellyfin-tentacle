"""
Tentacle - Radarr Scanner Service
Scans Radarr library, records downloaded movies in DB,
and writes NFO files with tags for Jellyfin.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
import requests
from sqlalchemy.orm import Session

from models.database import Movie, Duplicate, DownloadRequest, TentacleUser, get_setting
from services.tmdb import TMDBService
from services.nfo import write_movie_nfo, make_folder_name
from services.tagger import apply_tag_rules, get_list_tags_for_tmdb_id, detect_source_tag_from_studios
from services.exceptions import RadarrConnectionError
from services.logstream import emit_library_event

logger = logging.getLogger(__name__)

DOWNLOADED_MOVIES_TAG = "Downloaded Movies"
DOWNLOADED_TV_TAG = "Downloaded TV"


class RadarrService:
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
            raise RadarrConnectionError(f"Cannot reach Radarr at {self.url}: {e}")
        except Exception as e:
            logger.error(f"Radarr connection failed: {e}")
            return None

    def get_all_movies(self) -> list:
        try:
            r = self.session.get(f"{self.url}/api/v3/movie", timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch Radarr movies: {e}")
            return []

    def get_movie_by_tmdb(self, tmdb_id: int) -> Optional[dict]:
        movies = self.get_all_movies()
        return next((m for m in movies if m.get("tmdbId") == tmdb_id), None)

    def add_movie(self, tmdb_id: int, root_folder: str, quality_profile_id: int = 1) -> bool:
        try:
            r = self.session.post(
                f"{self.url}/api/v3/movie",
                json={
                    "tmdbId": tmdb_id,
                    "monitored": True,
                    "qualityProfileId": quality_profile_id,
                    "rootFolderPath": root_folder,
                    "addOptions": {"searchForMovie": True},
                },
                timeout=10
            )
            r.raise_for_status()
            return True
        except Exception as e:
            logger.debug(f"Radarr add movie {tmdb_id} failed: {e}")
            return False

    def delete_movie(self, tmdb_id: int, delete_files: bool = True) -> bool:
        """Delete a movie from Radarr by TMDB ID. Optionally deletes files on disk."""
        movie = self.get_movie_by_tmdb(tmdb_id)
        if not movie:
            logger.warning(f"Movie tmdb:{tmdb_id} not found in Radarr")
            return False
        radarr_id = movie.get("id")
        try:
            r = self.session.delete(
                f"{self.url}/api/v3/movie/{radarr_id}",
                params={"deleteFiles": str(delete_files).lower()},
                timeout=15,
            )
            r.raise_for_status()
            logger.info(f"Deleted movie tmdb:{tmdb_id} (radarr id:{radarr_id}) from Radarr (deleteFiles={delete_files})")
            return True
        except Exception as e:
            logger.error(f"Failed to delete movie tmdb:{tmdb_id} from Radarr: {e}")
            return False

    def get_quality_profiles(self) -> list:
        try:
            r = self.session.get(f"{self.url}/api/v3/qualityprofile", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch quality profiles: {e}")
            return []


def scan_radarr_library(db: Session) -> dict:
    """
    Scan Radarr library and:
    1. Record downloaded movies in Tentacle DB with full TMDB metadata
    2. Write NFO files with tags for Jellyfin to read
    3. Detect duplicates with VOD content
    """
    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")

    if not radarr_url or not radarr_key:
        return {"error": "Radarr not configured", "scanned": 0, "new": 0, "nfo_written": 0}

    radarr = RadarrService(radarr_url, radarr_key)

    from services.tmdb import get_tmdb_token
    bearer_token = get_tmdb_token(db)
    data_dir = get_setting(db, "data_dir", "/data")
    tmdb = TMDBService(bearer_token, data_dir) if bearer_token else None

    movies = radarr.get_all_movies()
    if not movies:
        return {"error": "No movies found in Radarr", "scanned": 0, "new": 0, "nfo_written": 0}

    # Filter to movies that are downloaded (have a file)
    downloaded = [m for m in movies if m.get("hasFile") and m.get("tmdbId")]
    logger.info(f"Radarr scan: {len(downloaded)} downloaded movies")

    stats = {"scanned": len(downloaded), "new": 0, "updated": 0, "nfo_written": 0, "duplicates": 0, "enriched": 0}

    # Pre-load existing radarr movies and VOD movies for batch lookup
    existing_radarr = {
        row.tmdb_id: row for row in db.query(Movie).filter(Movie.source == "radarr").all()
    }
    existing_vod_by_tmdb = {
        row.tmdb_id: row for row in db.query(Movie).filter(Movie.source.like("provider_%")).all()
    }
    existing_dup_tmdb_ids = {
        row.tmdb_id for row in db.query(Duplicate.tmdb_id).filter(
            Duplicate.media_type == "movie"
        ).all()
    }

    # Collect movies that need NFO writing
    movies_needing_nfo = []

    for movie in downloaded:
        tmdb_id = movie.get("tmdbId")
        title = movie.get("title", "")
        year = str(movie.get("year", "")) if movie.get("year") else None
        movie_file = movie.get("movieFile", {})
        file_path = movie_file.get("path", "") if movie_file else ""

        existing = existing_radarr.get(tmdb_id)
        details = None

        if existing:
            changed = False
            if file_path and existing.radarr_path != file_path:
                existing.radarr_path = file_path
                changed = True
            # Backfill TMDB metadata if missing
            if tmdb and not existing.poster_path:
                details = tmdb.get_movie_details(tmdb_id)
                if details:
                    existing.overview = details.get("overview") or existing.overview
                    existing.runtime = details.get("runtime") or existing.runtime
                    existing.rating = details.get("rating") or existing.rating
                    existing.genres = details.get("genres") or existing.genres
                    existing.poster_path = details.get("poster_path") or existing.poster_path
                    existing.backdrop_path = details.get("backdrop_path") or existing.backdrop_path
                    changed = True
                    stats["enriched"] += 1
            # Detect streaming service from TMDB studios
            if tmdb and not existing.source_tag:
                if not details:
                    details = tmdb.get_movie_details(tmdb_id)
                if details:
                    detected = detect_source_tag_from_studios(details.get("studios") or [])
                    if detected:
                        existing.source_tag = detected
                        changed = True
            if changed:
                existing.date_updated = datetime.utcnow()
                stats["updated"] += 1

            movies_needing_nfo.append((tmdb_id, existing))
        else:
            new_movie = Movie(
                tmdb_id=tmdb_id,
                title=title,
                year=year,
                source="radarr",
                radarr_path=file_path,
                tags=[],
                date_added=datetime.utcnow(),
            )
            # Fetch full TMDB metadata for new movies
            if tmdb:
                details = tmdb.get_movie_details(tmdb_id)
                if details:
                    new_movie.title = details.get("title") or title
                    new_movie.year = details.get("year") or year
                    new_movie.overview = details.get("overview")
                    new_movie.runtime = details.get("runtime")
                    new_movie.rating = details.get("rating")
                    new_movie.genres = details.get("genres") or []
                    new_movie.poster_path = details.get("poster_path")
                    new_movie.backdrop_path = details.get("backdrop_path")
                    # Detect streaming service from production companies
                    detected = detect_source_tag_from_studios(details.get("studios") or [])
                    if detected:
                        new_movie.source_tag = detected
                    stats["enriched"] += 1
            db.add(new_movie)
            existing_radarr[tmdb_id] = new_movie
            stats["new"] += 1

            emit_library_event("movie_added", {
                "tmdb_id": tmdb_id,
                "title": new_movie.title,
                "year": new_movie.year,
                "poster_path": new_movie.poster_path,
                "source": "radarr",
                "source_tag": new_movie.source_tag,
                "tags": new_movie.tags or [],
                "media_type": "movie",
                "in_library": True,
            })

            # Check for duplicate with VOD (using pre-loaded lookup)
            vod_copy = existing_vod_by_tmdb.get(tmdb_id)
            if vod_copy and tmdb_id not in existing_dup_tmdb_ids:
                db.add(Duplicate(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    sources=[
                        {"source": "radarr", "path": file_path},
                        {"source": vod_copy.source, "path": vod_copy.strm_path or ""},
                    ],
                    resolution="pending"
                ))
                existing_dup_tmdb_ids.add(tmdb_id)
                stats["duplicates"] += 1

            movies_needing_nfo.append((tmdb_id, new_movie))

    # Remove movies no longer in Radarr
    radarr_tmdb_ids = {m["tmdbId"] for m in downloaded}
    removed = 0
    for movie in db.query(Movie).filter(Movie.source == "radarr").all():
        if movie.tmdb_id not in radarr_tmdb_ids:
            emit_library_event("movie_removed", {
                "tmdb_id": movie.tmdb_id,
                "title": movie.title,
                "media_type": "movie",
            })
            db.delete(movie)
            removed += 1
    if removed:
        logger.info(f"Radarr scan: removed {removed} movies no longer in Radarr")
    stats["removed"] = removed

    # Single commit for all DB changes
    db.commit()

    # Write NFO files for all downloaded movies
    for tmdb_id, db_movie in movies_needing_nfo:
        try:
            if not db_movie.radarr_path:
                continue

            # Get movie folder from file path
            movie_folder = Path(db_movie.radarr_path).parent
            if not movie_folder.exists():
                continue

            # Build tag list: source tag + rule tags + list tags + user attribution
            tags = []
            if db_movie.source_tag:
                tags.append(db_movie.source_tag)

            metadata = {
                "genres": db_movie.genres or [],
                "rating": db_movie.rating or 0,
                "year": db_movie.year,
                "runtime": db_movie.runtime or 0,
                "tags": tags,
            }
            rule_tags = apply_tag_rules(metadata, "movie", "radarr", db_movie.source_tag, db)
            for rt in rule_tags:
                if rt not in tags:
                    tags.append(rt)

            list_tags = get_list_tags_for_tmdb_id(tmdb_id, "movie", db)
            for lt in list_tags:
                if lt not in tags:
                    tags.append(lt)

            # Attribution: tag with the user who requested the download
            dl_req = db.query(DownloadRequest).filter(
                DownloadRequest.tmdb_id == tmdb_id,
                DownloadRequest.media_type == "movie",
            ).first()
            if dl_req:
                req_user = db.query(TentacleUser).filter(TentacleUser.id == dl_req.user_id).first()
                if req_user:
                    user_tag = f"{req_user.display_name}'s Downloads"
                    if user_tag not in tags:
                        tags.append(user_tag)

            # Update tags on DB record
            db_movie.tags = tags

            # Build NFO metadata from DB record
            nfo_metadata = {
                "title": db_movie.title,
                "tmdb_id": tmdb_id,
                "year": db_movie.year,
                "overview": db_movie.overview,
                "runtime": db_movie.runtime,
                "rating": db_movie.rating,
                "genres": db_movie.genres or [],
                "poster_path": db_movie.poster_path,
                "backdrop_path": db_movie.backdrop_path,
            }

            # Write NFO to movie folder
            # Match NFO filename to actual video file
            video_file = None
            for ext in ('.mkv', '.mp4', '.avi', '.m4v'):
                files = list(movie_folder.glob(f'*{ext}'))
                if files:
                    video_file = files[0]
                    break
            folder_name = make_folder_name(db_movie.title, db_movie.year)
            nfo_path = video_file.with_suffix('.nfo') if video_file else movie_folder / f"{folder_name}.nfo"
            if write_movie_nfo(nfo_path, nfo_metadata, tags):
                db_movie.nfo_path = str(nfo_path)
                stats["nfo_written"] += 1

        except Exception as e:
            logger.debug(f"NFO write failed for {db_movie.title}: {e}")

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
                logger.info("Triggered Jellyfin library scan after NFO updates")
            except Exception as e:
                logger.warning(f"Failed to trigger Jellyfin scan: {e}")

        # Push tags to Jellyfin via API for all downloaded movies.
        # NFO tags are ignored by Jellyfin for .mkv files — API is the only way.
        # Build lookup once, then push in batch.
        try:
            jf_lookup, jf_title_lookup = jf.get_tmdb_lookup_with_fallback("Movie")
            tags_pushed = 0
            tags_failed = 0
            for tmdb_id, db_movie in existing_radarr.items():
                if not db_movie.tags:
                    continue
                jf_item = jf_lookup.get(tmdb_id)
                if not jf_item and db_movie.title:
                    norm_title = jf._normalize_title(db_movie.title)
                    year_str = str(db_movie.year or "")
                    jf_item = jf_title_lookup.get((norm_title, year_str))
                    # Jellyfin may not have identified the movie yet (no ProductionYear)
                    if not jf_item:
                        jf_item = jf_title_lookup.get((norm_title, ""))
                if jf_item:
                    existing_tags = set(jf_item.get("Tags", []))
                    desired_tags = set(db_movie.tags)
                    if not desired_tags.issubset(existing_tags):
                        merged = list(existing_tags | desired_tags)
                        if jf.set_item_tags(jf_item["Id"], merged):
                            tags_pushed += 1
                        else:
                            tags_failed += 1
                    # Refresh metadata for items missing poster/info
                    if not jf_item.get("ImageTags", {}).get("Primary"):
                        if jf.refresh_item_metadata(jf_item["Id"]):
                            logger.info(f"Triggered metadata refresh for '{db_movie.title}' (missing poster)")
                else:
                    tags_failed += 1
            stats["jf_tags_pushed"] = tags_pushed
            stats["jf_tags_failed"] = tags_failed
            logger.info(
                f"Jellyfin tag sync: {tags_pushed} pushed, {tags_failed} failed, "
                f"{len(existing_radarr)} total movies checked"
            )
        except Exception as e:
            logger.warning(f"Jellyfin tag push failed: {e}")

    logger.info(
        f"Radarr scan complete: {stats['new']} new, {stats['updated']} updated, "
        f"{stats['enriched']} enriched from TMDB, "
        f"{stats['nfo_written']} NFOs written, {stats['duplicates']} duplicates found"
    )
    return stats

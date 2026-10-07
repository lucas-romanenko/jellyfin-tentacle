"""
Tentacle - Radarr Scanner Service
Scans Radarr library, records downloaded movies in DB,
and writes NFO files with tags for Jellyfin.
"""

import logging
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional
import requests
from sqlalchemy.orm import Session

from datetime import timedelta
from models.database import Movie, Duplicate, DownloadRequest, TentacleUser, get_setting, DeletionLog, get_recently_added_days
from services.tmdb import TMDBService
from services.nfo import write_movie_nfo, make_folder_name, refresh_arr_nfo
from services.tagger import apply_tag_rules, get_list_tags_for_tmdb_id, detect_source_tag_from_studios
from services.exceptions import RadarrConnectionError
from services.logstream import emit_library_event

logger = logging.getLogger(__name__)

DOWNLOADED_MOVIES_TAG = "Downloaded Movies"
RECENTLY_ADDED_MOVIES_TAG = "Recently Added Movies"


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

    def lookup_by_term(self, query: str) -> list:
        """Search Radarr/TMDB by free-text query. Returns list of lookup results."""
        try:
            r = self.session.get(
                f"{self.url}/api/v3/movie/lookup",
                params={"term": query},
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Radarr text lookup failed for '{query}': {e}")
            return []

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

    def get_movie_files(self, movie_id: int) -> list:
        """Radarr's files for one movie. Raises on failure: callers delete
        files by this list, so "couldn't read" must not look like "none"."""
        r = self.session.get(f"{self.url}/api/v3/moviefile", params={"movieId": movie_id}, timeout=15)
        r.raise_for_status()
        return r.json()

    def delete_movie_file(self, file_id: int) -> None:
        """Delete one movie file (only that file, not its folder). A 404 means
        it is already gone. Raises on any other failure."""
        r = self.session.delete(f"{self.url}/api/v3/moviefile/{file_id}", timeout=30)
        if r.status_code != 404:
            r.raise_for_status()

    def search_movie(self, radarr_id: int) -> bool:
        """Ask Radarr to search its indexers for this movie now."""
        try:
            r = self.session.post(
                f"{self.url}/api/v3/command",
                json={"name": "MoviesSearch", "movieIds": [radarr_id]},
                timeout=10,
            )
            return r.status_code < 400
        except Exception as e:
            logger.warning(f"Radarr: failed to trigger search for movie {radarr_id}: {e}")
            return False

    def delete_movie_by_id(self, radarr_id: int, delete_files: bool = True) -> bool:
        """Delete a movie by Radarr id (the caller already looked it up)."""
        try:
            r = self.session.delete(
                f"{self.url}/api/v3/movie/{radarr_id}",
                params={"deleteFiles": str(delete_files).lower(), "addImportExclusion": "false"},
                timeout=15,
            )
            r.raise_for_status()
            logger.info(f"Deleted movie radarr id:{radarr_id} from Radarr (deleteFiles={delete_files})")
            return True
        except Exception as e:
            logger.error(f"Failed to delete movie radarr id:{radarr_id} from Radarr: {e}")
            return False

    def get_quality_profiles(self) -> list:
        try:
            r = self.session.get(f"{self.url}/api/v3/qualityprofile", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Failed to fetch quality profiles: {e}")
            return []



def file_loss_looks_like_an_outage(lost: int, total: int) -> bool:
    """True when so many still-listed titles lost their files at once that it
    is the *arr's storage that went away, not the library.

    Deleting a few files is ordinary housekeeping; an unmounted media share
    makes the *arr report EVERY title as having no file, all in one scan. The
    response is not empty, so an "is the list empty?" check cannot see it.
    Titles removed from the *arr itself are a decision somebody made and are
    not counted here -- that is also the way out for a genuinely large clean-up.
    """
    return lost >= 3 and lost * 2 > total


def download_kind(movie) -> str:
    """The kind of download a row holds: "radarr" (downloaded-only) or "vod"
    (a VOD title Radarr downloaded too, #378)."""
    return "radarr" if movie.source == "radarr" else "vod"


def download_loss_looks_like_an_outage(lost: dict, total: dict) -> bool:
    """file_loss_looks_like_an_outage over every download and over each kind
    on its own (#505), counts keyed by download_kind. Downloads may sit on two
    shares; a share lost under one kind must not be diluted by the other
    kind's healthy downloads."""
    kinds = set(lost) | set(total)
    return (file_loss_looks_like_an_outage(sum(lost.values()), sum(total.values()))
            or any(file_loss_looks_like_an_outage(lost.get(k, 0), total.get(k, 0)) for k in kinds))


def downloaded_movie_rows(db: Session):
    """Rows that hold a Radarr download: the downloaded-only ones, and VOD
    titles Radarr downloaded too (one row per title: source provider_N with
    radarr_path, #378). Both lose the download the same way."""
    from sqlalchemy import or_, and_
    return db.query(Movie).filter(or_(
        Movie.source == "radarr",
        and_(Movie.radarr_path.isnot(None), Movie.radarr_path != ""),
    ))


def release_vod_download(db: Session, row: Movie, drop_request_tags: bool = True,
                         owned: Optional[set] = None) -> None:
    """A VOD title's Radarr download is gone: the row goes back to a plain VOD
    title (#378). Its download path, download date and the download's
    Jellyfin item id go, and "Downloaded Movies" (with the requester's
    "<name>'s Downloads" when the request goes too) comes off the row and the
    .strm's NFO, so the next tag push takes it off the VOD item in Jellyfin
    and the playlists drop it. A pending duplicate for the pair is dismissed.
    The VOD copy stays. Does not commit."""
    from services.tagger import set_row_tags, tentacle_owned_tags, current_downloads_tags
    drop = {DOWNLOADED_MOVIES_TAG}
    if drop_request_tags:
        drop |= current_downloads_tags(db)
    row.radarr_path = None
    row.downloaded_at = None
    row.jellyfin_item_id = None   # the deleted download's item; re-matched on use
    if row.strm_path:             # the scan pointed it at the download's NFO
        row.nfo_path = str(Path(row.strm_path).with_suffix(".nfo"))
    row.date_updated = datetime.utcnow()
    set_row_tags(row, [t for t in (row.tags or []) if t not in drop],
                 owned if owned is not None else tentacle_owned_tags(db))
    from services.duplicates import droppable_duplicates   # not while Keep VOD holds it (#515)
    droppable_duplicates(db, "movie", row.tmdb_id).filter(
        Duplicate.resolution == "pending").delete(synchronize_session=False)

# One Radarr scan at a time (#268). A scan loads every row, asks TMDB about
# each new title and commits once, so two overlapping scans (two webhooks for
# different movies, a webhook during the nightly scan, "Scan now") both added
# the same new movie, and the second failed on UNIQUE(tmdb_id) and lost all
# its work. A waiting scan re-reads the rows, so it sees the first one's.
_scan_lock = threading.Lock()


def scan_radarr_library(db: Session) -> dict:
    """
    Scan Radarr library and:
    1. Record downloaded movies in Tentacle DB with full TMDB metadata
    2. Write NFO files with tags for Jellyfin to read
    3. Detect duplicates with VOD content
    """
    with _scan_lock:
        return _scan_radarr_library(db)


def _scan_radarr_library(db: Session) -> dict:
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

    # Pre-load ALL existing movies by tmdb_id to handle VOD overlap (UNIQUE constraint)
    all_movies_by_tmdb = {
        row.tmdb_id: row for row in db.query(Movie).all()
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

        existing = all_movies_by_tmdb.get(tmdb_id)
        details = None

        if existing:
            changed = False
            if file_path and existing.radarr_path != file_path:
                existing.radarr_path = file_path
                changed = True
            radarr_date = None
            if movie_file and movie_file.get("dateAdded"):
                try:
                    radarr_date = datetime.fromisoformat(movie_file["dateAdded"].replace("Z", "+00:00")).replace(tzinfo=None)
                except (ValueError, TypeError):
                    radarr_date = None
            # Backfill date_added from Radarr's file import date if more accurate
            if radarr_date and existing.source == "radarr" and existing.date_added != radarr_date:
                existing.date_added = radarr_date
                changed = True
            # The download date itself, whatever source owns the row: a title
            # that was VOD first keeps its old date_added, and "Downloaded
            # Movies" sorted by that put every such download mid-row.
            if radarr_date and existing.downloaded_at != radarr_date:
                existing.downloaded_at = radarr_date
                changed = True
            # If this was a VOD-only row, create a duplicate record
            if existing.source and existing.source.startswith("provider_") and tmdb_id not in existing_dup_tmdb_ids:
                db.add(Duplicate(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    sources=[
                        {"source": "radarr", "path": file_path},
                        {"source": existing.source, "path": existing.strm_path or ""},
                    ],
                    resolution="pending"
                ))
                existing_dup_tmdb_ids.add(tmdb_id)
                stats["duplicates"] += 1
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
            # Use Radarr's file import date for accurate chronological ordering
            radarr_date = None
            if movie_file and movie_file.get("dateAdded"):
                try:
                    radarr_date = datetime.fromisoformat(movie_file["dateAdded"].replace("Z", "+00:00")).replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass
            new_movie = Movie(
                tmdb_id=tmdb_id,
                title=title,
                year=year,
                source="radarr",
                radarr_path=file_path,
                tags=[],
                date_added=radarr_date or datetime.utcnow(),
                downloaded_at=radarr_date or datetime.utcnow(),
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
            all_movies_by_tmdb[tmdb_id] = new_movie
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

            movies_needing_nfo.append((tmdb_id, new_movie))

    # Remove movies no longer in Radarr
    radarr_tmdb_ids = {m["tmdbId"] for m in downloaded}
    listed_tmdb_ids = {m.get("tmdbId") for m in movies if m.get("tmdbId")}
    # VOD titles with a download count too: they lose it the same way (#378).
    rows = downloaded_movie_rows(db).all()
    # Still in Radarr, but Radarr says the file is gone.
    lost_file = [m for m in rows if m.tmdb_id not in radarr_tmdb_ids and m.tmdb_id in listed_tmdb_ids]
    refused = 0
    # Judged over all of them and over each kind alone (#505).
    if download_loss_looks_like_an_outage(Counter(download_kind(m) for m in lost_file),
                                          Counter(download_kind(m) for m in rows)):
        refused = len(lost_file)
        keep = {m.tmdb_id for m in lost_file}
        logger.error(
            f"Radarr scan: REFUSING to remove {refused} of {len(rows)} downloaded movies that "
            f"Radarr still lists but reports as having no file. That many at once looks like "
            f"Radarr's media storage being unavailable, not a clean-up. Rows kept; if the files "
            f"really are gone, remove the movies from Radarr.")
    else:
        keep = set()
    removed = 0
    released = 0
    for movie in rows:
        if movie.tmdb_id not in radarr_tmdb_ids and movie.tmdb_id not in keep:
            # The request that asked for this title goes with the download, as
            # the orphan sweep already does — otherwise "My Downloads" keeps a
            # stale entry and the requester keeps delete rights over the id (#107).
            db.query(DownloadRequest).filter(
                DownloadRequest.tmdb_id == movie.tmdb_id,
                DownloadRequest.media_type == "movie",
            ).delete()
            # And its duplicate tombstones, as the delete webhook does: with
            # the download gone, a "keep downloaded" one would stop the VOD
            # copy from ever coming back (#334). Not while Keep VOD holds
            # them, nor one holding saved watched state (#515).
            from services.duplicates import droppable_duplicates
            droppable_duplicates(db, "movie", movie.tmdb_id).delete(synchronize_session=False)
            if movie.source != "radarr":
                # A VOD title: only its download goes, the row stays.
                release_vod_download(db, movie)
                released += 1
                continue
            emit_library_event("movie_removed", {
                "tmdb_id": movie.tmdb_id,
                "title": movie.title,
                "media_type": "movie",
            })
            # Same transaction as the delete: the scan commits once, at the end.
            db.add(DeletionLog(
                kind="radarr-scan", media_type="movie", reason="removed-from-radarr",
                name=f"{movie.title} ({movie.year})" if movie.year else movie.title,
                detail="no longer in Radarr" if movie.tmdb_id not in listed_tmdb_ids
                else "Radarr reports no file"))
            db.delete(movie)
            removed += 1
    if removed:
        logger.info(f"Radarr scan: removed {removed} movies no longer in Radarr")
    if released:
        logger.info(f"Radarr scan: {released} VOD titles lost their download and are VOD only again")
    stats["removed"] = removed
    stats["released"] = released
    stats["removals_refused"] = refused
    from services.duplicates import drop_orphan_tombstones
    stats["tombstones_dropped"] = drop_orphan_tombstones(db, "movie")
    if stats["tombstones_dropped"]:
        logger.info(f"Radarr scan: dropped {stats['tombstones_dropped']} keep-downloaded resolutions "
                    f"whose download is gone; the VOD copy can come back")

    # Single commit for all DB changes
    db.commit()

    # Compute tags and write NFO files for all downloaded movies
    from services.tagger import tentacle_owned_tags
    owned = tentacle_owned_tags(db)
    recently_added_days = get_recently_added_days(db)
    for tmdb_id, db_movie in movies_needing_nfo:
        try:
            # Build tag list: built-in + source tag + rule tags + list tags + user attribution
            tags = [DOWNLOADED_MOVIES_TAG]

            # Recently added (within rolling window)
            cutoff = datetime.utcnow() - timedelta(days=recently_added_days)
            if db_movie.date_added and db_movie.date_added >= cutoff:
                tags.append(RECENTLY_ADDED_MOVIES_TAG)

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

            # Write NFO if movie folder exists on disk
            if not db_movie.radarr_path:
                continue
            # Remap Radarr's container path to Tentacle's mount
            local_path = db_movie.radarr_path.replace("/data/movies", "/media/movies", 1)
            movie_folder = Path(local_path).parent
            if not movie_folder.exists():
                continue

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

            video_file = None
            for ext in ('.mkv', '.mp4', '.avi', '.m4v'):
                files = list(movie_folder.glob(f'*{ext}'))
                if files:
                    video_file = files[0]
                    break
            folder_name = make_folder_name(db_movie.title, db_movie.year)
            nfo_path = video_file.with_suffix('.nfo') if video_file else movie_folder / f"{folder_name}.nfo"
            if refresh_arr_nfo(nfo_path, write_movie_nfo, nfo_metadata, tags, owned):
                stats["nfo_written"] += 1
            if nfo_path.exists():
                db_movie.nfo_path = str(nfo_path)

        except Exception as e:
            logger.debug(f"NFO/tag processing failed for {db_movie.title}: {e}")

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
            if not jf_lookup and not jf_title_lookup:
                logger.warning("Jellyfin tag sync: Jellyfin's movie listing came back empty, no tags pushed")
            tags_pushed = 0
            tags_failed = 0       # writes Jellyfin refused
            tags_not_found = 0    # tagged titles Jellyfin hasn't listed (not scanned yet)
            for tmdb_id, db_movie in all_movies_by_tmdb.items():
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
                    tags_not_found += 1
            stats["jf_tags_pushed"] = tags_pushed
            stats["jf_tags_failed"] = tags_failed
            stats["jf_tags_not_found"] = tags_not_found
            logger.info(
                f"Jellyfin tag sync: {tags_pushed} pushed, {tags_failed} failed, "
                f"{tags_not_found} not in Jellyfin yet, "
                f"{len(all_movies_by_tmdb)} total movies checked"
            )
        except Exception as e:
            logger.warning(f"Jellyfin tag push failed: {e}")

    logger.info(
        f"Radarr scan complete: {stats['new']} new, {stats['updated']} updated, "
        f"{stats['enriched']} enriched from TMDB, "
        f"{stats['nfo_written']} NFOs written, {stats['duplicates']} duplicates found"
    )
    return stats

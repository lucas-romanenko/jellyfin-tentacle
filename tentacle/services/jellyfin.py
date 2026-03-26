"""
Tentacle - Jellyfin Service
Manages Jellyfin items, tags, and playlists via the REST API.
"""

import re
import logging
import requests
from typing import Optional, List
from services.exceptions import JellyfinConnectionError

logger = logging.getLogger(__name__)


class JellyfinService:
    def __init__(self, url: str, api_key: str, user_id: str = ""):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.session = requests.Session()
        self.session.headers.update({
            "X-Emby-Token": api_key,
            "Content-Type": "application/json",
        })

    def _check_401(self, r, path: str):
        if r.status_code == 401:
            logger.error("[Jellyfin] API key is invalid or expired — update in Settings → Connections")
            raise requests.HTTPError(
                f"401 Unauthorized on {path}", response=r
            )

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        try:
            r = self.session.get(f"{self.url}{path}", params=params, timeout=15)
            self._check_401(r, path)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.debug(f"Jellyfin GET {path} failed: {e}")
            return None

    def _post(self, path: str, data=None) -> bool:
        try:
            r = self.session.post(f"{self.url}{path}", json=data, timeout=15)
            self._check_401(r, path)
            r.raise_for_status()
            return True
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.debug(f"Jellyfin POST {path} failed: {e}")
            return False

    def test_connection(self) -> bool:
        """Test Jellyfin connection. Returns True if healthy, False otherwise."""
        try:
            r = self.session.get(f"{self.url}/System/Info", timeout=10)
            if r.status_code == 401:
                logger.error("[Jellyfin] API key is invalid or expired — update in Settings → Connections")
                return False
            r.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"[Jellyfin] Connection failed: {e}")
            return False

    def _fetch_all_items(self, media_type: str = "Movie") -> List[dict]:
        """Fetch all items of a type from Jellyfin with ProviderIds and Tags."""
        data = self._get("/Items", params={
            "IncludeItemTypes": media_type,
            "Recursive": "true",
            "Fields": "ProviderIds,Tags",
            "Limit": 10000,
        })
        if data:
            return data.get("Items", [])
        return []

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize a title for flexible matching.

        Strips year suffixes like '(1979)', converts colons to ' -',
        collapses whitespace, lowercases.
        """
        title = re.sub(r'\s*\(\d{4}\)\s*$', '', title)
        title = title.replace(':', ' -').replace('  ', ' ')
        return title.lower().strip()

    def _title_variants(self, title: str) -> list:
        """Return a list of normalized title variants to try when matching."""
        variants = {self._normalize_title(title)}
        # Also try with colons kept (in case Jellyfin has the colon version)
        stripped = re.sub(r'\s*\(\d{4}\)\s*$', '', title).lower().strip()
        variants.add(stripped)
        return list(variants)

    def search_by_tmdb_id(self, tmdb_id: int, media_type: str = "Movie",
                          title: str = None, year: str = None) -> Optional[dict]:
        """Find a Jellyfin item by TMDB ID, with title+year fallback.

        Jellyfin has no server-side filter for a specific provider ID value.
        We fetch all items and filter client-side. Falls back to normalized
        title+year matching for items without TMDB metadata (e.g. scanned MKVs).
        """
        items = self._fetch_all_items(media_type)
        tmdb_str = str(tmdb_id)

        # Primary: match by TMDB ID
        for item in items:
            if item.get("ProviderIds", {}).get("Tmdb") == tmdb_str:
                return item

        # Fallback: match by normalized title + year
        if title:
            variants = self._title_variants(title)
            for item in items:
                item_norm = self._normalize_title(item.get("Name", ""))
                if (item_norm in variants
                        and (not year or str(item.get("ProductionYear", "")) == str(year))):
                    return item

        return None

    def get_tmdb_lookup(self, media_type: str = "Movie") -> dict:
        """Build a {tmdb_id: jellyfin_item} lookup for all items of a type.

        Also builds a (title_lower, year) fallback index for items
        without TMDB metadata. Returns (tmdb_lookup, title_lookup).
        Use get_tmdb_lookup_with_fallback() if you need the title fallback.

        Much more efficient than calling search_by_tmdb_id per item —
        makes one API call instead of N.
        """
        items = self._fetch_all_items(media_type)
        lookup = {}
        for item in items:
            tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
            if tmdb_id:
                try:
                    lookup[int(tmdb_id)] = item
                except ValueError:
                    pass
        return lookup

    def get_tmdb_lookup_with_fallback(self, media_type: str = "Movie") -> tuple:
        """Build both TMDB and title+year lookups in one API call.

        Returns (tmdb_lookup, title_lookup) where:
        - tmdb_lookup: {int(tmdb_id): item}
        - title_lookup: {(normalized_title, year_str): item}

        Title keys are normalized (year suffixes stripped, colons → hyphens).
        Callers should normalize their lookup keys with _normalize_title().
        """
        items = self._fetch_all_items(media_type)
        tmdb_lookup = {}
        title_lookup = {}
        for item in items:
            tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
            if tmdb_id:
                try:
                    tmdb_lookup[int(tmdb_id)] = item
                except ValueError:
                    pass
            name = item.get("Name", "")
            year = str(item.get("ProductionYear", ""))
            if name:
                norm = self._normalize_title(name)
                title_lookup[(norm, year)] = item
                # Also index the raw lowercase for exact matches
                raw = name.lower().strip()
                if raw != norm:
                    title_lookup[(raw, year)] = item
        return tmdb_lookup, title_lookup

    def _item_path(self, item_id: str) -> str:
        """Return the user-scoped item path if user_id is set, otherwise the global path"""
        if self.user_id:
            return f"/Users/{self.user_id}/Items/{item_id}"
        return f"/Items/{item_id}"

    def get_item_tags(self, item_id: str) -> List[str]:
        """Get current tags for a Jellyfin item"""
        data = self._get(self._item_path(item_id))
        if data:
            return data.get("Tags", [])
        return []

    def set_item_tags(self, item_id: str, tags: List[str]) -> bool:
        """Set tags on a Jellyfin item (replaces existing).

        GET via user-scoped endpoint, build a minimal payload with only the
        fields Jellyfin needs for ItemUpdate, POST to global /Items/{id}.
        Using the full DTO causes 500 errors on some Jellyfin versions.
        """
        item = self._get(self._item_path(item_id))
        if not item:
            logger.warning(f"[Jellyfin] Cannot GET item {item_id} — set_item_tags aborted")
            return False

        old_tags = item.get("Tags", [])
        logger.debug(f"[Jellyfin] set_item_tags {item_id}: {old_tags} → {tags}")

        minimal = {
            "Id": item["Id"],
            "Name": item.get("Name", ""),
            "OriginalTitle": item.get("OriginalTitle", ""),
            "Overview": item.get("Overview", ""),
            "Genres": item.get("Genres", []),
            "Tags": tags,
            "Studios": item.get("Studios", []),
            "People": item.get("People", []),
            "ProviderIds": item.get("ProviderIds", {}),
            "ProductionYear": item.get("ProductionYear"),
            "PremiereDate": item.get("PremiereDate"),
            "CommunityRating": item.get("CommunityRating"),
            "OfficialRating": item.get("OfficialRating", ""),
            "Taglines": item.get("Taglines", []),
        }

        try:
            r = self.session.post(
                f"{self.url}/Items/{item_id}",
                json=minimal,
                timeout=15
            )
            self._check_401(r, f"/Items/{item_id}")
            if r.status_code >= 400:
                body = r.text[:200] if r.text else "(empty)"
                logger.error(f"[Jellyfin] POST /Items/{item_id} returned {r.status_code}: {body}")
                return False
            logger.debug(f"[Jellyfin] POST /Items/{item_id} returned {r.status_code} OK")
            return True
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[Jellyfin] Failed to set tags on {item_id}: {e}")
            return False

    def add_tag_to_item(self, item_id: str, tag: str) -> bool:
        """Add a single tag without removing existing tags"""
        current = self.get_item_tags(item_id)
        if tag in current:
            return True  # Already tagged
        return self.set_item_tags(item_id, current + [tag])

    def trigger_library_scan(self, library_id: Optional[str] = None) -> bool:
        """Trigger a library refresh scan"""
        path = "/Library/Refresh"
        if library_id:
            path = f"/Items/{library_id}/Refresh"
        return self._post(path)

    def refresh_item_metadata(self, item_id: str) -> bool:
        """Trigger a metadata refresh on a single item (identify, fetch images).

        Uses Default mode so Jellyfin fills in missing metadata/images
        without replacing existing fields like tags.
        """
        params = {
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default",
            "ReplaceAllMetadata": "false",
            "ReplaceAllImages": "false",
        }
        try:
            r = self.session.post(
                f"{self.url}/Items/{item_id}/Refresh",
                params=params,
                timeout=15,
            )
            self._check_401(r, f"/Items/{item_id}/Refresh")
            r.raise_for_status()
            return True
        except Exception as e:
            logger.debug(f"Metadata refresh failed for {item_id}: {e}")
            return False

    def get_libraries(self) -> List[dict]:
        """Get all libraries"""
        data = self._get("/Library/VirtualFolders")
        return data or []

    def get_all_movies(self, limit: int = 5000) -> List[dict]:
        """Get all movies from Jellyfin"""
        data = self._get("/Items", params={
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Fields": "ProviderIds,Tags,Path",
            "Limit": limit,
        })
        if data:
            return data.get("Items", [])
        return []

    def get_all_series(self, limit: int = 2000) -> List[dict]:
        """Get all series from Jellyfin"""
        data = self._get("/Items", params={
            "IncludeItemTypes": "Series",
            "Recursive": "true",
            "Fields": "ProviderIds,Tags,Path",
            "Limit": limit,
        })
        if data:
            return data.get("Items", [])
        return []

    # ── Item Queries ─────────────────────────────────────────────────────

    def query_items(self, include_types: List[str], tags: List[str] = None,
                    genres: List[str] = None, years: List[int] = None,
                    min_rating: float = None, max_rating: float = None,
                    sort_by: str = None, sort_order: str = "Ascending",
                    limit: int = None, min_premiere_date: str = None,
                    max_premiere_date: str = None) -> List[dict]:
        """Query Jellyfin items with filters matching SmartList expression logic."""
        params = {
            "Recursive": "true",
            "Fields": "ProviderIds,Tags,Genres,CommunityRating",
        }
        if include_types:
            params["IncludeItemTypes"] = ",".join(include_types)
        if tags:
            params["Tags"] = "|".join(tags)
        if genres:
            params["Genres"] = "|".join(genres)
        if years:
            params["Years"] = ",".join(str(y) for y in years)
        if min_rating is not None:
            params["MinCommunityRating"] = min_rating
        if max_rating is not None:
            params["MaxCommunityRating"] = max_rating
        if min_premiere_date:
            params["MinPremiereDate"] = min_premiere_date
        if max_premiere_date:
            params["MaxPremiereDate"] = max_premiere_date
        if sort_by:
            params["SortBy"] = sort_by
            params["SortOrder"] = sort_order
        if limit:
            params["Limit"] = limit

        data = self._get("/Items", params=params)
        if data:
            return data.get("Items", [])
        return []

    # ── Playlist Management ──────────────────────────────────────────────

    def create_playlist(self, name: str, item_ids: List[str] = None) -> Optional[str]:
        """Create a new playlist. Returns the playlist ID or None."""
        body = {
            "Name": name,
            "MediaType": "Video",
        }
        if self.user_id:
            body["UserId"] = self.user_id
        if item_ids:
            body["Ids"] = item_ids
        try:
            r = self.session.post(f"{self.url}/Playlists", json=body, timeout=15)
            self._check_401(r, "/Playlists")
            r.raise_for_status()
            return r.json().get("Id")
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.debug(f"Failed to create playlist '{name}': {e}")
            return None

    def get_playlist_items(self, playlist_id: str, limit: int = 5000) -> List[dict]:
        """Get all items in a playlist."""
        params = {"Limit": limit}
        if self.user_id:
            params["UserId"] = self.user_id
        data = self._get(f"/Playlists/{playlist_id}/Items", params=params)
        if data:
            return data.get("Items", [])
        return []

    def add_to_playlist(self, playlist_id: str, item_ids: List[str]) -> bool:
        """Add items to an existing playlist in chunks of 50."""
        if not item_ids:
            return True
        chunk_size = 50
        for i in range(0, len(item_ids), chunk_size):
            chunk = item_ids[i:i + chunk_size]
            try:
                params = {"Ids": ",".join(chunk)}
                if self.user_id:
                    params["UserId"] = self.user_id
                r = self.session.post(
                    f"{self.url}/Playlists/{playlist_id}/Items",
                    params=params,
                    timeout=15,
                )
                self._check_401(r, f"/Playlists/{playlist_id}/Items")
                if r.status_code >= 400:
                    body = r.text[:200] if r.text else "(empty)"
                    logger.error(f"[Jellyfin] Failed to add items to playlist {playlist_id}: {r.status_code} {body}")
                    return False
            except requests.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[Jellyfin] Failed to add items to playlist {playlist_id}: {e}")
                return False
        return True

    def remove_from_playlist(self, playlist_id: str, entry_ids: List[str]) -> bool:
        """Remove items from a playlist by their PlaylistItemId, in chunks of 50."""
        if not entry_ids:
            return True
        chunk_size = 50
        for i in range(0, len(entry_ids), chunk_size):
            chunk = entry_ids[i:i + chunk_size]
            try:
                r = self.session.delete(
                    f"{self.url}/Playlists/{playlist_id}/Items",
                    params={"EntryIds": ",".join(chunk)},
                    timeout=30,
                )
                self._check_401(r, f"/Playlists/{playlist_id}/Items")
                if r.status_code >= 400:
                    body = r.text[:200] if r.text else "(empty)"
                    logger.error(f"[Jellyfin] Failed to remove items from playlist {playlist_id}: {r.status_code} {body}")
                    return False
                logger.debug(f"[Jellyfin] Removed {len(chunk)} items from playlist {playlist_id}")
            except requests.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[Jellyfin] Failed to remove items from playlist {playlist_id}: {e}")
                return False
        return True

    def delete_item(self, item_id: str) -> bool:
        """Delete an item (playlist, collection, etc.) from Jellyfin."""
        try:
            r = self.session.delete(f"{self.url}/Items/{item_id}", timeout=15)
            self._check_401(r, f"/Items/{item_id}")
            if r.status_code < 400:
                logger.info(f"[Jellyfin] Deleted item {item_id}")
                return True
            logger.warning(f"[Jellyfin] Failed to delete item {item_id}: {r.status_code}")
            return False
        except Exception as e:
            logger.error(f"[Jellyfin] Failed to delete item {item_id}: {e}")
            return False

    def get_item_by_id(self, item_id: str) -> Optional[dict]:
        """Check if an item exists by ID."""
        return self._get(self._item_path(item_id))

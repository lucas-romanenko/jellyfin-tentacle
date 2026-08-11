"""
Tentacle - Jellyfin Service
Manages Jellyfin items, tags, and playlists via the REST API.
"""

import re
import time
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
        """Fetch all items of a type from Jellyfin with ProviderIds and Tags.
        Paginates automatically for libraries with more than 10,000 items."""
        all_items = []
        start_index = 0
        page_size = 10000
        while True:
            data = self._get("/Items", params={
                "IncludeItemTypes": media_type,
                "Recursive": "true",
                "Fields": "ProviderIds,Tags",
                "Limit": page_size,
                "StartIndex": start_index,
            })
            if not data:
                break
            items = data.get("Items", [])
            all_items.extend(items)
            total = data.get("TotalRecordCount", 0)
            start_index += len(items)
            if start_index >= total or not items:
                break
        return all_items

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

    def wait_for_images(self, item_id: str, max_wait: int = 30, poll_interval: int = 3) -> bool:
        """Wait for Jellyfin to fetch poster/backdrop images after a metadata refresh.

        Polls the item until ImageTags.Primary exists or timeout is reached.
        Returns True if images are available, False on timeout.
        """
        import time
        elapsed = 0
        while elapsed < max_wait:
            item = self.get_item_by_id(item_id)
            if item and item.get("ImageTags", {}).get("Primary"):
                return True
            time.sleep(poll_interval)
            elapsed += poll_interval
        return False

    def get_libraries(self) -> List[dict]:
        """Get all libraries"""
        data = self._get("/Library/VirtualFolders")
        return data or []

    def get_genres(self) -> List[str]:
        """Get all genres from Jellyfin library."""
        data = self._get("/Genres", params={
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        })
        if data:
            return [item["Name"] for item in data.get("Items", []) if item.get("Name")]
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
            # An explicit limit is requested — a single page is enough.
            params["Limit"] = limit
            data = self._get("/Items", params=params)
            return data.get("Items", []) if data else []

        # No limit: page through ALL matching items. A single un-limited /Items
        # response is capped at ~500 by Jellyfin, which silently truncated large
        # playlists (e.g. Netflix Movies stuck at ~500 of 3,600+ tagged). Paging
        # with StartIndex/Limit preserves the sort (Jellyfin sorts before paging).
        all_items = []
        start = 0
        page = 2000
        while True:
            params["Limit"] = page
            params["StartIndex"] = start
            data = self._get("/Items", params=params)
            if not data:
                break
            items = data.get("Items", [])
            all_items.extend(items)
            start += len(items)
            total = data.get("TotalRecordCount") or 0
            if len(items) < page or start >= total or start > 100000:
                break
        return all_items

    # ── Playlist Management ──────────────────────────────────────────────

    def create_playlist(self, name: str, item_ids: List[str] = None,
                        user_id: str = None, is_public: bool = False) -> Optional[str]:
        """Create a new playlist. Returns the playlist ID or None.

        Args:
            user_id: Override self.user_id for the playlist owner.
            is_public: If False, only the owner can see the playlist.
        """
        uid = user_id or self.user_id
        body = {
            "Name": name,
            "MediaType": "Video",
            "IsPublic": is_public,
        }
        if uid:
            body["UserId"] = uid
        if item_ids:
            body["Ids"] = item_ids
        try:
            r = self.session.post(f"{self.url}/Playlists", json=body, timeout=120)
            self._check_401(r, "/Playlists")
            r.raise_for_status()
            return r.json().get("Id")
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.debug(f"Failed to create playlist '{name}': {e}")
            return None

    def get_playlist_items(self, playlist_id: str, limit: int = 50000) -> List[dict]:
        """Get all items in a playlist. Includes SeriesId so callers can group
        episodes (Jellyfin expands series into episodes inside playlists) back to
        their series for comparison."""
        params = {"Limit": limit, "Fields": "SeriesId"}
        if self.user_id:
            params["UserId"] = self.user_id
        data = self._get(f"/Playlists/{playlist_id}/Items", params=params)
        if data:
            return data.get("Items", [])
        return []

    def add_to_playlist(self, playlist_id: str, item_ids: List[str]) -> bool:
        """Add items to an existing playlist in chunks of 25.

        Jellyfin re-saves and re-indexes the whole playlist on every add, so the
        request time grows with playlist size. Smaller chunks keep each request
        light and the timeout is generous — a 50-item chunk at a 15s timeout was
        timing out on larger playlists, which aborted the whole rebuild and left
        playlists frozen at their previous (partial) contents.
        """
        if not item_ids:
            return True
        chunk_size = 25
        for i in range(0, len(item_ids), chunk_size):
            chunk = item_ids[i:i + chunk_size]
            try:
                params = {"Ids": ",".join(chunk)}
                if self.user_id:
                    params["UserId"] = self.user_id
                r = self.session.post(
                    f"{self.url}/Playlists/{playlist_id}/Items",
                    params=params,
                    timeout=120,
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

    def move_playlist_item(self, playlist_id: str, item_id: str, new_index: int) -> bool:
        """Move an item within a playlist to a new position."""
        try:
            # UserId for private per-user playlists (same reason as remove_from_playlist).
            params = {}
            if self.user_id:
                params["UserId"] = self.user_id
            r = self.session.post(
                f"{self.url}/Playlists/{playlist_id}/Items/{item_id}/Move/{new_index}",
                params=params,
                timeout=10,
            )
            self._check_401(r, f"/Playlists/{playlist_id}/Items/{item_id}/Move/{new_index}")
            if r.status_code >= 400:
                logger.warning(f"Move playlist item failed: HTTP {r.status_code} for playlist={playlist_id} item={item_id} index={new_index}")
            return r.status_code < 400
        except Exception as e:
            logger.warning(f"Move playlist item exception: {e}")
            return False

    def remove_from_playlist(self, playlist_id: str, entry_ids: List[str]) -> bool:
        """Remove items from a playlist by their PlaylistItemId, in chunks of 25.

        UserId is REQUIRED for private (per-user) playlists — without it Jellyfin
        returns 204 but silently removes nothing, which is what bloated playlists
        over time. Each chunk is retried a few times and a chunk that ultimately
        fails is skipped rather than aborting the whole clear — when many
        playlists rebuild at once Jellyfin can transiently error, and aborting on
        the first bad chunk left playlists only partially cleared. Returns True
        only if every chunk succeeded.
        """
        if not entry_ids:
            return True
        chunk_size = 25
        all_ok = True
        for i in range(0, len(entry_ids), chunk_size):
            chunk = entry_ids[i:i + chunk_size]
            params = {"EntryIds": ",".join(chunk)}
            if self.user_id:
                params["UserId"] = self.user_id
            removed = False
            for attempt in range(3):
                try:
                    r = self.session.delete(
                        f"{self.url}/Playlists/{playlist_id}/Items",
                        params=params,
                        timeout=120,
                    )
                    self._check_401(r, f"/Playlists/{playlist_id}/Items")
                    if r.status_code < 400:
                        removed = True
                        break
                    body = r.text[:200] if r.text else "(empty)"
                    logger.warning(f"[Jellyfin] Remove chunk failed (try {attempt + 1}/3) for playlist {playlist_id}: {r.status_code} {body}")
                except requests.HTTPError:
                    raise
                except Exception as e:
                    logger.warning(f"[Jellyfin] Remove chunk error (try {attempt + 1}/3) for playlist {playlist_id}: {e}")
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
            if not removed:
                all_ok = False
                logger.error(f"[Jellyfin] Gave up on a remove chunk for playlist {playlist_id} after 3 tries — continuing with the rest")
        return all_ok

    def get_playlists(self, user_id: str = None) -> List[dict]:
        """List all playlists owned by / visible to the user (name + id + counts)."""
        uid = user_id or self.user_id
        data = self._get("/Items", params={
            "IncludeItemTypes": "Playlist",
            "Recursive": "true",
            "UserId": uid,
            "Fields": "ChildCount",
        })
        if data:
            return data.get("Items", [])
        return []

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

    def wait_for_library_scan(self, expected_count: int = 0, media_type: str = "Movie",
                              max_wait: int = 120, poll_interval: int = 10) -> bool:
        """Wait for a library scan to complete by polling item count.

        Triggers a scan, then polls until item count stabilizes or increases
        past expected_count. Returns True if scan appears complete.
        """
        import time

        self.trigger_library_scan()
        logger.info(f"[Jellyfin] Triggered library scan, waiting for indexing (max {max_wait}s)...")

        # Give Jellyfin a moment to start scanning
        time.sleep(5)

        prev_count = 0
        stable_checks = 0
        elapsed = 5

        while elapsed < max_wait:
            items = self._fetch_all_items(media_type)
            current_count = len(items)

            if current_count == prev_count and current_count > 0:
                stable_checks += 1
                if stable_checks >= 2:
                    logger.info(f"[Jellyfin] Library scan appears complete: {current_count} {media_type} items (stable)")
                    return True
            else:
                stable_checks = 0

            if expected_count > 0 and current_count >= expected_count:
                logger.info(f"[Jellyfin] Library scan complete: {current_count} >= {expected_count} expected {media_type} items")
                return True

            prev_count = current_count
            time.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning(f"[Jellyfin] Library scan wait timed out after {max_wait}s")
        return False

    def get_home_sections(self) -> dict:
        """Read the user's current homesection0-9 display preferences.

        Returns {"homesection0": "resumevideo", ...} ("" for unset slots),
        or {} when the preferences could not be fetched.
        """
        if not self.user_id:
            return {}
        data = self._get("/DisplayPreferences/usersettings", params={"userId": self.user_id, "client": "emby"})
        if not data:
            return {}
        prefs = data.get("CustomPrefs", {}) or {}
        return {f"homesection{i}": prefs.get(f"homesection{i}") or "" for i in range(10)}

    def disable_home_sections(self):
        """Set all Jellyfin home sections to 'none' for the current user.

        This prevents overlap between Tentacle's managed home screen
        and Jellyfin's built-in home sections.

        Returns a snapshot dict of the PREVIOUS homesection values when
        sections were actively disabled (so callers can preserve what the
        user had configured in Jellyfin), {} if they were already disabled,
        or None on failure.
        """
        if not self.user_id:
            return None

        path = "/DisplayPreferences/usersettings"
        params = {"userId": self.user_id, "client": "emby"}
        data = self._get(path, params=params)
        if not data:
            return None

        custom_prefs = data.get("CustomPrefs", {})

        # Check if already all "none"
        already_disabled = all(
            custom_prefs.get(f"homesection{i}") in ("none", "")
            for i in range(10)
        )
        if already_disabled:
            return {}

        # Snapshot the user's configuration before overwriting it
        snapshot = {f"homesection{i}": custom_prefs.get(f"homesection{i}") or "" for i in range(10)}

        # Set all home sections to "none"
        for i in range(10):
            custom_prefs[f"homesection{i}"] = "none"
        data["CustomPrefs"] = custom_prefs

        try:
            r = self.session.post(
                f"{self.url}{path}",
                params=params,
                json=data,
                timeout=15,
            )
            self._check_401(r, path)
            if r.status_code < 400:
                logger.info(f"[Jellyfin] Disabled built-in home sections for user {self.user_id}")
                return snapshot
            logger.warning(f"[Jellyfin] Failed to disable home sections: {r.status_code}")
            return None
        except Exception as e:
            logger.warning(f"[Jellyfin] Failed to disable home sections: {e}")
            return None

    def restore_home_sections(self, snapshot: dict) -> bool:
        """Write saved homesection values back to Jellyfin.

        Only restores when the current values are all 'none'/empty (i.e. the
        blank state Tentacle wrote) — never clobbers settings the user has
        since changed by hand.
        """
        if not self.user_id or not snapshot:
            return False

        path = "/DisplayPreferences/usersettings"
        params = {"userId": self.user_id, "client": "emby"}
        data = self._get(path, params=params)
        if not data:
            return False

        custom_prefs = data.get("CustomPrefs", {}) or {}
        currently_blank = all(
            custom_prefs.get(f"homesection{i}") in ("none", "", None)
            for i in range(10)
        )
        if not currently_blank:
            logger.info(f"[Jellyfin] Home sections were changed manually for user {self.user_id} — not restoring snapshot")
            return False

        for i in range(10):
            custom_prefs[f"homesection{i}"] = snapshot.get(f"homesection{i}") or ""
        data["CustomPrefs"] = custom_prefs

        try:
            r = self.session.post(
                f"{self.url}{path}",
                params=params,
                json=data,
                timeout=15,
            )
            self._check_401(r, path)
            if r.status_code < 400:
                logger.info(f"[Jellyfin] Restored built-in home sections for user {self.user_id}")
                return True
            logger.warning(f"[Jellyfin] Failed to restore home sections: {r.status_code}")
            return False
        except Exception as e:
            logger.warning(f"[Jellyfin] Failed to restore home sections: {e}")
            return False


def sweep_orphaned_downloads(db) -> int:
    """Remove Tentacle DB records for downloaded content no longer in Jellyfin.

    Fetches all Jellyfin movie/series TMDB IDs, then deletes any Tentacle DB
    records with source='radarr' or 'sonarr' whose tmdb_id is missing from
    Jellyfin. Also cleans up associated DownloadRequest records.

    Should run BEFORE the per-user playlist rebuild so rebuilt playlists
    won't reference dead items.
    """
    from models.database import Movie, Series, DownloadRequest, get_setting, log_deletion

    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    jf_uid = get_setting(db, "jellyfin_user_id", "")
    if not jf_url or not jf_key:
        return 0

    jf = JellyfinService(jf_url, jf_key, jf_uid)

    # Fetch all TMDB IDs currently in Jellyfin
    jf_movie_ids = set()
    for item in jf._fetch_all_items("Movie"):
        tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
        if tmdb_id:
            try:
                jf_movie_ids.add(int(tmdb_id))
            except (ValueError, TypeError):
                pass

    jf_series_ids = set()
    for item in jf._fetch_all_items("Series"):
        tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
        if tmdb_id:
            try:
                jf_series_ids.add(int(tmdb_id))
            except (ValueError, TypeError):
                pass

    orphans_removed = 0
    swept_titles = []

    # Check radarr movies
    radarr_movies = db.query(Movie).filter(Movie.source == "radarr").all()
    for movie in radarr_movies:
        if movie.tmdb_id not in jf_movie_ids:
            logger.info(f"[Orphan sweep] Removing orphaned radarr movie: {movie.title} (tmdb:{movie.tmdb_id})")
            swept_titles.append(movie.title)
            db.query(DownloadRequest).filter(
                DownloadRequest.tmdb_id == movie.tmdb_id,
                DownloadRequest.media_type == "movie",
            ).delete()
            db.delete(movie)
            orphans_removed += 1

    # Check sonarr series
    sonarr_series = db.query(Series).filter(Series.source == "sonarr").all()
    for series in sonarr_series:
        if series.tmdb_id not in jf_series_ids:
            logger.info(f"[Orphan sweep] Removing orphaned sonarr series: {series.title} (tmdb:{series.tmdb_id})")
            swept_titles.append(series.title)
            db.query(DownloadRequest).filter(
                DownloadRequest.tmdb_id == series.tmdb_id,
                DownloadRequest.media_type == "series",
            ).delete()
            db.delete(series)
            orphans_removed += 1

    if orphans_removed:
        db.commit()
        log_deletion(db, kind="orphan-sweep", name=f"{orphans_removed} download record(s)", reason="auto",
                     detail="DB records removed — no longer present in Jellyfin: " + ", ".join(swept_titles[:20])
                            + ("…" if len(swept_titles) > 20 else ""))
        logger.info(f"[Orphan sweep] Removed {orphans_removed} orphaned download(s)")

    return orphans_removed


def push_tags_to_jellyfin(db, log_prefix: str = "Pipeline") -> int:
    """Push tags from Tentacle DB to Jellyfin for all movies and series.

    Shared helper used by VOD sync, nightly sync, and refresh-tags.
    Returns total number of items successfully tagged.
    """
    from models.database import Movie, Series, get_setting

    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    jf_uid = get_setting(db, "jellyfin_user_id", "")
    if not jf_url or not jf_key:
        logger.info(f"[{log_prefix}] Jellyfin not configured — skipping tag push")
        return 0

    jf = JellyfinService(jf_url, jf_key, jf_uid)
    jf_tagged = 0

    # Push movie tags
    jf_movie_lookup, jf_movie_title_lookup = jf.get_tmdb_lookup_with_fallback("Movie")
    for movie in db.query(Movie).filter(Movie.tags.isnot(None)).all():
        try:
            jf_item = jf_movie_lookup.get(movie.tmdb_id)
            if not jf_item and movie.title:
                norm = JellyfinService._normalize_title(movie.title)
                jf_item = jf_movie_title_lookup.get((norm, str(movie.year or "")))
                if not jf_item:
                    jf_item = jf_movie_title_lookup.get((norm, ""))
            if jf_item:
                existing_tags = set(jf_item.get("Tags", []))
                desired_tags = set(movie.tags)
                if not desired_tags.issubset(existing_tags):
                    merged = list(existing_tags | desired_tags)
                    if jf.set_item_tags(jf_item["Id"], merged):
                        jf_tagged += 1
        except Exception:
            pass

    # Push series tags
    jf_series_lookup, jf_series_title_lookup = jf.get_tmdb_lookup_with_fallback("Series")
    for series in db.query(Series).filter(Series.tags.isnot(None)).all():
        try:
            jf_item = jf_series_lookup.get(series.tmdb_id)
            if not jf_item and series.title:
                norm = JellyfinService._normalize_title(series.title)
                jf_item = jf_series_title_lookup.get((norm, str(series.year or "")))
                if not jf_item:
                    jf_item = jf_series_title_lookup.get((norm, ""))
            if jf_item:
                existing_tags = set(jf_item.get("Tags", []))
                desired_tags = set(series.tags)
                if not desired_tags.issubset(existing_tags):
                    merged = list(existing_tags | desired_tags)
                    if jf.set_item_tags(jf_item["Id"], merged):
                        jf_tagged += 1
        except Exception:
            pass

    logger.info(f"[{log_prefix}] Pushed tags to Jellyfin for {jf_tagged} items")
    return jf_tagged


def run_full_jellyfin_pipeline(db, log_prefix: str = "Pipeline") -> dict:
    """Run the complete Jellyfin integration pipeline after content changes.

    1. Trigger Jellyfin library scan (so new .strm files are indexed)
    2. Wait for scan to complete
    3. Push tags via API
    4. Refresh SmartList playlists
    5. Write home config

    Returns stats dict.
    """
    from models.database import get_setting, set_setting, log_activity
    from datetime import datetime

    stats = {"library_scan": False, "tags_pushed": 0, "playlists_refreshed": False}

    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    jf_uid = get_setting(db, "jellyfin_user_id", "")

    if not jf_url or not jf_key:
        logger.info(f"[{log_prefix}] Jellyfin not configured — skipping pipeline")
        return stats

    # Step 1+2: Library scan + wait
    jf = JellyfinService(jf_url, jf_key, jf_uid)
    try:
        stats["library_scan"] = jf.wait_for_library_scan(max_wait=120)
    except Exception as e:
        logger.warning(f"[{log_prefix}] Library scan failed: {e}")

    # Step 3: Push tags
    try:
        from services.tagger import refresh_recently_added_tags
        refresh_recently_added_tags(db)

        tagged = push_tags_to_jellyfin(db, log_prefix)
        stats["tags_pushed"] = tagged
        if tagged > 0:
            set_setting(db, "last_jellyfin_push", datetime.utcnow().isoformat())
            log_activity(db, "jellyfin_push", f"Pushed tags to Jellyfin — {tagged} items updated")
    except Exception as e:
        logger.error(f"[{log_prefix}] Tag push failed: {e}")

    # Step 4: Refresh playlist contents only (don't rebuild configs or home layout)
    try:
        from services.smartlists import refresh_smartlist_playlists
        refresh_smartlist_playlists(db)
        stats["playlists_refreshed"] = True
        logger.info(f"[{log_prefix}] Playlist contents refreshed")
    except Exception as e:
        logger.warning(f"[{log_prefix}] Playlist refresh failed: {e}")

    return stats

"""
Tentacle - TMDB Service
Full metadata fetching with caching.
Ported and expanded from xtream_to_jellyfin.py
"""

import re
import json
import hashlib
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Optional, Tuple, Dict, Any
from services.exceptions import TMDBConnectionError

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"

# Built-in TMDB API key for Tentacle (read-only, non-commercial open-source use)
# Users can override this in Settings with their own token
TMDB_DEFAULT_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiIzYTExMzFlMWZlOGQxODdiMzI2OGZkY2RkOWZjYWU0ZiIsIm5iZiI6MTc3NDU2MTU0Ny4zOSwic3ViIjoiNjljNWE5MGJjM2JjMTcwYjg5YWQ4OThjIiwic2NvcGVzIjpbImFwaV9yZWFkIl0sInZlcnNpb24iOjF9._Fi__rAY1Qd5PcpA0KNF6iN1mXLI0Rs2AvDZwmW53Rw"


def get_tmdb_token(db) -> str:
    """Get TMDB token: user override from settings, or built-in default."""
    from models.database import get_setting
    token = get_setting(db, "tmdb_bearer_token")
    return token if token else TMDB_DEFAULT_TOKEN


class TMDBService:
    def __init__(self, bearer_token: str, cache_dir: str, match_threshold: float = 0.7):
        self.bearer_token = bearer_token
        self.match_threshold = match_threshold
        self.cache_dir = Path(cache_dir) / "tmdb_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json"
        })
        self.enabled = bool(bearer_token)

    # ── Cache ──────────────────────────────────────────────────────────────

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{hashlib.md5(key.encode()).hexdigest()}.json"

    def _cache_get(self, key: str, ttl_seconds: int = 30 * 86400) -> Optional[Any]:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            if data.get("ts", 0) > datetime.now().timestamp() - ttl_seconds:
                return data.get("v")
        except Exception:
            pass
        return None

    def _cache_set(self, key: str, value: Any):
        try:
            self._cache_path(key).write_text(
                json.dumps({"ts": datetime.now().timestamp(), "v": value})
            )
        except Exception:
            pass

    def cleanup_cache(self):
        """Remove expired cache files (older than 30 days)"""
        if not self.cache_dir.exists():
            return 0
        cutoff = datetime.now().timestamp() - (30 * 86400)
        removed = 0
        for path in self.cache_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("ts", 0) < cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                path.unlink(missing_ok=True)
                removed += 1
        logger.info(f"TMDB cache cleanup: removed {removed} expired entries")
        return removed

    # ── API ────────────────────────────────────────────────────────────────

    def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        if not self.enabled:
            return None
        try:
            r = self.session.get(
                f"{TMDB_BASE}/{endpoint}",
                params=params or {},
                timeout=10
            )
            r.raise_for_status()
            return r.json()
        except requests.ConnectionError as e:
            raise TMDBConnectionError(f"Cannot reach TMDB API: {e}")
        except requests.HTTPError as e:
            logger.warning(f"TMDB HTTP error {endpoint}: {e}")
            return None
        except Exception as e:
            logger.debug(f"TMDB request failed {endpoint}: {e}")
            return None

    # ── Matching ───────────────────────────────────────────────────────────

    def _similarity(self, a: str, b: str) -> float:
        a = re.sub(r'[^a-z0-9]', '', a.lower())
        b = re.sub(r'[^a-z0-9]', '', b.lower())
        return SequenceMatcher(None, a, b).ratio()

    def _find_best_match(self, results: list, title: str, year: Optional[str],
                          title_key: str, year_key: str) -> Optional[Tuple]:
        best = None
        best_score = 0

        for result in results[:10]:
            tmdb_title = result.get(title_key, '')
            tmdb_year_raw = result.get(year_key, '')
            tmdb_year = tmdb_year_raw[:4] if tmdb_year_raw else None

            score = self._similarity(title, tmdb_title)
            if year and tmdb_year and year == tmdb_year:
                score += 0.15
            if result.get('popularity', 0) > 50:
                score += 0.05
            score = min(score, 1.0)

            if score > best_score:
                best_score = score
                best = (result.get('id'), tmdb_title, tmdb_year, score)

        if best and best_score >= self.match_threshold:
            return best
        return None

    # ── Search ─────────────────────────────────────────────────────────────

    def search_movie(self, title: str, year: Optional[str] = None) -> Optional[dict]:
        """Search for movie, return full metadata dict or None"""
        if not self.enabled:
            return None

        cache_key = f"movie_search:{title.lower()}:{year}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached if cached else None

        # Try with year, then without
        for params in [
            {"query": title, "year": year} if year else {"query": title},
            {"query": title}
        ]:
            data = self._request("search/movie", params)
            if data and data.get("results"):
                match = self._find_best_match(
                    data["results"], title, year, "title", "release_date"
                )
                if match:
                    tmdb_id, tmdb_title, tmdb_year, score = match
                    # Fetch full details
                    full = self.get_movie_details(tmdb_id)
                    self._cache_set(cache_key, full)
                    return full
            if year:
                break  # Already tried without year in second iteration

        self._cache_set(cache_key, None)
        return None

    def search_series(self, title: str, year: Optional[str] = None) -> Optional[dict]:
        """Search for series, return full metadata dict or None"""
        if not self.enabled:
            return None

        cache_key = f"series_search:{title.lower()}:{year}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached if cached else None

        for params in [
            {"query": title, "first_air_date_year": year} if year else {"query": title},
            {"query": title}
        ]:
            data = self._request("search/tv", params)
            if data and data.get("results"):
                match = self._find_best_match(
                    data["results"], title, year, "name", "first_air_date"
                )
                if match:
                    tmdb_id, tmdb_title, tmdb_year, score = match
                    full = self.get_series_details(tmdb_id)
                    self._cache_set(cache_key, full)
                    return full
            if year:
                break

        self._cache_set(cache_key, None)
        return None

    # ── Full Details ───────────────────────────────────────────────────────

    def get_movie_details(self, tmdb_id: int) -> Optional[dict]:
        cache_key = f"movie_details:{tmdb_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        data = self._request(f"movie/{tmdb_id}", {"append_to_response": "credits"})
        if not data:
            return None

        result = {
            "tmdb_id": data.get("id"),
            "title": data.get("title", ""),
            "year": data.get("release_date", "")[:4] if data.get("release_date") else None,
            "overview": data.get("overview", ""),
            "runtime": data.get("runtime"),
            "rating": round(data.get("vote_average", 0), 1),
            "vote_count": data.get("vote_count", 0),
            "genres": [g["name"] for g in data.get("genres", [])],
            "poster_path": data.get("poster_path"),
            "backdrop_path": data.get("backdrop_path"),
            "imdb_id": data.get("imdb_id"),
            "tagline": data.get("tagline", ""),
            "status": data.get("status", ""),
            "original_language": data.get("original_language", ""),
            "cast": [
                {"name": c["name"], "character": c["character"]}
                for c in data.get("credits", {}).get("cast", [])[:10]
            ],
            "directors": [
                c["name"] for c in data.get("credits", {}).get("crew", [])
                if c.get("job") == "Director"
            ][:3],
            "studios": [s["name"] for s in data.get("production_companies", [])[:3]],
            "media_type": "movie",
        }

        self._cache_set(cache_key, result)
        return result

    def get_series_details(self, tmdb_id: int) -> Optional[dict]:
        cache_key = f"series_details:{tmdb_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        data = self._request(f"tv/{tmdb_id}", {"append_to_response": "credits"})
        if not data:
            return None

        result = {
            "tmdb_id": data.get("id"),
            "title": data.get("name", ""),
            "year": data.get("first_air_date", "")[:4] if data.get("first_air_date") else None,
            "overview": data.get("overview", ""),
            "rating": round(data.get("vote_average", 0), 1),
            "vote_count": data.get("vote_count", 0),
            "genres": [g["name"] for g in data.get("genres", [])],
            "poster_path": data.get("poster_path"),
            "backdrop_path": data.get("backdrop_path"),
            "status": data.get("status", ""),
            "number_of_seasons": data.get("number_of_seasons"),
            "number_of_episodes": data.get("number_of_episodes"),
            "original_language": data.get("original_language", ""),
            "cast": [
                {"name": c["name"], "character": c["character"]}
                for c in data.get("credits", {}).get("cast", [])[:10]
            ],
            "creators": [c["name"] for c in data.get("created_by", [])[:3]],
            "studios": [s["name"] for s in data.get("production_companies", [])[:3]],
            "media_type": "series",
        }

        self._cache_set(cache_key, result)
        return result

    def find_by_imdb_id(self, imdb_id: str) -> Optional[dict]:
        """Look up a movie or series by IMDb ID using TMDB's /find endpoint.

        Returns the same metadata dict as get_movie_details/get_series_details,
        or None if not found.
        """
        if not self.enabled or not imdb_id:
            return None

        cache_key = f"find_imdb:{imdb_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached if cached else None

        data = self._request(f"find/{imdb_id}", {"external_source": "imdb_id"})
        if not data:
            self._cache_set(cache_key, None)
            return None

        # Check movie results first, then TV
        movie_results = data.get("movie_results", [])
        if movie_results:
            tmdb_id = movie_results[0].get("id")
            if tmdb_id:
                result = self.get_movie_details(tmdb_id)
                self._cache_set(cache_key, result)
                return result

        tv_results = data.get("tv_results", [])
        if tv_results:
            tmdb_id = tv_results[0].get("id")
            if tmdb_id:
                result = self.get_series_details(tmdb_id)
                self._cache_set(cache_key, result)
                return result

        self._cache_set(cache_key, None)
        return None

    # ── Trending / Discover ──────────────────────────────────────────────

    def get_trending(self, media_type: str = "movie", page: int = 1) -> list:
        """Fetch trending movies or TV shows for the week. Cached for 2 hours."""
        if not self.enabled:
            return []

        cache_key = f"trending:{media_type}:{page}"
        cached = self._cache_get(cache_key, ttl_seconds=7200)
        if cached is not None:
            return cached

        endpoint = f"trending/{'tv' if media_type == 'series' else 'movie'}/week"
        data = self._request(endpoint, {"page": page, "language": "en-US"})
        result = self._parse_results(data, media_type)
        if result:
            self._cache_set(cache_key, result)
            logger.info(f"TMDB trending {media_type} page {page}: {len(result)} items")
        return result

    def _parse_results(self, data: dict, media_type: str) -> list:
        """Parse TMDB results into our standard item format."""
        if not data or not data.get("results"):
            return []
        is_tv = media_type == "series"
        result = []
        for item in data["results"]:
            result.append({
                "tmdb_id": item["id"],
                "title": item.get("name", "") if is_tv else item.get("title", ""),
                "year": (item.get("first_air_date" if is_tv else "release_date") or "")[:4],
                "overview": item.get("overview", ""),
                "rating": round(item.get("vote_average", 0), 1),
                "poster_path": item.get("poster_path"),
                "backdrop_path": item.get("backdrop_path"),
                "media_type": media_type,
            })
        return result

    def get_popular(self, media_type: str = "movie", page: int = 1) -> list:
        """Fetch popular movies or TV shows. Cached for 6 hours."""
        if not self.enabled:
            return []

        cache_key = f"popular:{media_type}:{page}"
        cached = self._cache_get(cache_key, ttl_seconds=21600)
        if cached is not None:
            return cached

        endpoint = f"{'tv' if media_type == 'series' else 'movie'}/popular"
        data = self._request(endpoint, {"page": page, "language": "en-US"})
        result = self._parse_results(data, media_type)
        if result:
            self._cache_set(cache_key, result)
            logger.info(f"TMDB popular {media_type} page {page}: {len(result)} items")
        return result

    def get_now_playing(self, page: int = 1) -> list:
        """Fetch movies currently in theaters. Cached for 6 hours."""
        if not self.enabled:
            return []

        cache_key = f"now_playing:{page}"
        cached = self._cache_get(cache_key, ttl_seconds=21600)
        if cached is not None:
            return cached

        data = self._request("movie/now_playing", {
            "page": page,
            "language": "en-US",
            "region": "US",
        })
        result = self._parse_results(data, "movie")
        if result:
            self._cache_set(cache_key, result)
            logger.info(f"TMDB now_playing page {page}: {len(result)} items")
        return result

    def get_upcoming(self, page: int = 1) -> list:
        """Fetch upcoming movies not yet released. Cached for 6 hours.
        Filters out movies with release dates more than 14 days in the past."""
        if not self.enabled:
            return []

        cache_key = f"upcoming_v3:{page}"
        cached = self._cache_get(cache_key, ttl_seconds=21600)
        if cached is not None:
            return cached

        data = self._request("movie/upcoming", {
            "page": page,
            "language": "en-US",
            "region": "US",
        })
        result = self._parse_results(data, "movie")
        # Filter out old movies — only keep items releasing within 14 days ago or in the future
        if data and data.get("results"):
            date_lookup = {
                item["id"]: (item.get("release_date") or "")
                for item in data["results"]
            }
            cutoff_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
            result = [
                item for item in result
                if date_lookup.get(item["tmdb_id"], "") >= cutoff_date
                or not date_lookup.get(item["tmdb_id"], "")
            ]
        if result:
            self._cache_set(cache_key, result)
            logger.info(f"TMDB upcoming page {page}: {len(result)} items")
        return result

    def get_on_the_air(self, page: int = 1) -> list:
        """Fetch TV shows currently on the air. Cached for 6 hours."""
        if not self.enabled:
            return []

        cache_key = f"on_the_air:{page}"
        cached = self._cache_get(cache_key, ttl_seconds=21600)
        if cached is not None:
            return cached

        data = self._request("tv/on_the_air", {
            "page": page,
            "language": "en-US",
        })
        result = self._parse_results(data, "series")
        if result:
            self._cache_set(cache_key, result)
            logger.info(f"TMDB on_the_air page {page}: {len(result)} items")
        return result

    def get_top_rated(self, media_type: str = "series", page: int = 1) -> list:
        """Fetch top rated TV shows. Cached for 6 hours."""
        if not self.enabled:
            return []

        cache_key = f"top_rated:{media_type}:{page}"
        cached = self._cache_get(cache_key, ttl_seconds=21600)
        if cached is not None:
            return cached

        endpoint = f"{'tv' if media_type == 'series' else 'movie'}/top_rated"
        data = self._request(endpoint, {
            "page": page,
            "language": "en-US",
        })
        result = self._parse_results(data, media_type)
        if result:
            self._cache_set(cache_key, result)
            logger.info(f"TMDB top_rated {media_type} page {page}: {len(result)} items")
        return result

    def search_multi_results(self, query: str, media_type: str = "all") -> list:
        """Search TMDB and return multiple results in standard item format.
        media_type: 'all', 'movie', or 'series'."""
        if not self.enabled or not query or not query.strip():
            return []

        results = []
        if media_type in ("all", "movie"):
            data = self._request("search/movie", {"query": query, "language": "en-US"})
            results.extend(self._parse_results(data, "movie"))
        if media_type in ("all", "series"):
            data = self._request("search/tv", {"query": query, "language": "en-US"})
            results.extend(self._parse_results(data, "series"))

        return results

    def poster_url(self, path: str, size: str = "w500") -> Optional[str]:
        if not path:
            return None
        return f"{TMDB_IMAGE_BASE}/{size}{path}"

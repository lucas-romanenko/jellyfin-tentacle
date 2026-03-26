"""
Tentacle - Custom Exceptions
Structured error types for better error handling across services.
"""


class TentacleError(Exception):
    """Base exception for all Tentacle errors"""
    pass


class ProviderConnectionError(TentacleError):
    """Failed to connect to an IPTV provider"""
    def __init__(self, provider_name: str, detail: str = ""):
        self.provider_name = provider_name
        super().__init__(f"Provider '{provider_name}' connection failed: {detail}")


class TMDBMatchError(TentacleError):
    """TMDB lookup or matching failed"""
    def __init__(self, title: str, detail: str = ""):
        self.title = title
        super().__init__(f"TMDB match failed for '{title}': {detail}")


class TMDBConnectionError(TentacleError):
    """Failed to connect to TMDB API"""
    pass


class SyncError(TentacleError):
    """Error during sync operation"""
    pass


class SyncCancelledError(TentacleError):
    """Sync was cancelled by user"""
    pass


class JellyfinConnectionError(TentacleError):
    """Failed to connect to Jellyfin"""
    pass


class RadarrConnectionError(TentacleError):
    """Failed to connect to Radarr"""
    pass


class SonarrConnectionError(TentacleError):
    """Failed to connect to Sonarr"""
    pass

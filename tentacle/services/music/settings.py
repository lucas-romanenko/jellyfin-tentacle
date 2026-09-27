"""Every music setting, with its default, in one place.

The module is off by default and changes nothing until it is turned on. Every
setup-specific value (addresses, folders, profiles, the original-release rule)
is a setting here, never hard-coded elsewhere.
"""
import secrets

# No models.database import at module level: models.database reads DEFAULTS
# and NON_EMPTY from here while it is being imported.

DEFAULTS = {
    # The module itself (Settings → Music)
    "music_enabled": "false",
    # Original-release rule
    "music_preferred_countries": "US,GB,CA,XW",  # XW = MusicBrainz's "[Worldwide]"
    "music_preferred_formats": "CD,Digital Media",
    "music_edition_words": "deluxe,expanded,anniversary,bonus,live,mono,stereo,box,collector,legacy,super",
    # Reconcile: a daily pass over the library. Every category goes to review
    # until the user turns on "apply automatically" for it.
    "music_reconcile_time": "04:00",
    "music_auto_repin": "false",           # re-pin
    "music_auto_repin_trim": "false",      # re-pin + remove extra tracks
    "music_auto_repin_download": "false",  # re-pin + download missing tracks
    # Lidarr (Settings → Connections)
    "lidarr_url": "",
    "lidarr_api_key": "",
    "lidarr_root_folder": "",
    "lidarr_quality_profile_id": "",
    "lidarr_metadata_profile_id": "",
    # Navidrome
    "navidrome_enabled": "false",
    "navidrome_url": "",
    "navidrome_public_url": "",   # optional: the address for "Open in Navidrome" links
    "navidrome_username": "",
    "navidrome_password": "",
    "navidrome_upload_artist_images": "true",
    "navidrome_rescan_after_import": "true",
    # Jellyfin as a music player (reuses Tentacle's Jellyfin connection)
    "jellyfin_music_enabled": "false",
    "jellyfin_music_library_id": "",
    "jellyfin_music_set_artist_images": "true",
    # MusicBrainz (its rules require a contact in the User-Agent)
    "musicbrainz_contact": "",
    "musicbrainz_cache_days": "30",
    # Deezer as a source of artist pictures (no key needed)
    "deezer_enabled": "true",
    # Optional, read-only: Lidarr's root folder as Tentacle sees it, so an
    # existing artist.jpg can be reused. Empty = skip artist.jpg, use Deezer.
    "music_library_path": "",
}

# Parsed by their readers; an empty value means the default, not "nothing".
NON_EMPTY = {
    "music_reconcile_time": DEFAULTS["music_reconcile_time"],
    "musicbrainz_cache_days": DEFAULTS["musicbrainz_cache_days"],
}

# Masked by GET /api/settings, like the other keys and passwords.
SECRET_KEYS = {"lidarr_api_key", "navidrome_password", "music_webhook_secret"}

WEBHOOK_PATH = "/api/music/webhook"

# What to tick in Lidarr → Settings → Connect → + → Webhook.
LIDARR_WEBHOOK_TRIGGERS = [
    ("onArtistAdd", "On Artist Add"),
    ("onReleaseImport", "On Release Import"),
    ("onUpgrade", "On Upgrade"),
]


def is_enabled(db) -> bool:
    from models.database import get_setting
    return (get_setting(db, "music_enabled", "false") or "").lower() == "true"


def ensure_webhook_secret(db) -> str:
    from models.database import get_setting
    secret = get_setting(db, "music_webhook_secret")
    if not secret:
        secret = new_webhook_secret(db)
    return secret


def new_webhook_secret(db) -> str:
    from models.database import set_setting
    secret = secrets.token_urlsafe(24)
    set_setting(db, "music_webhook_secret", secret)
    return secret


def webhook_base_url(db) -> str:
    """Where Lidarr can reach Tentacle: Tentacle's own address if set, else the Radarr webhook host."""
    from models.database import get_setting
    base = (get_setting(db, "youtube_base_url") or "").strip().rstrip("/")
    if base:
        return base
    host = (get_setting(db, "webhook_host") or "").strip()
    if host:
        host = host.split("://", 1)[-1].rstrip("/")
        return f"http://{host}"
    return ""

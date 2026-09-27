"""
Tentacle - Database Models
SQLAlchemy models for all entities
"""

from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, Float,
    DateTime, Text, JSON, ForeignKey, UniqueConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os
import logging
import sqlite3
import secrets

logger = logging.getLogger(__name__)

_data_dir = os.getenv('DATA_DIR', '/data')
_db_name = "tentacle.db"
if not os.path.exists(os.path.join(_data_dir, _db_name)) and os.path.exists(os.path.join(_data_dir, "mediahub.db")):
    _db_name = "mediahub.db"
DATABASE_URL = f"sqlite:///{_data_dir}/{_db_name}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30},
                       pool_size=10, max_overflow=20, pool_timeout=60)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# Enable WAL mode for concurrent read/write access (critical for background sync threads)
from sqlalchemy import event

@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── Settings ─────────────────────────────────────────────────────────────────

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── Providers ────────────────────────────────────────────────────────────────

class Provider(Base):
    __tablename__ = "providers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    provider_type = Column(String, default="xtream")  # xtream, m3u_url, m3u_file
    server_url = Column(String, nullable=False)
    username = Column(String, nullable=False)
    password = Column(String, nullable=False)
    active = Column(Boolean, default=False)
    priority = Column(Integer, default=1)  # Lower = higher priority
    status = Column(String, default="untested")  # untested, ok, error
    last_tested = Column(DateTime, nullable=True)
    expiry = Column(DateTime, nullable=True)
    max_connections = Column(Integer, nullable=True)

    # Live TV settings
    m3u_url = Column(String, nullable=True)  # For m3u_url provider type
    epg_url = Column(String, nullable=True)  # Override EPG/XMLTV URL
    user_agent = Column(String, default="TiviMate/4.7.0 (Linux; Android 12)")
    live_tv_enabled = Column(Boolean, default=False)
    last_live_sync = Column(DateTime, nullable=True)

    # Auto-detected capabilities
    has_live = Column(Boolean, default=False)
    has_vod = Column(Boolean, default=False)
    has_series = Column(Boolean, default=False)
    require_tmdb_match = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    categories = relationship("ProviderCategory", back_populates="provider", cascade="all, delete-orphan")
    sync_runs = relationship("SyncRun", back_populates="provider", cascade="all, delete-orphan")
    live_channels = relationship("LiveChannel", back_populates="provider", cascade="all, delete-orphan")


# ─── Provider Categories ──────────────────────────────────────────────────────

class ProviderCategory(Base):
    __tablename__ = "provider_categories"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False, index=True)
    category_id = Column(String, nullable=False)  # Provider's internal ID
    category_name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # movie | series
    whitelisted = Column(Boolean, default=False)
    source_tag = Column(String, nullable=True)  # Netflix, Amazon, etc.
    title_count = Column(Integer, default=0)
    last_seen = Column(DateTime, default=datetime.utcnow)
    last_sync_matched = Column(Integer, nullable=True)  # Items matched TMDB last sync
    last_sync_skipped = Column(Integer, nullable=True)  # Items with no TMDB match last sync
    # Consecutive syncs where this category returned nothing while it was known
    # to hold titles. title_count keeps its last non-zero value throughout, so a
    # multi-night outage can't be mistaken for a genuinely emptied category.
    consecutive_empty_syncs = Column(Integer, default=0)

    provider = relationship("Provider", back_populates="categories")
    snapshots = relationship("CategorySnapshot", back_populates="category", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("provider_id", "category_id", "type", name="uq_provider_category"),
    )


# ─── Category Snapshots (for graphs) ─────────────────────────────────────────

class CategorySnapshot(Base):
    __tablename__ = "category_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    category_id = Column(Integer, ForeignKey("provider_categories.id"), nullable=False)
    title_count = Column(Integer, default=0)
    new_count = Column(Integer, default=0)
    removed_count = Column(Integer, default=0)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    category = relationship("ProviderCategory", back_populates="snapshots")


# ─── Movies ───────────────────────────────────────────────────────────────────

class Movie(Base):
    __tablename__ = "movies"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, unique=True, nullable=False)
    title = Column(String, nullable=False)
    year = Column(String, nullable=True)
    overview = Column(Text, nullable=True)
    runtime = Column(Integer, nullable=True)
    rating = Column(Float, nullable=True)
    genres = Column(JSON, default=list)  # ["Action", "Drama"]
    poster_path = Column(String, nullable=True)
    backdrop_path = Column(String, nullable=True)

    # Source info
    source = Column(String, nullable=False, index=True)  # "radarr" | "provider_{id}"
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=True, index=True)
    strm_path = Column(String, nullable=True)
    nfo_path = Column(String, nullable=True)
    radarr_path = Column(String, nullable=True)

    # Tags
    source_tag = Column(String, nullable=True)  # Netflix, Amazon etc
    tags = Column(JSON, default=list)  # All tags applied

    # Jellyfin
    jellyfin_item_id = Column(String, nullable=True)

    # First time each guard found this title gone. Deletion only happens once a
    # second, independent run of the SAME guard agrees it is still gone, so a
    # transient provider or mount outage can never destroy the library.
    #
    # The two are deliberately separate. They mean different things — "the
    # provider stopped listing it" vs "its .strm is not on disk" — and sharing
    # one column made them corrupt each other: the sweep cleared the prune's
    # mark every night (so a genuinely removed title was never pruned), and a
    # mark from one guard satisfied the other's second strike (so a title could
    # be deleted after a single night).
    provider_missing_since = Column(DateTime, nullable=True)
    file_missing_since = Column(DateTime, nullable=True)

    # "Keep this in the catalog but stop writing/repairing .strm files for it."
    # For titles the user has deliberately switched to downloaded copies — the
    # sync would otherwise regenerate the .strm every night and, in a merged
    # folder, leave two sources competing.
    strm_disabled = Column(Boolean, default=False)

    # Dates
    date_added = Column(DateTime, default=datetime.utcnow)
    # When the downloaded copy arrived (Radarr's file import date / Sonarr's
    # latest episode import). date_added is when the title first entered the
    # library, which for a title that was VOD first is months earlier — sorting
    # "Downloaded Movies" by it buried every such download mid-row.
    downloaded_at = Column(DateTime, nullable=True)
    date_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── Series ───────────────────────────────────────────────────────────────────

class Series(Base):
    __tablename__ = "series"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, unique=True, nullable=False)
    title = Column(String, nullable=False)
    year = Column(String, nullable=True)
    overview = Column(Text, nullable=True)
    genres = Column(JSON, default=list)
    poster_path = Column(String, nullable=True)
    backdrop_path = Column(String, nullable=True)
    rating = Column(Float, nullable=True)
    status = Column(String, nullable=True)  # Continuing, Ended etc

    source = Column(String, nullable=False, index=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=True, index=True)
    strm_path = Column(String, nullable=True)
    nfo_path = Column(String, nullable=True)
    sonarr_path = Column(String, nullable=True)

    source_tag = Column(String, nullable=True)
    tags = Column(JSON, default=list)
    sonarr_monitored = Column(Boolean, default=False)  # True = following for new episodes

    # Jellyfin
    jellyfin_item_id = Column(String, nullable=True)
    last_downloaded_episode = Column(String, nullable=True)  # e.g. "S02E05 · Episode Title"

    # See Movie.provider_missing_since / file_missing_since — each guard keeps
    # its own mark and deletion requires two runs of that guard to agree.
    provider_missing_since = Column(DateTime, nullable=True)
    file_missing_since = Column(DateTime, nullable=True)

    # See Movie.strm_disabled — opt this title out of .strm writing/repair.
    strm_disabled = Column(Boolean, default=False)

    date_added = Column(DateTime, default=datetime.utcnow)
    # When the downloaded copy arrived (Radarr's file import date / Sonarr's
    # latest episode import). date_added is when the title first entered the
    # library, which for a title that was VOD first is months earlier — sorting
    # "Downloaded Movies" by it buried every such download mid-row.
    downloaded_at = Column(DateTime, nullable=True)
    date_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── YouTube channels as a source ────────────────────────────────────────────
# Deliberately separate from Movie/Series. Those tables carry a unique, non-null
# tmdb_id and the VOD sync runs TMDB matching over them, so a video called
# "Frozen" would be imported as the real film and share its media folder — and
# the prune would then delete the IPTV movie. YouTube content also lives under
# its own media root so nothing else walks it.

class YouTubeChannel(Base):
    __tablename__ = "youtube_channels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    input_url = Column(String, nullable=False)          # what the user pasted
    kind = Column(String, default="channel")            # channel | playlist
    channel_id = Column(String, nullable=True, index=True)   # UC…
    handle = Column(String, nullable=True)              # @handle
    playlist_id = Column(String, nullable=True)
    title = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)  # filesystem-safe, used in tags
    avatar_url = Column(String, nullable=True)
    banner_url = Column(String, nullable=True)

    # What to index. The one setting a user chooses is keep_count — "keep the
    # newest N" — which drives both how far back the listing is read and how
    # many videos survive retention. The rest are derived or advanced.
    include_videos = Column(Boolean, default=True)
    # Finished broadcasts. Set automatically on add: on for a channel with no
    # uploads (it only ever streams), off otherwise. Not a user-facing choice.
    include_streams = Column(Boolean, default=False)
    include_shorts = Column(Boolean, default=False)
    min_duration = Column(Integer, default=0)           # seconds; 0 = no length filter
    keep_count = Column(Integer, nullable=True, default=10)
    keep_days = Column(Integer, nullable=True)
    max_height = Column(Integer, default=1080)

    # Jellyfin-side metadata
    rating = Column(String, nullable=True)              # NFO <mpaa>, for parental controls
    extra_tags = Column(JSON, default=list)             # NFO <tag>, for allowed-tags policies

    enabled = Column(Boolean, default=True)
    # Expose this channel's live/upcoming streams as a Live TV channel.
    live_enabled = Column(Boolean, default=False)
    channel_number = Column(String, nullable=True)
    last_checked = Column(DateTime, nullable=True)
    # The last time the channel's tabs were listed in full. A scheduled check
    # reads the channel's feed first and lists the tabs only when something is
    # new, or when this is a day old (services.youtube.indexer._light_check).
    last_full_check = Column(DateTime, nullable=True)
    # The ids the feed showed when the tabs were last listed: already dealt
    # with, so the next feed check reacts only to ids beyond these.
    feed_ids = Column(JSON, nullable=True)
    last_error = Column(String, nullable=True)
    error_count = Column(Integer, default=0)
    # Why videos were passed over on the last index, as {reason: count}. Skips
    # were silent, so a setting quietly excluding every upload (a min duration
    # above the channel's typical video length, say) looked like nothing had
    # been indexed at all.
    last_skips = Column(JSON, default=dict)
    last_indexed_count = Column(Integer, default=0)
    # What each listing tab returned on the last index, e.g. {"videos": 12,
    # "streams": 6}. Separates "YouTube listed nothing" from "settings excluded
    # everything" — opposite problems that look identical from outside.
    last_listing = Column(JSON, default=dict)
    # Set when YouTube asks us to prove we're not a bot. Indexing backs off
    # until this passes rather than hammering and making it worse.
    blocked_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    videos = relationship("YouTubeVideo", back_populates="channel", cascade="all, delete-orphan")


class YouTubeVideo(Base):
    __tablename__ = "youtube_videos"
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_fk = Column(Integer, ForeignKey("youtube_channels.id"), nullable=False, index=True)
    video_id = Column(String, unique=True, nullable=False, index=True)  # 11 chars
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    published_at = Column(DateTime, nullable=True)
    duration = Column(Integer, nullable=True)           # seconds
    live_status = Column(String, nullable=True)
    media_type = Column(String, default="video")        # video | livestream
    thumbnail_url = Column(String, nullable=True)
    is_made_for_kids = Column(Boolean, nullable=True)   # unknown until details are fetched

    folder_path = Column(String, nullable=True)
    strm_path = Column(String, nullable=True)

    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    removed_at = Column(DateTime, nullable=True)
    # When to read this video's details again, and how many reads failed in a
    # row: a video that could not be read (members-only, private, a hiccup) is
    # retried later, not on every check; a live or upcoming stream the listing no
    # longer shows is re-read at most this often.
    next_check_at = Column(DateTime, nullable=True)
    check_failures = Column(Integer, default=0)
    # Why a video was passed over, recorded on the row so it is never detailed
    # again. Details are rate-limited to one every few seconds, and without this
    # a channel's excluded back catalogue was re-fetched on every single
    # refresh — minutes of work per run, to reach the same answer.
    skip_reason = Column(String, nullable=True)

    channel = relationship("YouTubeChannel", back_populates="videos")


# ─── Duplicates ───────────────────────────────────────────────────────────────

class Duplicate(Base):
    __tablename__ = "duplicates"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String, nullable=False)  # movie | series
    sources = Column(JSON, default=list)  # [{"source": "radarr", "path": "..."}, ...]
    resolution = Column(String, default="pending")  # pending | keep_radarr | keep_provider_1 | keep_both
    detected_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)


# ─── Sync Runs ────────────────────────────────────────────────────────────────

class SyncRun(Base):
    __tablename__ = "sync_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False, index=True)
    status = Column(String, default="running", index=True)  # running | completed | failed
    sync_type = Column(String, default="full")  # full | movies | series

    # Totals
    movies_new = Column(Integer, default=0)
    movies_existing = Column(Integer, default=0)
    movies_failed = Column(Integer, default=0)
    movies_skipped = Column(Integer, default=0)  # No TMDB match
    series_new = Column(Integer, default=0)
    series_existing = Column(Integer, default=0)
    series_failed = Column(Integer, default=0)
    series_skipped = Column(Integer, default=0)

    # Per-category breakdown
    category_stats = Column(JSON, default=dict)  # {cat_name: {new: x, existing: y}}

    # New additions feed
    new_movies = Column(JSON, default=list)  # [{tmdb_id, title, year, tags}]
    new_series = Column(JSON, default=list)

    # New unrecognized categories
    new_categories = Column(JSON, default=list)

    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)

    provider = relationship("Provider", back_populates="sync_runs")


# ─── List Subscriptions ───────────────────────────────────────────────────────

class ListSubscription(Base):
    __tablename__ = "list_subscriptions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # trakt | letterboxd | imdb_rss
    url = Column(String, nullable=False)
    tag = Column(String, nullable=False)  # Tag to apply to matched content
    active = Column(Boolean, default=True)
    auto_add_radarr = Column(Boolean, default=False)
    playlist_enabled = Column(Boolean, default=False)  # Generate a Jellyfin playlist from this list
    last_fetched = Column(DateTime, nullable=True)
    last_item_count = Column(Integer, default=0)
    # What the user needs to know about the last refresh when it did not read
    # the whole list (a movies-only fallback, a page that failed), else NULL.
    last_fetch_note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("TentacleUser", backref="list_subscriptions")
    items = relationship("ListItem", back_populates="list_subscription", cascade="all, delete-orphan")


# ─── List Items (cached TMDB IDs per list) ────────────────────────────────

class ListItem(Base):
    __tablename__ = "list_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    list_id = Column(Integer, ForeignKey("list_subscriptions.id"), nullable=False, index=True)
    tmdb_id = Column(Integer, nullable=True, index=True)
    imdb_id = Column(String, nullable=True, index=True)
    media_type = Column(String, default="movie")  # movie | series
    title = Column(String, nullable=True)
    year = Column(String, nullable=True)
    poster_path = Column(String, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    list_subscription = relationship("ListSubscription", back_populates="items")

    __table_args__ = (
        UniqueConstraint("list_id", "tmdb_id", name="uq_list_item"),
    )


# ─── Tag Rules ────────────────────────────────────────────────────────────────

class TagRule(Base):
    __tablename__ = "tag_rules"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    conditions = Column(JSON, default=list)  # [{field, operator, value}]
    output_tag = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    apply_to = Column(String, default="both")  # movies | series | both
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("TentacleUser", backref="tag_rules")


# ─── Home Row Order (display_order persistence for SmartList rows) ───────────

class HomeRowOrder(Base):
    __tablename__ = "home_row_order"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id"), nullable=True, index=True)
    playlist_id = Column(String, nullable=False)
    display_order = Column(Integer, nullable=False, default=0)

    user = relationship("TentacleUser", backref="home_row_orders")

    __table_args__ = (
        UniqueConstraint("user_id", "playlist_id", name="uq_home_row_user_playlist"),
    )


# ─── Auto Playlist Toggles ────────────────────────────────────────────────────

class AutoPlaylistToggle(Base):
    __tablename__ = "auto_playlist_toggles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id"), nullable=True, index=True)
    key = Column(String, nullable=False)  # e.g. "source:Netflix:movies", "builtin:recently_added_movies"
    enabled = Column(Boolean, default=False)

    user = relationship("TentacleUser", backref="auto_playlist_toggles")

    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_auto_toggle_user_key"),
    )


# ─── Tentacle Users ──────────────────────────────────────────────────────────

class TentacleUser(Base):
    __tablename__ = "tentacle_users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    jellyfin_user_id = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    profile_image_tag = Column(String, nullable=True)  # Jellyfin image tag for avatar
    notifications_enabled = Column(Boolean, default=True)  # Per-user download notifications
    # Bumped by logout: every session token carries the version it was issued
    # under, so a copy of a cookie stops working when its owner logs out.
    session_version = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── Notifications ───────────────────────────────────────────────────────────

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id"), nullable=False, index=True)
    tmdb_id = Column(Integer, nullable=False)
    media_type = Column(String, nullable=False, default="movie")  # movie | series
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    poster_path = Column(String, nullable=True)
    jellyfin_item_id = Column(String, nullable=True)  # For click-to-play navigation
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    dismissed_at = Column(DateTime, nullable=True)

    user = relationship("TentacleUser", backref="notifications")


# ─── Download Requests (track who requested a download via Tentacle UI) ──────

class DownloadRequest(Base):
    __tablename__ = "download_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String, nullable=False, default="movie")  # movie | series
    user_id = Column(Integer, ForeignKey("tentacle_users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("TentacleUser", backref="download_requests")

    __table_args__ = (
        UniqueConstraint("tmdb_id", "media_type", name="uq_download_request"),
    )


class MusicArtist(Base):
    """An artist in Lidarr, as Tentacle last saw it (music module)."""
    __tablename__ = "music_artists"
    id = Column(Integer, primary_key=True, autoincrement=True)
    mbid = Column(String, nullable=False, unique=True, index=True)   # MusicBrainz artist id
    lidarr_artist_id = Column(Integer, index=True)
    name = Column(String, nullable=False, default="")
    sort_name = Column(String, default="")
    disambiguation = Column(String, default="")
    path = Column(String, default="")          # the artist's folder, as Lidarr sees it
    updated_at = Column(DateTime, default=datetime.utcnow)
    # Artist picture (services/music/pictures.py): "" (not checked) | set | ok |
    # waiting (the player hasn't scanned the artist yet) | review | error
    picture_status = Column(String, default="", index=True)
    picture_source = Column(String, default="")    # artist.jpg | deezer | upload
    picture_note = Column(Text)                    # why it needs review / what failed
    picture_candidates = Column(JSON)              # Deezer candidates for the review page
    picture_checked_at = Column(DateTime)


class MusicAlbum(Base):
    """An album (MusicBrainz release group) in Lidarr, with Tentacle's verdict on
    its pinned release. A snapshot kept by the music module; Lidarr is the truth."""
    __tablename__ = "music_albums"
    id = Column(Integer, primary_key=True, autoincrement=True)
    mbid = Column(String, nullable=False, unique=True, index=True)   # release group id
    lidarr_album_id = Column(Integer, index=True)
    lidarr_artist_id = Column(Integer, index=True)
    artist_mbid = Column(String, index=True, default="")
    artist_name = Column(String, default="")
    title = Column(String, nullable=False, default="")
    album_type = Column(String, default="")          # Album | EP | Single | ...
    secondary_types = Column(String, default="")     # comma-separated: Live, Compilation, ...
    release_date = Column(String, default="")
    monitored = Column(Boolean, default=False)
    any_release_ok = Column(Boolean, default=True)
    track_count = Column(Integer, default=0)         # of the pinned release
    track_file_count = Column(Integer, default=0)
    size_on_disk = Column(Integer, default=0)
    cover_url = Column(String, default="")
    # Tentacle's verdict (services/music/original.py): right | repin | repin_trim |
    # repin_download | review; empty until checked.
    category = Column(String, default="", index=True)
    verdict = Column(JSON)
    checked_at = Column(DateTime)
    check_error = Column(Text)
    requested_by = Column(Integer, ForeignKey("tentacle_users.id"))
    requested_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.utcnow)


class MusicImport(Base):
    """A Spotify playlist imported by a user (music module): its songs and the
    original studio album each one resolved to (services/music/spotify.py)."""
    __tablename__ = "music_imports"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id"), index=True)
    name = Column(String, default="")
    source = Column(String, default="")          # spotify_url | exportify_csv
    url = Column(String, default="")             # the playlist link, for Refresh
    status = Column(String, default="resolving")  # resolving | ready | error
    done = Column(Integer, default=0)            # songs resolved so far
    total = Column(Integer, default=0)
    # [{title, artist, album, album_artist, result: {mbid, title, artist, artist_mbid,
    #   year} | {reason}}]
    tracks = Column(JSON)
    outcomes = Column(JSON)                      # {release group id: "requested" | why not}
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


# ─── Activity Log ─────────────────────────────────────────────────────────────

class ActivityLog(Base):
    __tablename__ = "activity_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event = Column(String, nullable=False)       # e.g. "vod_sync", "radarr_scan", "radarr_add", "sonarr_add", "list_fetch", "jellyfin_push"
    message = Column(String, nullable=False)      # Human-readable: "VOD sync completed — 12 new movies, 3 new series"
    detail = Column(JSON, nullable=True)          # Optional extra data
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ─── Mislabelled provider streams ─────────────────────────────────────────────

class BlockedStream(Base):
    """A provider stream that must never be imported again.

    IPTV providers mislabel streams: one called "The Decline of Western
    Civilization" served a different film entirely. Tentacle can only trust
    the provider's name, so the fix is to remember the stream and skip it on
    every sync. Keyed by the Xtream stream id, or the full stream URL for an M3U
    provider (whose entries have no stable id).
    """
    __tablename__ = "blocked_streams"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False, index=True)
    media_type = Column(String, nullable=False, default="movie")
    stream_key = Column(String, nullable=False)    # stream id, or the stream URL
    tmdb_id = Column(Integer, nullable=True)        # what it had been matched to
    title = Column(String, nullable=True)
    reason = Column(String, nullable=True)
    blocked_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("provider_id", "media_type", "stream_key", name="uq_blocked_stream"),
    )


class MatchOverride(Base):
    """"This provider stream is really this film." Set when an admin fixes a
    mislabelled stream; the sync uses it instead of matching the provider's
    label, so the fix survives every night."""
    __tablename__ = "match_overrides"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False, index=True)
    media_type = Column(String, nullable=False, default="movie")
    stream_key = Column(String, nullable=False)     # stream id, or the stream URL
    tmdb_id = Column(Integer, nullable=False)        # the film it really is
    previous_tmdb_id = Column(Integer, nullable=True)
    title = Column(String, nullable=True)
    set_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("provider_id", "media_type", "stream_key", name="uq_match_override"),
    )


class MatchSuspect(Base):
    """A VOD title whose real length is far from what TMDB says it should be —
    likely a different film under the provider's label. Found once Jellyfin has
    probed the stream (on first play); the admin removes it or dismisses it."""
    __tablename__ = "match_suspects"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, nullable=False)
    media_type = Column(String, nullable=False, default="movie")
    title = Column(String, nullable=True)
    expected_minutes = Column(Integer, nullable=True)   # TMDB runtime
    actual_minutes = Column(Integer, nullable=True)     # Jellyfin's probe of the stream
    jellyfin_item_id = Column(String, nullable=True)
    dismissed = Column(Boolean, default=False)
    detected_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("tmdb_id", "media_type", name="uq_match_suspect"),
    )


# ─── Deletion Log ─────────────────────────────────────────────────────────────

class DeletionLog(Base):
    """Audit trail for every destructive action Tentacle takes — manual deletes,
    webhook-driven cleanup, nightly sweeps, provider cascades, download fixes.
    Separate from ActivityLog: structured per-item records with size/reason,
    not feed messages."""
    __tablename__ = "deletion_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String, nullable=False)         # download-delete | jellyfin-delete | provider-cascade | duplicate-resolve | orphan-sweep | vod-sweep | stale-cleanup | download-fix | stream-health
    name = Column(String, nullable=False)          # Title or item label
    media_type = Column(String, nullable=True)     # movie | series | None
    size_bytes = Column(Integer, nullable=True)
    reason = Column(String, nullable=False, default="manual")  # manual | auto | webhook
    detail = Column(Text, nullable=True)           # What exactly happened
    user_name = Column(String, nullable=True)      # Display name for manual actions
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ─── Stream Health ────────────────────────────────────────────────────────────

class StreamHealth(Base):
    """Known-bad registry for VOD .strm files whose streams no longer work.
    Entries are re-verified periodically and auto-cleared if the stream
    recovers — a bad mark must never silently become permanent bookkeeping."""
    __tablename__ = "stream_health"
    id = Column(Integer, primary_key=True, autoincrement=True)
    media_type = Column(String, nullable=False)     # movie | series
    tmdb_id = Column(Integer, nullable=False, index=True)
    title = Column(String, nullable=False)
    episode = Column(String, nullable=True)          # "S01E02" for series entries
    strm_path = Column(String, nullable=False)
    stream_url = Column(String, nullable=True)
    fail_count = Column(Integer, default=1)
    first_failed_at = Column(DateTime, default=datetime.utcnow)
    last_checked_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("strm_path", name="uq_stream_health_path"),
    )


# ─── Live TV Channels ────────────────────────────────────────────────────────

class LiveChannel(Base):
    __tablename__ = "live_channels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False, index=True)

    # Channel identity
    name = Column(String, nullable=False)
    # The user's own name for the channel. Channel syncs rewrite `name` from
    # the provider every time and never touch this, so a rename survives.
    custom_name = Column(String, nullable=True)
    channel_number = Column(Integer, nullable=True)  # User-assignable
    stream_id = Column(String, nullable=True)  # Xtream stream_id or M3U index

    # Stream info
    stream_url = Column(String, nullable=False)

    # Metadata
    logo_url = Column(String, nullable=True)
    group_title = Column(String, nullable=True)  # Category/group from provider
    epg_channel_id = Column(String, nullable=True)  # tvg-id for EPG matching
    # The admin's own guide id for the channel. Channel syncs never touch it,
    # and it wins over everything below (#141).
    epg_id_override = Column(String, nullable=True)
    # The feed channel the last EPG sync matched by NAME, because the tvg-id
    # was missing or the feed did not carry it (#141). Recomputed every sync.
    epg_name_match = Column(String, nullable=True)

    # Management
    enabled = Column(Boolean, default=False)  # User must enable channels
    sort_order = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    provider = relationship("Provider", back_populates="live_channels")

    __table_args__ = (
        UniqueConstraint("provider_id", "stream_id", name="uq_live_channel_stream"),
    )

    @property
    def guide_name(self) -> str:
        """The name Jellyfin's guide shows: the user's name, else the provider's."""
        return (self.custom_name or "").strip() or self.name

    @property
    def guide_epg_id(self):
        """The guide id this channel's programmes are stored and served under:
        the override, else a name match, else the provider's tvg-id."""
        return ((self.epg_id_override or "").strip() or self.epg_name_match
                or (self.epg_channel_id or "").strip() or None)

    @property
    def epg_match(self):
        """How guide_epg_id was chosen: "override", "name", "tvg-id" or None."""
        if (self.epg_id_override or "").strip():
            return "override"
        if self.epg_name_match:
            return "name"
        return "tvg-id" if (self.epg_channel_id or "").strip() else None


class LiveChannelGroup(Base):
    __tablename__ = "live_channel_groups"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    category_id = Column(String, nullable=True)  # Provider's internal category ID
    enabled = Column(Boolean, default=False)  # Enable/disable entire group
    channel_count = Column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("provider_id", "name", name="uq_live_group"),
    )


class EPGProgram(Base):
    __tablename__ = "epg_programs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(String, nullable=False, index=True)  # Matches epg_channel_id

    title = Column(String, nullable=False)
    # The provider's <sub-title>: an episode or match name ("TOR vs MTL").
    sub_title = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    start = Column(DateTime, nullable=False)
    stop = Column(DateTime, nullable=False)

    category = Column(String, nullable=True)
    icon_url = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("channel_id", "start", name="uq_epg_program"),
    )


def log_activity(db, event: str, message: str, detail: dict = None):
    """Write an activity log entry"""
    db.add(ActivityLog(event=event, message=message, detail=detail))
    db.commit()


def log_deletion(db, kind: str, name: str, media_type: str = None,
                 size_bytes: int = None, reason: str = "manual",
                 detail: str = None, user_name: str = None):
    """Write a deletion-audit entry. Never raises — an audit write must not
    break the delete it's recording."""
    try:
        db.add(DeletionLog(
            kind=kind, name=name, media_type=media_type, size_bytes=size_bytes,
            reason=reason, detail=detail, user_name=user_name,
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.warning(f"Failed to write deletion log entry for {name}", exc_info=True)


def create_notification(db, user_id: int, tmdb_id: int, media_type: str,
                        title: str, message: str, poster_path: str = None,
                        jellyfin_item_id: str = None):
    """Create a notification for a user if they have notifications enabled."""
    user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
    if not user or not user.notifications_enabled:
        return None
    notif = Notification(
        user_id=user_id, tmdb_id=tmdb_id, media_type=media_type,
        title=title, message=message, poster_path=poster_path,
        jellyfin_item_id=jellyfin_item_id,
    )
    db.add(notif)
    db.commit()
    return notif


# Settings whose readers parse or compare the stored value and need one: an
# empty string is not "unset" to them (int("") raises). Seeded with these, and
# a Save that sends an empty field stores these back instead of "".
NON_EMPTY_DEFAULTS = {
    "sync_schedule": "0 3 * * *",
    "recently_added_days": "30",
    "tmdb_match_threshold": "0.7",
    "hybrid_series_layout": "vod_root",
}
from services.music.settings import NON_EMPTY as _MUSIC_NON_EMPTY  # noqa: E402
NON_EMPTY_DEFAULTS.update(_MUSIC_NON_EMPTY)


def get_setting(db, key: str, default: str = "") -> str:
    """Get a single setting value by key"""
    s = db.query(Setting).filter(Setting.key == key).first()
    if s and key in NON_EMPTY_DEFAULTS and not (s.value or "").strip():
        return default or NON_EMPTY_DEFAULTS[key]
    return s.value if s else default


def set_setting(db, key: str, value: str):
    """Set a single setting value"""
    s = db.query(Setting).filter(Setting.key == key).first()
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def _sqlite_type_for(column) -> str:
    """Map a SQLAlchemy column to a SQLite column-type string for ALTER TABLE.

    Used by the generic migration pass to add model-defined columns that are
    absent on an upgraded DB. Best-effort — falls back to the SQLAlchemy
    compiled type, then TEXT.
    """
    from sqlalchemy import Integer as _Int, Boolean as _Bool, Float as _Float, DateTime as _DT
    t = column.type
    if isinstance(t, _Bool):
        base = "BOOLEAN"
    elif isinstance(t, _Int):
        base = "INTEGER"
    elif isinstance(t, _Float):
        base = "REAL"
    elif isinstance(t, _DT):
        base = "DATETIME"
    else:
        try:
            base = t.compile(dialect=engine.dialect)
        except Exception:
            base = "TEXT"
    return base


def _backfill_default(cursor, conn, table: str, col) -> None:
    """Give existing rows the model's default for a newly added column.

    ALTER TABLE ADD COLUMN fills existing rows with NULL. A model default is
    applied by SQLAlchemy at INSERT time only, so it never reaches rows that
    were already there — and for a column that defaults to True, NULL is read
    as False. A boolean like `include_videos` flipping to False on upgrade
    silently stops work happening at all, with nothing in the logs to say why.
    Only plain scalar defaults are backfilled; callables (datetime.utcnow,
    dict) are left to the application.
    """
    default = getattr(col.default, "arg", None) if col.default is not None else None
    if default is None or callable(default):
        return
    if not isinstance(default, (bool, int, float, str)):
        return
    try:
        cursor.execute(
            f"UPDATE {table} SET {col.name} = ? WHERE {col.name} IS NULL", (default,))
        if cursor.rowcount:
            logger.info(
                f"[migrate] Backfilled {cursor.rowcount} row(s) of "
                f"{table}.{col.name} with {default!r}")
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.error(f"[migrate] Backfill of {table}.{col.name} failed: {e}")


def _existing_columns(cursor, table: str) -> set:
    """Return the set of existing column names for a table (empty if no table)."""
    try:
        cursor.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        return set()


def _migrate_columns():
    """Add columns that may be missing from older databases.

    Two passes:
      1. An explicit list for columns that need a specific SQL default / FK.
      2. A generic pass that introspects each mapped table and adds any
         model-defined column that's absent — so newly added model columns
         don't silently go missing on upgraded DBs.
    """
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    migrations = [
        ("list_subscriptions", "playlist_enabled", "BOOLEAN DEFAULT 0"),
        ("providers", "provider_type", "TEXT DEFAULT 'xtream'"),
        ("providers", "m3u_url", "TEXT"),
        ("providers", "epg_url", "TEXT"),
        ("providers", "user_agent", "TEXT DEFAULT 'TiviMate/4.7.0 (Linux; Android 12)'"),
        ("providers", "live_tv_enabled", "BOOLEAN DEFAULT 0"),
        ("providers", "last_live_sync", "DATETIME"),
        ("providers", "has_live", "BOOLEAN DEFAULT 0"),
        ("providers", "has_vod", "BOOLEAN DEFAULT 0"),
        ("providers", "has_series", "BOOLEAN DEFAULT 0"),
        ("provider_categories", "last_sync_matched", "INTEGER"),
        ("provider_categories", "last_sync_skipped", "INTEGER"),
        ("providers", "require_tmdb_match", "BOOLEAN DEFAULT 1"),
        # Multi-user: add user_id columns
        ("list_subscriptions", "user_id", "INTEGER REFERENCES tentacle_users(id)"),
        ("tag_rules", "user_id", "INTEGER REFERENCES tentacle_users(id)"),
        ("series", "sonarr_monitored", "BOOLEAN DEFAULT 0"),
        ("movies", "jellyfin_item_id", "TEXT"),
        ("series", "jellyfin_item_id", "TEXT"),
        ("series", "last_downloaded_episode", "TEXT"),
        ("tentacle_users", "notifications_enabled", "BOOLEAN DEFAULT 1"),
    ]
    for table, column, col_type in migrations:
        existing = _existing_columns(cursor, table)
        if not existing:
            continue  # table doesn't exist yet (create_all handles it)
        if column in existing:
            continue
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
        except sqlite3.OperationalError as e:
            # Only "duplicate column" is benign; surface anything else.
            if "duplicate column" in str(e).lower():
                pass
            else:
                logger.error(f"[migrate] ALTER {table}.{column} failed: {e}")

    # Generic pass: add any model-defined column missing from the live table.
    # SQLite can't add NOT NULL columns without a default, so skip those.
    for table_name, table_obj in Base.metadata.tables.items():
        existing = _existing_columns(cursor, table_name)
        if not existing:
            continue  # table not created yet
        for col in table_obj.columns:
            if col.name in existing:
                continue
            if not col.nullable and col.default is None and col.server_default is None:
                logger.warning(
                    f"[migrate] Skipping NOT NULL column {table_name}.{col.name} "
                    f"(no default — needs a manual migration)"
                )
                continue
            col_type = _sqlite_type_for(col)
            try:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}")
                conn.commit()
                logger.info(f"[migrate] Added missing column {table_name}.{col.name} ({col_type})")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    pass
                else:
                    logger.error(f"[migrate] ALTER {table_name}.{col.name} failed: {e}")
                continue
            _backfill_default(cursor, conn, table_name, col)

    # Recreate tables that need PK changes (HomeRowOrder, AutoPlaylistToggle)
    _migrate_home_row_order(cursor, conn)
    _migrate_auto_playlist_toggles(cursor, conn)
    _drop_retired_tables(cursor, conn)
    _reset_guessed_made_for_kids(cursor, conn)
    conn.close()


def _reset_guessed_made_for_kids(cursor, conn):
    """Forget the Made for Kids values the age_limit guess wrote (#130).

    Until this fix the indexer stored True for any unrestricted video whose
    details came back without is_live, which is not what the designation
    means, and never stored False. yt-dlp does not report the designation, so
    none of the stored True values came from YouTube: reset them to NULL
    ("unknown"). Runs once, recorded in settings, so values a later extractor
    supplies for real are never touched.
    """
    import sqlite3
    marker = "migrated_youtube_made_for_kids_reset"
    try:
        cursor.execute("SELECT 1 FROM settings WHERE key = ?", (marker,))
        if cursor.fetchone():
            return
        cursor.execute("UPDATE youtube_videos SET is_made_for_kids = NULL "
                       "WHERE is_made_for_kids IS NOT NULL")
        if cursor.rowcount:
            logger.info(f"[migrate] Cleared {cursor.rowcount} guessed youtube_videos.is_made_for_kids value(s)")
        cursor.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (marker, "1"))
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.error(f"[migrate] Could not reset youtube_videos.is_made_for_kids: {e}")


def _drop_retired_tables(cursor, conn):
    """Remove tables whose concept no longer exists.

    youtube_row_subscriptions held a per-user opt-in to a channel's playlist.
    A channel's playlist now exists for every user and the per-user choice is
    the home row itself, so the table is meaningless; leaving it would only
    invite something to read it again.
    """
    import sqlite3
    try:
        cursor.execute("DROP TABLE IF EXISTS youtube_row_subscriptions")
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.error(f"[migrate] Could not drop youtube_row_subscriptions: {e}")


def _migrate_home_row_order(cursor, conn):
    """Recreate home_row_order with id PK + user_id column."""
    try:
        cursor.execute("SELECT user_id FROM home_row_order LIMIT 1")
        return  # Already migrated
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE home_row_order RENAME TO _home_row_order_old")
        cursor.execute("""
            CREATE TABLE home_row_order (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES tentacle_users(id),
                playlist_id TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, playlist_id)
            )
        """)
        cursor.execute("""
            INSERT INTO home_row_order (playlist_id, display_order)
            SELECT playlist_id, display_order FROM _home_row_order_old
        """)
        cursor.execute("DROP TABLE _home_row_order_old")
        conn.commit()
    except Exception:
        pass


def _migrate_auto_playlist_toggles(cursor, conn):
    """Recreate auto_playlist_toggles with id PK + user_id column."""
    try:
        cursor.execute("SELECT user_id FROM auto_playlist_toggles LIMIT 1")
        return  # Already migrated
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE auto_playlist_toggles RENAME TO _auto_toggles_old")
        cursor.execute("""
            CREATE TABLE auto_playlist_toggles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES tentacle_users(id),
                key TEXT NOT NULL,
                enabled BOOLEAN DEFAULT 0,
                UNIQUE(user_id, key)
            )
        """)
        cursor.execute("""
            INSERT INTO auto_playlist_toggles (key, enabled)
            SELECT key, enabled FROM _auto_toggles_old
        """)
        cursor.execute("DROP TABLE _auto_toggles_old")
        conn.commit()
    except Exception:
        pass


def create_tables():
    Base.metadata.create_all(bind=engine)
    _migrate_columns()


def seed_defaults(db):
    """Seed default settings if not present"""
    defaults = {
        # YouTube source. Off by default: it needs its own media mount and a
        # Jellyfin library, so it should never start indexing unasked.
        "youtube_enabled": "false",
        "youtube_base_url": "",          # what goes in .strm; must be reachable BY Jellyfin
        "youtube_index_interval_minutes": "60",
        # Scheduled checks for new uploads and live streams (on by default: the
        # feature is about the latest uploads). Off: only "Refresh now".
        "youtube_background_checks": "true",
        # Optional: a YouTube Data API key (metadata from Google's API instead of
        # YouTube's pages) and an HTTP proxy for YouTube traffic only.
        "youtube_api_key": "",
        "youtube_proxy": "",
        "tmdb_bearer_token": "",
        "tmdb_api_key": "",
        "radarr_url": "",
        "radarr_api_key": "",
        "sonarr_url": "",
        "sonarr_api_key": "",
        "jellyfin_url": "",
        "jellyfin_api_key": "",
        "sync_schedule": NON_EMPTY_DEFAULTS["sync_schedule"],
        "recently_added_days": NON_EMPTY_DEFAULTS["recently_added_days"],
        "tmdb_match_threshold": NON_EMPTY_DEFAULTS["tmdb_match_threshold"],
        "smartlists_path": "/data/smartlists",
        "jellyfin_user_id": "",
        "jellyfin_user_name": "",
        "logodev_api_key": "",
        "trakt_client_id": "",
        "home_row_limit": "20",
        # How hybrid VOD+downloaded series are unified in Jellyfin:
        #   vod_root       - Sonarr downloads INTO the VOD show folder (needs a
        #                    VOD root folder registered in Sonarr)
        #   shared_library - Sonarr downloads to its own root using the SAME
        #                    folder name as the VOD show; Jellyfin merges the
        #                    two folders (both must be in ONE Jellyfin library)
        "hybrid_series_layout": NON_EMPTY_DEFAULTS["hybrid_series_layout"],
        "setup_complete": "false",
        "data_dir": os.getenv("DATA_DIR", "/data"),
        "hdhr_tuner_count": "3",
        "hdhr_device_id": "TENTACLE1",
        "session_secret": secrets.token_hex(32),
        # Shared secret for trusted server-to-server callers (the Jellyfin plugin's
        # delete/plugin-keys calls, Radarr/Sonarr webhooks) that cannot present a
        # user session. Copied into the plugin config / webhook URL by the operator.
        "internal_secret": secrets.token_hex(32),
        # Secret Lidarr's webhook presents (?secret=) to /api/music/webhook.
        "music_webhook_secret": secrets.token_urlsafe(24),
    }
    # The music module's settings: off by default, every value a setting.
    from services.music.settings import DEFAULTS as MUSIC_DEFAULTS
    defaults.update(MUSIC_DEFAULTS)
    # Insert each missing default in its own transaction so a concurrent
    # worker seeding the same key (IntegrityError on the PK) doesn't abort the
    # whole batch — we just roll back and move on (get-or-create semantics).
    from sqlalchemy.exc import IntegrityError
    for key, value in defaults.items():
        existing = db.query(Setting).filter(Setting.key == key).first()
        if existing:
            continue
        db.add(Setting(key=key, value=value))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # another worker inserted it first — fine


def migrate_orphaned_data_to_user(db, user_id: int):
    """Assign any user_id=NULL rows to a user (called on first admin login for migration)."""
    for model in [ListSubscription, TagRule, HomeRowOrder, AutoPlaylistToggle]:
        db.query(model).filter(model.user_id == None).update({"user_id": user_id})
    db.commit()

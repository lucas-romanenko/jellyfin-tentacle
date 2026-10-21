"""
Tentacle - Database Models
SQLAlchemy models for all entities
"""

from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, Float,
    DateTime, Text, JSON, ForeignKey, UniqueConstraint, or_,
    CheckConstraint, ForeignKeyConstraint, case, cast, literal, text
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import sessionmaker, relationship
from models.data_fields import DataField, register as _register_data_fields
from models.legacy_schema import LEGACY
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
class _Row:
    """What every table has: `data`, the row's details as JSON (Database.md
    in the project page: a few real columns, everything else in data).
    Models declare them as DataField(type)."""
    data = Column(JSON, nullable=False, default=dict, server_default=text("'{}'"))


Base = declarative_base(cls=_Row)
_register_data_fields(Base)


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
    updated_at = DataField(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── Providers ────────────────────────────────────────────────────────────────

class Provider(Base):
    __tablename__ = "providers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    provider_type = DataField(String, default="xtream")  # xtream, m3u_url, m3u_file
    server_url = DataField(String)
    username = DataField(String)
    password = DataField(String)
    active = Column(Boolean, default=False)
    priority = Column(Integer, default=1)  # Lower = higher priority
    status = DataField(String, default="untested")  # untested, ok, error
    last_tested = DataField(DateTime)
    expiry = DataField(DateTime)
    max_connections = DataField(Integer)

    # Live TV settings
    m3u_url = DataField(String)  # For m3u_url provider type
    epg_url = DataField(String)  # Override EPG/XMLTV URL
    user_agent = DataField(String, default="TiviMate/4.7.0 (Linux; Android 12)")
    live_tv_enabled = Column(Boolean, default=False)
    last_live_sync = DataField(DateTime)

    # Auto-detected capabilities
    has_live = DataField(Boolean, default=False)
    has_vod = DataField(Boolean, default=False)
    has_series = DataField(Boolean, default=False)
    require_tmdb_match = DataField(Boolean, default=True)

    created_at = DataField(DateTime, default=datetime.utcnow)
    updated_at = DataField(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Several addresses (services/provider_addresses.py): what to do when the
    # address in use fails and a backup works. "switch" (the default),
    # "switch_back" (and back to preferred_address once it works again) or
    # "off" (only say so).
    address_auto_switch = DataField(String, default="switch")
    # The address the admin chose by hand ("Use this one"), for switch_back.
    preferred_address = DataField(String)
    # Live TV: streams this provider's account carries at once (None: the
    # account's own max_connections, else the Live TV default).
    live_max_streams = DataField(Integer)
    # Live TV: added to this provider's own channel numbers, so two providers
    # never hand Jellyfin the same one (routers/livetv.py guide_number). The
    # first Live TV provider keeps its own (0); others get one once, kept.
    channel_number_offset = DataField(Integer)

    categories = relationship("ProviderCategory", back_populates="provider", cascade="all, delete-orphan")
    sync_runs = relationship("SyncRun", back_populates="provider", cascade="all, delete-orphan")
    live_channels = relationship("LiveChannel", back_populates="provider", cascade="all, delete-orphan")
    addresses = relationship("ProviderAddress", back_populates="provider", cascade="all, delete-orphan",
                             order_by="ProviderAddress.id")


# ─── Provider addresses ───────────────────────────────────────────────────────

class ProviderAddress(Base):
    """One address a provider can be reached at. The one in use is the
    provider's server_url; the others are backups. Tests and the hourly
    health check keep what each answered (services/provider_addresses.py)."""
    __tablename__ = "provider_addresses"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    url = Column(String, nullable=False)          # scheme://host[:port], no path, no login
    added_at = DataField(DateTime, default=datetime.utcnow)
    last_result = DataField(JSON)  # the latest test (see provider_addresses.test_address)
    history = DataField(JSON, default=list)  # [{"at", "ok", "word"}], newest last, a few days' worth

    provider = relationship("Provider", back_populates="addresses")

    __table_args__ = (
        UniqueConstraint("provider_id", "url", name="uq_provider_address"),
    )


# ─── Provider Categories ──────────────────────────────────────────────────────

class ProviderCategory(Base):
    __tablename__ = "provider_categories"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    category_id = Column(String, nullable=False)  # Provider's internal ID
    category_name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # movie | series
    whitelisted = Column(Boolean, default=False)
    source_tag = DataField(String)  # Netflix, Amazon, etc.
    title_count = DataField(Integer, default=0)
    last_seen = DataField(DateTime, default=datetime.utcnow)
    last_sync_matched = DataField(Integer)  # Items matched TMDB last sync
    last_sync_skipped = DataField(Integer)  # Items with no TMDB match last sync
    # Consecutive syncs where this category returned nothing while it was known
    # to hold titles. title_count keeps its last non-zero value throughout, so a
    # multi-night outage can't be mistaken for a genuinely emptied category.
    consecutive_empty_syncs = DataField(Integer, default=0)

    provider = relationship("Provider", back_populates="categories")
    snapshots = relationship("CategorySnapshot", back_populates="category", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("provider_id", "category_id", "type", name="uq_provider_category"),
    )


# ─── Category Snapshots (for graphs) ─────────────────────────────────────────

class CategorySnapshot(Base):
    __tablename__ = "category_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    category_id = Column(Integer, ForeignKey("provider_categories.id", ondelete="CASCADE"), nullable=False)
    title_count = DataField(Integer, default=0)
    new_count = DataField(Integer, default=0)
    removed_count = DataField(Integer, default=0)
    recorded_at = DataField(DateTime, default=datetime.utcnow)

    category = relationship("ProviderCategory", back_populates="snapshots")


# ─── Movies ───────────────────────────────────────────────────────────────────

def _source(downloaded: str):
    """A Movie/Series row's `source`: "provider_<id>" for a provider's title,
    else the app that downloaded it ("radarr" / "sonarr"). It's provider_id
    said another way, so it is worked out, not stored; setting it sets
    provider_id. Works in queries too (Movie.source == "radarr")."""
    def get(row):
        return f"provider_{row.provider_id}" if row.provider_id is not None else downloaded

    def set_(row, value):
        value = str(value or "")
        row.provider_id = int(value[len("provider_"):]) if value.startswith("provider_") else None

    def expr(cls):
        return case((cls.provider_id.isnot(None), literal("provider_") + cast(cls.provider_id, String)),
                    else_=literal(downloaded))

    prop = hybrid_property(get, set_, expr=expr)
    prop.__name__ = "source"
    return prop


class Movie(Base):
    __tablename__ = "movies"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, unique=True, nullable=False)
    title = Column(String, nullable=False)
    year = DataField(String)
    overview = DataField(Text)
    runtime = DataField(Integer)
    rating = DataField(Float)
    genres = DataField(JSON, default=list)  # ["Action", "Drama"]
    poster_path = DataField(String)
    backdrop_path = DataField(String)

    # Source info: `source` ("radarr" | "provider_{id}") is provider_id said
    # another way, so it isn't stored (see _source below)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=True, index=True)
    strm_path = DataField(String)
    nfo_path = DataField(String)
    radarr_path = DataField(String)

    # Tags
    source_tag = DataField(String)  # Netflix, Amazon etc
    tags = DataField(JSON, default=list)  # All tags applied

    # Jellyfin
    jellyfin_item_id = DataField(String)

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
    provider_missing_since = DataField(DateTime)
    file_missing_since = DataField(DateTime)

    # "Keep this in the catalog but stop writing/repairing .strm files for it."
    # For titles the user has deliberately switched to downloaded copies — the
    # sync would otherwise regenerate the .strm every night and, in a merged
    # folder, leave two sources competing.
    strm_disabled = DataField(Boolean, default=False)

    # Dates
    date_added = Column(DateTime, default=datetime.utcnow)
    # When the downloaded copy arrived (Radarr's file import date / Sonarr's
    # latest episode import). date_added is when the title first entered the
    # library, which for a title that was VOD first is months earlier — sorting
    # "Downloaded Movies" by it buried every such download mid-row.
    downloaded_at = DataField(DateTime)
    date_updated = DataField(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    source = _source("radarr")


# ─── Series ───────────────────────────────────────────────────────────────────

class Series(Base):
    __tablename__ = "series"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, unique=True, nullable=False)
    title = Column(String, nullable=False)
    year = DataField(String)
    overview = DataField(Text)
    genres = DataField(JSON, default=list)
    poster_path = DataField(String)
    backdrop_path = DataField(String)
    rating = DataField(Float)
    status = DataField(String)  # Continuing, Ended etc

    # `source` ("sonarr" | "provider_{id}"): provider_id, see _source below
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=True, index=True)
    strm_path = DataField(String)
    nfo_path = DataField(String)
    sonarr_path = DataField(String)

    source_tag = DataField(String)
    tags = DataField(JSON, default=list)
    sonarr_monitored = DataField(Boolean, default=False)  # True = following for new episodes
    # The provider's series ids (strings) its listings placed on this row: a
    # relabelled listing whose label no longer resolves is recognised by them (#621)
    provider_series_ids = DataField(JSON)

    # Jellyfin
    jellyfin_item_id = DataField(String)
    last_downloaded_episode = DataField(String)  # e.g. "S02E05 · Episode Title"

    # See Movie.provider_missing_since / file_missing_since — each guard keeps
    # its own mark and deletion requires two runs of that guard to agree.
    provider_missing_since = DataField(DateTime)
    file_missing_since = DataField(DateTime)

    # See Movie.strm_disabled — opt this title out of .strm writing/repair.
    strm_disabled = DataField(Boolean, default=False)

    date_added = Column(DateTime, default=datetime.utcnow)
    # When the downloaded copy arrived (Radarr's file import date / Sonarr's
    # latest episode import). date_added is when the title first entered the
    # library, which for a title that was VOD first is months earlier — sorting
    # "Downloaded Movies" by it buried every such download mid-row.
    downloaded_at = DataField(DateTime)
    date_updated = DataField(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    source = _source("sonarr")


# ─── YouTube channels as a source ────────────────────────────────────────────
# Deliberately separate from Movie/Series. Those tables carry a unique, non-null
# tmdb_id and the VOD sync runs TMDB matching over them, so a video called
# "Frozen" would be imported as the real film and share its media folder — and
# the prune would then delete the IPTV movie. YouTube content also lives under
# its own media root so nothing else walks it.

class YouTubeChannel(Base):
    __tablename__ = "youtube_channels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    input_url = DataField(String)  # what the user pasted
    kind = Column(String, default="channel")            # channel | playlist
    channel_id = DataField(String)  # UC…
    handle = DataField(String)  # @handle
    playlist_id = DataField(String)
    title = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)  # filesystem-safe, used in tags
    avatar_url = DataField(String)
    banner_url = DataField(String)

    # What to index. The one setting a user chooses is keep_count — "keep the
    # newest N" — which drives both how far back the listing is read and how
    # many videos survive retention. The rest are derived or advanced.
    include_videos = DataField(Boolean, default=True)
    # Finished broadcasts. Set automatically on add: on for a channel with no
    # uploads (it only ever streams), off otherwise. Not a user-facing choice.
    include_streams = DataField(Boolean, default=False)
    include_shorts = DataField(Boolean, default=False)
    min_duration = DataField(Integer, default=0)  # seconds; 0 = no length filter
    keep_count = DataField(Integer, default=10)
    keep_days = DataField(Integer)
    max_height = DataField(Integer, default=1080)

    # Jellyfin-side metadata
    rating = DataField(String)  # NFO <mpaa>, for parental controls
    extra_tags = DataField(JSON, default=list)  # NFO <tag>, for allowed-tags policies

    enabled = Column(Boolean, default=True)
    # Expose this channel's live/upcoming streams as a Live TV channel.
    live_enabled = Column(Boolean, default=False)
    channel_number = DataField(String)
    last_checked = DataField(DateTime)
    # The last time the channel's tabs were listed in full. A scheduled check
    # reads the channel's feed first and lists the tabs only when something is
    # new, or when this is a day old (services.youtube.indexer._light_check).
    last_full_check = DataField(DateTime)
    # The ids the feed showed when the tabs were last listed: already dealt
    # with, so the next feed check reacts only to ids beyond these.
    feed_ids = DataField(JSON)
    # A playlist source's item count (Data API) at its last full listing. New
    # videos usually go to the bottom of a playlist, where its feed never shows
    # them; a change in size is what tells the light check to list it (#544).
    playlist_size = DataField(Integer)
    last_error = DataField(String)
    error_count = DataField(Integer, default=0)
    # Why videos were passed over on the last index, as {reason: count}. Skips
    # were silent, so a setting quietly excluding every upload (a min duration
    # above the channel's typical video length, say) looked like nothing had
    # been indexed at all.
    last_skips = DataField(JSON, default=dict)
    last_indexed_count = DataField(Integer, default=0)
    # What each listing tab returned on the last index, e.g. {"videos": 12,
    # "streams": 6}. Separates "YouTube listed nothing" from "settings excluded
    # everything" — opposite problems that look identical from outside.
    last_listing = DataField(JSON, default=dict)
    # Set when YouTube asks us to prove we're not a bot. Indexing backs off
    # until this passes rather than hammering and making it worse.
    blocked_until = DataField(DateTime)
    created_at = DataField(DateTime, default=datetime.utcnow)

    videos = relationship("YouTubeVideo", back_populates="channel", cascade="all, delete-orphan")


class YouTubeVideo(Base):
    __tablename__ = "youtube_videos"
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_fk = Column(Integer, ForeignKey("youtube_channels.id", ondelete="CASCADE"), nullable=False, index=True)
    video_id = Column(String, unique=True, nullable=False, index=True)  # 11 chars
    title = Column(String, nullable=False)
    description = DataField(Text)
    published_at = DataField(DateTime)
    duration = DataField(Integer)  # seconds
    live_status = DataField(String)
    media_type = Column(String, default="video")        # video | livestream
    thumbnail_url = DataField(String)
    is_made_for_kids = DataField(Boolean)  # unknown until details are fetched

    folder_path = DataField(String)
    strm_path = DataField(String)

    first_seen = DataField(DateTime, default=datetime.utcnow)
    last_seen = DataField(DateTime, default=datetime.utcnow)
    removed_at = DataField(DateTime)
    # When to read this video's details again, and how many reads failed in a
    # row: a video that could not be read (members-only, private, a hiccup) is
    # retried later, not on every check; a live or upcoming stream the listing no
    # longer shows is re-read at most this often.
    next_check_at = DataField(DateTime)
    check_failures = DataField(Integer, default=0)
    # Why a video was passed over, recorded on the row so it is never detailed
    # again. Details are rate-limited to one every few seconds, and without this
    # a channel's excluded back catalogue was re-fetched on every single
    # refresh — minutes of work per run, to reach the same answer.
    skip_reason = DataField(String)

    channel = relationship("YouTubeChannel", back_populates="videos")


# ─── Duplicates ───────────────────────────────────────────────────────────────

class Duplicate(Base):
    __tablename__ = "duplicates"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String, nullable=False)  # movie | series
    sources = DataField(JSON, default=list)  # [{"source": "radarr", "path": "..."}, ...]
    resolution = Column(String, default="pending")  # pending | keep_radarr | keep_provider_1 | keep_both
    detected_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = DataField(DateTime)
    # Users' watched state saved before a same-folder copy was deleted, waiting
    # for the item Jellyfin creates for the kept copy (services/duplicates.py, #333)
    pending_user_data = DataField(JSON)


# ─── Sync Runs ────────────────────────────────────────────────────────────────

class SyncRun(Base):
    __tablename__ = "sync_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, default="running", index=True)  # running | completed | failed
    sync_type = DataField(String, default="full")  # full | movies | series

    # Totals
    movies_new = DataField(Integer, default=0)
    movies_existing = DataField(Integer, default=0)
    movies_failed = DataField(Integer, default=0)
    movies_skipped = DataField(Integer, default=0)  # No TMDB match
    series_new = DataField(Integer, default=0)
    series_existing = DataField(Integer, default=0)
    series_failed = DataField(Integer, default=0)
    series_skipped = DataField(Integer, default=0)

    # Per-category breakdown
    category_stats = DataField(JSON, default=dict)  # {cat_name: {new: x, existing: y}}

    # New additions feed
    new_movies = DataField(JSON, default=list)  # [{tmdb_id, title, year, tags}]
    new_series = DataField(JSON, default=list)

    # New unrecognized categories
    new_categories = DataField(JSON, default=list)

    error_message = DataField(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = DataField(DateTime)
    duration_seconds = DataField(Integer)

    provider = relationship("Provider", back_populates="sync_runs")


# ─── List Subscriptions ───────────────────────────────────────────────────────

class ListSubscription(Base):
    __tablename__ = "list_subscriptions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String, nullable=False)
    type = DataField(String)  # trakt | letterboxd | imdb_rss
    url = DataField(String)
    tag = Column(String, nullable=False)  # Tag to apply to matched content
    active = Column(Boolean, default=True)
    auto_add_radarr = DataField(Boolean, default=False)
    playlist_enabled = Column(Boolean, default=False)  # Generate a Jellyfin playlist from this list
    last_fetched = DataField(DateTime)
    last_item_count = DataField(Integer, default=0)
    # What the user needs to know about the last refresh when it did not read
    # the whole list (a movies-only fallback, a page that failed), else NULL.
    last_fetch_note = DataField(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("TentacleUser", backref="list_subscriptions")
    items = relationship("ListItem", back_populates="list_subscription", cascade="all, delete-orphan")


# ─── List Items (cached TMDB IDs per list) ────────────────────────────────

class ListItem(Base):
    __tablename__ = "list_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    list_id = Column(Integer, ForeignKey("list_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    tmdb_id = Column(Integer, nullable=True, index=True)
    imdb_id = DataField(String)
    media_type = Column(String, default="movie")  # movie | series
    title = DataField(String)
    year = DataField(String)
    poster_path = DataField(String)
    added_at = DataField(DateTime, default=datetime.utcnow)

    list_subscription = relationship("ListSubscription", back_populates="items")

    # TMDB numbers films and shows separately: movie/N and tv/N are two titles,
    # and a mixed list may hold both (#365). Rows without a type are films.
    __table_args__ = (
        UniqueConstraint("list_id", "tmdb_id", "media_type", name="uq_list_item"),
    )

    @classmethod
    def of_type(cls, media_type: str):
        """Filter for the rows of one TMDB namespace ("series", else films)."""
        if media_type == "series":
            return cls.media_type == "series"
        return or_(cls.media_type.is_(None), cls.media_type != "series")


# ─── Tag Rules ────────────────────────────────────────────────────────────────

class TagRule(Base):
    __tablename__ = "tag_rules"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id", ondelete="CASCADE"), nullable=True, index=True)
    name = DataField(String)
    conditions = DataField(JSON, default=list)  # [{field, operator, value}]
    output_tag = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    apply_to = DataField(String, default="both")  # movies | series | both
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("TentacleUser", backref="tag_rules")


# ─── Home Row Order (display_order persistence for SmartList rows) ───────────

class HomeRowOrder(Base):
    __tablename__ = "home_row_order"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id", ondelete="CASCADE"), nullable=True, index=True)
    playlist_id = Column(String, nullable=False)
    display_order = DataField(Integer, default=0)

    user = relationship("TentacleUser", backref="home_row_orders")

    __table_args__ = (
        UniqueConstraint("user_id", "playlist_id", name="uq_home_row_user_playlist"),
    )


# ─── Auto Playlist Toggles ────────────────────────────────────────────────────

class AutoPlaylistToggle(Base):
    __tablename__ = "auto_playlist_toggles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id", ondelete="CASCADE"), nullable=True, index=True)
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
    profile_image_tag = DataField(String)  # Jellyfin image tag for avatar
    notifications_enabled = DataField(Boolean, default=True)  # Per-user download notifications
    # Bumped by logout: every session token carries the version it was issued
    # under, so a copy of a cookie stops working when its owner logs out.
    session_version = DataField(Integer, default=0)
    created_at = DataField(DateTime, default=datetime.utcnow)
    # Family (services/family.py). A kids profile is an age level ("young",
    # "kids", "teens"); None = no limit. The Jellyfin policy fields it changes
    # are saved first (JSON) and put back when it's switched off.
    kids_level = DataField(String)
    kids_saved_policy = DataField(Text)
    # Requests from this person wait for an admin ("ask") or go straight to
    # Radarr/Sonarr/Lidarr ("direct"). Admins' own always go straight through.
    request_mode = DataField(String, default="ask")
    invited_by = DataField(String)  # the admin's name, for an account made in Tentacle
    invited_at = DataField(DateTime)


# ─── Notifications ───────────────────────────────────────────────────────────

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id", ondelete="CASCADE"), nullable=False, index=True)
    tmdb_id = DataField(Integer)
    media_type = DataField(String, default="movie")  # movie | series
    title = DataField(String)
    message = DataField(String)
    poster_path = DataField(String)
    jellyfin_item_id = DataField(String)  # For click-to-play navigation
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    dismissed_at = DataField(DateTime)
    # "download" (ready to watch), "request" (asked for / approved / declined)
    # or "problem" (admins only: services/admin_alerts.py).
    kind = DataField(String, default="download")

    user = relationship("TentacleUser", backref="notifications")


# ─── Download Requests (track who requested a download via Tentacle UI) ──────

class DownloadRequest(Base):
    __tablename__ = "download_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String, nullable=False, default="movie")  # movie | series
    user_id = Column(Integer, ForeignKey("tentacle_users.id", ondelete="CASCADE"), nullable=False)
    created_at = DataField(DateTime, default=datetime.utcnow)

    user = relationship("TentacleUser", backref="download_requests")

    __table_args__ = (
        UniqueConstraint("tmdb_id", "media_type", name="uq_download_request"),
    )


# ─── Requests waiting for an admin (services/approvals.py) ────────────────────

class MediaRequest(Base):
    """A family member's request that waits for an admin's yes or no. `payload`
    is the original request body (JSON), replayed as that person on approval."""
    __tablename__ = "media_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String, nullable=False)           # movie | series | album
    tmdb_id = DataField(Integer)
    tvdb_id = DataField(Integer)
    title = DataField(String, default="")
    year = DataField(String)
    poster_path = DataField(String)
    payload = DataField(Text, default="{}")
    status = Column(String, nullable=False, default="pending", index=True)  # pending | approved | declined | failed
    note = DataField(String)  # the admin's reason, or why the approved add failed
    decided_by = DataField(String)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    decided_at = DataField(DateTime)

    user = relationship("TentacleUser", backref="media_requests")


# ─── Problems the admin was told about (services/admin_alerts.py) ─────────────

class AdminAlert(Base):
    """One problem, told to the admins once when it starts and once when it's over."""
    __tablename__ = "admin_alerts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, nullable=False, unique=True)   # e.g. "disk:/media/movies", "livetv:setup"
    area = Column(String, nullable=False)               # livetv | library | requests | downloads | server
    title = DataField(String)
    message = DataField(String)
    active = Column(Boolean, default=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = DataField(DateTime, default=datetime.utcnow)
    resolved_at = DataField(DateTime)


class MusicArtist(Base):
    """An artist in Lidarr, as Tentacle last saw it (music module)."""
    __tablename__ = "music_artists"
    id = Column(Integer, primary_key=True, autoincrement=True)
    mbid = Column(String, nullable=False, unique=True, index=True)   # MusicBrainz artist id
    lidarr_artist_id = DataField(Integer)
    name = Column(String, nullable=False, default="")
    sort_name = DataField(String, default="")
    disambiguation = DataField(String, default="")
    path = DataField(String, default="")  # the artist's folder, as Lidarr sees it
    updated_at = DataField(DateTime, default=datetime.utcnow)
    # Artist picture (services/music/pictures.py): "" (not checked) | set | ok |
    # waiting (the player hasn't scanned the artist yet) | review | error
    picture_status = Column(String, default="", index=True)
    picture_source = DataField(String, default="")  # artist.jpg | deezer | upload
    picture_note = DataField(Text)  # why it needs review / what failed
    picture_candidates = DataField(JSON)  # Deezer candidates for the review page
    picture_checked_at = DataField(DateTime)


class MusicAlbum(Base):
    """An album (MusicBrainz release group) in Lidarr, with Tentacle's verdict on
    its pinned release. A snapshot kept by the music module; Lidarr is the truth."""
    __tablename__ = "music_albums"
    id = Column(Integer, primary_key=True, autoincrement=True)
    mbid = Column(String, nullable=False, unique=True, index=True)   # release group id
    lidarr_album_id = DataField(Integer)
    lidarr_artist_id = DataField(Integer)
    artist_mbid = DataField(String, default="")
    artist_name = DataField(String, default="")
    title = DataField(String, default="")
    album_type = DataField(String, default="")  # Album | EP | Single | ...
    secondary_types = DataField(String, default="")  # comma-separated: Live, Compilation, ...
    release_date = DataField(String, default="")
    monitored = Column(Boolean, default=False)
    any_release_ok = Column(Boolean, default=True)
    track_count = DataField(Integer, default=0)  # of the pinned release
    track_file_count = DataField(Integer, default=0)
    size_on_disk = DataField(Integer, default=0)
    cover_url = DataField(String, default="")
    # Tentacle's verdict (services/music/original.py): right | repin | repin_trim |
    # repin_download | review; empty until checked.
    category = Column(String, default="", index=True)
    verdict = DataField(JSON)
    checked_at = DataField(DateTime)
    check_error = DataField(Text)
    requested_by = Column(Integer, ForeignKey("tentacle_users.id", ondelete="SET NULL"))
    requested_at = DataField(DateTime)
    # A request whose pin and search haven't happened yet (jobs.finish_request):
    # kept in the database so a restart or a lost reply can't drop it (#242).
    request_pending = Column(Boolean, default=False)
    request_choice = DataField(JSON)  # the user's pick of a tracklist, for that job
    updated_at = DataField(DateTime, default=datetime.utcnow)


class MusicImport(Base):
    """A Spotify playlist imported by a user (music module): its songs and the
    original studio album each one resolved to (services/music/spotify.py)."""
    __tablename__ = "music_imports"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("tentacle_users.id", ondelete="CASCADE"), index=True)
    name = DataField(String, default="")
    source = DataField(String, default="")  # spotify_url | exportify_csv
    url = DataField(String, default="")  # the playlist link, for Refresh
    status = DataField(String, default="resolving")  # resolving | ready | error
    done = DataField(Integer, default=0)  # songs resolved so far
    total = DataField(Integer, default=0)
    # [{title, artist, album, album_artist, result: {mbid, title, artist, artist_mbid,
    #   year} | {reason}}]
    tracks = DataField(JSON)
    outcomes = DataField(JSON)  # {release group id: "requested" | why not}
    left_out = DataField(Integer, default=0)  # songs past MAX_TRACKS, not imported (shown)
    error = DataField(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = DataField(DateTime, default=datetime.utcnow)


# ─── Activity Log ─────────────────────────────────────────────────────────────

class ActivityLog(Base):
    __tablename__ = "activity_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event = Column(String, nullable=False)       # e.g. "vod_sync", "radarr_scan", "radarr_add", "sonarr_add", "list_fetch", "jellyfin_push"
    message = DataField(String)  # Human-readable: "VOD sync completed — 12 new movies, 3 new series"
    detail = DataField(JSON)  # Optional extra data
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
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    media_type = Column(String, nullable=False, default="movie")
    stream_key = Column(String, nullable=False)    # stream id, or the stream URL
    tmdb_id = DataField(Integer)  # what it had been matched to
    title = DataField(String)
    reason = DataField(String)
    blocked_by = DataField(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    # The provider's own label of the stream ("<name>\t<container>"), stored by
    # the sync while it lists the stream: a stream re-listed under a new id
    # with this exact label is held for the admin, never imported silently.
    label = DataField(String)

    __table_args__ = (
        UniqueConstraint("provider_id", "media_type", "stream_key", name="uq_blocked_stream"),
    )


class MatchOverride(Base):
    """"This provider stream is really this film." Set when an admin fixes a
    mislabelled stream; the sync uses it instead of matching the provider's
    label, so the fix survives every night."""
    __tablename__ = "match_overrides"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    media_type = Column(String, nullable=False, default="movie")
    stream_key = Column(String, nullable=False)     # stream id, or the stream URL
    tmdb_id = Column(Integer, nullable=False)        # the film it really is
    previous_tmdb_id = DataField(Integer)
    title = DataField(String)
    set_by = DataField(String)
    created_at = DataField(DateTime, default=datetime.utcnow)
    # The provider's label of the stream ("<name>\t<container>"), stored by the
    # sync: when the provider re-lists the stream under a new id with this
    # exact label, the fix follows it (moved_from = the key it had before, for
    # the admin's Undo).
    label = DataField(String)
    moved_from = DataField(String)

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
    title = DataField(String)
    expected_minutes = DataField(Integer)  # TMDB runtime
    actual_minutes = DataField(Integer)  # Jellyfin's probe of the stream
    jellyfin_item_id = DataField(String)
    dismissed = Column(Boolean, default=False)
    detected_at = Column(DateTime, default=datetime.utcnow)
    # None: flagged by its played length. "relist_followed": a fixed stream was
    # re-listed and the fix followed it; "relist_blocked": a re-listed stream is
    # labelled like one the admin blocked. The runtime check leaves these alone.
    reason = DataField(String)

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
    kind = DataField(String)  # download-delete | jellyfin-delete | provider-cascade | duplicate-resolve | orphan-sweep | vod-sweep | stale-cleanup | download-fix | stream-health
    name = DataField(String)  # Title or item label
    media_type = DataField(String)  # movie | series | None
    size_bytes = DataField(Integer)
    reason = DataField(String, default="manual")  # manual | auto | webhook
    detail = DataField(Text)  # What exactly happened
    user_name = DataField(String)  # Display name for manual actions
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ─── Stream Health ────────────────────────────────────────────────────────────

class StreamHealth(Base):
    """Known-bad registry for VOD .strm files whose streams no longer work.
    Entries are re-verified periodically and auto-cleared if the stream
    recovers — a bad mark must never silently become permanent bookkeeping."""
    __tablename__ = "stream_health"
    id = Column(Integer, primary_key=True, autoincrement=True)
    media_type = DataField(String)  # movie | series
    tmdb_id = DataField(Integer)
    title = DataField(String)
    episode = DataField(String)  # "S01E02" for series entries
    strm_path = Column(String, nullable=False)
    stream_url = DataField(String)
    fail_count = DataField(Integer, default=1)
    first_failed_at = Column(DateTime, default=datetime.utcnow)
    last_checked_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("strm_path", name="uq_stream_health_path"),
    )


# ─── Live TV Channels ────────────────────────────────────────────────────────

class LiveChannel(Base):
    __tablename__ = "live_channels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)

    # Channel identity
    name = Column(String, nullable=False)
    # The user's own name for the channel. Channel syncs rewrite `name` from
    # the provider every time and never touch this, so a rename survives.
    custom_name = DataField(String)
    channel_number = DataField(Integer)  # User-assignable
    stream_id = Column(String, nullable=True)  # Xtream stream_id or M3U index
    # M3U only: the hash of the channel's CURRENT name + URL, what a sync
    # matches the playlist by. stream_id is the GuideNumber Jellyfin keys the
    # channel, its timers and favourites on, so it keeps its first value when
    # the URL changes (#259). NULL (rows from before it existed) = stream_id.
    m3u_key = DataField(String)

    # Stream info
    stream_url = DataField(String)

    # Metadata
    logo_url = DataField(String)
    group_title = Column(String, nullable=True)  # Category/group from provider
    epg_channel_id = DataField(String)  # tvg-id for EPG matching
    # The admin's own guide id for the channel. Channel syncs never touch it,
    # and it wins over everything below (#141).
    epg_id_override = DataField(String)
    # The feed channel the last EPG sync matched by NAME, because the tvg-id
    # was missing or the feed did not carry it (#141), or the feed's spelling
    # of a tvg-id it lists only in other case (#523). Recomputed every sync.
    epg_name_match = DataField(String)

    # Management
    enabled = Column(Boolean, default=False)  # User must enable channels
    sort_order = Column(Integer, default=0)

    created_at = DataField(DateTime, default=datetime.utcnow)
    updated_at = DataField(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    provider = relationship("Provider", back_populates="live_channels")

    __table_args__ = (
        UniqueConstraint("provider_id", "stream_id", name="uq_live_channel_stream"),
        # The channel's group by its name. Not enforced (foreign keys are off):
        # an Xtream channel whose category the provider doesn't list has the
        # group "" and no live_channel_groups row.
        ForeignKeyConstraint(["provider_id", "group_title"],
                             ["live_channel_groups.provider_id", "live_channel_groups.name"],
                             onupdate="CASCADE", name="fk_live_channel_group"),
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
        """How guide_epg_id was chosen: "override", "name", "tvg-id" or None.
        A name match that is the tvg-id in other case is a tvg-id match (#523)."""
        if (self.epg_id_override or "").strip():
            return "override"
        tvg = (self.epg_channel_id or "").strip()
        if self.epg_name_match and self.epg_name_match.casefold() != tvg.casefold():
            return "name"
        return "tvg-id" if tvg else None


class LiveChannelGroup(Base):
    __tablename__ = "live_channel_groups"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    category_id = DataField(String)  # Provider's internal category ID
    enabled = Column(Boolean, default=False)  # Enable/disable entire group
    channel_count = DataField(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("provider_id", "name", name="uq_live_group"),
    )


class LiveUserLineup(Base):
    """One person's Live TV: everything the household has (the shared "All
    channels" tuner), or a list of their own (a tuner of their own in
    Jellyfin). No row = all channels."""
    __tablename__ = "live_user_lineups"
    id = Column(Integer, primary_key=True, autoincrement=True)
    jellyfin_user_id = Column(String, ForeignKey("tentacle_users.jellyfin_user_id", ondelete="CASCADE"),
                              unique=True, nullable=False, index=True)  # no dashes
    own_list = DataField(Boolean, default=False)
    # The address Jellyfin was given for this person's tuner, kept as it was
    # first given: Jellyfin keys every channel (and so its favourites and
    # recordings) on the tuner's URL, so it must never change by itself.
    tuner_url = DataField(String)
    # What Jellyfin was last set up with (own_list as of the last sync), so a
    # switch is noticed and the person's favourites are carried across.
    synced_own_list = DataField(Boolean)
    updated_at = DataField(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LiveUserChannel(Base):
    """A channel on someone's own list, in their order (their numbers are
    1, 2, 3... in this order): a provider's channel or a YouTube channel,
    exactly one of the two. `channel_key` names it the way the code and the
    API do: "c<live_channels.id>" or "y<youtube_channels.id>"."""
    __tablename__ = "live_user_channels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    jellyfin_user_id = Column(String, ForeignKey("tentacle_users.jellyfin_user_id", ondelete="CASCADE"),
                              nullable=False, index=True)
    live_channel_id = Column(Integer, ForeignKey("live_channels.id", ondelete="CASCADE"), nullable=True)
    youtube_channel_id = Column(Integer, ForeignKey("youtube_channels.id", ondelete="CASCADE"), nullable=True)
    position = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("jellyfin_user_id", "live_channel_id", name="uq_live_user_channel"),
        UniqueConstraint("jellyfin_user_id", "youtube_channel_id", name="uq_live_user_youtube_channel"),
        CheckConstraint("live_channel_id IS NULL OR youtube_channel_id IS NULL", name="ck_live_user_channel_one"),
    )

    @hybrid_property
    def channel_key(self):
        if self.live_channel_id is not None:
            return f"c{self.live_channel_id}"
        if self.youtube_channel_id is not None:
            return f"y{self.youtube_channel_id}"
        return None

    @channel_key.setter
    def channel_key(self, key):
        key = str(key or "")
        kind, number = key[:1], key[1:]
        if kind not in ("c", "y") or not number.isdigit():
            raise ValueError(f"not a channel key: {key!r}")
        self.live_channel_id = int(number) if kind == "c" else None
        self.youtube_channel_id = int(number) if kind == "y" else None

    @channel_key.expression
    def channel_key(cls):
        return case((cls.live_channel_id.isnot(None), literal("c") + cast(cls.live_channel_id, String)),
                    (cls.youtube_channel_id.isnot(None), literal("y") + cast(cls.youtube_channel_id, String)))


class EPGProgram(Base):
    __tablename__ = "epg_programs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(String, nullable=False, index=True)  # Matches epg_channel_id

    title = DataField(String)
    # The provider's <sub-title>: an episode or match name ("TOR vs MTL").
    sub_title = DataField(String)
    description = DataField(Text)
    start = Column(DateTime, nullable=False)
    stop = Column(DateTime, nullable=False)

    category = DataField(String)
    icon_url = DataField(String)

    __table_args__ = (
        UniqueConstraint("channel_id", "start", name="uq_epg_program"),
    )


for _table in Base.metadata.tables.values():
    _table.append_constraint(CheckConstraint("json_valid(data)", name=f"ck_{_table.name}_data"))


def log_activity(db, event: str, message: str, detail: dict = None):
    """Write an activity log entry.

    Messages often carry an exception's text, and httpx/requests put the whole
    URL in it -- /live/<user>/<pass>/ or ?username=&password= for an Xtream
    provider. The Activity feed is stored and served, so redact like the log."""
    from services.log_redaction import redact
    if isinstance(message, str):
        message = redact(message)
    if isinstance(detail, dict):
        detail = {k: (redact(v) if isinstance(v, str) else v) for k, v in detail.items()}
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
                        jellyfin_item_id: str = None, kind: str = "download",
                        force: bool = False):
    """Create a notification for a user if they have notifications enabled.
    `force`: an admin's request or problem message, which their own
    "download notifications" switch doesn't turn off."""
    user = db.query(TentacleUser).filter(TentacleUser.id == user_id).first()
    if not user or not (user.notifications_enabled or force):
        return None
    notif = Notification(
        user_id=user_id, tmdb_id=tmdb_id, media_type=media_type,
        title=title, message=message, poster_path=poster_path,
        jellyfin_item_id=jellyfin_item_id, kind=kind,
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


RECENTLY_ADDED_DAYS_MAX = 36500   # 100 years holds every title; timedelta overflows past year 1


def parse_recently_added_days(value):
    """Whole days from a stored or typed value, or None if no number can be read.

    The field is <input type="number">: "14.5", "7.0" and "1e2" arrive as
    typed, and a bare int() on them failed every sync and tag refresh (#536).
    """
    try:
        days = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):   # "abc"; nan, inf, 1e309 pass float() but not int()
        return None
    return min(max(days, 0), RECENTLY_ADDED_DAYS_MAX)


def get_recently_added_days(db) -> int:
    """The "Recently added" window in whole days, whatever an older save stored."""
    days = parse_recently_added_days(get_setting(db, "recently_added_days"))
    return int(NON_EMPTY_DEFAULTS["recently_added_days"]) if days is None else days


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


def _old_layout(cursor, table: str) -> bool:
    """The table exists and has no `data` column: a column per detail, as
    before the data-column layout (models/db_design.py)."""
    existing = _existing_columns(cursor, table)
    return bool(existing) and "data" not in existing


def _migrate_legacy_layout():
    """Bring the tables still in the old layout up to its last version
    (models/legacy_schema.py), so db_design.convert() starts from one known
    layout. Tables already in the data-column layout are left alone.

    Two passes:
      1. An explicit list for columns that need a specific SQL default / FK.
      2. A generic pass that adds every column of the last old layout that's
         absent, with its default.
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
    # Recreate tables that need PK changes (HomeRowOrder, AutoPlaylistToggle)
    # before the generic pass, which would otherwise give their old layout a
    # user_id column and nothing else (#295).
    _migrate_home_row_order(cursor, conn)
    _migrate_auto_playlist_toggles(cursor, conn)
    for table, column, col_type in migrations:
        existing = _existing_columns(cursor, table)
        if not existing or "data" in existing:
            continue  # no table yet (create_all makes it), or the data-column layout
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

    _add_missing_columns(cursor, conn, LEGACY, old_layout=True)

    _migrate_list_items_key(conn)
    _drop_retired_tables(cursor, conn)
    _reset_guessed_made_for_kids(cursor, conn)
    conn.close()


def _add_missing_columns(cursor, conn, metadata, old_layout: bool):
    """Add every column of `metadata`'s tables that a table lacks: the last old
    layout's (old_layout=True, only tables still in it) or this version's real
    columns (old_layout=False, only tables in the data-column layout).
    SQLite can't add NOT NULL columns without a default, so skip those."""
    for table_name, table_obj in metadata.tables.items():
        existing = _existing_columns(cursor, table_name)
        if not existing or ("data" in existing) == old_layout:
            continue  # table not created yet, or in the other layout
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
    if not _old_layout(cursor, "settings") or not _old_layout(cursor, "youtube_videos"):
        # A fresh install (create_all comes next), or the data-column layout,
        # which a database only reaches after this step
        return
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


def _recreate_per_user_table(conn, table, old_name, columns):
    """Recreate a pre-multi-user table with the model's id primary key (#295).

    Before multi-user support home_row_order and auto_playlist_toggles were
    keyed by playlist_id / key alone. The recreate is decided by the id
    column: user_id is no proof, because the generic pass adds it on its own.
    The whole copy is one transaction, and a copy left half done by an older
    build (its data only in `old_name`) is finished from there.
    """
    import sqlite3
    from sqlalchemy.schema import CreateIndex, CreateTable

    cursor = conn.cursor()
    model = LEGACY.tables[table]
    has_old = bool(cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (old_name,)).fetchone())
    existing = _existing_columns(cursor, table)
    if not has_old and (not existing or "id" in existing):
        return  # current schema, a fresh install, or no table yet
    conn.commit()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        if existing and "id" not in existing:
            if has_old:
                raise RuntimeError(f"both {table} (old layout) and {old_name} exist")
            cursor.execute(f"ALTER TABLE {table} RENAME TO {old_name}")
            existing = set()
        old_cols = _existing_columns(cursor, old_name)
        if not existing:
            cursor.execute(str(CreateTable(model).compile(dialect=engine.dialect)))
        keep = [c for c in columns if c in old_cols]
        cols = ", ".join(f'"{c}"' for c in keep)
        cursor.execute(f"INSERT OR IGNORE INTO {table} ({cols}) SELECT {cols} FROM {old_name}")
        copied = cursor.rowcount
        cursor.execute(f"DROP TABLE {old_name}")
        if not existing:
            for index in model.indexes:
                cursor.execute(str(CreateIndex(index).compile(dialect=engine.dialect)))
        conn.commit()
        logger.info(f"[migrate] Recreated {table} for per-user rows ({copied} row(s) kept)")
    except (sqlite3.Error, RuntimeError) as e:
        conn.rollback()
        logger.error(f"[migrate] Could not recreate {table} with an id column: {e}")


def _migrate_list_items_key(conn):
    """Rebuild list_items with the unique key (list_id, tmdb_id, media_type) (#365).

    The old key (list_id, tmdb_id) let a list hold only one of a film and a
    show with the same TMDB number. SQLite can't change a constraint in place,
    so the table is recreated in one transaction. Rows without a type are
    copied as films: SQLite treats NULLs as distinct in a UNIQUE key, so an
    untyped row would escape it. Existing rows already satisfy the new key.
    """
    import sqlite3
    from sqlalchemy.schema import CreateIndex, CreateTable

    cursor = conn.cursor()
    old_key = False
    for index in cursor.execute("PRAGMA index_list(list_items)").fetchall():
        if index[2]:  # unique
            cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{index[1]}")')]
            old_key = old_key or cols == ["list_id", "tmdb_id"]
    if not old_key:
        return  # current schema, a fresh install, or no table yet
    model = LEGACY.tables["list_items"]
    old_cols = _existing_columns(cursor, "list_items")
    cols = [c.name for c in model.columns if c.name in old_cols]
    select = ", ".join("COALESCE(media_type, 'movie')" if c == "media_type" else f'"{c}"' for c in cols)
    conn.commit()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("ALTER TABLE list_items RENAME TO _list_items_old")
        cursor.execute(str(CreateTable(model).compile(dialect=engine.dialect)))
        cursor.execute(f"INSERT INTO list_items ({', '.join(cols)}) SELECT {select} FROM _list_items_old")
        copied = cursor.rowcount
        cursor.execute("DROP TABLE _list_items_old")
        for index in model.indexes:
            cursor.execute(str(CreateIndex(index).compile(dialect=engine.dialect)))
        conn.commit()
        logger.info(f"[migrate] Rebuilt list_items with a key per film/show ({copied} row(s) kept)")
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"[migrate] Could not rebuild list_items with a key per film/show: {e}")


def _migrate_home_row_order(cursor, conn):
    """Recreate home_row_order with id PK + user_id column."""
    _recreate_per_user_table(conn, "home_row_order", "_home_row_order_old",
                             ("user_id", "playlist_id", "display_order"))


def _migrate_auto_playlist_toggles(cursor, conn):
    """Recreate auto_playlist_toggles with id PK + user_id column."""
    _recreate_per_user_table(conn, "auto_playlist_toggles", "_auto_toggles_old",
                             ("user_id", "key", "enabled"))


def create_tables():
    """Bring the database to this version's layout: tables in the old layout
    first to its last version, then once to the data-column layout (one
    transaction, a copy of the database first: models/db_design.py), then the
    tables and real columns this version adds. A conversion that fails stops
    the start: the database is as it was, and the models can't use it."""
    from models import db_design
    _migrate_legacy_layout()
    db_design.convert(DATABASE_URL.replace("sqlite:///", ""))
    Base.metadata.create_all(bind=engine)
    conn = sqlite3.connect(DATABASE_URL.replace("sqlite:///", ""))
    try:
        _add_missing_columns(conn.cursor(), conn, Base.metadata, old_layout=False)
    finally:
        conn.close()


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
    _forget_stored_builtin_tmdb_token(db)
    _one_tentacle_address(db)


# Tentacle's own address used to be asked in six places. One is kept, saved
# as `youtube_base_url` (Settings → Services, "Tentacle's address"); the
# others were filled in by older dashboards and the setup wizard.
_OLD_ADDRESS_KEYS = ("vod_base_url", "hdhr_base_url", "external_url", "webhook_host", "sonarr_webhook_host")


def tentacle_address(db) -> str:
    """Tentacle's address as Jellyfin and the Arr apps reach it, or ""."""
    return (get_setting(db, "youtube_base_url", "") or "").strip().rstrip("/")


def _one_tentacle_address(db):
    """Once: an install that has no Tentacle address yet takes the first one
    an older setting holds (with http:// for a bare host:port). The old keys
    are left as they are: vod_base_url is what the .strm files carry, and
    changing it repoints every file, which is the admin's call (Services)."""
    if tentacle_address(db):
        return
    for key in _OLD_ADDRESS_KEYS:
        val = (get_setting(db, key, "") or "").strip().rstrip("/")
        if not val:
            continue
        if not val.lower().startswith(("http://", "https://")):
            val = f"http://{val}"
        set_setting(db, "youtube_base_url", val)
        logger.info(f"[migrate] Tentacle's address is now kept in one place: {val} (was {key})")
        return


def _forget_stored_builtin_tmdb_token(db):
    """A stored tmdb_bearer_token equal to the built-in token means "none of
    my own": until #383 any Settings save stored the built-in token that
    GET /api/settings/raw filled into the field. Clear it, so the field shows
    "Using built-in key" again and a later built-in token reaches this install.
    The token every TMDB call uses stays the same (get_tmdb_token falls back
    to the built-in one), and a token of the user's own is never equal to it.
    """
    from services.tmdb import TMDB_DEFAULT_TOKEN
    row = db.query(Setting).filter(Setting.key == "tmdb_bearer_token",
                                   Setting.value == TMDB_DEFAULT_TOKEN).first()
    # With a v3 tmdb_api_key stored, /plugin-keys stops adding the built-in
    # bearer once none is stored: the plugin would switch to that key. Keep it.
    if row is not None and not get_setting(db, "tmdb_api_key"):
        row.value = ""
        db.commit()
        logger.info("[migrate] Cleared a stored copy of the built-in TMDB token (Settings shows the built-in key again)")


def migrate_orphaned_data_to_user(db, user_id: int):
    """Assign any user_id=NULL rows to a user (called on first admin login for migration)."""
    for model in [ListSubscription, TagRule, HomeRowOrder, AutoPlaylistToggle]:
        db.query(model).filter(model.user_id == None).update({"user_id": user_id})
    db.commit()

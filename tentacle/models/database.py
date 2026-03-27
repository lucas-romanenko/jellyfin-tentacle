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

_data_dir = os.getenv('DATA_DIR', '/data')
_db_name = "tentacle.db"
if not os.path.exists(os.path.join(_data_dir, _db_name)) and os.path.exists(os.path.join(_data_dir, "mediahub.db")):
    _db_name = "mediahub.db"
DATABASE_URL = f"sqlite:///{_data_dir}/{_db_name}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})
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

    # Dates
    date_added = Column(DateTime, default=datetime.utcnow)
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

    date_added = Column(DateTime, default=datetime.utcnow)
    date_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # trakt | letterboxd | imdb_rss
    url = Column(String, nullable=False)
    tag = Column(String, nullable=False)  # Tag to apply to matched content
    active = Column(Boolean, default=True)
    auto_add_radarr = Column(Boolean, default=False)
    playlist_enabled = Column(Boolean, default=False)  # Generate a Jellyfin playlist from this list
    last_fetched = Column(DateTime, nullable=True)
    last_item_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

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
    name = Column(String, nullable=False)
    conditions = Column(JSON, default=list)  # [{field, operator, value}]
    output_tag = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    apply_to = Column(String, default="both")  # movies | series | both
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── Home Row Order (display_order persistence for SmartList rows) ───────────

class HomeRowOrder(Base):
    __tablename__ = "home_row_order"
    playlist_id = Column(String, primary_key=True)
    display_order = Column(Integer, nullable=False, default=0)


# ─── Auto Playlist Toggles ────────────────────────────────────────────────────

class AutoPlaylistToggle(Base):
    __tablename__ = "auto_playlist_toggles"
    key = Column(String, primary_key=True)  # e.g. "source:Netflix:movies", "builtin:recently_added_movies"
    enabled = Column(Boolean, default=False)


# ─── Activity Log ─────────────────────────────────────────────────────────────

class ActivityLog(Base):
    __tablename__ = "activity_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event = Column(String, nullable=False)       # e.g. "vod_sync", "radarr_scan", "radarr_add", "sonarr_add", "list_fetch", "jellyfin_push"
    message = Column(String, nullable=False)      # Human-readable: "VOD sync completed — 12 new movies, 3 new series"
    detail = Column(JSON, nullable=True)          # Optional extra data
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ─── Live TV Channels ────────────────────────────────────────────────────────

class LiveChannel(Base):
    __tablename__ = "live_channels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False, index=True)

    # Channel identity
    name = Column(String, nullable=False)
    channel_number = Column(Integer, nullable=True)  # User-assignable
    stream_id = Column(String, nullable=True)  # Xtream stream_id or M3U index

    # Stream info
    stream_url = Column(String, nullable=False)

    # Metadata
    logo_url = Column(String, nullable=True)
    group_title = Column(String, nullable=True)  # Category/group from provider
    epg_channel_id = Column(String, nullable=True)  # tvg-id for EPG matching

    # Management
    enabled = Column(Boolean, default=False)  # User must enable channels
    sort_order = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    provider = relationship("Provider", back_populates="live_channels")

    __table_args__ = (
        UniqueConstraint("provider_id", "stream_id", name="uq_live_channel_stream"),
    )


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


def get_setting(db, key: str, default: str = "") -> str:
    """Get a single setting value by key"""
    s = db.query(Setting).filter(Setting.key == key).first()
    return s.value if s else default


def set_setting(db, key: str, value: str):
    """Set a single setting value"""
    s = db.query(Setting).filter(Setting.key == key).first()
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def _migrate_columns():
    """Add columns that may be missing from older databases."""
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
    ]
    for table, column, col_type in migrations:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
    conn.close()


def create_tables():
    Base.metadata.create_all(bind=engine)
    _migrate_columns()


def seed_defaults(db):
    """Seed default settings if not present"""
    defaults = {
        "tmdb_bearer_token": "",
        "tmdb_api_key": "",
        "radarr_url": "",
        "radarr_api_key": "",
        "sonarr_url": "",
        "sonarr_api_key": "",
        "jellyfin_url": "",
        "jellyfin_api_key": "",
        "sync_schedule": "0 3 * * *",
        "recently_added_days": "30",
        "tmdb_match_threshold": "0.7",
        "smartlists_path": "/data/smartlists",
        "jellyfin_user_id": "",
        "jellyfin_user_name": "",
        "logodev_api_key": "",
        "trakt_client_id": "",
        "home_row_limit": "20",
        "setup_complete": "false",
        "data_dir": os.getenv("DATA_DIR", "/data"),
        "discover_in_jellyfin": "false",
        "hdhr_tuner_count": "3",
        "hdhr_device_id": "TENTACLE1",
    }
    for key, value in defaults.items():
        existing = db.query(Setting).filter(Setting.key == key).first()
        if not existing:
            db.add(Setting(key=key, value=value))
    db.commit()

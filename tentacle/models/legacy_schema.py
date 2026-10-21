"""The tables as they were before the db-design layout (Tentacle 1.11, main 68d3ef2).

Frozen: the startup steps that bring an older database up to that layout
(models/database.py _migrate_legacy_layout) and the way back
(models/db_design.py revert) use these, never the current models. Generated
from the old models; do not edit."""
from sqlalchemy import (MetaData, Table, Column, Integer, String, Boolean, Float,
                        DateTime, Text, JSON, ForeignKey, UniqueConstraint)
from datetime import datetime

LEGACY = MetaData()

Table("settings", LEGACY,
      Column('key', String, primary_key=True),
      Column('value', Text),
      Column('updated_at', DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
)

Table("providers", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('name', String, nullable=False),
      Column('provider_type', String, default='xtream'),
      Column('server_url', String, nullable=False),
      Column('username', String, nullable=False),
      Column('password', String, nullable=False),
      Column('active', Boolean, default=False),
      Column('priority', Integer, default=1),
      Column('status', String, default='untested'),
      Column('last_tested', DateTime),
      Column('expiry', DateTime),
      Column('max_connections', Integer),
      Column('m3u_url', String),
      Column('epg_url', String),
      Column('user_agent', String, default='TiviMate/4.7.0 (Linux; Android 12)'),
      Column('live_tv_enabled', Boolean, default=False),
      Column('last_live_sync', DateTime),
      Column('has_live', Boolean, default=False),
      Column('has_vod', Boolean, default=False),
      Column('has_series', Boolean, default=False),
      Column('require_tmdb_match', Boolean, default=True),
      Column('created_at', DateTime, default=datetime.utcnow),
      Column('updated_at', DateTime, default=datetime.utcnow, onupdate=datetime.utcnow),
      Column('address_auto_switch', String, default='switch'),
      Column('preferred_address', String),
      Column('live_max_streams', Integer),
      Column('channel_number_offset', Integer)
)

Table("provider_addresses", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('provider_id', Integer, ForeignKey("providers.id"), nullable=False, index=True),
      Column('url', String, nullable=False),
      Column('added_at', DateTime, default=datetime.utcnow),
      Column('last_result', JSON),
      Column('history', JSON, default=list),
      UniqueConstraint('provider_id', 'url', name="uq_provider_address")
)

Table("provider_categories", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('provider_id', Integer, ForeignKey("providers.id"), nullable=False, index=True),
      Column('category_id', String, nullable=False),
      Column('category_name', String, nullable=False),
      Column('type', String, nullable=False),
      Column('whitelisted', Boolean, default=False),
      Column('source_tag', String),
      Column('title_count', Integer, default=0),
      Column('last_seen', DateTime, default=datetime.utcnow),
      Column('last_sync_matched', Integer),
      Column('last_sync_skipped', Integer),
      Column('consecutive_empty_syncs', Integer, default=0),
      UniqueConstraint('provider_id', 'category_id', 'type', name="uq_provider_category")
)

Table("category_snapshots", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('category_id', Integer, ForeignKey("provider_categories.id"), nullable=False),
      Column('title_count', Integer, default=0),
      Column('new_count', Integer, default=0),
      Column('removed_count', Integer, default=0),
      Column('recorded_at', DateTime, default=datetime.utcnow)
)

Table("movies", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('tmdb_id', Integer, nullable=False, unique=True),
      Column('title', String, nullable=False),
      Column('year', String),
      Column('overview', Text),
      Column('runtime', Integer),
      Column('rating', Float),
      Column('genres', JSON, default=list),
      Column('poster_path', String),
      Column('backdrop_path', String),
      Column('source', String, nullable=False, index=True),
      Column('provider_id', Integer, ForeignKey("providers.id"), index=True),
      Column('strm_path', String),
      Column('nfo_path', String),
      Column('radarr_path', String),
      Column('source_tag', String),
      Column('tags', JSON, default=list),
      Column('jellyfin_item_id', String),
      Column('provider_missing_since', DateTime),
      Column('file_missing_since', DateTime),
      Column('strm_disabled', Boolean, default=False),
      Column('date_added', DateTime, default=datetime.utcnow),
      Column('downloaded_at', DateTime),
      Column('date_updated', DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
)

Table("series", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('tmdb_id', Integer, nullable=False, unique=True),
      Column('title', String, nullable=False),
      Column('year', String),
      Column('overview', Text),
      Column('genres', JSON, default=list),
      Column('poster_path', String),
      Column('backdrop_path', String),
      Column('rating', Float),
      Column('status', String),
      Column('source', String, nullable=False, index=True),
      Column('provider_id', Integer, ForeignKey("providers.id"), index=True),
      Column('strm_path', String),
      Column('nfo_path', String),
      Column('sonarr_path', String),
      Column('source_tag', String),
      Column('tags', JSON, default=list),
      Column('sonarr_monitored', Boolean, default=False),
      Column('provider_series_ids', JSON),
      Column('jellyfin_item_id', String),
      Column('last_downloaded_episode', String),
      Column('provider_missing_since', DateTime),
      Column('file_missing_since', DateTime),
      Column('strm_disabled', Boolean, default=False),
      Column('date_added', DateTime, default=datetime.utcnow),
      Column('downloaded_at', DateTime),
      Column('date_updated', DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
)

Table("youtube_channels", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('input_url', String, nullable=False),
      Column('kind', String, default='channel'),
      Column('channel_id', String, index=True),
      Column('handle', String),
      Column('playlist_id', String),
      Column('title', String, nullable=False),
      Column('slug', String, nullable=False, unique=True),
      Column('avatar_url', String),
      Column('banner_url', String),
      Column('include_videos', Boolean, default=True),
      Column('include_streams', Boolean, default=False),
      Column('include_shorts', Boolean, default=False),
      Column('min_duration', Integer, default=0),
      Column('keep_count', Integer, default=10),
      Column('keep_days', Integer),
      Column('max_height', Integer, default=1080),
      Column('rating', String),
      Column('extra_tags', JSON, default=list),
      Column('enabled', Boolean, default=True),
      Column('live_enabled', Boolean, default=False),
      Column('channel_number', String),
      Column('last_checked', DateTime),
      Column('last_full_check', DateTime),
      Column('feed_ids', JSON),
      Column('playlist_size', Integer),
      Column('last_error', String),
      Column('error_count', Integer, default=0),
      Column('last_skips', JSON, default=dict),
      Column('last_indexed_count', Integer, default=0),
      Column('last_listing', JSON, default=dict),
      Column('blocked_until', DateTime),
      Column('created_at', DateTime, default=datetime.utcnow)
)

Table("youtube_videos", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('channel_fk', Integer, ForeignKey("youtube_channels.id"), nullable=False, index=True),
      Column('video_id', String, nullable=False, unique=True, index=True),
      Column('title', String, nullable=False),
      Column('description', Text),
      Column('published_at', DateTime),
      Column('duration', Integer),
      Column('live_status', String),
      Column('media_type', String, default='video'),
      Column('thumbnail_url', String),
      Column('is_made_for_kids', Boolean),
      Column('folder_path', String),
      Column('strm_path', String),
      Column('first_seen', DateTime, default=datetime.utcnow),
      Column('last_seen', DateTime, default=datetime.utcnow),
      Column('removed_at', DateTime),
      Column('next_check_at', DateTime),
      Column('check_failures', Integer, default=0),
      Column('skip_reason', String)
)

Table("duplicates", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('tmdb_id', Integer, nullable=False, index=True),
      Column('media_type', String, nullable=False),
      Column('sources', JSON, default=list),
      Column('resolution', String, default='pending'),
      Column('detected_at', DateTime, default=datetime.utcnow),
      Column('resolved_at', DateTime),
      Column('pending_user_data', JSON(none_as_null=True))
)

Table("sync_runs", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('provider_id', Integer, ForeignKey("providers.id"), nullable=False, index=True),
      Column('status', String, index=True, default='running'),
      Column('sync_type', String, default='full'),
      Column('movies_new', Integer, default=0),
      Column('movies_existing', Integer, default=0),
      Column('movies_failed', Integer, default=0),
      Column('movies_skipped', Integer, default=0),
      Column('series_new', Integer, default=0),
      Column('series_existing', Integer, default=0),
      Column('series_failed', Integer, default=0),
      Column('series_skipped', Integer, default=0),
      Column('category_stats', JSON, default=dict),
      Column('new_movies', JSON, default=list),
      Column('new_series', JSON, default=list),
      Column('new_categories', JSON, default=list),
      Column('error_message', Text),
      Column('started_at', DateTime, default=datetime.utcnow),
      Column('completed_at', DateTime),
      Column('duration_seconds', Integer)
)

Table("list_subscriptions", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('user_id', Integer, ForeignKey("tentacle_users.id"), index=True),
      Column('name', String, nullable=False),
      Column('type', String, nullable=False),
      Column('url', String, nullable=False),
      Column('tag', String, nullable=False),
      Column('active', Boolean, default=True),
      Column('auto_add_radarr', Boolean, default=False),
      Column('playlist_enabled', Boolean, default=False),
      Column('last_fetched', DateTime),
      Column('last_item_count', Integer, default=0),
      Column('last_fetch_note', String),
      Column('created_at', DateTime, default=datetime.utcnow)
)

Table("list_items", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('list_id', Integer, ForeignKey("list_subscriptions.id"), nullable=False, index=True),
      Column('tmdb_id', Integer, index=True),
      Column('imdb_id', String, index=True),
      Column('media_type', String, default='movie'),
      Column('title', String),
      Column('year', String),
      Column('poster_path', String),
      Column('added_at', DateTime, default=datetime.utcnow),
      UniqueConstraint('list_id', 'tmdb_id', 'media_type', name="uq_list_item")
)

Table("tag_rules", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('user_id', Integer, ForeignKey("tentacle_users.id"), index=True),
      Column('name', String, nullable=False),
      Column('conditions', JSON, default=list),
      Column('output_tag', String, nullable=False),
      Column('active', Boolean, default=True),
      Column('apply_to', String, default='both'),
      Column('created_at', DateTime, default=datetime.utcnow)
)

Table("home_row_order", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('user_id', Integer, ForeignKey("tentacle_users.id"), index=True),
      Column('playlist_id', String, nullable=False),
      Column('display_order', Integer, nullable=False, default=0),
      UniqueConstraint('user_id', 'playlist_id', name="uq_home_row_user_playlist")
)

Table("auto_playlist_toggles", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('user_id', Integer, ForeignKey("tentacle_users.id"), index=True),
      Column('key', String, nullable=False),
      Column('enabled', Boolean, default=False),
      UniqueConstraint('user_id', 'key', name="uq_auto_toggle_user_key")
)

Table("tentacle_users", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('jellyfin_user_id', String, nullable=False, unique=True, index=True),
      Column('display_name', String, nullable=False),
      Column('is_admin', Boolean, default=False),
      Column('profile_image_tag', String),
      Column('notifications_enabled', Boolean, default=True),
      Column('session_version', Integer, default=0),
      Column('created_at', DateTime, default=datetime.utcnow),
      Column('kids_level', String),
      Column('kids_saved_policy', Text),
      Column('request_mode', String, default='ask'),
      Column('invited_by', String),
      Column('invited_at', DateTime)
)

Table("notifications", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('user_id', Integer, ForeignKey("tentacle_users.id"), nullable=False, index=True),
      Column('tmdb_id', Integer, nullable=False),
      Column('media_type', String, nullable=False, default='movie'),
      Column('title', String, nullable=False),
      Column('message', String, nullable=False),
      Column('poster_path', String),
      Column('jellyfin_item_id', String),
      Column('created_at', DateTime, index=True, default=datetime.utcnow),
      Column('dismissed_at', DateTime),
      Column('kind', String, default='download')
)

Table("download_requests", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('tmdb_id', Integer, nullable=False, index=True),
      Column('media_type', String, nullable=False, default='movie'),
      Column('user_id', Integer, ForeignKey("tentacle_users.id"), nullable=False),
      Column('created_at', DateTime, default=datetime.utcnow),
      UniqueConstraint('tmdb_id', 'media_type', name="uq_download_request")
)

Table("media_requests", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('user_id', Integer, ForeignKey("tentacle_users.id"), nullable=False, index=True),
      Column('kind', String, nullable=False),
      Column('tmdb_id', Integer, index=True),
      Column('tvdb_id', Integer),
      Column('title', String, nullable=False, default=''),
      Column('year', String),
      Column('poster_path', String),
      Column('payload', Text, nullable=False, default='{}'),
      Column('status', String, nullable=False, index=True, default='pending'),
      Column('note', String),
      Column('decided_by', String),
      Column('created_at', DateTime, index=True, default=datetime.utcnow),
      Column('decided_at', DateTime)
)

Table("admin_alerts", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('key', String, nullable=False, unique=True),
      Column('area', String, nullable=False),
      Column('title', String, nullable=False),
      Column('message', String, nullable=False),
      Column('active', Boolean, index=True, default=True),
      Column('started_at', DateTime, default=datetime.utcnow),
      Column('last_seen_at', DateTime, default=datetime.utcnow),
      Column('resolved_at', DateTime)
)

Table("music_artists", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('mbid', String, nullable=False, unique=True, index=True),
      Column('lidarr_artist_id', Integer, index=True),
      Column('name', String, nullable=False, default=''),
      Column('sort_name', String, default=''),
      Column('disambiguation', String, default=''),
      Column('path', String, default=''),
      Column('updated_at', DateTime, default=datetime.utcnow),
      Column('picture_status', String, index=True, default=''),
      Column('picture_source', String, default=''),
      Column('picture_note', Text),
      Column('picture_candidates', JSON),
      Column('picture_checked_at', DateTime)
)

Table("music_albums", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('mbid', String, nullable=False, unique=True, index=True),
      Column('lidarr_album_id', Integer, index=True),
      Column('lidarr_artist_id', Integer, index=True),
      Column('artist_mbid', String, index=True, default=''),
      Column('artist_name', String, default=''),
      Column('title', String, nullable=False, default=''),
      Column('album_type', String, default=''),
      Column('secondary_types', String, default=''),
      Column('release_date', String, default=''),
      Column('monitored', Boolean, default=False),
      Column('any_release_ok', Boolean, default=True),
      Column('track_count', Integer, default=0),
      Column('track_file_count', Integer, default=0),
      Column('size_on_disk', Integer, default=0),
      Column('cover_url', String, default=''),
      Column('category', String, index=True, default=''),
      Column('verdict', JSON),
      Column('checked_at', DateTime),
      Column('check_error', Text),
      Column('requested_by', Integer, ForeignKey("tentacle_users.id")),
      Column('requested_at', DateTime),
      Column('request_pending', Boolean, default=False),
      Column('request_choice', JSON),
      Column('updated_at', DateTime, default=datetime.utcnow)
)

Table("music_imports", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('user_id', Integer, ForeignKey("tentacle_users.id"), index=True),
      Column('name', String, default=''),
      Column('source', String, default=''),
      Column('url', String, default=''),
      Column('status', String, default='resolving'),
      Column('done', Integer, default=0),
      Column('total', Integer, default=0),
      Column('tracks', JSON),
      Column('outcomes', JSON),
      Column('left_out', Integer, default=0),
      Column('error', Text),
      Column('created_at', DateTime, default=datetime.utcnow),
      Column('updated_at', DateTime, default=datetime.utcnow)
)

Table("activity_log", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('event', String, nullable=False),
      Column('message', String, nullable=False),
      Column('detail', JSON),
      Column('created_at', DateTime, index=True, default=datetime.utcnow)
)

Table("blocked_streams", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('provider_id', Integer, ForeignKey("providers.id"), nullable=False, index=True),
      Column('media_type', String, nullable=False, default='movie'),
      Column('stream_key', String, nullable=False),
      Column('tmdb_id', Integer),
      Column('title', String),
      Column('reason', String),
      Column('blocked_by', String),
      Column('created_at', DateTime, default=datetime.utcnow),
      Column('label', String),
      UniqueConstraint('provider_id', 'media_type', 'stream_key', name="uq_blocked_stream")
)

Table("match_overrides", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('provider_id', Integer, ForeignKey("providers.id"), nullable=False, index=True),
      Column('media_type', String, nullable=False, default='movie'),
      Column('stream_key', String, nullable=False),
      Column('tmdb_id', Integer, nullable=False),
      Column('previous_tmdb_id', Integer),
      Column('title', String),
      Column('set_by', String),
      Column('created_at', DateTime, default=datetime.utcnow),
      Column('label', String),
      Column('moved_from', String),
      UniqueConstraint('provider_id', 'media_type', 'stream_key', name="uq_match_override")
)

Table("match_suspects", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('tmdb_id', Integer, nullable=False),
      Column('media_type', String, nullable=False, default='movie'),
      Column('title', String),
      Column('expected_minutes', Integer),
      Column('actual_minutes', Integer),
      Column('jellyfin_item_id', String),
      Column('dismissed', Boolean, default=False),
      Column('detected_at', DateTime, default=datetime.utcnow),
      Column('reason', String),
      UniqueConstraint('tmdb_id', 'media_type', name="uq_match_suspect")
)

Table("deletion_log", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('kind', String, nullable=False),
      Column('name', String, nullable=False),
      Column('media_type', String),
      Column('size_bytes', Integer),
      Column('reason', String, nullable=False, default='manual'),
      Column('detail', Text),
      Column('user_name', String),
      Column('created_at', DateTime, index=True, default=datetime.utcnow)
)

Table("stream_health", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('media_type', String, nullable=False),
      Column('tmdb_id', Integer, nullable=False, index=True),
      Column('title', String, nullable=False),
      Column('episode', String),
      Column('strm_path', String, nullable=False),
      Column('stream_url', String),
      Column('fail_count', Integer, default=1),
      Column('first_failed_at', DateTime, default=datetime.utcnow),
      Column('last_checked_at', DateTime, default=datetime.utcnow),
      UniqueConstraint('strm_path', name="uq_stream_health_path")
)

Table("live_channels", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('provider_id', Integer, ForeignKey("providers.id"), nullable=False, index=True),
      Column('name', String, nullable=False),
      Column('custom_name', String),
      Column('channel_number', Integer),
      Column('stream_id', String),
      Column('m3u_key', String),
      Column('stream_url', String, nullable=False),
      Column('logo_url', String),
      Column('group_title', String),
      Column('epg_channel_id', String),
      Column('epg_id_override', String),
      Column('epg_name_match', String),
      Column('enabled', Boolean, default=False),
      Column('sort_order', Integer, default=0),
      Column('created_at', DateTime, default=datetime.utcnow),
      Column('updated_at', DateTime, default=datetime.utcnow, onupdate=datetime.utcnow),
      UniqueConstraint('provider_id', 'stream_id', name="uq_live_channel_stream")
)

Table("live_channel_groups", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('provider_id', Integer, ForeignKey("providers.id"), nullable=False, index=True),
      Column('name', String, nullable=False),
      Column('category_id', String),
      Column('enabled', Boolean, default=False),
      Column('channel_count', Integer, default=0),
      UniqueConstraint('provider_id', 'name', name="uq_live_group")
)

Table("live_user_lineups", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('jellyfin_user_id', String, nullable=False, unique=True, index=True),
      Column('own_list', Boolean, default=False),
      Column('tuner_url', String),
      Column('synced_own_list', Boolean),
      Column('updated_at', DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
)

Table("live_user_channels", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('jellyfin_user_id', String, nullable=False, index=True),
      Column('channel_key', String, nullable=False),
      Column('position', Integer, nullable=False, default=0),
      UniqueConstraint('jellyfin_user_id', 'channel_key', name="uq_live_user_channel")
)

Table("epg_programs", LEGACY,
      Column('id', Integer, primary_key=True, autoincrement=True),
      Column('channel_id', String, nullable=False, index=True),
      Column('title', String, nullable=False),
      Column('sub_title', String),
      Column('description', Text),
      Column('start', DateTime, nullable=False),
      Column('stop', DateTime, nullable=False),
      Column('category', String),
      Column('icon_url', String),
      UniqueConstraint('channel_id', 'start', name="uq_epg_program")
)

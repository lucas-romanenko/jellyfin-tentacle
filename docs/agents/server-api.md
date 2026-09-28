# Tentacle server: HTTP API

Every route of the FastAPI app (`tentacle/routers/*.py`), generated from
the code on 2026-09-28 (268 routes). Update it when you add, remove or
change a route; list them with

```
grep -n -E '@\w+\.(get|post|put|delete|patch)\(' tentacle/routers/*.py
```

Auth, as the handler gets it (router `dependencies`, its `Depends(...)`
parameters, or a helper it calls):

- **admin**: `require_admin` (a router-level dependency on settings,
  providers, sync, radarr, sonarr, duplicates, collections, health, music
  management, most Live TV management).
- **admin or internal**: `require_internal_or_admin`: an admin, or the
  plugin/server calling with the internal secret.
- **user**: `get_user_from_request` / `get_current_user` (dashboard cookie,
  or a verified Jellyfin token from the plugin or the TV app), or a helper
  built on them (`music_user`).
- **none**: no user auth by design: login and the user picker, the
  HDHomeRun emulation and guide (Jellyfin's tuner), stream URLs addressed by
  tokens, the image proxy, `smartlists/version`, the widget, and the
  Radarr/Sonarr webhooks (protected only by the optional `webhook_secret`
  query parameter; set it) and the music webhook (secret always required).

| Method | Path | Auth | Handler |
|---|---|---|---|
| GET | `/api/activity` | user | activity.py:get_activity |
| POST | `/api/activity/arr/check` | user | activity.py:check_releases |
| POST | `/api/activity/arr/grab` | user | activity.py:grab_release |
| POST | `/api/activity/arr/remove` | user | activity.py:remove_from_arr |
| POST | `/api/activity/arr/search` | user | activity.py:search_again |
| POST | `/api/activity/arr/stop-missing` | user | activity.py:stop_missing |
| POST | `/api/auth/login` | none | auth.py:login |
| POST | `/api/auth/logout` | none | auth.py:logout |
| GET | `/api/auth/managed-users` | admin | auth.py:get_managed_users |
| GET | `/api/auth/me` | user | auth.py:get_me |
| POST | `/api/auth/set-admin` | admin | auth.py:set_user_admin |
| GET | `/api/auth/users` | none | auth.py:get_jellyfin_users |
| POST | `/api/collections/sync-artwork` | admin | collections.py:sync_artwork |
| GET | `/api/discover` | user | discover.py:get_discover |
| GET | `/api/discover/config` | none | discover.py:get_discover_config |
| GET | `/api/discover/detail-tvdb/{tvdb_id}` | user | discover.py:get_discover_detail_tvdb |
| GET | `/api/discover/detail/{media_type}/{tmdb_id}` | user | discover.py:get_discover_detail |
| GET | `/api/discover/genre` | user | discover.py:get_discover_by_genre |
| GET | `/api/discover/genres` | user | discover.py:get_discover_genres |
| GET | `/api/discover/image-proxy/{cache_key}` | none | discover.py:image_proxy |
| GET | `/api/discover/list-missing` | user | discover.py:get_discover_list_missing |
| GET | `/api/discover/lists` | user | discover.py:get_discover_lists |
| POST | `/api/discover/manage-episodes` | user | discover.py:manage_episodes |
| GET | `/api/discover/providers` | user | discover.py:get_streaming_providers |
| GET | `/api/discover/search` | user | discover.py:search_discover |
| GET | `/api/discover/season-tvdb/{tvdb_id}/{season_number}` | user | discover.py:get_season_episodes_tvdb |
| GET | `/api/discover/season/{tmdb_id}/{season_number}` | user | discover.py:get_season_episodes |
| GET | `/api/discover/seasons-tvdb/{tvdb_id}` | user | discover.py:get_seasons_tvdb |
| GET | `/api/discover/seasons/{tmdb_id}` | user | discover.py:get_seasons |
| GET | `/api/discover/sonarr-episodes/{tmdb_id}` | user | discover.py:get_sonarr_episodes |
| GET | `/api/discover/streaming` | user | discover.py:get_new_on_streaming |
| GET | `/api/discover/vod-episodes/{tmdb_id}` | user | discover.py:get_vod_episodes |
| GET | `/api/duplicates` | admin | duplicates.py:get_duplicates |
| POST | `/api/duplicates/resolve-all` | admin | duplicates.py:resolve_all |
| POST | `/api/duplicates/{dup_id}/resolve` | admin | duplicates.py:resolve_duplicate |
| GET | `/api/health/arr-problems` | admin | health.py:arr_problems |
| GET | `/api/health/deletions` | admin | health.py:get_deletions |
| POST | `/api/health/downloads/fix` | admin | health.py:fix_stuck_download |
| POST | `/api/health/downloads/manual-import` | admin | health.py:manual_import_download |
| POST | `/api/health/downloads/remove` | admin | health.py:remove_queued_download |
| GET | `/api/health/downloads/settings` | admin | health.py:get_download_settings |
| POST | `/api/health/downloads/settings` | admin | health.py:save_download_settings |
| GET | `/api/health/missing/diagnose` | admin | health.py:diagnose_missing |
| GET | `/api/health/missing/episodes` | admin | health.py:missing_episodes |
| POST | `/api/health/missing/grab` | admin | health.py:grab_missing_release |
| GET | `/api/health/missing/movies` | admin | health.py:missing_movies |
| POST | `/api/health/missing/search` | admin | health.py:search_missing_item |
| GET | `/api/health/streams` | admin | health.py:get_stream_health |
| POST | `/api/health/streams/check` | admin | health.py:check_stream_now |
| POST | `/api/health/streams/clear` | admin | health.py:clear_stream_entry |
| POST | `/api/health/streams/recheck` | admin | health.py:recheck_streams |
| POST | `/api/health/streams/remove` | admin | health.py:remove_stream_entry |
| POST | `/api/health/streams/sweep` | admin | health.py:trigger_stream_sweep |
| GET | `/api/library/blocked-streams` | admin | library.py:list_blocked_streams |
| DELETE | `/api/library/blocked-streams/{block_id}` | admin | library.py:unblock_stream |
| DELETE | `/api/library/delete-download/{tmdb_id}` | user | library.py:delete_download |
| POST | `/api/library/fix-match/movie/{tmdb_id}` | admin | library.py:fix_match |
| GET | `/api/library/fix-match/movie/{tmdb_id}/frames` | admin | library.py:fix_match_frames |
| GET | `/api/library/fix-match/movie/{tmdb_id}/suggestions` | admin | library.py:fix_match_suggestions |
| POST | `/api/library/follow/{tmdb_id}` | user | library.py:toggle_follow |
| GET | `/api/library/following` | user | library.py:get_following_series |
| DELETE | `/api/library/item/{media_type}/{tmdb_id}` | user | library.py:delete_library_item |
| GET | `/api/library/item/{media_type}/{tmdb_id}` | user | library.py:get_item_detail |
| GET | `/api/library/items` | user | library.py:get_library_items |
| GET | `/api/library/match-suspects` | admin | library.py:list_match_suspects |
| POST | `/api/library/match-suspects/check` | admin | library.py:check_match_suspects_now |
| POST | `/api/library/match-suspects/{tmdb_id}/dismiss` | admin | library.py:dismiss_match_suspect |
| POST | `/api/library/replace/{media_type}/{tmdb_id}` | user | library.py:replace_copy |
| GET | `/api/library/stream` | user | library.py:stream_library_events |
| POST | `/api/library/strm-managed/{media_type}/{tmdb_id}` | admin | library.py:set_strm_managed |
| GET | `/api/library/tmdb/{media_type}/{tmdb_id}` | user | library.py:get_tmdb_detail |
| POST | `/api/library/wrong-match/movie/{tmdb_id}` | admin | library.py:report_wrong_match |
| GET | `/api/lists` | user | lists.py:get_lists |
| POST | `/api/lists` | user | lists.py:create_list |
| POST | `/api/lists/add-to-radarr` | user | lists.py:add_to_radarr |
| POST | `/api/lists/add-to-sonarr` | user | lists.py:add_to_sonarr |
| GET | `/api/lists/radarr-folders` | user | lists.py:radarr_folders |
| GET | `/api/lists/radarr-profiles` | user | lists.py:radarr_profiles |
| POST | `/api/lists/refresh-all` | user | lists.py:refresh_all_lists |
| GET | `/api/lists/sonarr-folders` | user | lists.py:sonarr_folders |
| GET | `/api/lists/sonarr-profiles` | user | lists.py:sonarr_profiles |
| DELETE | `/api/lists/{list_id}` | user | lists.py:delete_list |
| POST | `/api/lists/{list_id}/add-missing-to-radarr` | user | lists.py:add_missing_to_radarr |
| POST | `/api/lists/{list_id}/add-missing-to-sonarr` | user | lists.py:add_missing_to_sonarr |
| GET | `/api/lists/{list_id}/coverage` | user | lists.py:get_list_coverage |
| POST | `/api/lists/{list_id}/fetch` | user | lists.py:fetch_list |
| POST | `/api/lists/{list_id}/playlist-toggle` | user | lists.py:toggle_list_playlist |
| GET | `/api/live/capacity` | admin | livetv.py:live_capacity |
| GET | `/api/live/channels` | admin | livetv.py:list_channels |
| POST | `/api/live/channels/bulk` | admin | livetv.py:bulk_update_channels |
| POST | `/api/live/channels/bulk-filter` | admin | livetv.py:bulk_update_channels_by_filter |
| PUT | `/api/live/channels/{channel_id}` | admin | livetv.py:update_channel |
| GET | `/api/live/epg-coverage` | admin | livetv.py:epg_coverage |
| GET | `/api/live/groups` | admin | livetv.py:list_groups |
| PUT | `/api/live/groups/bulk` | admin | livetv.py:bulk_update_groups |
| PUT | `/api/live/groups/{group_id}` | admin | livetv.py:update_group |
| GET | `/api/live/playlist.m3u` | none | livetv.py:live_playlist_m3u |
| GET | `/api/live/provider` | admin | livetv.py:get_live_provider |
| POST | `/api/live/provider` | admin | livetv.py:save_live_provider |
| POST | `/api/live/provider/test` | admin | livetv.py:test_live_provider |
| POST | `/api/live/refresh-guide` | admin | livetv.py:refresh_jellyfin_guide |
| POST | `/api/live/reserve` | admin or internal | livetv.py:live_reserve |
| DELETE | `/api/live/reserve/{channel_id}` | admin or internal | livetv.py:live_unreserve |
| GET | `/api/live/status` | admin | livetv.py:live_status |
| GET | `/api/live/stream/{channel_id}` | none | livetv.py:stream_proxy |
| GET | `/api/live/streams` | admin or internal | livetv.py:live_streams |
| POST | `/api/live/sync-channels/{provider_id}` | admin | livetv.py:sync_live_channels |
| POST | `/api/live/sync-epg/{provider_id}` | admin | livetv.py:sync_epg |
| GET | `/api/live/sync-status` | admin | livetv.py:sync_status_endpoint |
| POST | `/api/live/sync/{provider_id}` | admin | livetv.py:sync_live_groups |
| GET | `/api/live/xmltv.xml` | none | livetv.py:hdhr_xmltv |
| GET | `/api/music/album/{rgid}` | user | music.py:music_album |
| POST | `/api/music/apply` | admin | music.py:apply_fixes |
| GET | `/api/music/artist/{mbid}` | user | music.py:music_artist |
| POST | `/api/music/artist/{mbid}/picture` | admin | music.py:upload_artist_picture |
| POST | `/api/music/artist/{mbid}/picture/deezer` | admin | music.py:pick_deezer_picture |
| GET | `/api/music/config` | user | music.py:music_config |
| GET | `/api/music/discover` | user | music.py:music_discover |
| GET | `/api/music/discover/list/{series_id}` | user | music.py:music_discover_list |
| POST | `/api/music/discover/refresh` | admin | music.py:refresh_discover |
| GET | `/api/music/imports` | user | music.py:list_imports |
| POST | `/api/music/imports` | user | music.py:create_import |
| DELETE | `/api/music/imports/{import_id}` | user | music.py:delete_import |
| GET | `/api/music/imports/{import_id}` | user | music.py:import_detail |
| POST | `/api/music/imports/{import_id}/refresh` | user | music.py:refresh_import |
| POST | `/api/music/imports/{import_id}/request` | user | music.py:request_from_import |
| GET | `/api/music/library` | user | music.py:music_library |
| POST | `/api/music/lists` | admin | music.py:add_list |
| DELETE | `/api/music/lists/{series_id}` | admin | music.py:remove_list |
| POST | `/api/music/lock` | admin | music.py:lock_right |
| GET | `/api/music/open/{rgid}` | user | music.py:music_open |
| POST | `/api/music/pictures/check` | admin | music.py:check_pictures |
| POST | `/api/music/reconcile` | admin | music.py:start_reconcile |
| POST | `/api/music/request` | user | music.py:music_request |
| GET | `/api/music/review` | admin | music.py:review_page |
| POST | `/api/music/review/{rgid}` | admin | music.py:resolve_review |
| GET | `/api/music/search` | user | music.py:music_search |
| GET | `/api/music/song` | user | music.py:music_song |
| GET | `/api/music/status` | admin or internal | music.py:music_status |
| POST | `/api/music/webhook` | none | music.py:lidarr_webhook |
| GET | `/api/music/webhook-info` | admin | music.py:webhook_info |
| POST | `/api/music/webhook/regenerate` | admin | music.py:regenerate_webhook_secret |
| POST | `/api/music/webhook/test` | admin | music.py:test_webhook |
| GET | `/api/notifications` | user | notifications.py:get_notifications |
| POST | `/api/notifications/dismiss-all` | user | notifications.py:dismiss_all |
| POST | `/api/notifications/toggle` | user | notifications.py:toggle_notifications |
| POST | `/api/notifications/{notification_id}/dismiss` | user | notifications.py:dismiss_notification |
| GET | `/api/providers` | admin | providers.py:list_providers |
| POST | `/api/providers` | admin | providers.py:create_provider |
| DELETE | `/api/providers/{provider_id}` | admin | providers.py:delete_provider |
| PUT | `/api/providers/{provider_id}` | admin | providers.py:update_provider |
| GET | `/api/providers/{provider_id}/categories` | admin | providers.py:get_categories |
| POST | `/api/providers/{provider_id}/categories/update` | admin | providers.py:update_categories |
| POST | `/api/providers/{provider_id}/categories/{category_id}/tag` | admin | providers.py:update_category_tag |
| POST | `/api/providers/{provider_id}/fetch-categories` | admin | providers.py:fetch_categories |
| GET | `/api/providers/{provider_id}/preview` | admin | providers.py:preview_sync |
| POST | `/api/providers/{provider_id}/test` | admin | providers.py:test_provider |
| GET | `/api/radarr/logs/recent` | admin | radarr.py:get_recent_log_entries |
| GET | `/api/radarr/logs/stream` | admin | radarr.py:stream_logs |
| GET | `/api/radarr/migration/preview` | admin | radarr.py:preview_migration_endpoint |
| POST | `/api/radarr/migration/run` | admin | radarr.py:run_migration |
| GET | `/api/radarr/quality-profiles` | admin | radarr.py:get_quality_profiles |
| GET | `/api/radarr/rootfolders` | admin | radarr.py:get_root_folders |
| POST | `/api/radarr/scan` | admin | radarr.py:trigger_radarr_scan |
| GET | `/api/radarr/scan/status` | admin | radarr.py:get_scan_status |
| POST | `/api/radarr/webhook` | none | radarr.py:radarr_webhook |
| POST | `/api/radarr/write-nfos` | admin | radarr.py:write_nfos |
| GET | `/api/settings` | admin | settings.py:get_settings |
| POST | `/api/settings` | admin | settings.py:update_settings |
| POST | `/api/settings/check` | admin | settings.py:check_service |
| GET | `/api/settings/connection-status` | admin | settings.py:connection_status |
| POST | `/api/settings/initial-scan` | admin | settings.py:trigger_initial_scan |
| POST | `/api/settings/jellyfin-login` | admin | settings.py:jellyfin_login |
| POST | `/api/settings/jellyfin-music/create-library` | admin | settings.py:create_jellyfin_music_library |
| GET | `/api/settings/jellyfin-music/libraries` | admin | settings.py:jellyfin_music_libraries |
| GET | `/api/settings/paths` | admin | settings.py:check_paths |
| GET | `/api/settings/plugin-keys` | user | settings.py:get_plugin_keys |
| GET | `/api/settings/raw` | admin | settings.py:get_settings_raw |
| GET | `/api/settings/schedule-info` | admin | settings.py:schedule_info |
| POST | `/api/settings/service-options` | admin | settings.py:service_options |
| GET | `/api/settings/stale-files` | admin | settings.py:check_stale_files |
| POST | `/api/settings/stale-files/delete` | admin | settings.py:delete_stale_files |
| POST | `/api/settings/stale-files/dismiss` | admin | settings.py:dismiss_stale_files |
| POST | `/api/settings/test` | admin | settings.py:test_connection |
| POST | `/api/settings/test-webhook` | admin | settings.py:test_webhook |
| GET | `/api/smartlists` | user | smartlists.py:list_smartlists |
| POST | `/api/smartlists/add-row` | user | smartlists.py:add_row |
| GET | `/api/smartlists/all-playlists` | user | smartlists.py:all_playlists |
| GET | `/api/smartlists/auto-playlists` | user | smartlists.py:list_auto_playlists |
| POST | `/api/smartlists/auto-playlists/toggle` | user | smartlists.py:toggle_auto_playlist |
| GET | `/api/smartlists/available-playlists` | user | smartlists.py:available_playlists |
| GET | `/api/smartlists/builtin-sections` | user | smartlists.py:list_builtin_sections |
| POST | `/api/smartlists/card-previews` | user | smartlists.py:set_card_previews |
| GET | `/api/smartlists/health` | user | smartlists.py:playlist_health |
| POST | `/api/smartlists/hero` | user | smartlists.py:set_hero |
| POST | `/api/smartlists/hero-sort` | user | smartlists.py:set_hero_sort |
| GET | `/api/smartlists/home-config` | user | smartlists.py:read_home |
| POST | `/api/smartlists/merge-continue-watching` | user | smartlists.py:set_merge_continue_watching |
| POST | `/api/smartlists/notify` | user | smartlists.py:notify |
| POST | `/api/smartlists/preview-count` | user | smartlists.py:preview_count |
| POST | `/api/smartlists/refresh-playlists` | user | smartlists.py:refresh_playlists |
| POST | `/api/smartlists/remove-row` | user | smartlists.py:remove_row |
| POST | `/api/smartlists/reorder` | user | smartlists.py:reorder |
| POST | `/api/smartlists/row-max-items` | user | smartlists.py:set_row_max_items |
| POST | `/api/smartlists/row-shape` | user | smartlists.py:set_row_shape |
| POST | `/api/smartlists/sort` | user | smartlists.py:set_playlist_sort |
| POST | `/api/smartlists/sync` | user | smartlists.py:sync |
| POST | `/api/smartlists/sync-one` | user | smartlists.py:sync_one |
| GET | `/api/smartlists/sync-status` | user | smartlists.py:sync_status |
| POST | `/api/smartlists/toolbar` | user | smartlists.py:set_toolbar |
| GET | `/api/smartlists/version` | none | smartlists.py:playlist_version |
| POST | `/api/smartlists/write-home-config` | user | smartlists.py:write_home |
| GET | `/api/sonarr/quality-profiles` | admin | sonarr.py:get_quality_profiles |
| GET | `/api/sonarr/rootfolders` | admin | sonarr.py:get_root_folders |
| POST | `/api/sonarr/scan` | admin | sonarr.py:trigger_sonarr_scan |
| GET | `/api/sonarr/scan/status` | admin | sonarr.py:get_scan_status |
| POST | `/api/sonarr/webhook` | none | sonarr.py:sonarr_webhook |
| POST | `/api/sonarr/write-nfos` | admin | sonarr.py:write_nfos |
| GET | `/api/sync/activity` | admin | sync.py:get_activity |
| POST | `/api/sync/cancel` | admin | sync.py:cancel_sync |
| GET | `/api/sync/dashboard` | admin | sync.py:get_dashboard |
| GET | `/api/sync/feed` | admin | sync.py:get_new_additions_feed |
| GET | `/api/sync/history` | admin | sync.py:get_sync_history |
| GET | `/api/sync/new-content-notice` | admin | sync.py:get_new_content_notice |
| POST | `/api/sync/new-content-notice/dismiss` | admin | sync.py:dismiss_new_content_notice |
| GET | `/api/sync/progress/{provider_id}` | admin | sync.py:stream_sync_progress |
| GET | `/api/sync/progress/{provider_id}/poll` | admin | sync.py:poll_sync_progress |
| POST | `/api/sync/refresh-tags` | admin | sync.py:refresh_tags |
| GET | `/api/sync/stats` | admin | sync.py:get_library_stats |
| GET | `/api/sync/status` | admin | sync.py:get_sync_status |
| GET | `/api/sync/summary` | admin | sync.py:get_sync_summary |
| POST | `/api/sync/trigger` | admin | sync.py:trigger_sync |
| GET | `/api/tags/condition-options` | user | tags.py:condition_options |
| GET | `/api/tags/rules` | user | tags.py:list_rules |
| POST | `/api/tags/rules` | user | tags.py:create_rule |
| DELETE | `/api/tags/rules/{rule_id}` | user | tags.py:delete_rule |
| PUT | `/api/tags/rules/{rule_id}` | user | tags.py:update_rule |
| GET | `/api/vod/{kind}/{token_file}` | none | vod.py:vod_stream |
| GET | `/api/widget/status` | none | widget.py:widget_status |
| GET | `/api/youtube/channels` | admin | youtube.py:list_channels |
| POST | `/api/youtube/channels` | admin | youtube.py:add_channel |
| DELETE | `/api/youtube/channels/{channel_id}` | admin | youtube.py:delete_channel |
| POST | `/api/youtube/channels/{channel_id}/live` | admin | youtube.py:toggle_live |
| GET | `/api/youtube/diagnose` | admin | youtube.py:diagnose |
| GET | `/api/youtube/live/{channel_id}/master.m3u8` | none | youtube.py:live_master |
| GET | `/api/youtube/live/{channel_id}/stream.ts` | none | youtube.py:live_stream |
| GET | `/api/youtube/ping` | none | youtube.py:ping |
| POST | `/api/youtube/refill` | admin | youtube.py:refill |
| POST | `/api/youtube/refresh` | admin | youtube.py:refresh_now |
| GET | `/api/youtube/refresh/status` | admin | youtube.py:refresh_status |
| POST | `/api/youtube/reprobe` | admin | youtube.py:reprobe |
| POST | `/api/youtube/setup` | admin | youtube.py:save_setup |
| GET | `/api/youtube/status` | admin | youtube.py:status |
| GET | `/api/youtube/traffic` | admin | youtube.py:traffic_status |
| POST | `/api/youtube/traffic` | admin | youtube.py:save_traffic |
| GET | `/api/youtube/v/{video_id}/master.m3u8` | none | youtube.py:master_playlist |
| GET | `/api/youtube/v/{video_id}/r/{token}` | none | youtube.py:proxied |
| GET | `/device.xml` | none | livetv.py:hdhr_device_xml |
| GET | `/discover.json` | none | livetv.py:hdhr_discover |
| GET | `/hdhr/device.xml` | none | livetv.py:hdhr_device_xml |
| GET | `/hdhr/discover.json` | none | livetv.py:hdhr_discover |
| GET | `/hdhr/lineup.json` | none | livetv.py:hdhr_lineup |
| POST | `/hdhr/lineup.post` | none | livetv.py:hdhr_lineup_post |
| GET | `/hdhr/lineup_status.json` | none | livetv.py:hdhr_lineup_status |
| GET | `/hdhr/xmltv.xml` | none | livetv.py:hdhr_xmltv |
| GET | `/lineup.json` | none | livetv.py:hdhr_lineup |
| POST | `/lineup.post` | none | livetv.py:hdhr_lineup_post |
| GET | `/lineup_status.json` | none | livetv.py:hdhr_lineup_status |

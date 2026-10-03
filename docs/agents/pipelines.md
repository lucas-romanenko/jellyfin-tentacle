# Pipelines: what happens when

How content and changes flow from Tentacle into Jellyfin and the clients.
The code wins where they differ. Internals: [server.md](server.md).

## Content → tags → playlists → home screen

1. **Content enters**
   - VOD sync (IPTV, Xtream or M3U): each catalog item is matched on TMDB;
     a match writes a `.strm` (the stream URL) and an `.nfo` with full
     metadata and `<tag>`s; the DB gets a `Movie`/`Series` with
     `source='vod'`.
   - Radarr/Sonarr webhook (import): `scan_radarr_library()` /
     `scan_sonarr_library()` enrich from TMDB, store `source='radarr'` /
     `'sonarr'`, write an NFO named exactly like the video
     (`Alien (1979) Bluray-1080p.nfo`) and push tags through the Jellyfin
     API. The webhook handler retries while Radarr is still finishing the
     import (waits of 15, 30, 45, 60 s). An NFO that already exists only
     gets Tentacle's `<tag>` lines changed (`refresh_arr_nfo`), never
     rebuilt: a rebuild reset `<dateadded>`, which Jellyfin reads as
     DateCreated, so every scan moved old downloads to the top of "Latest"
     (#266). The scans run one at a time per app (`_scan_lock`, #268).
2. **Tags**: VOD through NFO; downloads through the API
   (`set_item_tags`): "Downloaded Movies", "Recently Added Movies", list
   tags (e.g. "IMDB TOP 250"). Source tags carry the type suffix.
3. **Playlists computed** per user by `get_desired_smartlists()`.
4. **Playlists written** to Jellyfin by `sync_smartlists()` and filled by
   `refresh_smartlist_playlists()`.
5. **Home config** written by `write_home_config()`; the plugin reads it.

## Events

**Radarr downloads a movie** (webhook): scan → TMDB → DB → NFO + API tags →
Jellyfin item refresh with `ReplaceAllMetadata=false` (true would wipe the
tags; an older note said true) → playlist refresh (all playlists: the
webhook can't know which) → home config → plugin notified → version bumped.

**Radarr deletes a movie** (MovieDelete): DB record and `DownloadRequest`s
removed, then `remove_item_from_playlists()` for every user in the
background; the Library shows it as missing again. A file delete
(MovieFileDelete) does the same at once, except reason `upgrade` (ignored)
and `missingFromDisk` (Radarr can't see the file): those are collected until
none has come for 10 minutes and judged with the scan's storage-outage guard
(`file_loss_looks_like_an_outage`, #106/#381), counted together with every
`missingFromDisk` report of the last 6 hours: a loss of 3 or more and over
half of the downloads (a share that dropped out) removes nothing, however the
burst was spread out.

**Sonarr deletes a series** (SeriesDelete): a hybrid keeps its VOD record
(`sonarr_path`, `sonarr_monitored` cleared); a Sonarr-only series is
deleted; then playlists as above.

**"Download more episodes"** on a VOD series: the client loads TMDB seasons,
VOD episodes and Sonarr episodes in parallel; the picker shows VOD
episodes ("VOD") and downloaded ones ("DL") as checked and disabled, and
season coverage ("5/8"). The chosen episodes go to
`POST /api/lists/add-to-sonarr` with `selected_episodes`; Tentacle adds the
series with an explicit `path` in the existing VOD folder, `monitor: none`,
then monitors only the chosen episodes, sets `monitorNewItems="all"` when
"Auto-download new episodes" (default on) is ticked, starts a search, and
records `sonarr_path`/`sonarr_monitored` at once.

**"Manage episodes"** on a Sonarr series: `GET /api/discover/sonarr-episodes/{tmdb_id}`,
then `POST /api/discover/manage-episodes`: everything unmonitored, the
chosen ones monitored again, those without files searched.

**A custom playlist is created**: `POST /api/tags/rules`, then
`POST /api/smartlists/sync` (refreshes only `changed_names`, clears their
artwork cache, uploads artwork, writes home config, notifies the plugin,
bumps the version) and answers with item counts per playlist.

**An auto playlist is toggled**: `POST /api/smartlists/auto-playlists/toggle`
saves the toggle, then a full sync/refresh/artwork/home config/notify/bump.
The UI flips the switch before the answer (optimistic).

**Home rows** (Home Screen tab): `add-row` (inserted at the top),
`remove-row`, `reorder`, `hero`, `hero-sort`, `row-max-items`, `toolbar`
under `/api/smartlists/`: each writes the home JSON, notifies the plugin
and bumps the version.

## Nightly sync

`sync_schedule` (cron, default `0 3 * * *`), `run_scheduled_sync()` in
`main.py`. A run the scheduler wakes late for (container frozen by a backup,
clock step) still runs once within 6 h (`misfire_grace_time`; the other
daily jobs 1 h, everything else 5 min); APScheduler's default of 1 s dropped
it. A container that is down at the trigger still skips that night.

The value is read as standard cron by `_sync_trigger()` (0 and 7 = Sunday;
both day fields set = either one), not as APScheduler's own fields (0 =
Monday); a stored value it cannot read runs at the default time.

1. refresh list subscriptions; 2. VOD sync from active providers; 3. Radarr
scan; 4. Sonarr scan (and Following state for every series); 5. recently
added tags; 6. Jellyfin pipeline (scan, push tags, refresh playlists);
7. clean the TMDB cache; 8. `sweep_orphaned_downloads()`; 9. per user:
`migrate_global_smartlists_to_user()` (one-time), `sync_smartlists()`,
`write_home_config()`; 10. playlist artwork; 11. `POST /Tentacle/Refresh` to
clear the plugin's caches.

## VOD sync runs

`sync_provider()` (`services/sync.py`) creates a `SyncRun` row ("running")
and always ends it; the manual trigger, the nightly guard and Cancel all
read that row.

- An error ends the run through `_finish_run()`: it commits the end as is,
  and after a failed flush ("database is locked", a constraint) rolls the
  session back first (only the category in progress is lost; each category
  commits its own), or records it with a fresh session. A run left
  "running" makes the nightly skip the provider every night (#270).
- A category the provider could not be read for (down, a timeout, an HTTP
  error, an HTML page instead of JSON, or an empty answer for a category
  that held titles) is counted with a short reason
  (`_provider_error_reason()`; never a URL, it carries the login). Every
  category failing makes the run `failed`; some failing keeps it
  `completed` with `error_message` "N of M categories could not be read
  (reason)". Both go to Activity, for "Sync now" and the nightly. Pruning
  is skipped either way (`fetch_ok`), as before (#267).
- TMDB matching (`search_movie` / `search_series`): with the provider's
  year, then, only if that found nothing good enough, once without it,
  keeping results within one year of the provider's (a local or streaming
  release year is often one off; a remake decades apart is not taken). No
  year: one search (#265).
- A stream stays with the film whose `.strm` plays it (#185). After every
  category, `_place_relisted_movies()` applies that by the files: a stream
  no label placed that a film's `.strm` plays is that film, relabelled
  (counted as existing and seen, so never pruned, #262); a film met this
  sync whose `.strm` plays a stream of this provider no longer listed
  anywhere (a complete fetch, Xtream only) is pointed at its current
  stream in place (#263). Episodes: a file whose episode id the show no
  longer lists gets the id listed at its SxxEyy (`_plays_delisted_episode`).
  While both ids are listed nothing flips.
- `.strm` files are written with `_write_strm()`: a hidden temp file in the
  same folder, then a rename, so a write cut short leaves the old file
  whole. An existing `.strm` that is empty or blank is rewritten like a
  missing one (`_strm_is_blank()`; a movie only from the row's own stream,
  as for a restore), since it plays nothing (#283).

## How changes reach the clients

| Channel | Client | How |
|---|---|---|
| Version polling | Jellyfin web (`tentacle-home.js`) | polls `GET /api/smartlists/version`; a new number re-fetches and redraws the rows |
| WebSocket | Android TV (`HomeRowsFragment.kt`) | Jellyfin's `LibraryChangedMessage` (fired when playlists change) triggers a row refresh; no polling |
| Plugin cache clear | the plugin | `POST /Tentacle/Refresh` after the nightly sync and settings changes |

## Known inefficiencies (improvement ideas)

1. Playlists are rebuilt from scratch every night even when nothing
   changed: diff current vs desired and skip identical playlists.
2. Tag-based playlists round-trip through Jellyfin (tag → Jellyfin indexes →
   query by tag), which races with indexing after a webhook; Tentacle's DB
   already knows the tags and could compute membership itself (native
   genre/rating/year playlists still need Jellyfin).
3. The nightly per-user loop is sequential (users are independent), and has
   no dirty tracking (unchanged users are rebuilt too).

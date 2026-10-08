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
One pass per film at a time: an event for a film whose pass still runs (it
waits its turn for the scan) is queued and runs after it in the same thread,
never dropped; queued events make one pass, Download outranking MovieAdded
(#380). A quality upgrade (`isUpgrade`, Radarr and Sonarr) gets the pass
but no second "ready to watch"; a bad copy being replaced still gets "A new
copy of ... is ready to watch".

**Radarr deletes a movie** (MovieDelete): DB record and `DownloadRequest`s
removed (its duplicates too, unless Keep VOD is resolving the title or one
holds saved watched state: server.md "Duplicates", #515), then `remove_item_from_playlists()` for every user in the
background; the Library shows it as missing again. A file delete
(MovieFileDelete) does the same at once, except reason `upgrade` (ignored)
and `missingFromDisk` (Radarr can't see the file): those are collected until
none has come for 10 minutes and judged with the scan's storage-outage guard
(`file_loss_looks_like_an_outage`, #106/#381), counted together with every
`missingFromDisk` report of the last 6 hours: a loss of 3 or more and over
half of the downloads (a share that dropped out) removes nothing, however the
burst was spread out. The loss is judged over all downloads and over each kind
alone (downloaded-only films, VOD titles' downloads: `download_kind`); any
that looks like an outage refuses (`download_loss_looks_like_an_outage`, #505),
so a share lost under one kind isn't diluted by the other's healthy downloads. A VOD title Radarr downloaded too (one row: source
`provider_N` with `radarr_path`) keeps its row and goes back to VOD only
(`release_vod_download`, #378): `radarr_path`, `downloaded_at`, the
download's `jellyfin_item_id` and "Downloaded Movies" (plus the requester's
"<name>'s Downloads" when the request goes) cleared, `nfo_path` back to the
`.strm`'s NFO, the pending duplicate dismissed; the next tag push takes the
tag off the VOD item. The Radarr scan does the same for a download Radarr
no longer has, and counts these rows in its outage guard
(`downloaded_movie_rows`). Keep VOD on a film duplicate releases the row
the same way (and deletes the request) without waiting for Radarr's delete
webhook (#549).

**Sonarr deletes a series** (SeriesDelete): a hybrid keeps its VOD record
(`sonarr_path`, `sonarr_monitored` cleared); a Sonarr-only series is
deleted; then playlists as above.

**"Download more episodes"** on a VOD series: the client loads TMDB seasons,
VOD episodes and Sonarr episodes in parallel; the picker shows VOD
episodes ("VOD") and downloaded ones ("DL") as checked and disabled, and
season coverage ("5/8"). The chosen episodes go to
`POST /api/lists/add-to-sonarr` with `selected_episodes`; Tentacle adds the
series with an explicit `path` in the existing VOD folder, `monitor: none`,
waits until Sonarr has set it up (up to 3 minutes from the add; past that
the request fails with the reason, the series stays in Sonarr), then
monitors only the chosen episodes, sets `monitorNewItems="all"` when
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

**A user is renamed in Jellyfin**: Tentacle learns it at their next
dashboard sign-in (`login()` copies the Jellyfin name to `display_name`).
That commit retires "<old name>'s Downloads" (so it comes off titles and a
later user given the old name keeps theirs: `dynamic_tags` leaves out every
current user's Downloads tag). Then `follow_user_rename()` in the background
moves the tag on the titles they requested (rows, NFOs, Jellyfin, only those
titles), and `sync_smartlists()` renames the playlist in place
(`_follow_downloads_rename`: same folder, same Jellyfin id and entries, only
Tentacle's own playlist renamed), so the home row and hero stay on it. If the
background job fails the nightly catches up (the scans retag, the sync
renames), but its playlist refresh runs first and can leave the playlist
empty until the next refresh. The sync reads the name from the DB (`_user_display_name`), not a
`TentacleUser` loaded earlier (#454).

**A provider is saved** (Providers page `PUT /api/providers/{id}`, or
`POST /api/live/provider`): a new server, username or password on an Xtream
provider rewrites its Live TV channels' `stream_url`
(`{server}/live/{user}/{pass}/{id}.{ext}`, keeping each channel's format) in
the same commit, `rewrite_xtream_channel_urls()` in `routers/livetv.py`; no
request to the provider, no channel sync (#469). M3U channel URLs come from
the playlist and only a channel sync changes them.

**A stuck download is fixed** (Health → Downloads → **Fix**, or the 5-minute
auto-fix sweep; `resolve_stuck_download()` in `services/download_health.py`):
the queue item is removed with `blocklist=true`, and exactly one side grabs
the replacement (#444). Removing with blocklist makes Radarr/Sonarr search
again by themselves ("Redownload Failed", on by default) unless
`skipRedownload=true`, and their grab reaches their queue seconds later, so
two grabbers download the title twice. Tentacle picks the replacement (other
protocol first) and removes with `skipRedownload=true`; when it grabs nothing
(search failed, nothing grabbable, grab refused: 4xx or the arr's 500) it
sends a `MoviesSearch` / `EpisodeSearch` command instead. A grab with no
answer (timeout, reset, 502/503/504) may have gone through, so no search. A
Sonarr download of several episodes (several queue records, one
`downloadId`) is left to Sonarr's own re-search (a season search for a pack).

## Nightly sync

`sync_schedule` (cron, default `0 3 * * *`), `run_scheduled_sync()` in
`main.py`. A run the scheduler wakes late for (container frozen by a backup,
clock step) still runs once within 6 h (`misfire_grace_time`; the other
daily jobs 1 h, everything else 5 min); APScheduler's default of 1 s dropped
it. A container that is down at the trigger still skips that night.
`services/sync_schedule.py` builds the trigger with cron's meaning
(APScheduler counts 0 = Monday and refuses 7; day of week becomes day names,
both day fields set becomes an `OrTrigger`). A stored value it refuses runs
at the default with a warning; the settings form answers 400 for one.

1. refresh list subscriptions; 2. VOD sync from active providers (each read
when its turn comes, under `_sync_lock`: one deleted or switched off since
the job started is skipped, #517); 3. Radarr
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

- One provider's VOD sync runs at a time (`_vod_sync_lock`, #446): a
  category's rows commit only at its end, so two providers syncing at once
  both wrote the shared title's `.strm` and row, and the second commit
  failed on UNIQUE(tmdb_id). A sync of another provider waits with its run
  already "running", shows "Waiting for <name>'s sync to finish", stays
  cancellable, and its wait is booked to the run (`booked_wait`) so the
  status route does not auto-fail it as stuck.
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
- A library root (`/media/vod/movies`, `/media/vod/shows`) that is missing
  or empty while the sweep's rows are recorded (`_swept_rows()`) means the
  share is not mounted: the run fails before writing anything
  (`_check_vod_root_before_sync()`, #439). Writing there put the files on
  the container's disk, made the root non-empty for the VOD sweep's own
  check, and the sweep deleted every title not written back. A new install
  (no rows) syncs; a deliberately empty folder needs any file in it.
  `_repair_movie_strm()` and the show-folder rebuild check the root too.
  A root that raises `OSError` when read (a stale NFS/SMB/FUSE mount) counts
  as unmounted (#440).
- TMDB matching (`search_movie` / `search_series`): with the provider's
  year, then, only if that found nothing good enough, once without it,
  keeping results within one year of the provider's (a local or streaming
  release year is often one off; a remake decades apart is not taken). No
  year: one search (#265). TMDB's `year` filter matches any release date
  (re-releases too), so the first pass can bring back an older film exactly
  titled as the label ("Dune (2024)" finds Dune 2021). Against such a
  far-year exact title, a film within a year of the label's wins only with
  `_CREDIBLE_VOTE_SHARE` (3 %) of its TMDB votes: a sequel under its base
  title or a remake does, a re-release label's namesakes don't (#310).
  Limits: two films within a year ("Wicked (2025)") take the exact title;
  a label one off from a film the first pass doesn't return ("Dune (2020)")
  takes the far exact title.
- A re-searched stream whose search now finds another film keeps the film
  its `.strm` already plays while the label still fits it by the scorer
  (`_keeps_its_film()`, `label_names_film()`): a matcher change reaches new
  imports only, a wrong row is Fix it's. A provider id naming the new film,
  or a label that no longer fits (a reused stream number), moves it (#310).
- A stream stays with the film whose `.strm` plays it (#185). After every
  category, `_place_relisted_movies()` applies that by the files: a stream
  no label placed that a film's `.strm` plays is that film, relabelled
  (counted as existing and seen, so never pruned, #262); a film met this
  sync whose `.strm` plays a stream of this provider no longer listed
  anywhere (a complete fetch, Xtream only) is pointed at its current
  stream in place (#263). Episodes: a file whose episode id no listing of
  the show offers at its SxxEyy (replaced, or renumbered to another SxxEyy)
  gets the id listed there now. `_EpisodeSlots` collects every listing of
  the show (one show can sit under several series ids, e.g. an EN and a DE
  category) and the series sync settles it once, after a complete fetch
  (#376). While a file's id is listed at its number by any listing nothing
  flips. A listing whose fetch fails makes the show's files wait; a
  listing that answers with no episodes offers nothing and the other
  listings decide (#512).
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

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
     (#266). One with neither `</movie>` nor `</tvshow>` (a metadata-URL
     NFO, a release `.nfo`) is left alone and not counted as written, else
     every scan triggered a full library scan (#628). The scans run one at
     a time per app (`_scan_lock`, #268). The tag push lists Jellyfin
     *before* the scan asks Jellyfin for a library scan: during a full
     library scan the 10,000-row listing doesn't answer within 15 s and
     nothing was pushed (#819).
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

**Sonarr deletes a series** (SeriesDelete): a Sonarr-only series is
deleted, then playlists as above. A VOD show Sonarr held too keeps its row
(`sonarr_monitored` cleared). One that held a download (`downloaded_at` or
"Downloaded TV": `holds_series_download`) goes back to VOD only
(`release_vod_series_download`, the series twin of `release_vod_download`,
#617): `sonarr_path`, `downloaded_at`, `last_downloaded_episode`, the
download's `jellyfin_item_id`, "Downloaded TV" and the requester's
"<name>'s Downloads" cleared, `nfo_path` back to the VOD folder's
`tvshow.nfo`, the pending duplicate dismissed. One Sonarr was still
searching for only loses `sonarr_path`. No playlist clean-up for the VOD
row: Jellyfin usually merges both folders into one show, which stays in its
VOD playlists; the tag push takes it out of the others. The Sonarr scan
releases a VOD show whose download Sonarr no longer has (left Sonarr, or no
episode files; it keeps `sonarr_path` while Sonarr still lists it) and
counts these rows in its outage guard per kind (`downloaded_series_rows`:
source sonarr, or any row with `downloaded_at`, since Add to Sonarr sets
`sonarr_path` before anything is downloaded). A VOD show Sonarr holds
with only Tentacle's `.strm` files (`series_has_real_download` is False:
Sonarr 4 counts them as episode files) is no download (#682): the scan
keeps its `sonarr_path` and Following state but gives it no date, no tags
and no NFO write, and releases a row an older scan tagged
(`keep_sonarr_path=True`, its duplicate left to
`_dismiss_strm_only_duplicates`; "Recently Added TV" comes off too unless
the row's own `date_added` is in the window), then triggers a Jellyfin
library scan. Sonarr not answering keeps the download path. Keep VOD on a series
duplicate releases the row the same way and deletes the request.

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
then `POST /api/discover/manage-episodes`: the chosen ones monitored, the
rest unmonitored, those without files searched. A failed Sonarr read is a
503 (never an empty list); Save stays disabled until a list is shown, and an
empty selection is refused (400) unless the body says `unmonitor_all: true`
(the person unticked every episode on purpose) (#764).

**A custom playlist is created**: `POST /api/tags/rules`, then
`POST /api/smartlists/sync` (refreshes only `changed_names`, clears their
artwork cache, uploads artwork, writes home config, notifies the plugin,
bumps the version) and answers with item counts per playlist.

**An auto playlist is toggled**: `POST /api/smartlists/auto-playlists/toggle`
saves the toggle, then a full sync/refresh/artwork/home config/notify/bump.
The UI flips the switch before the answer (optimistic).

**A list is fetched, refreshed or deleted** (`POST /api/lists/{id}/fetch`,
`POST /api/lists/refresh-all`, `DELETE /api/lists/{id}`):
`apply_list_tags_to_library()` rewrites the rows and NFOs and collects the
titles whose tags it changed (a delete runs it with no items, so the tag
comes off the titles only the deleted list gave it). After the endpoint's
commit, `_follow_list_change()` in the background pushes those titles' tags
to Jellyfin (`sync_owned_tags(only=...)`: Jellyfin keeps tags set through
its API, so NFOs alone change nothing), then for the owner and any other
user with a list or rule on the tag refreshes the playlists (a delete runs
`sync_smartlists()` first, so the playlist goes) and writes the home
config, bumps the version and notifies the plugin. A deleted list's row goes
at once (`write_home_config(dropped_ids=...)`) instead of lingering 7 days
as unresolvable. Refresh all makes one push for all its lists. If Jellyfin
fails, one WARNING and the nightly catches up (#679).

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

**A provider is saved** (the provider card's Edit, `PUT /api/providers/{id}`,
or a switch to another of its addresses, `services/provider_addresses.switch_to`:
"Use this one" or the Health banner's Switch, never by itself): a new server, username or password on an Xtream
provider rewrites its Live TV channels' `stream_url`
(`{server}/live/{user}/{pass}/{id}.{ext}`, keeping each channel's format) in
the same commit, `rewrite_xtream_channel_urls()` in `routers/livetv.py`; no
request to the provider, no channel sync (#469). M3U channel URLs come from
the playlist and only a channel sync changes them. After the commit the
same change starts `provider_relink.start_relink()` (#142): a background
thread waits for a running sync of that provider, rewrites every direct VOD
`.strm` of its rows (`Movie.strm_path`, every `*.strm` under
`Series.strm_path`) that still holds the old `{server}/movie|series/{user}/{pass}/`
to the form the sync writes today (direct, or a Tentacle link when VOD
through Tentacle is on), then triggers a Jellyfin scan (Jellyfin keeps the
old URL until it sees the file change) and logs a `provider_relinked`
Activity entry with the counts. Tentacle links, proxies and other
providers' files are left alone. The save also writes the old address to
the setting `provider_relink_pending` (`{provider_id: [server, user, pass]}`,
the oldest one when saved twice) in its own commit; `_run` clears it, and
`resume_pending()` (scheduled a minute after start-up) runs what a restart
cut short (#149). Every Xtream sync first runs `relink_before_sync()`, before
its storage checks and any provider request: `relink_strms(db, provider,
before)` with the pending old address if any, and always the rule "this
provider's current login on another host" (`sync._MOVED_RE`, the same rule
the sync's `_strm_needs_rewrite()` applies to listed titles, minus hosts of
other providers with the same login: those files play the other provider),
so files in categories the provider refused follow too. An M3U provider whose `m3u_url`
changed gets `{"m3u": true}`: while it is pending the sync's client has
`repoint_all`, `_strm_needs_rewrite()` rewrites any listed file that differs
from the playlist, and a full sync with every category read clears it.
Without one M3U files are never rewritten (tokens rotate).

**A provider refuses this server** (#149): both Xtream clients
(`sync.XtreamClient`, `xtream_client.XtreamClient`) send every API request
through a `RequestGuard`: at least `XTREAM_MIN_REQUEST_GAP` (0.25 s) between
two requests, and after `PROVIDER_REFUSAL_LIMIT` (3) answers 401/403 in a row
every further call raises `ProviderRefusedError` without a request (a success
resets the count; 5xx, 429 and timeouts don't count). The VOD sync marks every
category left unread without its pause, stops a series category mid-way
(not counted: the empty-category guard's memory stays), and the run's message
carries the refusal reason. The Live TV channel sync re-raises it: no
lineup is saved. Only a new run (a new client) asks again. Below the limit
(one or two enabled groups) or on 5xx, a channel sync where every enabled
group fails raises `ProviderDataError` (reason redacted) before the upsert:
phase `error`, `last_live_sync` and Activity untouched (#630). Some groups
failing, or a group answering `[]`, is still a success.

**Gentle on the provider** (after #709): a household's panel held the
account back after the nightly burst (1-14 Oct 2026: 14-16 of 42
categories 403; 23 Oct: categories 2-14 empty; the guide download after the
sync refused). The VOD sync reads every category through `_read_category()`
(`services/sync.py`) with the Xtream client's `ListingPacer`: listings at
least `CATEGORY_GAP_SECONDS` (5 s) apart; a listing answered 403, 429, 5xx,
509 or 511, or empty for a category that held titles, is asked again after
`CATEGORY_RETRY_WAITS` (60 s, 180 s; the panel's Retry-After up to 10 min
instead) and the gap doubles (up to 40 s); at most `RETRY_BUDGET_SECONDS`
(15 min) of such waits per run, then as before. Three 403s in a row still
stop the run (`RequestGuard`), minutes apart now. The run's last line says
how many were asked again and how long it waited. In the nightly job:
discovery (`discover_new_provider_content(db, gentle=True)`) asks for an
Xtream panel's category counts (every film and every series) only when a
category is new, its requests and the Live TV group count `DISCOVERY_GAP_SECONDS`
(10 s) apart; the guide download waits until no provider API request has
gone out for `GUIDE_QUIET_SECONDS` (180 s, at most 10 min,
`_quiet_before_guide()`; requests are noted by `RequestGuard` and
discovery, `xtream_client.note_api_request`); the hourly provider check is
skipped while the nightly run runs (`routers.sync.nightly_running`). Under
the test suite these waits pass on a virtual clock (`TENTACLE_PACING=virtual`,
set by tests/hermetic.py).

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
7. clean the TMDB cache, then discovery of new categories and Live TV
groups (gently, see "Gentle on the provider"; existing VOD categories get
the provider's name every night and its counts on a night a category is new
or every `VOD_COUNTS_EVERY_DAYS` (7), as Sources' "Fetch categories" does:
chosen state and source tag stay, an M3U playlist's unchosen categories it
no longer lists go unless that is over a fifth of them), then the Live TV channel sync
of every Xtream Live TV provider with an enabled group
(`discovery.refresh_live_channels()`: the Live TV page's channel sync, one
listing per enabled group `CHANNEL_GAP_SECONDS` (5 s) apart; names, links,
logos, guide ids and new channels; enabled flags, numbers and own names
stay; skipped for a provider whose sync runs and while a protected recording
runs; ends its sync status itself). A channel the provider no longer lists
is never deleted: after a complete read (every enabled group answered), in a
group that listed channels, it gets `provider_missing_since` (at most a
fifth of a group's channels at once, at least 25, or none: an outage), and
`_shown_channels()` leaves it out of the lineup after `GONE_DAYS` (3); listed
again, it is back with its number, name and switch. The manual sync does
the same. An event channel is named for the day's
game and the lineup carries only today's events, so without it the names
went stale until someone pressed the button (26 Oct 2026). M3U playlists are
read whole by the group step already; 8. `sweep_orphaned_downloads()`,
then the guide download after a quiet spell, then, when the guide or the
channels were synced, `refresh_jellyfin_guide_after_sync()`: what the Live
TV page does after a channel sync (POST /api/live/refresh-guide: re-create
the guide source, Refresh Guide, tag the new channels and
`livetv_tuners.tell_clients()`); 9. forget people
no longer in Jellyfin (`gone_people.forget_gone`), then per user:
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
  check, and the sweep deleted every title not written back. A root that
  holds none of the recorded titles' files counts as unmounted too
  (`_vod_root_holds_none()`, #698): in a merged setup a Radarr/Sonarr import
  onto the bare mount point makes it non-empty. A show counts only when its
  folder holds a `.strm` (`_holds_own_file()`): Sonarr re-creates the show's
  own folder when it imports into it (vod_root layout, or one folder for
  both). The sync and the sweep both check it. A new install (no rows) syncs; a library emptied on purpose (a
  new disk) needs an empty `.tentacle-library` file in the root
  (`VOD_LIBRARY_MARKER`).
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
  no label placed (no TMDB match, or a label the cleaner rejects) that a
  film's `.strm` plays is that film, relabelled (counted as existing and
  seen, so never pruned, #262, #621). A known label is checked against
  the files too when this provider has the same title in another year
  (`_known_title_id()` `rivals`): a stream relabelled onto the year of our
  other remake stays with the film whose `.strm` plays it (#627). A film met this
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
- Series rows remember the provider series ids their listings were met by
  (`Series.provider_series_ids`, `_record_series_listings()`: tonight's set
  after a complete fetch, added to after a partial one; cleared on a
  takeover). A listing no label placed whose id a row of ours recorded,
  and that no listing met tonight, is that show relabelled when its
  episodes are ones the row's `.strm` files play
  (`_place_relabelled_series()`, one `get_series_info`): kept, seen,
  back-filled. Files that play none of them: the id now names another
  show, left to the prune. No files to compare (opted out, or the listing
  can't be read): kept, nothing written (#621). A show relabelled before
  its id was ever recorded is still pruned.
- A re-searched listing whose TMDB answer now names a show this provider
  has no row for stays with the row it was met by before
  (`provider_series_ids`, ids two rows share left out) when that row's
  `.strm` files play its episodes (`_keeps_its_show()`, the series twin of
  `_keeps_its_film()`): a new row would be a new Jellyfin series, every
  user's watched state gone. A wrong row is fixed by hand; a listing whose
  episodes aren't the row's (a reused series id) still moves (#796).
- `.strm` files are written with `_write_strm()`: a hidden temp file in the
  same folder, then a rename, so a write cut short leaves the old file
  whole. An existing `.strm` that is empty or blank is rewritten like a
  missing one (`_strm_is_blank()`; a movie only from the row's own stream,
  as for a restore), since it plays nothing (#283). An Xtream `.strm` that
  holds only the start of the address the sync writes today (a strict
  prefix) is rewritten too (`_strm_needs_rewrite()`, #629). "Fix it"
  moving a copy (`wrong_match.rematch_movie`) and the YouTube library
  (`write_video`, `rewrite_strm`) write through `_write_strm()` as well;
  `write_video` repairs a blank or cut-short YouTube `.strm`.

## How changes reach the clients

| Channel | Client | How |
|---|---|---|
| Version polling | Jellyfin web (`tentacle-home.js`) | polls `GET /api/smartlists/version`; a new number re-fetches and redraws the rows |
| WebSocket + home version | Android TV (`SocketHandler`, `HomeRowsFragment.kt`, navbars) | the plugin's `LibraryChanged` push carries the user's home version (`tentacle-home:<user\|*>:<v>` in `CollectionFolders`); the app keeps the newest one and, on Home, the navbar, a return to the front or a WebSocket reconnect, compares it with what it shows and refreshes only if it moved; no polling |
| WebSocket (Jellyfin's own + Tentacle's Live TV push) | Jellyfin web Live TV guide (`tentacle-livetv.js`) | UserDataChanged (favourites, sent by Jellyfin to the person's sessions whoever made the change) updates stars and Favorites rows in place; LibraryChanged (e.g. `tentacle-livetv:<v>` from `livetv_tuners.tell_clients()` after every Live TV setup run) re-reads the channel list and redraws only what differs; tabs relay to each other (`BroadcastChannel`) since Jellyfin talks to one socket per browser |
| Live channel (`GET /api/updates`, server-sent events) | Tentacle's dashboard (`static/js/live.js`) | `services/live_updates.py`: one channel per signed-in tab; `publish(topic, user_ids=, admins=, everyone=)` sends a "this changed" note (requests, activity, notifications, people, me, health, home, livetv, mychannels, groups, library, settings) and the tab re-reads only what its open page shows; a (re)connect, also after the tab was hidden a minute, re-reads everything. Radarr/Sonarr send no grab or progress webhook, so a watcher reads their queues every 3 s while a tab is open (15 s otherwise) and says "activity" to the title's requester and the admins (progress alone: dashboards only, `{"progress": true}`) |
| Plugin notify (`POST /Tentacle/Notify`) | Jellyfin web (`tentacle-notifications.js` → `tentacle-changed` window event → Activity overlay, navbar badge), Android TV | `live_updates.changed(..., clients=True)` / `tell_clients()` for requests, activity and notifications: gathered 0.3 s, then one `RefreshProgress` message (ItemId "tentacle") per person's sessions; the clients read again at once, and poll slowly as a safety net |
| WebSocket (Jellyfin's own) | Jellyfin web Favorites tab (`tentacle-favorites.js`) | UserDataChanged with an IsFavorite of this person re-reads the three lists (~150 ms later), LibraryChanged after 1.5 s; only rows whose cards changed are redrawn; relayed between tabs on `BroadcastChannel('tentacle-favorites')`; no socket: every 10 s; what's on now: every minute |
| Plugin cache clear | the plugin | `POST /Tentacle/Refresh` after the nightly sync and Settings saves (everything, every user), or `?userId=&homeVersion=` after a save of one user's home or toolbar (that user's home caches and clients only) |

The home version (`services/smartlists.py`: `get_home_version`,
`bump_home_version`) is per user and in memory; `_notify_jellyfin_plugin`
bumps it (one user's with `user=`, everyone's without), so call it after
the config is written. Numbers are at least the time in ms, so a restart
never repeats one a client remembers.

## Known inefficiencies (improvement ideas)

1. Playlists are rebuilt from scratch every night even when nothing
   changed: diff current vs desired and skip identical playlists.
2. Tag-based playlists round-trip through Jellyfin (tag → Jellyfin indexes →
   query by tag), which races with indexing after a webhook; Tentacle's DB
   already knows the tags and could compute membership itself (native
   genre/rating/year playlists still need Jellyfin).
3. The nightly per-user loop is sequential (users are independent), and has
   no dirty tracking (unchanged users are rebuilt too).

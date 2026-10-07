# Tentacle server (`tentacle/`): internals

Reference for coding agents; the overview is in [CLAUDE.md](../../CLAUDE.md).
Where this and the code disagree, the code wins, and fix this file.

## Stack

FastAPI + SQLite (SQLAlchemy) + APScheduler, one uvicorn worker; the
dashboard is a vanilla-JS single-page app (`static/index.html`,
`static/js/app.js`, `static/js/pages.js`, `static/js/music.js`); one
container. All routes: [server-api.md](server-api.md).

```
main.py                 app, lifespan, scheduler (sync_schedule, default "0 3 * * *"), routers
models/database.py      every model + seed defaults (NON_EMPTY_DEFAULTS)
routers/                one per area: auth, settings, providers, sync, library, duplicates,
                        lists, smartlists, tags, radarr, sonarr, discover, collections,
                        activity, widget, livetv, vod, notifications, health, youtube, music
services/               the work: sync (VOD engine), tmdb, nfo, cleaner, tagger, smartlists,
                        jellyfin, radarr, sonarr, artwork, logstream, migration, xmltv,
                        xtream_client, m3u_parser, media_requests, lidarr, musicbrainz, music/
```

- `/api/health`: the container healthcheck. `/api/version`
  (unauthenticated): commit, build date, `code.matches` (the running files
  match the image's fingerprint).
- Paths inside the container are fixed (users map host folders with
  volumes; Settings → Library Paths checks them): `/data` (DB, caches,
  per-user `smartlists/` and `home-configs/`), `/media/movies` (Radarr),
  `/media/shows` (Sonarr), `/media/vod/movies`, `/media/vod/shows` (VOD
  `.strm`), `/media/youtube`.
- Logs: `services/log_redaction.py` strips credentials from every log record
  (uvicorn's access log included): Xtream paths and any query parameter
  named like a secret (`*secret*`, `*token*`, `*password*`, `*api_key*`,
  `key`, ...). A new credential in a URL needs such a name, or a rule there.
- One worker, one event loop: anything blocking in an `async def` freezes
  every stream, recording and request. The SSRF guards (`services/ssrf.py`:
  `is_safe_url`, `lan_origin_guard` and the guards it returns) resolve DNS
  with a blocking `getaddrinfo`, so async code calls them through
  `asyncio.to_thread` (#371, #464); `url_host_allowed` is the allowlist
  half, no lookup.

## Auth and users (`routers/auth.py`)

- Login is a Jellyfin user picker: `GET /api/auth/users` (no auth), then
  `POST /api/auth/login` authenticates through Jellyfin
  `/Users/AuthenticateByName`. Only Jellyfin's 401 reads "Invalid username
  or password"; its 503 (starting up) is a 503 "starting up", any other
  status a 502 naming it. Session: HMAC-signed cookie
  `tentacle_session` (30 days, HttpOnly), secret in the `session_secret`
  setting. After login the page does a full reload (clears SPA state).
- Dependencies: `get_current_user` (cookie, else 401),
  `get_current_user_optional`, `get_user_from_request` (cookie, or a
  *verified* `?api_key=` Jellyfin token: `userId` is only a claim; the token
  is resolved to its owner through Jellyfin), `require_admin`,
  `require_internal_or_admin` (the plugin's server-to-server calls).
  Admin-only routers declare `dependencies=[Depends(require_admin)]`.
- Bootstrap: with no `TentacleUser` yet, `require_admin` lets the setup
  wizard through. Until then `GET /api/auth/users` also answers 400 when the
  saved Jellyfin address doesn't answer, so the dashboard reopens the wizard
  instead of a login screen nobody can get past. `setup_complete` is set only
  by the wizard's "Get Started" or "Skip everything", never by a settings save.
- Roles: admin status is copied from Jellyfin's `Policy.IsAdministrator` on
  every login; the first user (lowest id) is the owner and can't lose admin;
  the login refuses a non-admin while no user exists (the owner becomes
  `jellyfin_user_id`, the account Tentacle reads Jellyfin as);
  Settings → Users toggles admin through Jellyfin's policy API. Non-admins
  see only Library and Jellyfin pages (`data-admin-only` in the nav,
  `applyUserRole()`).
- Global (shared): providers, VOD sync, Radarr/Sonarr scans, library
  content, tags on content, Live TV. Per user: playlists (Jellyfin playlists
  with `IsPublic=false`), auto-playlist toggles, list subscriptions, tag
  rules (custom playlists), home layout. Per-user files:
  `/data/smartlists/{jellyfin_user_id}/`, `/data/home-configs/{jellyfin_user_id}.json`.
- `DownloadRequest` (tmdb_id, media_type, user_id) records who asked for a
  download; non-admins may delete only what they requested.

## Key concepts

- **TMDB is the gatekeeper**: no TMDB match = the item is skipped, unless
  the provider has `require_tmdb_match=False` (then the provider's title and
  a negative tmdb_id). A built-in project TMDB key ships with Tentacle
  (`services/tmdb.py`); a user key/token in settings overrides it. It is
  never stored: `/api/settings/raw` doesn't serve it (the Settings page posts
  every field back) and a Save that sends it clears `tmdb_bearer_token`;
  only `/api/settings/plugin-keys` hands it out.
- **Two ways to tag**: VOD `.strm` items get `<tag>` elements in their NFO
  (Jellyfin reads NFO tags for `.strm` only); downloaded `.mkv` items must be
  tagged through the Jellyfin API (`services/jellyfin.py` `set_item_tags`),
  because Jellyfin ignores NFO tags on real video files. See
  [jellyfin-notes.md](jellyfin-notes.md).
- **Tag suffix**: source tags get the media type appended ("Netflix" →
  "Netflix Movies" / "Netflix TV"); playlist expressions must use the
  suffixed tag (`_extract_source_value()` in `services/smartlists.py`).
- **Recently Added** is a rolling window (default 30 days), refreshed on
  every scheduled sync.
- **Duplicates**: found when a download also exists as VOD; resolved ones
  stay in the DB with their resolution (the sync enforces keep_radarr).
  A keep_radarr tombstone goes with its download: the delete webhooks, the
  Radarr/Sonarr scans' removals, and after each scan any tombstone left
  without a row (`services/duplicates.drop_orphan_tombstones`), so the VOD
  copy can come back.
  A duplicate is resolved once (#328): `routers/duplicates.py` resolves
  each one under `_resolve_lock` (Resolve All per duplicate), re-reading
  its resolution from the DB first; one no longer pending gets 409 (Resolve
  All counts it as `skipped`), so a stale tab can't delete the other copy.
  The resolution is a Literal (keep_radarr, keep_vod, keep_both; "pending"
  is a 422).
  Keep VOD first needs the VOD copy on disk (`services/duplicates.vod_copy_on_disk`:
  a provider source's `.strm`, or a show folder with one; 409 otherwise):
  Keep Downloaded deletes the `.strm` before it saves the resolution, so a
  restart in between leaves it pending without one, and a film downloaded
  first never gets its provider `.strm`.
  It (`routers/duplicates.py:_delete_downloaded_copy`) deletes the
  imported files through Radarr's `moviefile` / Sonarr's `episodefile/bulk`
  API (never a `.strm`: Sonarr 4 lists Tentacle's `.strm` files as episode
  files), then removes the title with `deleteFiles=false` when its folder is
  also the VOD folder (`services/duplicates.arr_folder_is_vod_folder`).
  Radarr/Sonarr's `deleteFiles=true` deletes the title's whole folder, after
  answering 200. A series only counts as downloaded when Sonarr lists an
  episode file that is not a `.strm` (`series_has_real_download`): the
  Sonarr scan records no duplicate otherwise and dismisses (keep_both)
  pending ones like that, and Keep Downloaded on a series checks again
  before deleting anything (409 when nothing is downloaded, 502 when
  Sonarr can't be asked). Before either Keep deletes a copy,
  `services/duplicates.carry_user_data` merges every Jellyfin user's
  UserData (`/UserItems/{id}/UserData?userId=`) from the removed copy's
  item onto the kept one (films by path, shows per season/episode):
  Jellyfin 10.11 does not share it between two items of one TMDB id.
  Jellyfin down, or a film's kept copy not scanned yet while the other has
  user data: nothing is deleted (502 / 409). A film whose two copies share
  one folder is one Jellyfin item with both as versions (the other version
  is an owned item `/Items` leaves out; its path is in the item's
  `MediaSources`, read by `?Ids=`), and users' data is on that item. When
  the removed copy is its main version, the delete makes Jellyfin create a
  new item for the kept file with no data: the data is saved on the
  duplicate (`pending_user_data`) and committed before the delete, so a
  resolution that fails after it keeps it (#506; a retry replaces its
  entry), Jellyfin gets
  `/Library/Media/Updated` for the removed file, and
  `apply_pending_user_data` merges it onto the item whose Path is the kept
  file (a worker polls 30 s × 30, the nightly run catches up, dropped after
  30 days) (#333).
- **Following** = Sonarr `monitorNewItems="all"` (stricter than
  `monitored`), mirrored in `Series.sonarr_monitored`, synced both ways on
  every Sonarr scan; unfollowing keeps `monitored=true`. Hidden for ended
  or canceled series.
- **Hybrid series**: VOD `.strm` and downloaded episodes in one folder.
  "Download more episodes" adds the series to Sonarr with an explicit `path`
  in the VOD folder (needs a Sonarr root folder on the VOD shows directory;
  `add_to_sonarr` in `routers/lists.py` picks the root with "vod" in its
  path) and sets `Series.sonarr_path`, which stops the next scan from
  calling it a duplicate. Sonarr SeriesDelete on a hybrid clears
  `sonarr_path`/`sonarr_monitored` and keeps the VOD record.
- **Unaired episodes** are left out of counts (Sonarr `airDateUtc`): "7/7
  +1 upcoming", not "7/8".

## Playlists

Two kinds, both real Jellyfin playlists managed through the API, per user
(`IsPublic=false`). There is no playlist table: `get_desired_smartlists(db,
user_id)` computes them every time from source tags, list subscriptions
(`ListSubscription.playlist_enabled`), tag rules and built-ins, filtered by
`AutoPlaylistToggle` (keys like `source:Netflix:movies`,
`builtin:recently_added_movies`).

- **Auto**: one per provider source tag, one per list subscription, and the
  built-ins "Recently Added Movies", "Recently Added TV" (capped at 50),
  "Downloaded Movies". Tag-based expressions (`Tags Contains <tag>`).
- **Custom** (tag rules from the Playlists page): conditions that Jellyfin
  knows natively (genre, rating, year) become Jellyfin expressions and match
  the *whole* library; Tentacle-only conditions (source, source_tag,
  runtime) go through tags; a mix falls back to tags
  (`_classify_conditions()`, `_conditions_to_expressions()`).
- Pipeline: `sync_smartlists(db, user_id)` writes the configs to disk and
  creates/updates the Jellyfin playlists, returning `changed_names`;
  `refresh_smartlist_playlists(db, user_id, only_names=...)` fills them
  (only the changed ones after an edit, all at night), in chunks of 50,
  under the process-wide `_playlist_refresh_lock` (concurrent refreshes
  corrupted Jellyfin's playlist folders); `write_home_config(db, user_id)`
  regenerates the home layout.
- Sort: per playlist, stored in the on-disk config's `Order`;
  `PRESERVED_FIELDS = ["LastRefreshed", "DateCreated", "ItemCount", "Order"]`
  survive rebuilds. Built-ins are `(name, media_types, sort, max_items)`
  tuples (`services/smartlists.py`). Changing the sort clears the playlist
  and re-adds items in order; `DateCreated` sorts use Tentacle's own
  `date_added`, because Jellyfin's is unreliable for bulk imports.
- Every change in the UI syncs at once (no "Sync" button). Each mutation in
  `routers/smartlists.py` calls `bump_playlist_version()`; the web plugin
  polls `GET /api/smartlists/version` to redraw.

## Home screen config (per user)

`/data/home-configs/{jellyfin_user_id}.json`, read by the plugin through
`GET /api/smartlists/home-config?userId=` (no shared volume):

```json
{"hero": {"enabled": true, "playlist_id": "…", "display_name": "…", "sort_by": "random",
          "sort_order": "Descending", "require_logo": true, "require_trailer": false},
 "rows": [{"type": "playlist", "playlist_id": "…", "display_name": "…", "order": 1, "max_items": 20},
          {"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 2}]}
```

- Row keys: `playlist:<guid>` or `builtin:<section_id>` (reorder/remove).
- The hero has its own sort; `require_logo` (default on) keeps only items
  with a backdrop and a logo; `require_trailer` only items with a trailer.
  The plugin applies both when rendering.
- `write_home_config()` checks the hero playlist still exists (else first
  available, or off) and remaps rows and hero by `display_name` when a sync
  recreated playlists with new ids (otherwise rows silently vanish).
- When a user has a Tentacle home, `disable_home_sections()`
  (`services/jellyfin.py`) turns Jellyfin's own home sections off for that
  user (DisplayPreferences), so rows don't appear twice.

## Deleting things

- Deleting a provider removes its VOD files and DB records, then rebuilds
  playlists and checks the hero. It takes the provider's sync slot
  (`_running_syncs`): refused (409) while a sync of it runs, and holds the
  slot until its commit so no sync starts meanwhile. A sync writes files
  before it commits their rows, so a delete under it left orphan files.
- Downloaded content only (never VOD, which is admin-only from the
  dashboard): the TV app or the web plugin calls
  `DELETE /TentacleDiscover/LibraryItem/{type}/{id}?jellyfinItemId=`, the
  plugin proxies to `DELETE /api/library/delete-download/{tmdb_id}`, which
  checks permission (admin, or the `DownloadRequest` owner), deletes in
  Radarr/Sonarr (files), Jellyfin and the DB, then removes the item from
  every user's playlists in the background. `can_delete` in the discover
  detail response tells clients whether to show the button.
- Deleted in Jellyfin's own UI: the plugin's `LibraryDeleteHandler` sees
  `ItemRemoved` (2 s debounce) and calls `DELETE /api/library/item/...`
  with the deleted item's `item_id` and `path`. The row goes only when the
  path is the row's own copy (its `.strm`, or its Radarr/Sonarr file,
  compared by folder/file name); deleting the other copy of a title in
  Jellyfin twice (VOD + download) keeps the row and drops only a pending
  duplicate record (#296). Playlist entries are removed by the deleted
  item's id; an older plugin sends none, and the next playlist refresh
  prunes the dead entry. It forwards nothing while Jellyfin re-reads the
  folder the item left (`IProviderManager.GetRefreshProgress(e.Parent.Id)`:
  the library monitor, `/Library/Media/Updated`, a one-library scan) or the
  Scan Media Library task runs: those removals are files Jellyfin can't see
  (a dropped mount), not user deletions (#448).
- The nightly `sweep_orphaned_downloads()` removes downloaded records
  Jellyfin no longer has.
- "Fix it" (`services/wrong_match.py:rematch_movie`) keeps the `.strm`'s
  path when its folder holds only that copy: it rewrites the NFO and asks
  Jellyfin for a full refresh of the same item, so users' data and playlist
  entries stay (a new path is a new Jellyfin item, #294). A copy sharing its
  folder with a download moves, and never into a folder another title owns
  (`_folder_taken`, #293).
- Never act on "the first Jellyfin item with this TMDB id": the same film
  can be there twice (VOD + download). Resolve the item from its path or
  its own id.

## UI words

The UI says "Playlist" everywhere; the code says SmartList, TagRule,
output_tag. Never "Collection" (a different Jellyfin thing) or "Tag" in the
UI. The "Jellyfin" page has three tabs: Home Screen, Playlists, Discover (the
`discover_in_jellyfin` toggle lives there and saves at once).

Every function called from an `onclick=""` in `index.html` must be exported
in the `exposeGlobals()` block at the bottom of `pages.js` (else
`ReferenceError`); top-level functions in `app.js` are global already.

## Database

SQLite `/data/tentacle.db` (WAL). Models in `models/database.py`:
settings, providers, provider_categories, category_snapshots, movies,
series, youtube_channels, youtube_videos, duplicates, sync_runs,
list_subscriptions, list_items, tag_rules, home_row_order,
auto_playlist_toggles, tentacle_users, notifications, download_requests,
music_artists, music_albums, and the Live TV tables. Credentials live in
`settings` (key/value) and `providers`; never log or print them.

A list item is keyed on (list_id, tmdb_id, media_type): TMDB numbers films
and shows separately, so a list may hold movie/N and tv/N. Anything that
looks up list items by TMDB number filters on the type too
(`ListItem.of_type()`; a row without a type is a film).

## Live TV

Tentacle is the HDHomeRun tuner Jellyfin sees (it replaced Threadfin):
`/discover.json`, `/lineup.json`, `/device.xml` (also under `/hdhr/`), the
guide at `/api/live/xmltv.xml`, streams through `/api/live/stream/{id}`
(follows provider redirects with the provider's user agent, HLS → MPEG-TS).
User docs: `docs/features/live-tv.md`.

- Raw MPEG-TS streams re-dial from the channel URL when the provider drops
  them (`stream_generator()` in `_stream_proxy_inner`). Waited out like a
  509 (the viewer budget; no limit while a recording is attached): transport
  errors, `_raw_retryable()` statuses (the open set plus 407 and any 5xx:
  providers answer 407 for an ended session, 513/520-524 for minutes), and a
  200 whose first bytes are an error page (`_looks_like_error_page()`: not
  MPEG-TS -- the sync byte 0x47 first, or 0x47 every 188 bytes from within
  the first packet for a start mid-packet, judged on the first 564 bytes
  held by `_decidable_start()` -- and a text type or a `{`/`<` start; never
  proxied). A mid-packet start's partial packet is dropped. 401/403/404 stop
  at once. A re-dial counts as a reconnect only once it delivers, and as a
  recovery (backoff and budget reset) only once it delivered past 10 s.
- Many panels start every raw connection with their buffer (~20 s already
  sent, byte for byte). `_ReplaySplicer` (#368) joins a re-dialled
  connection right after the last byte sent: it holds the connection until
  its first frame start (a video/audio PES with a PTS); if that packet was
  sent, every later frame start up to the join point must sit where it was
  sent, the bytes since the last frame start must match, and no unjoined
  connection may have begun in front of it. Then the replay is dropped
  (`replays_joined`, `replay_bytes_skipped` in the health); anything
  unproven (a gap, a seamless re-dial, a remux, a replay over 32 MB or 30 s,
  a break before the join) goes out as it came: duplicates, never a loss.
  A joined reconnect is not damage in `_stream_ended`. A provider that
  loops already-aired packets verbatim can be joined inside its loop (only
  repeated bytes are dropped).
- The HLS worker (`hls_to_mpegts()`) classifies statuses with the same
  `_raw_retryable()`: a 5xx on a playlist, a re-resolve or a segment is
  waited out (a 5xx segment is fetched again, not skipped). An expired token
  (401/403/404/407/410 from a token URL) re-resolves the channel URL, at most
  `_MAX_RERESOLVE` times in a row; a 407 from the channel URL itself (an
  ended session while the token is renewed) does not count for a recording
  and is waited out on the refusal cap. Viewers keep the three tries.
- Channel ids are the provider's `stream_id` (stable across changes), used
  as `GuideNumber`; Jellyfin keys the channel, its timers and favourites on
  `hdhr_<GuideNumber>`, so it must never change. M3U channels have no
  provider id: `stream_id` is the hash of the first name + URL seen, and
  `m3u_key` the hash of the current ones, which a sync matches by. A URL
  change (rotated token, new host) moves `m3u_key` only (#259).
- A running HLS stream reads every playlist and segment body within a total
  bound (`_aread_within()`): 10 s for a playlist, max(20 s, 3 x the target
  duration) for a segment. httpx's read timeout is per read, so a body that
  trickles never reaches it. Past the bound it is a ReadTimeout, retried
  like a stall; the next read of that kind gets twice the time (up to 4x)
  and a body that arrives within the plain bound resets it, so a provider
  that turned slow but still delivers is waited for.
- Two-phase sync: groups with counts, then channels for enabled groups; a
  channel sync chains into an EPG sync. The EPG (XMLTV, cached on disk) is
  stored for *all* provider channels, so newly enabled ones have a guide.
  Channels may share one `epg_channel_id` (one-to-many in the XMLTV output).
  An EPG sync first deletes every id it could store programmes under (each
  channel's guide id, name match and tvg-id, and every name match of this
  run, kept or dropped), or the insert hits `uq_epg_program` (#467).
- A group is unique on (provider, name) with one Xtream `category_id`, but
  Xtream category names aren't unique: `_sync_groups` gives each category
  its own group (the one already on it, else its name, first listed wins,
  else `NAME (id)`). Matching by name alone crashed the sync or moved an
  enabled group to the other category.
- After an EPG sync Tentacle deletes and re-adds its XMLTV listing provider
  in Jellyfin, then runs RefreshGuide: re-POSTing a listing provider with
  the same id does *not* remap new channels. `services/jellyfin_guide.py`
  does it under one lock, decides from Jellyfin's config whether the copy
  was saved (Jellyfin 10.11 can save it and answer 500), and keeps one
  provider per Path, deleting leftover copies (#274).
- `POST /api/live/refresh-guide` first runs the EPG sync inline when an
  enabled channel's guide id has no programmes, unless that can't help: a
  successful sync stores in `livetv_epg_synced_<pid>` a hash of what it read
  (feed URL, cached feed file, each channel's override, tvg-id and name) and
  the guide ids it left empty. Same hash and every missing id among those:
  no re-run (a tvg-id the feed lacks would re-run it on every call, #466).
- Provider fields: `provider_type` (xtream, m3u_url, m3u_file),
  `user_agent`, `epg_url`, `require_tmdb_match`, `live_tv_enabled`. The
  dashboard adds providers only in Settings → Providers, and one row serves
  VOD and Live TV: `POST /api/providers/{id}/test` sets `live_tv_enabled`
  when an Xtream account has live categories (an M3U one never gets it).
  `/api/live/provider` (GET, POST, test) has no form in the dashboard, and
  `user_agent` / `epg_url` no field: API only.

## Music (Lidarr + MusicBrainz; off by default)

"I like this song/album/artist" → the original studio album through Lidarr.
Tentacle never touches music files or a player's database, only APIs. User
docs: `docs/features/music.md`; plugin side: `Api/MusicController.cs`
(`TentacleMusic/*`), 404 unless the integration is on.

- One request path: `services/media_requests.py` (`request_movies`,
  `request_series`, `request_album`). Profiles and root folders come from
  settings, with no fallback to profile 1 (it refuses instead); a per-request
  choice is `quality_profile_override` (the legacy `quality_profile_id` is
  ignored: old clients always sent the first profile).
- `services/lidarr.py`: one request at a time, 15 s timeout, refuses
  `include*`, history, `since` and pageSize > 50, 2 retries. `/album` can't
  be paged: read per artist. Adding an artist with
  `addOptions.monitor: none` unmonitors every album on the first scan: pass
  `albumsToMonitor: [rgid]`.
- `services/musicbrainz.py`: 1 request/s, a contact e-mail in the user
  agent (setting), file cache `musicbrainz_cache`. Pages go before the
  worker, but give up after `INTERACTIVE_WAIT` (20 s) with
  `MusicBrainzBusy` (503). The page routes (search, album, artist, song) are
  `async` and run through `routers/music.py:_run_page`: at most
  `PAGE_THREADS` (4) build at once, the rest wait on the event loop up to
  `PAGE_QUEUE_WAIT` (then 503), and `MusicBrainz.from_settings(db,
  page=True)` hands the session's connection back before each lookup. A
  burst of music pages must never take the shared thread pool or the DB
  pool (10 + 20): that stalled all of Tentacle (#243).
- An album request: `request_album` adds (a failed answer to the add is
  checked with `album_by_mbid` before it counts as refused: it may have
  landed), marks the row `request_pending` and queues `jobs.finish_request`
  (pin the original, search). The worker's queue is in memory only, so a
  pending row is finished at startup (`resume_requests`) and at the start of
  each daily check (`finish_pending_requests`). An album that has files (one
  Lidarr had unmonitored, or one that downloaded since) is never pinned by a
  request: it is checked, so Fix library offers the pin, and searched only if
  its pin is already right and locked (#242, #422). An add whose answer failed (timeout, dropped connection, 5xx)
  and that Lidarr doesn't list yet may still commit: `jobs.owe_add` keeps a
  row with no Lidarr id, not monitored (still requestable), and
  `finish_pending_requests` looks it up by MBID 1, 5, 30 and 120 min later,
  at startup and each daily check; once listed it is pinned and searched,
  after `OWED_ADD_HOURS` (6) unlisted the row is dropped (#431). A 4xx owes
  nothing.
- `services/music/`: `worker.py` (one thread; urgent > normal > background),
  `jobs.py`, `original.py` (the original-release rules, pure), `apply.py`
  (pins and trims; deletions only when exactly the expected leftovers
  remain, each logged), `pictures.py`, `players.py` (Navidrome, Jellyfin),
  `discover.py`, `spotify.py`.
- The daily check's clean-up (`jobs._forget_gone_artists`, and
  `library.sync_artist` for albums) removes only what Lidarr really dropped:
  nothing on an empty artist list, no artist written after the list was read
  (a request finished during the check), and no album still
  `request_pending` (nor its artist).
- Webhook `POST /api/music/webhook?secret=` (secret always required);
  status `GET /api/music/status`.

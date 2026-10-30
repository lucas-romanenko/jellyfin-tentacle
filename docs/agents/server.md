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
  (unauthenticated): release version, commit, build date, `code.matches`
  (the running files match the image's fingerprint).
- Versions (`services/versions.py`): the image's `TENTACLE_VERSION` build
  arg is the release tag (Docker Publish) or `git describe --tags --match
  'v[0-9]*'` of a build of main, shown as "v1.23.0+3" (sidebar: "v1.23.0 + 3
  changes", the commit in its tooltip). An image built without it (from a
  GitHub URL at a commit: no tags in the context) asks GitHub at start-up
  which release its commit follows (`releases/latest`, then
  `compare/<tag>...<commit>`), once; offline it stays unknown and the
  sidebar shows the commit. Settings → About (`/api/about`, admins): the
  server and the newest release (GitHub, cached 6 h), the plugin running in
  Jellyfin (`/Plugins`) against `MIN_PLUGIN_VERSION` (raise it when the
  server starts needing a newer plugin), whether Tentacle's catalogue is in
  Jellyfin's `/Repositories`, Jellyfin's version, and each person's TV app
  with the features of `TV_APP_NEEDS` this install can't serve yet.
- Paths inside the container are fixed (users map host folders with
  volumes; Health → Media folders checks them): `/data` (DB, caches,
  per-user `smartlists/` and `home-configs/`), `/media/movies` (Radarr),
  `/media/shows` (Sonarr), `/media/vod/movies`, `/media/vod/shows` (VOD
  `.strm`), `/media/youtube`.
- Logs: `services/log_redaction.py` strips credentials from every log record
  (uvicorn's access log included): Xtream paths and any query parameter
  named like a secret (`*secret*`, `*token*`, `*password*`, `*api_key*`,
  `key`, ...). A new credential in a URL needs such a name, or a rule there.
- Every Xtream URL (player_api.php, xmltv.php, the `/movie|series|live/`
  stream paths) puts the login in through `quote_cred()`
  (`services/xtream_client.py`); never `provider.username`/`password` raw: a
  `#&+?/%` in it cuts or splits the URL (#529). Plain logins stay byte-identical.
- One worker, one event loop: anything blocking in an `async def` freezes
  every stream, recording and request. The SSRF guards (`services/ssrf.py`:
  `is_safe_url`, `lan_origin_guard` and the guards it returns) resolve DNS
  with a blocking `getaddrinfo`, so async code calls them through
  `asyncio.to_thread` (#371, #464); `url_host_allowed` is the allowlist
  half, no lookup. Stream paths use `await check_url(guard, url)`: off the
  loop, with the reason, and a name that did not resolve (`LookupFailed`)
  asked again after 0.5 s and 1.5 s (providers redirect each open to a new
  random subdomain; a resolver blip is not an SSRF block). A non-public
  answer is refused at once. `refusal_text(why)` is what the caller is told.
- Dashboard dialogs (`.modal-overlay`) open and close only through
  `showModal()`/`closeModal()` (`static/js/app.js`, "Modals"), never by
  setting `style.display`: they move the focus in, keep Tab inside, give
  the focus back to the opener and set the dialog role (#552). Their test
  runs that block under node (`tests/test_dashboard_dialog_keyboard.py`), so
  its helpers stay inside it.

## Auth and users (`routers/auth.py`)

- Login is a Jellyfin user picker: `GET /api/auth/users` (no auth:
  `{people, at_home, quick_connect}`), then `POST /api/auth/login`
  authenticates through Jellyfin `/Users/AuthenticateByName`. People are
  Jellyfin's `/Users/Public` (accounts not "hidden from login screens":
  Jellyfin hides new accounts, so invites un-hide theirs and People has an
  "On sign-in screen" switch writing `IsHidden`). Names go only to a browser
  at home (`services/sign_in.py:at_home`: the peer and every
  CF-Connecting-IP / X-Real-IP / X-Forwarded-For / Forwarded address must be
  private or Tailscale); away, the list is empty and the page asks for a
  name. The plugin's `PublicUsersFilter` does the same to Jellyfin's
  `/Users/Public`. Sign in with a code: `POST /api/auth/quick-connect`
  (Jellyfin's `/QuickConnect/Initiate`; the browser gets the code and a
  random handle, Jellyfin's secret stays in memory, 10 min, 200 at most),
  polled at `GET /api/auth/quick-connect/{handle}` until someone approves
  it (sidebar → Sign in a TV), then `/Users/AuthenticateWithQuickConnect`
  and the same `_finish_sign_in` as a password. Only Jellyfin's 401 reads "Invalid username
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
  People toggles admin through Jellyfin's policy API. Non-admins
  see only Library, Discover and My Home Screen (`data-admin-only` in the
  nav, `applyUserRole()`; `showPage()` sends them from a server page to
  Library).
- Global (shared): providers, VOD sync, Radarr/Sonarr scans, library
  content, tags on content, Live TV. Per user: playlists (Jellyfin playlists
  with `IsPublic=false`), auto-playlist toggles, list subscriptions, tag
  rules (custom playlists), home layout. Per-user files:
  `/data/smartlists/{jellyfin_user_id}/`, `/data/home-configs/{jellyfin_user_id}.json`.
- `DownloadRequest` (tmdb_id, media_type, user_id) records who asked for a
  download; non-admins may delete only what they requested.

## Family (Roadmap 1c: `routers/family.py`, `services/family.py`, `approvals.py`, `admin_alerts.py`, `tv_app.py`)

- People (admins): every Jellyfin account gets a `TentacleUser` row when
  People is opened (`_ensure_rows`; a new row starts on the defaults as at a
  first sign-in). Settings → Users is People now (old links redirect).
- Gone people (`services/gone_people.py`): a row whose account Jellyfin's
  `/Users` no longer lists is forgotten when People is opened and at the
  start of the nightly per-user step: its per-person rows (list
  subscriptions and their items, tag rules, row order, toggles,
  notifications, download and media requests, music imports,
  `live_user_lineups`/`live_user_channels`), `smartlists/<id>/`,
  `home-configs/<id>.json` and its backups, `family_defaults` naming them,
  and the token/home-version caches. Never the owner or `jellyfin_user_id`,
  never on an empty list. Their Jellyfin playlists: `DELETE /Users/{id}`
  removes private unshared ones from the library (`RemovePlaylistsAsync`)
  but leaves their folders, which the next scan brings back (jellyfin-notes.md);
  the plugin removes those folders when the user is deleted
  (`Services/DeletedUserPlaylists.cs`). Should one come back still naming its
  owner, `/Tentacle/Playlists/Ownerless` marks it `ownerGone` and the nightly
  ownerless clean-up (`_cleanup_ownerless_playlists_locked`) deletes it when
  private and unshared.
- Add someone (Roadmap 1e; the dashboard's three steps, one `POST
  /api/family/invite`): `POST /Users/New {Name, Password}` with the admin's
  password or `make_password(name)` (lowercase name, 5 digits, "!"; the
  browser suggests the same shape), then one policy POST with
  `person_access.new_person_policy` (shown, Live TV or not, recording for
  grown-ups, lock after `LOCKOUT_AFTER` wrong passwords). The row is made at
  once (`invited_by`, `request_mode`, `live_tv_off`, `welcomed_at` None),
  then `start_new_person(id, home, channels)` in the background: what the
  admin picked, else `starting_point` (grown-ups: `family_defaults`, whose
  home and whose favourites to copy, `copy_home` / `copy_channels`; kids:
  `family_kids_defaults`, default the kids setup in
  `services/kids_start.py`: the kid's own genre rule playlists Kids Movies
  (Animation or Family) and Kids Shows (+ Kids), native rules that Tentacle
  fills by querying Jellyfin as the kid, so their age limit applies; home
  Continue Watching + those two, Discover/Activity off; the kids channels
  starred), else the usual new-user playlists. The answer carries
  `invite.message` (`services/welcome.py:message`): Tentacle first, the
  sign-in, Jellyfin's address, the TV app last.
- Addresses (`family.addresses`): the "for your browser" ones
  (`tentacle_public_url`, `jellyfin_public_url`, Settings → Services), else
  the Tentacle address / `jellyfin_url` when a browser can open them (never
  a Docker name); `ask` until Add someone has asked once
  (`invite_addresses_asked`) while one works only at home.
- Welcome (`services/welcome.py`): `TentacleUser.welcomed_at` None and not
  the owner = shown at sign-in (people from before it came were marked
  welcomed by the migration); `/api/welcome/done` ends it. Pick my own
  password: the current one checked with `AuthenticateByName` (counts
  towards the lock), then `/Users/Password` with the admin key.
- Lock: `LoginAttemptsBeforeLockout` = 5 for every non-admin whose policy
  still has Jellyfin's never (-1): on invite, on People load
  (`person_access.ensure_lockout` with the `/Users` it read) and 90 s after
  start. Locked = IsDisabled and `InvalidLoginAttemptCount` >= the limit;
  Unlock = the access route's `disabled: false`, which zeroes the count.
- Live TV per person (`live_tv` in the access route): EnableLiveTvAccess;
  `live_tv_off` keeps the admin's choice for kids, whose access otherwise
  follows the kids channels (`set_kids`, `set_kids_channels`).
- Kids profile (`kids_level` young/kids/teens = Jellyfin scores 7/10/14):
  MaxParentalRating, BlockUnratedItems += Movie, Series, Trailer (not
  LiveTvProgram: Tentacle's guide has no ratings, it emptied a kid's guide);
  Live TV = the household's kids channels (`livetv_kids_channels`)
  by tag on the one lineup (1b), or EnableLiveTvAccess off while none are
  picked. The fields it changes and request mode are saved in
  `kids_saved_policy` first and restored when it's switched off. A kid's My
  Channels shows only the kids channels (they can star them). Policy posts are
  always the full policy re-read just before (the Live TV sync's
  `apply_policies` re-reads too, so it never puts an old policy back).
- TV codes: `POST /QuickConnect/Authorize?code=&userId=<dashed id>` with the
  admin key (Jellyfin allows an administrator to authorize for any user);
  404 = no device shows that code, 401 = Quick Connect off.
- Requests: `approvals.needs_approval(user)` (non-admin and `request_mode`
  not "direct") makes add-to-radarr/sonarr and music/request keep a
  `MediaRequest` (the original body in `payload`) and answer
  `{added:0, already_exists:0, failed:0, pending:N, detail}`. Approve replays
  the body through media_requests as the requester (`via` "approved by X");
  a refusal keeps it as `failed` with the reason for another try. A list's
  auto-add never runs for someone whose requests wait.
- Notifications: `Notification.kind` download | request | problem; `force`
  sends requests and problems to admins whatever their download switch says.
- Problems (`admin_alerts.check`, every 10 min): providers failing (from
  provider_health's state; it sends its own Pushover), Radarr/Sonarr
  unreachable and disk errors (arr_insight), stuck downloads, Live TV set-up
  failing, Jellyfin down. `AdminAlert` rows: told once when new, once when
  over. A check that fails itself leaves its alerts alone.
  `family_messages()` gives one plain line per area for everyone (no app
  names, addresses or codes): `/api/family/status`, the dashboard banner,
  the plugin's Live TV page and the TV app.
- TV app: `GET /tv` redirects to the newest release APK (GitHub API, cached
  6 h); People shows each person's Tentacle TV app version from Jellyfin's
  `/Devices` (`AppName` "Tentacle Android TV", debug builds left out).

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
  suffixed tag.
- **A rule's playlist query** (`_rule_expressions()` in
  `services/smartlists.py`, shared by the full sync, saving the rule and the
  builder's preview count): genre/rating/year only → Jellyfin's own fields;
  one provider plus zero or more genre/rating/year → the provider's suffixed
  tag(s) AND those fields (#540); anything else (list, runtime, downloaded)
  → the rule's own output tag, filled only after the tagger runs.
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
  is a 422). Nothing makes `(tmdb_id, media_type)` unique (two scans at
  once can each write a row): `_resolve` merges a title's pending twin rows'
  sources into the one resolved and marks the twins with it (#504), so a
  twin can't later be resolved the other way and delete the kept copy.
  Keep Downloaded on a film checks Radarr first (`_require_movie_download`):
  409 when no `radarr` source, Radarr not configured, the film not listed or
  no non-`.strm` file; 502 when its files can't be read.
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
  pending ones like that; it also treats such a VOD show as no download (no
  `downloaded_at`, no "Downloaded TV", no tag or NFO write: it skips the
  tag/NFO step) and releases one an older scan tagged (#682, see
  pipelines.md), and Keep Downloaded on a series checks again
  before deleting anything (409 when nothing is downloaded or when some
  VOD episode, by SxxEyy, has no downloaded file: `vod_only_episodes`, #800;
  502 when Sonarr can't be asked). Resolve All leaves such shows pending. Before either Keep deletes a copy,
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
  Radarr/Sonarr post their delete webhooks inside Keep VOD's delete calls,
  while the resolve request holds the title. The handlers and scans drop a
  title's duplicates only through `services/duplicates.droppable_duplicates`:
  none while `_resolve` runs inside `resolving(media_type, tmdb_id)` (a
  process set; the webhook can't wait on `_resolve_lock`, the resolve waits
  for the arr, which waits for its webhook), and never a keep_vod one with
  `pending_user_data`. After the arr call Keep VOD re-reads the row
  (`populate_existing`): the webhook may have released or deleted it (#515).
  It then releases the VOD row itself (films `release_vod_download`, #549;
  series `release_vod_series_download`, #617) and deletes the request, so
  it doesn't depend on the webhook coming in time or at all.
- **Duplicates rule** (#872, `services/duplicates_auto.py`): setting
  `duplicates_rule` = off (default, also on upgrade) / download / stream;
  a 15-min job, nothing while off. It decides each pending duplicate
  (`decide`, read-only: arr titles/files/history, `stream_health`
  known-bad, `provider_health` state, qBittorrent's torrents via
  `services/qbittorrent.py`, Jellyfin's played file names) and applies it
  through `routers/duplicates._resolve` under `_resolve_lock`, so every
  guard above holds (#800, user data, tombstone); Deletion Log reason
  `duplicates-rule`, `Duplicate.resolved_by = "rule"` + `resolved_why`.
  Stream first never contacts a provider: "plays" = not known-bad, provider
  state ok, .strm on disk. More changes than `duplicates_rule_cap` (10) in
  one run: nothing changes, summary state "held" until POST
  `/rule/run {confirm: n}` with n >= the changes. Summary JSON in
  `duplicates_rule_last`. Prefer-download list = `prefer_download` on
  movies/series. qBittorrent password is a SENSITIVE/PASSWORD key.
- **Following** = Sonarr `monitorNewItems="all"` (stricter than
  `monitored`), mirrored in `Series.sonarr_monitored`, synced both ways on
  every Sonarr scan; unfollowing keeps `monitored=true`. Hidden for ended
  or canceled series.
- **Hybrid series**: VOD `.strm` and downloaded episodes in one folder.
  "Download more episodes" adds the series to Sonarr with an explicit `path`
  in the VOD folder (needs a Sonarr root folder on the VOD shows directory;
  `add_to_sonarr` in `routers/lists.py` picks the root with "vod" in its
  path) and sets `Series.sonarr_path`, which stops the next scan from
  calling it a duplicate. Sonarr SeriesDelete on a hybrid keeps the VOD
  record and releases its download (pipelines.md "Sonarr deletes a series").
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
  survive rebuilds. A built-in gets its default sort only when created;
  no sync rewrites the user's `Order`. Built-ins are `(name, media_types, sort, max_items)`
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
          "sort_order": "Descending", "require_logo": true, "require_trailer": false,
          "trailer_audio": false, "item_count": 10},
 "rows": [{"type": "playlist", "playlist_id": "…", "display_name": "…", "order": 1, "max_items": 20},
          {"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 2,
           "shape": "wide", "max_items": 20}],
 "merge_continue_watching": true}
```

- Row keys: `playlist:<guid>` or `builtin:<section_id>` (reorder/remove).
- **No Jellyfin home sections** (Lucas, 2026-10-26). The `builtin` rows are
  Tentacle's own (`services/home_rows.py`): Continue Watching
  (`resumevideo`: resume + next episodes when `merge_continue_watching`, its
  "Next episodes" switch; poster/wide, default wide), Next Up (`nextup`: the
  next episodes as a row of their own, 2026-10-30; offered by
  `builtin-sections`/`add-row` only while `merges_next_up()` is false, i.e.
  Continue Watching isn't on the home with its switch on; switching it on
  removes the Next Up row, and `normalize_rows()` does the same, so no card
  shows twice; adding Continue Watching next to a Next Up row turns the switch
  off; poster/wide, default wide), On Now (`onnow`: its own square channel
  card, no shape) and Recordings (`recordings`: the latest recordings,
  poster/wide, default poster; picker only). The plugin works out Continue
  Watching, Next Up and Recordings once (`/TentacleHome/ContinueWatching`,
  `/NextUp`, `/Recordings`) for the web and the TV app, and its `Sections`
  sends no other section. In both next-episode lists a show counts as last
  watched when its next episode arrived, if that was after the last play and
  at least 6 h after the last watched episode was added (a new season jumps to
  the front; a library re-scan that re-dates every file doesn't):
  `TentacleRows.NextUpDate`; the plugin reads up to 200 of Jellyfin's Next Up
  before ordering, as Jellyfin lists them by last play. `normalize_rows()` keeps
  every config that way (`write_home_config`, the default); once per install
  (`home_rows_tentacle_only`, 26 Oct) every saved home lost Jellyfin's
  sections (Jellyfin's lone Next Up became Continue Watching with next
  episodes on; `nextup` is Tentacle's own row since 30 Oct).
- The hero has its own sort; `require_logo` (default on) keeps only items
  with a backdrop and a logo; `require_trailer` only items with a trailer.
  The plugin applies both when rendering. `trailer_audio` is off when
  missing everywhere (plugin `HeroConfig`, `write_home_config`, the
  dashboard); `set_hero` carries every setting over to a newly picked
  playlist.
- `set_hero` names the hero from its row, else Tentacle's playlist, else
  Jellyfin's item `Name` read as that user (`/Users/{uid}/Items/{id}`, so a
  playlist they can't see stays unnamed); the id is the last resort.
- `write_home_config()` checks the hero playlist still exists (else first
  available, or off) and remaps rows and hero by `display_name` when a sync
  recreated playlists with new ids (otherwise rows silently vanish).
- **Default home** (`services/default_home.py`): one layout for everyone
  without their own, stored in the `default_home` setting with playlist rows
  keyed by ready-made playlist (`builtin:recently_added_movies`,
  `source:<name>:movies`, `youtube:<id>`), since each person's playlist ids
  differ. Unset = `suggested_default()` (Continue Watching with next
  episodes, On Now, recently added movies/TV, the biggest sources; spotlight
  on recently added movies). Recently added reaches back past the window when
  fewer than 20 titles are in it (`recently_added_cutoff`), so a new or quiet
  server's rows are never empty.
  A person's file carries `follows_default: true` while it is the default;
  `apply_default()` writes it with their own playlist ids (switching on the
  ready-made playlists it needs). Any change on their own Home Screen drops
  the flag (theirs now); `POST /home/reset-to-default` sets it again. Admins
  edit the default with the same endpoints plus `?scope=default`; a save
  rewrites every follower in the background (`apply_to_followers_soon`), so
  it reaches clients through the home version like any edit. New people (no
  file) get it on first read or `write_home_config`; the nightly sync
  re-applies it (new sources). Once per install (`default_home_migrated`)
  homes that are still the old starter (Jellyfin's sections as rows, never
  changed) move to the default; anyone's own layout is never touched. Kids
  get the same rows: the plugin only returns items they may open.
- When a user has a Tentacle home, `disable_home_sections()`
  (`services/jellyfin.py`) turns Jellyfin's own home sections off for that
  user (DisplayPreferences), so rows don't appear twice.

## Discover search (`routers/discover.py:search_discover`)

The TV app searches on every keystroke, so `/api/discover/search` must
answer fast (the plugin gives up after 15 s and the TV says "Tentacle is
busy"). Order: enabled Live TV channels (local table, max 20), then TMDB
movies/series, then Sonarr/Radarr text-lookup titles TMDB lacks (TheTVDB).

- Radarr/Sonarr `lookup_by_term` asks the internet metadata services and can
  take its whole 15 s. It runs on the shared `_search_executor("arr")`
  threads and gets `SEARCH_ARR_WAIT` (2 s) from the request's start; later
  answers fill the cache for the next request. Never wait on it longer.
- Per query (lowercased, spaces collapsed) and type, the futures are kept
  `SEARCH_CACHE_TTL` (5 min); a request for a query still running shares it.
  A failed TMDB search is dropped from the cache. 1 character = channels
  only; 2 = no *arr lookups (`SEARCH_MIN_CHARS`, `SEARCH_ARR_MIN_CHARS`).
- "In Library" / "Requested" come from the Movie/Series tables and the
  cached Jellyfin and *arr id maps; a stale map is used while it refreshes in
  the background (`_search_jellyfin_ids`, `_search_arr_ids`), only a first
  fetch ever is waited for (up to the same 2 s).

## Folders Radarr/Sonarr don't know about (`services/untracked.py`, #871)

Library → **Not in Radarr/Sonarr** (admins, `static/js/untracked.js`) and
a line on the last setup step when there are any. Read-only list: each top
folder of `/media/movies` and `/media/shows` with a real video file (to
depth 2 / 4; `.strm`-only and empty folders are Tentacle's or nothing) whose
NAME no Radarr movie / Sonarr series path ends in (as `local_arr_folder`
finds downloads, #649). Matched to TMDB by the ids in its NFO (`uniqueid`,
`tmdbid`, `imdbid`, `<id>tt…`; `tvshow.nfo` for shows), else ids in the
folder name (`{tmdb-603}`, `[imdbid-tt…]`, `{tvdb-…}`): 100 %; else title
and year from the name (or the NFO's `title`/`year`), scored by
`confidence()` (99 at most). Under 80, or a runner-up within 5 points (a
namesake, no year), is in doubt: the row offers Pick (`/search`). A match
Radarr/Sonarr already has in another folder is shown, not addable.

Add goes through the shared path (`media_requests.add_existing_movie` /
`add_existing_series` → `arr_add.add_movie_to_radarr(path=…, search=False)`
/ `SonarrService.add_series(series_path=…, search=False)`): the default
quality profile (refused without one), the folder as the title's `path`,
`searchForMovie` / `searchForMissingEpisodes` false, unmonitored unless
`untracked_monitored`. Radarr/Sonarr read the files where they are; nothing
is moved, renamed or downloaded, and Tentacle writes nothing there. Their
path for the folder is the parent most of their titles that Tentacle sees
by name sit in, else the default root folder setting, else their first
non-VOD root. Only a folder on the current list is sent. A Radarr/Sonarr
scan follows 30 s later. The list lives in memory (`_state`), checked again
when the page opens on one older than 6 h, on "Check again", and nightly
with `untracked_auto_add` (off by default), which adds the 95 %+ sure ones.

## Deleting things

- Deleting a provider removes its VOD files and DB records, then rebuilds
  playlists and checks the hero. It takes the provider's sync slot
  (`_running_syncs`): refused (409) while a sync of it runs, and holds the
  slot until its commit so no sync starts meanwhile. A sync writes files
  before it commits their rows, so a delete under it left orphan files.
  It also removes what is keyed by the provider's id: the Live TV settings
  (`livetv.PROVIDER_SETTING_KEYS`), its `provider_relink_pending` and
  `provider_health` entries, blocked streams and match fixes. SQLite gives the
  next provider the highest deleted id again, so anything new keyed by
  provider id must go there too (#625).
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
- Every `log_deletion()`/`DeletionLog(...)` kind needs a label in
  `_DELETION_KIND_META` (`static/js/pages.js`), or Health → What happened → Removals
  shows the raw code (`tests/test_deletion_log_kinds.py` enforces it).
  `*-blocked` kinds record removals a safety limit refused (nothing
  deleted): label "… refused", gray badge.

## VOD through Tentacle (`routers/vod.py`)

- The default: Xtream `.strm` files point at
  `/api/vod/{movie|series}/{provider}.{id}.{sig}.{ext}` (`services/vod_tokens.py`).
  `vod_via_tentacle_enabled` is only an opt-out ("false", no dashboard
  switch); unset is on (`vod_enabled`). Opted out, files play straight from
  the provider and a drop mid-episode ends the episode.
- The address (`sync.vod_base_url_for`): `vod_base_url` as saved, else the
  Jellyfin plugin's TentacleUrl, then the YouTube Tentacle address; the
  first that is not loopback or a one-word (container) name and answers
  `check_base_url` is saved as `vod_base_url`, so files never flip. None:
  direct links stay, with a warning in the log.
- Existing files move over without a sync: `provider_relink.start_vod_route_relink()`
  runs 90 s after start-up (main.py) and after a save that flips the
  setting; it waits for running syncs, rewrites each Xtream provider's own
  direct/`/api/vod` files to the wanted form (`relink_vod_route`,
  idempotent), then a Jellyfin scan and a `vod_route_relinked` Activity
  entry when anything changed. Every sync applies the same rule
  (`_strm_needs_rewrite`).
- The relay: a drop or a body that ends cleanly short of its
  Content-Length/range ("Stream ends prematurely" in ffmpeg) is resumed
  with Range from the byte reached (first try after ~0.5 s, doubling to
  5 s, 6 tries); a connection that delivered 8 MB or lasted 30 s resets the
  reconnect budget. Always, whatever the first answer said: the provider
  answers a request from byte 0 with a 200 naming no Accept-Ranges yet
  serves `bytes=N-` with a 206. Only a 206 starting at exactly that byte is
  spliced in; anything else ends the body (the player asks again). One provider connection per
  playback (title + client address): Jellyfin's parallel requests for it
  (subtitle extraction, transcode restarts, seeks) share it. The newest
  request takes it (`_Playback.take`: the old one closed before the new one
  opens, under `open_lock`), the parked ones wait and carry on from their
  own byte when the holder ends or has had it `QUANTUM_SECONDS` (8 s); a
  holder stuck on a paused player is taken from after 2 s more. A HEAD
  during a playback is answered from it. Different titles are separate
  playbacks (separate connections, counted by the lease broker), and so
  are different clients of a title when Jellyfin shows as many people
  watching it.
- Fast starts: every VOD request goes through one pooled client
  (`_client()`, keep-alive 5 s, redirects still followed by hand and
  re-validated), and a title's redirect target (the edge server the
  provider's panel URL 302s to) is remembered for `TARGET_SECONDS` (5 min,
  `_targets`, keyed by the provider URL, so per title and account). A cached
  target that answers anything but 200/206 or cannot be reached is
  forgotten and the panel asked again in the same request. Measured (Oct
  2026): 0.5-0.8 s to first bytes per request through the panel, 0.15 s
  straight to the known edge; a play is a dozen requests before the first
  picture. The plugin's `VodProbeOncePatch` stops Jellyfin re-probing a
  title it already knows (jellyfin-notes.md "Who reads a .strm").
- Kept indexes: a player cannot start an MP4 before it has its whole
  `moov` (2.5-8 MB on the provider measured, after the `ftyp` or at the very
  end), and the provider's edge sends 1.5-6 MB/s: 1-3 s of every start.
  `services/vod_index_cache.py` keeps it from the first play (a `Tap` on the
  body: bytes 0 to the end of a moov at the head, or a body that starts at a
  moov box) in `/data/cache/vod-index` (1 GB, oldest out, 7 days). A request
  that starts inside the kept bytes gets them from disk while the rest is
  opened from the provider at once (`open_rest`); ExoPlayer's jump to a
  moov at the end opens no provider connection at all. A continuation whose
  Content-Range names another size or start drops the entry (the file changed).
- One person watching = one provider connection (`_watch_viewers`): while
  any playback runs, Jellyfin's `/Sessions` is asked every 2 s which items
  are playing, and each item's media source address names its title
  (`vod_tokens.title_in_url`, looked up once per item via `/Items`). A
  request from another client joins a running playback of its title while
  Jellyfin shows no more viewers of the title than there are playbacks
  (Jellyfin Web direct-plays an mkv from the browser's address while
  Jellyfin's subtitle extraction reads it from Jellyfin's). Inside a
  playback the viewer's own requests come first (`_next_turn`): a request
  from an address Jellyfin lists for a session playing the title takes the
  connection back at the holder's next chunk whenever it asks for bytes
  (`_Reader.waiting`), is never timed out for Jellyfin's work while it
  reads, and Jellyfin's work reads while the player has passed nothing for
  `PREEMPT_GRACE_SECONDS` (paused, buffer full). Shared in equal turns, the
  web player stalled 18 s at a film's start. Without a viewer address (a
  Jellyfin transcode: every request is Jellyfin's) it is equal turns. A playback
  whose viewer has gone (stopped, next episode) for 4 s is closed and its
  slot freed: what still reads it is Jellyfin's own work (that extraction
  reads the whole file, minutes after the stop). ffmpeg answers that early
  end by asking for the rest at once (`Range: bytes=<where it was>-`), so
  for `GONE_REFUSE_SECONDS` (60 s) a request for such a title that starts
  past byte 0 while nothing of it runs is refused (503) unless Jellyfin
  shows a viewer again (`_refuse_continuation`); a player (re)starting the
  title reads byte 0 first, which lifts it. A playback no session has
  reported yet, and everything while Jellyfin can't say (not set up, an
  error), is left alone. The plugin keeps a cut extraction out of
  Jellyfin's subtitle cache (plugin.md).

## Provider health (`services/provider_health.py`)

- Hourly (job `provider_health`, plus once 2 min after start unless the
  newest `checked_at` is under an hour old: then that start checks nothing,
  logs "start-up check skipped", and the hourly run starts an hour after the
  last check, `next_due`; "Check now" always runs), per provider
  that is `active` or has `live_tv_enabled`: one light check of the address
  in use, the API login (Xtream `player_api.php`: status, `auth`, account
  `status`, `exp_date`; M3U URL: the first 64 KB must be `#EXTM3U`; M3U
  file: nothing to ask, verdict `untested`, no verdict). No test stream
  (Lucas, 25 Oct 2026: "we can not be using 7 connections every hour to
  test"): refused or broken streams come from the streams people open
  (`note`, provider_status). 429/509 is no verdict.
- A new failure is checked again 2 minutes later (`RETRY_DELAY_SECONDS`)
  before it counts. "Check now" (`POST /api/health/providers/check`) skips
  that confirmation. Both look for `provider_activity.something_busy()`:
  `live_streams_active()`, `recording_protected()`, `jellyfin_playing()`
  (Jellyfin `/Sessions` has a `NowPlayingItem`, or doesn't answer: a VOD
  .strm plays straight from the provider) or `routers.recordings.busy_now()`
  (GET /api/live/recordings/busy's answer: recording now or within 20 min;
  Jellyfin set up but its timers unreadable counts as busy), before every
  request (each provider, each confirming re-check, each other address)
  and stand aside once one is true: no
  verdict, the state kept, `skipped_at` set; the result says `skipped` and
  `checked` counts only the providers that got a verdict. One lock keeps
  them from running at once (409).
- State: setting `provider_health` (JSON by provider id: `state`
  ok/failing/unknown, `since`, `checked_at`, `skipped_at`, `step`, `code`,
  `kind`, `reason`, `alerted`, `recovery_due`, `suspect`, `offer`, `kept`).
  Reasons never
  carry a URL or a login; times are the container's local time (`TZ`).
- Syncs and live streams feed the same state through `note(db, provider_id,
  ok, step, code)` (#652): no connection of its own, commits its own write,
  never raises, ignores providers not watched. `step` "api": the VOD sync
  (`sync._tell_provider_health`, xtream and m3u_url) and the Live TV
  channel sync; "stream": a live stream (`livetv._tell_provider_health`, in
  the executor on its own session). Refused = 401/403 only: the guard
  stopped asking, or nothing was read and the last answer was a refusal
  (`RequestGuard.refused`; one restricted category is nothing); a raw
  re-dial or HLS channel-URL refusal the stream ended on (`health["_refused"]`);
  a first open answered 401/403. A stream refusal is not noted while
  another channel of the same `provider_id` delivers (streaming, heard past
  `_HEALTHY_AFTER`, marked within `_RIVAL_STREAMING_FRESH`:
  `_delivering_channels`, read on the loop): a per-channel 401/403, a
  channel not in the package (#674). A first refusal sets `suspect`; a second
  one `RETRY_DELAY_SECONDS` to `SUSPECT_SECONDS` (1 h) later is the failure
  (since the first, API 401 = "login"); older starts again. Worked = a sync
  that read every category (channel sync: one group), a stream delivering
  for `_HEALTHY_AFTER` (10 s, once per streaming stretch): it clears
  `suspect` and a failure of the same step only (the hourly check covers the
  other). Already failing: a refusal changes nothing. A check verdict
  clears `suspect`.
- `_state_lock` covers every read-change-write: `note`, and the check's
  stand-aside save and final save, which re-read the state fresh
  (`_load_fresh`, `populate_existing`) so a note during the confirmation
  wait survives. Notes never take `_run_lock` ("Check now" would 409).
- Messages through `services/pushover.py` (settings `pushover_app_token`,
  `pushover_user_key`, masked; sent in the POST body): one when a provider
  starts failing, one when it works again (only if the first went out);
  a failed send is tried on the next run.
- Shown on the dashboard by `loadProviderProblems()` (`static/js/app.js`,
  admins, every 2 min and on the "health"/"status" live updates), and to
  monitors by `GET /api/provider-status` (503 while failing, no names).
- Which kind of trouble (`services/provider_status.py`, Roadmap 25 Oct
  2026): a failure the check confirmed gets `case` down / blocked /
  account / other and `case_detail` from `diagnose()`: the outcome (API
  refusals keep Cloudflare's 1xxx `cf_error` and the panel's "Database
  Error" `hint`, `_page_hints`) plus `reach()` of every address (GET "/"
  with the player User-Agent, no login, no account connection: answers /
  origin_down (CF 52x, 530) / blocked (CF 403 + 10xx, 511) / server_error /
  refused / no_answer / name_not_found) and whether a backup logged in
  within 2 h (`look()`). Every address silent on an "unreachable" failure
  also asks `INTERNET_PROBE` (TMDB's API host): nothing there either is
  case `offline` ("this home's internet connection seems to be down"),
  never "down, not your setup". Python's own User-Agent gets CF 1010 everywhere: never test
  without the player's. A confirmation that would stand aside (someone
  watching) confirms an API failure with `reach()` of the address in use
  instead (`_confirm_by_reach`; no second login). A live stream delivering
  clears a server-side failure (`_server_side`) as well as a stream one.
  `expires` (the login's `exp_date`) is kept for "ends soon" (7 days,
  admins only, alert `provider_expiry:<id>`); `back` {since, until, case}
  for "is back" (30 min).
- Live TV feeds it per channel, in memory: `channel_failed` (an open that
  ended in `_PROVIDER_FAILED`, a viewer's stream that ended while failing)
  and `channel_ok` (heard delivering, `_tell_provider_health`). 2 failures
  in 30 min while another channel of the provider delivered = "some
  channels" (`broken_channels`, `provider_view().channels_down`). Two
  channels failing within 10 min with nothing delivering starts
  `quick_look` in a thread (only in the running app, `start()`; at most
  every 5 min): `reach()` only, and a failure there (or 3+ channels failing
  with none delivering) is recorded at once (`provider_health.record`).
  `watch()` (job `provider_status_watch`, 5 min): a provider down or
  blocked whose address answers "/" again gets `check_now` when nobody
  watches.
- Words: `messages(db, admin)` (`/api/family/status` and the plugin's
  FamilyStatus through `admin_alerts.family_messages`: `kind`, `provider`,
  `since`, `until`, `title`, `text`; provider alerts add no general line),
  `describe()` for the admin banner (`headline`, `plain`) and the Jellyfin
  messages' titles, `slate_lines()` for the "Not coming through" picture.
  A change in the words publishes "status" to every tab and, through the
  plugin's Notify, to every Jellyfin client (Jellyfin Web's guide, the TV
  app); nothing while nobody listens.

## Brand (`static/brand/`, drawn by `scripts/brand.py`)

The jellyfish mark, the thin TENTACLE wordmark and their lockups, as on the
TV app's banner, are code: `scripts/brand.py` draws every SVG in
`static/brand/` (plus the TV app's one-colour vector `android/ic_tentacle.xml`)
and copies them to `docs/assets/brand/` and the plugin's `tentacle-plugin/Brand/`
(each is built from its own folder); `--png` renders the favicons, app
icons, `docs/images/banner.png`, `social-preview.png` and the plugin's
`tentacle-logo.png`. Don't edit those files by hand (`tests/test_brand.py`
compares them with the script). `static/brand/tokens.css` holds the colours;
`index.html`'s `:root` maps the dashboard's tokens onto them. Accent: one
colour can't carry white text and be read as text on the dark background, so
`--accent` (the fill, `--tentacle-accent-fill`) is for buttons and fills under
white text and `--accent-text` (the TV app's #8b80ff) for text, icons and
focus; `color: var(--accent)` is a contrast bug. Small sizes (sidebar,
favicons, navbar) use `mark-small.svg`: thicker lines that survive 16-48 px.

## UI words

The UI says "Playlist" everywhere; the code says SmartList, TagRule,
output_tag. Never "Collection" (a different Jellyfin thing) or "Tag" in the
UI; "TV guide", never "EPG" (`plainWords()` in `sources.js` rewords server
messages that still say EPG or SmartList).

Dashboard layout (`static/js/sources.js` holds the Sources page):

- Everyone: **Library** (Browse, Music, Following; content only, one Source
  dropdown), **Discover** (Browse, My Lists, Activity), **My Home Screen**
  (page id `jellyfin`: Home rows, Playlists with the "How playlists work" help).
- Admins (`ADMIN_PAGES` in `app.js`; anyone else is sent to Library):
  **Sources** (IPTV providers as cards with "Use for Movies & Series" =
  `active` and "Use for Live TV" = `live_tv_enabled`, each card holding its
  Categories / Channel groups; a Live TV tab once a provider has Live TV on;
  YouTube; Radarr & Sonarr; Music), **Health** (every admin notice and
  banner, downloads, missing, streams, duplicates, media folders, the "What
  happened" log), **Settings** (Services, Schedule & notifications, Users,
  Advanced).
- Every page and tab has an address (`#health`, `#sources/live`,
  `#settings/users`; `setRoute()`/`showRouteFromHash()` in `app.js`); old page
  names (`vod`, `live-tv`, `youtube`) are aliases.
- The sidebar's one Health count (`setHealthAlert()`): red only for real
  problems (Radarr/Sonarr errors, a failing provider, Jellyfin not answering,
  a required folder missing), grey for decisions (duplicates, new categories,
  possibly wrong movies).
- One Tentacle address: Settings → Services saves it through
  `/api/youtube/setup` (`youtube_base_url`) and stores `live_setup_host/port`
  from it; the webhook and tuner addresses are read-only copies made from it.
- Settings that are defaults now have no field (they keep their stored value):
  Deezer, Navidrome/Jellyfin artist-picture and rescan switches, MusicBrainz
  cache days, the music re-pin automation, YouTube background checks and
  interval, Health's download auto-fix switches.

Every function called from an `onclick=""` in `index.html` must be exported
in the `exposeGlobals()` block at the bottom of `pages.js` (else
`ReferenceError`); top-level functions in `app.js` are global already.

## Backup & restore (`services/backup.py`, `routers/backup.py`, dashboard `static/js/backup.js`, #870)

Settings → Backup & restore (admins). The setup an admin made by hand, as one
JSON file (`format` "tentacle-setup", `version` 1), in parts (`PARTS`), each
picked on its own when backing up and when restoring:

- per provider (`providers[]`, by provider name): `vod_categories`
  (whitelisted or source-tagged, by type + name) and `live_channels`
  (groups switched on; channels on, off inside a group that is on, or with
  the admin's own name/number/logo/guide id);
- the household's: `home_rows` (each person's home file, or "follows the
  default", plus the `default_home` setting), `playlists`
  (`AutoPlaylistToggle` + `TagRule` per person), `lists`
  (`ListSubscription` per person, without items: read again after a
  restore), `recording_rules` (rules, terms, channels as names, owner as a
  person).

Matched by name, never by id (ids ride along as hints): a category by type
and name, a group by name, a channel by group + name (else a unique name),
a person by Jellyfin id else name, a home row's playlist by its name
(`write_home_config` remaps rows by `display_name` once the playlists
exist), a rule by kind + league/team (keywords: name) + owner, its channels
by the name the pool shows. A file provider with no provider of the same
name here is skipped unless the admin maps it to one (`providers` {file
name: id | null}); the preview counts what matches in each provider here.

`plan(apply=False)` is the preview (added / changed / unchanged / removed /
not found / notes per part) and writes nothing; `apply=True` first saves a
"before restore" snapshot (refuses to write if it can't), then writes. Merge
is the default: nothing is removed or switched off, and someone's own home
keeps its rows (the file's missing rows go after them). `replace` per part
also un-whitelists categories, switches off groups and channels, deletes
playlists, lists and rules (`routers/recordings.remove_rule`: booked
recordings cancelled) the file hasn't, and writes homes as in the file.
After writing: `_pool_changed` for channels, the rules' scan, and in a
thread lists read, each touched person's full playlist resync
(`_run_full_resync`: playlists, home config, plugin notified) and
`apply_default` for people back on the default.

Never in the file: settings (no keys at all), provider addresses and
logins, and any exported string that contains a stored secret (provider
login, a `SENSITIVE_KEYS` value) or that `log_redaction.redact` would
change (a list address with `?api_key=`): left out and named in `left_out`.

Snapshots: `/data/backups/tentacle-setup-<UTC stamp>-<daily|now|before-restore>.json`,
daily at 02:30 (main.py `setup_backup`), "Back up now", and before each
restore; the last 14 kept (other files in the folder are left alone).

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

Plain columns, no JSON bag: a new fact gets a named column (Boolean,
Integer, String, DateTime; a short list of fixed words is a comma-separated
String, as `music_albums.secondary_types` and `live_networks.types`). The
generic migration pass adds new model columns on start-up; moving values out
of an old layout is a one-off in `_migrate_columns()` (example:
`_migrate_live_catalog_data`, which moved Plan B's `data` columns of
`live_networks`, `live_channel_catalog` and `live_key_aliases` into
`auto_channel_id`/`name_set`/`excluded`/`dissolved`/`types`,
`pinned`/`types`/`pool_key` and `created_at`, then dropped `data`).

## Live TV

Tentacle is the tuner Jellyfin sees (it replaced Threadfin): one M3U tuner
it adds to Jellyfin itself (below); the HDHomeRun
emulation stays for hand-made setups:
`/discover.json`, `/lineup.json`, `/device.xml` (also under `/hdhr/`), the
guide at `/api/live/xmltv.xml`, streams through `/api/live/stream/{id}`
(follows provider redirects with the provider's user agent, HLS → MPEG-TS).
User docs: `docs/features/live-tv.md`.

- **New from your provider** (admin; `services/provider_new.py`,
  `routers/provider_new.py`, dashboard `static/js/newgroups.js`, Sources →
  Live TV → New groups, plus a card on the VOD providers tab):
  every group or category a refresh or the nightly discovery creates gets
  `review_state` "new" (not on a provider's first list, its setup), counted
  on the sidebar's Sources badge. Each group name is read by
  `services/livetv_groups.py` (country or language from the prefix, a clean
  title, tags such as Raw / Gold / Backup, event groups; `CodeBook` learns a
  provider's own codes from all its group names: "SR| SERBIA" is Serbia,
  "AR|" Arabic, "LA|" Latin America). `household()` = places holding 5% of
  the pool's channels + the admin's region (`livetv_region` / time zone) +
  the browser's locale (`?country=&lang=`), and their languages: those new
  groups are "Probably for you" on top, the rest folded by country, with
  sample channels (`_samples`). Step 1 ticks groups or dismisses them, one,
  a country, or everything not for you, with Undo ("dismissed" never comes
  back as new; the Dismissed fold lists them); step 2
  shows the ticked groups' channels folded like the pool (Works / Guide,
  "No event" slots hidden) and adds the picked ones (`LiveChannel.enabled`,
  the group switched on); the groups become "reviewed". Their channels are
  read from the provider switched off (discovery, or the page in the
  background) and the stream checker tries the never-checked ones before
  re-checking the pool. VOD categories: add (whitelist) or dismiss. The
  old `new_content_notice` setting is read once (`ensure_migrated`) to mark
  what it listed. Pool keys that joined in the last 14 days are New on My
  channels (`live_pool_arrivals`).
- **One lineup** (Roadmap 1b, Plan B of 21 Oct 2026; `services/livetv_lineups.py`,
  `services/livetv_tuners.py`, `routers/my_livetv.py`, dashboard
  `static/js/mychannels.js`): the pool is `LiveChannel.enabled` (groups the
  admin switched on, single channels unticked) plus YouTube Live channels,
  regional/quality duplicates folded into networks (`livetv_catalog`, keys
  `c<id>`/`n<id>`/`y<id>`). `L.lineup()` = the pool minus event channels
  with no event now, numbered 1..N: the one M3U tuner
  `/api/live/tuner/all.m3u` (address kept as first given in
  `livetv_all_tuner_url`; a v1.11-1.12 server's "All channels" tuner is it,
  so its favourites never move). tvg-id = the XMLTV id, tvg-chno = the
  lineup number, no group-title. What is personal is the view: Jellyfin
  favourites per person. No per-person tuners: N M3U tuners make Jellyfin's
  guide refresh quadratic (jellyfin-notes.md). Kids: kids channels
  (`livetv_kids_channels`) tagged `tentacle-livetv-kids`, the rest
  `tentacle-livetv-all`, kids' BlockedTags `-all`. `live_user_lineups` /
  `live_user_channels` (the per-person lists of before) are only read to
  move a server off per-person tuners (`/api/live/users/<id>/tuner.m3u`
  serves a list of before until its tuner is removed) and kept in backups.
  Jellyfin keys an M3U channel on MD5(tuner URL)+MD5(stream URL), so
  streams are under the tuner's own address, not the request's;
  `livetv_lineups` computes every Jellyfin channel id itself; stored keys
  are read through `livetv_catalog.aliases()` (`c123` folded into `n7`).
- **Places** (`services/livetv_locations.py`): an admin sets where each
  person watches from (`tentacle_users.livetv_location`, a place's slug;
  None = the household's = the first admin's, else setting
  `livetv_location`, else the place `livetv_region` / TZ names first; the
  invite form and People). A place's words, nearest first: the city, its
  province/state and its cities by distance, nearby cities over the border
  within 700 km (Toronto → Buffalo for US networks), its time zone feed
  (East/West), each tiered 4/3/2 with its country and state (`US: ...
  VICTORIA` is Texas, `PORTLAND MAINE` not Oregon). A border station's
  call sign names its city when the name doesn't (`US: PBS 17 (WNED)` is
  Buffalo; `livetv_names._CALL_CITIES`: Buffalo, Detroit, Seattle). The choices are the
  places the provider's names mention. For every place in use other than
  the household's, a network whose best variant there (`place_variants`:
  the same `_Scorer` as the default, without people's lists) names the
  place nearer than the default does gets an extra lineup entry
  `n<id>@<place>` ("CTV · Vancouver"), right after the network's, with its
  number, the variant's guide number (guide fixed like `network_guides`),
  streamed at `/api/live/net/<id>/at/<place>` (one Jellyfin channel per
  network and place, whichever variant). A nearby city or feed only with a
  guide when the default has one; never broken or foreign over the
  default. Hiding: `L.place_tags()` gives a place's entry
  `tentacle-notlocal-<p>` for every other place in use and a network's
  default the tags of the places that have their own; everyone blocks
  their own place's tag (no place entries: none). The sync reads everyone's
  favourites before the refresh and moves those on another place's entry
  (or the default where theirs exists, or a place entry that is gone) to
  their place's, before their blocked tags change (`place_moves`); the
  place keys carried are kept in `livetv_place_keys`. `L.view_for()` is
  My channels' view; quick picks star `n<id>` as the person's own entry.
- The sync (`livetv_tuners.sync`, one at a time, debounced 15 s by
  `schedule_sync`, started on pool changes (`_pool_changed`), kids changes,
  new people, Tentacle's guide refreshes, at start-up + hourly): adds the
  lineup tuner if Jellyfin hasn't got it ("Tentacle · Live TV", settings
  copied from the old HDHomeRun tuner) and Tentacle's XMLTV listing, refreshes
  the guide when anything changed and waits for a run that *completed
  after* the request (saving a tuner cancels and requeues a running
  refresh), tags the lineup's channels (`/Items` asked without a user), sets
  BlockedTags (kids `-all`; everyone the per-person tuner tags still in
  Jellyfin; your own blocked tags kept), then moves everyone onto the
  lineup: favourites and schedules from per-person tuners of before, the
  old HDHomeRun / playlist.m3u tuner, and channels whose key changed (old
  stream address -> old Jellyfin id; favourites read before the refresh
  drops them and before policies change, set after: Jellyfin 404s a
  favourite on a channel you can't see; not until the lineup's channels
  exist); then removes those tuners (a per-person one stays while a
  schedule on it can't move: exact title match in the new guide) and waits
  for the refresh that drops their channels. Backups of Jellyfin's state
  and the stored lists before any tuner change or carry:
  `/data/livetv-backups/`.
- My Channels API: `GET /api/my/channels` = `channels` (the lineup as I see
  it: number, favourite, catalog/checker fields; a kids profile only the
  kids channels), `mine` (my favourites in lineup order), `count`, `total`,
  `favourites_known`, `setting_up`. Stars: `PUT /api/my/channels/favourite`
  `{key, favourite}`, quick picks `PUT /api/my/channels/favourites`
  `{keys, favourite}` (Jellyfin favourites, set with the admin key for the
  signed-in person only). Admins: `GET /api/live/people` = each person's
  `favourites` and `channels` (kids: the kids channels) and the setup
  status; the dashboard shows only `total` and `setup` (Sources → Live TV →
  More): people's favourites are theirs, never on the admin's pages. The
  Household channels count is the pool without `no_event` slots, = the
  lineup (`L.lineup()`), = Jellyfin's channels.
- Stream checker (`services/livetv_checker.py`, on by default, started in
  main.py's lifespan; an admin pauses it on Live TV > More, setting
  `livetv_checker_enabled` = "false", read every round, so paused = no
  opens at all; stored results stay): opens one channel at a time over all providers and
  judges its first bytes (MPEG-TS sync, or an HLS playlist's first segment;
  the whole check within 5 s, nothing by then = a failure),
  stored on `LiveChannel.check_*` ("broken" only after 2 failures in a row);
  `results(db)` gives "c<id>"/"n<nid>" -> {state, at}. Order: the pool
  (single channels, networks' chosen variants) unchecked then oldest, every
  24 h; other enabled channels every 3 days; one check per
  max(20 s, 24 h / pool size). Opens nothing while any lease, shared stream
  or pending open exists, a recording runs, Jellyfin plays anything, the
  health check or stream sweep runs, or within 2 min of the last stream
  open. Every stream open (`acquire_lease`, `_open_shared_upstream`) awaits
  `preempt()`, which cancels the check in flight and waits for its
  connection to close. 401/403/407/429/458/509/551 or no connection: no
  verdict, the provider is backed off 5 min doubling to 6 h; a provider the
  health check calls failing is skipped. Status: `/api/live/checker/status`
  (`paused_by_admin`, state "off"); switch: PUT `/api/live/checker/enabled`.
- **Catalog** (`services/livetv_catalog.py`, `livetv_names.py`, `livetv_events.py`,
  `routers/livetv_catalog.py`): channels that are one network in several
  places, qualities or time zone feeds (`livetv_names.fold_key`: prefixes,
  quality tags, call signs, a place list, East/West; conservative: sports
  channels keep their place, numbered channels stay apart, event slots
  never fold) become one `LiveNetwork`, pool key `n<id>`, streamed at
  `/api/live/net/<id>` from the chosen variant (admin's, else: not broken,
  not foreign (country from the name's or group's code, "CA EN:"/"CA FR:" or
  `_FRENCH` for language; `livetv_language`, default en; CA and US are
  neighbours; language decides only when another variant isn't foreign),
  the household's place (city, province, nearby by distance, feed; the
  place's words, see Places; a "TV: CITY, ST ... A3 HD OTHER CITY" name
  counts only its market), works, has a guide of the household's, the
  place's country, in people's lists, quality). The network's guide
  (`network_guides`, served under the variant's number in the XMLTV) is the
  variant's own unless that one is foreign to the stream (country from the
  id's ending, "TLC.rs") and a sibling, switched on or not, has a fitting
  guide with programmes; an `epg_id_override` on the variant always wins,
  and a network without a guide borrows none. Folding runs over every
  channel when a fingerprint of the list (plus guide ids, broken checks,
  `livetv_region`/`livetv_language`, the household's place and `_CHOICE_RULES`) changes; the admin's split/merge
  pin channels (`live_channel_catalog.pinned`) and dissolved networks are
  never refolded. A key that stops being a pool key leaves
  `live_key_aliases` (old -> new; `aliases()` cached) and schedules a tuner
  sync. pool() entries carry types, has_guide (programmes in the next 24 h),
  event (None / "no_event" / {title,start,stop,timed}), variants,
  variant_name, channel_id, check. Event channels without a guide get one
  programme from their name in `/api/live/xmltv.xml`.
- **Separator rows** (`routers/livetv._is_separator`, one regex, tests in
  `test_livetv_separator_rows.py`): a row whose name begins and ends with a
  run of two or more of `# = * ~ _ | - ★ ▬ ◉ ● •` ("####### SPORTSNET PPV
  #######"), or is only those. A new one starts switched off (#158); one
  switched on anyway (before #158, a group switched on, a bulk tick) is left
  out by `_shown_channels()`: the pool (so the lineup M3U, My Channels, the
  pool page, the checker's pool), `/api/live/playlist.m3u`, `lineup.json`,
  the XMLTV, Discover's channel search and the checker. A custom name that
  isn't one makes it a channel. The Live TV sync refreshes Jellyfin once
  when a separator newly leaves (`livetv_hidden_separators`).
- **Name and logo**: `LiveChannel.custom_name` / `custom_logo_url` (channel
  syncs never touch them), `LiveNetwork.name` (`name_set`) /
  `custom_logo_url`; edited on the pool page's Edit (Name and logo), logos
  offered by `livetv_catalog.logo_choices`. A network's logo: the admin's,
  else its variant's admin logo, else the logo most of its switched-on
  versions share (query strings ignored), the variant's on a tie. Jellyfin
  renames a channel at a guide refresh but sets its image only while it has
  none (GuideManager), so the sync keeps the logos it gave Jellyfin
  (`livetv_lineup_logos`) and, for a changed one, deletes the channel's
  Primary image before the refresh.
- **Automatic logos** (#137, `services/channel_logos.py`, hourly pass):
  for a provider logo that is missing, dead (fails twice), no picture, or a
  group picture (3+ different names share it and no word is in 60 % of
  them), the pass picks the same channel elsewhere (same
  `channel_name_key`), the guide feed's `<icon>`, then iptv-org's open
  logo library (by guide id, else by name in the channel's country);
  stored on `LiveChannel.auto_logo_url` / `auto_logo_source` (NULL = the
  provider's), so `guide_logo` = admin's, else automatic, else provider's,
  and a network votes with `automatic_logo`. Every logo it uses is checked
  and copied into `DATA_DIR/channel_logos` (`LiveLogo` rows); a dead one
  with a copy is served from `GET /api/live/logo/<file>`. A changed pick
  schedules the Live TV sync, whose `stale_logos` swaps Jellyfin's image.
- **Fast start** (jellyfin-notes.md "How long a live channel takes to
  start"): the lineup tuner never has Jellyfin's "Read at native frame
  rate" (not carried from an old tuner; switched off once on a tuner that
  had it, `livetv_tuner_native_rate_off`, then the admin's call); a raw
  stream's first 2 MB go out as they come (`_FAST_START_BYTES`), then in
  ~128 KB pieces; the plugin skips Jellyfin's 3 s pre-probe sleep.
- **Nothing on right now** (`services/livetv_slate.py`): a viewer (not a
  recording) of an event channel whose event starts more than 30 min from
  now (`livetv_events`), or of a "no event" slot, gets at once a 30 s
  MPEG-TS picture "Nothing on right now / <event> / starts Friday at
  8:00 PM", made by ffmpeg (cached per text) and sent without a provider
  connection or a slot; so does a viewer whose open meets the provider's
  placeholder (black.ts), or whose raw stream meets it on a re-dial before
  anything was delivered (`nothing_on`). Header `X-Tentacle-Reason: nothing
  on right now`. Recordings keep the 503 (Jellyfin retries). Without ffmpeg
  the open goes on as before.
  The same picture with its own title tells a viewer what went wrong
  instead of a spinner and "playback error": **Not coming through** ("Your
  provider isn't sending this channel / Try again in a minute or pick
  another channel") when the open fails on the provider (the 502
  `_PROVIDER_FAILED`: refused, timed out, no data), and **Connection in
  use** ("Someone else is using your provider connection", or "All N
  provider connections are in use") when the ceiling refuses a viewer, or
  the provider refuses while another stream of that provider runs here
  (most likely the account's own limit). Headers `X-Tentacle-Reason:
  provider not delivering` / `connection in use`. Recordings keep the
  refusal.
- Stream format: Xtream channels (and M3U channels whose link is an Xtream panel's) open as continuous MPEG-TS (`.ts`) by
  default, HLS only when the account's `allowed_output_formats` lacks ts or
  the channel's ts open is refused outright (then remembered per channel
  until restart). Chosen at open time (`_live_open_urls`), so no channel sync
  is needed; `livetv_stream_format` = `m3u8`/`ts` overrides. Why: the comment
  above `_LIVE_STREAM_FORMATS`.
- `discover.json` `TunerCount` is at least the stream ceiling
  (`_hdhr_tuner_count`). Jellyfin 10.11 enforces the count saved with its
  tuner: copied from TunerCount only when it discovers the device on the
  network; a tuner added by URL saves 0 (no limit; the field is hidden for
  HDHomeRun). Jellyfin lists TunerCount tuners either way. The lineup's M3U
  tuner gets the same number from the Live TV sync
  (`livetv_tuners.tuner_count`), set again whenever it differs (a hand edit
  in Jellyfin too) and within ~2 s of a change to a provider's "Streams at
  once", the default, or a provider added/removed (`stream_limit_changed`).
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
  at once, except 401/403 on a recording: waited out on the refusal cap for
  `_DENIED_GRACE` (180 s) of unbroken refusal (#646: an IP block, Cloudflare
  or flood protection refuses everything for a minute or two), then it ends.
  A re-dial counts as a reconnect only once it delivers, and as a
  recovery (backoff and budget reset) only once it delivered past 10 s.
- Many panels start every raw connection with their buffer (~20 s already
  sent, byte for byte). `_ReplaySplicer` (#368) joins a re-dialled
  connection right after the last byte sent: it holds the connection until
  its first frame start (a video/audio PES with a PTS); if that packet was
  sent, every later frame start up to the join point must sit where it was
  sent, every packet dropped must equal the packet sent at that place (a
  ring of one `hash()` per packet sent, as far back as a hold reaches), and
  no unjoined connection may have begun in front of it. Then the replay is
  dropped (`replays_joined`, `replay_bytes_skipped` in the health). A
  connection closed again before the join point, after its first frame and
  every whole packet it held were proven so, is dropped the same way and
  counted joined (#609); its outage counts as covered only once a later
  re-dial joins. Anything unproven (a gap, a seamless re-dial, a remux, a
  replay over 32 MB or 30 s, a break before the first frame start) goes out
  as it came: duplicates, never a loss.
  A joined reconnect is not damage in `_stream_ended`. A join drops only
  packets byte-identical to the ones sent at the same place (#520: checking
  only the frame starts let a provider's verbatim loop hide the unique
  packets in front of it). A verbatim loop can at worst be joined one loop
  period off, which repeats or skips only bytes already sent.
- A viewer's stream starts on a keyframe with sound (`services/ts_start.py`,
  `AvStart` in `_subscriber_body`). Some encoders mux the audio seconds
  ahead of the video (TSN 2, 2026-10-24: ~9 s): a client joining gets that
  much picture without sound, Jellyfin's remux writes its first HLS segments
  without audio, and Jellyfin Web (hls.js: no audio SourceBuffer,
  bufferAppendError) froze at ~6 s every time. `AvStart` holds the client's
  first bytes until the first video keyframe (random-access flag) at or
  after the first audio PTS less 1 s, then sends PAT, PMT and the stream
  from that keyframe; a stream in step is untouched from there. When the
  audio runs ahead, `_AudioMover` also sends each audio PES next to the
  video it plays with for the rest of the stream (sent first as a block,
  Jellyfin's 3 s probe saw only audio and remuxed without the video); a
  video jump over 5 s (a re-dial) or 8 MB waiting flushes it. Not TS, no
  video+audio programme, no keyframe flags within 4 s or no start within
  20 s / 48 MB: as it came. Panels open with ~20-30 s of buffer, so the hold
  costs ~0.05-0.7 s. A viewer joining a running TS upstream starts in its
  backlog (last 15 s / 32 MB). Recordings (the opener's `lease.kind`, a
  joiner by `_joins_as_viewer`) get neither: the bytes as they come. A
  joiner on a channel being recorded is the recording arriving only while
  the pull is not the recording's yet (its lease says `live`); once it is,
  whoever attaches is watching and gets a viewer's start (it got the
  recording's: first keyframe 1.6 s in on a 2 s GOP, 0.01 s now).
- One provider connection per channel, two layers. Jellyfin shares a tuner
  stream itself when the tuner has `AllowStreamSharing` (the default
  Tentacle sets): every viewer and recording of a channel is one consumer
  of one Jellyfin live stream ("consumer count is now N" in its log), and
  Tentacle sees one request; its TunerCount then counts channels, not
  viewers. A joiner reads Jellyfin's buffer from ~20 KB before the live
  edge, mid-GOP: its picture comes with the next keyframe. When Jellyfin
  opens a second tuner stream for the same channel (sharing off, the
  channel on two tuners), Tentacle's `_SharedUpstream` fans one pull out:
  a queue per client (a client over 32 pieces and 64 MB behind loses its
  oldest piece, never stalling the others or a recording), the pull ends
  with its last client, and it holds one slot (Streams at once counts
  pulls). Measured on a test stack: 4 viewers, or a recording and 2
  viewers, on one channel = 1 provider connection; with the provider
  closing every connection each 15 s, a recording, 3 viewers and a client
  reading at a quarter of the rate stayed on 1 connection (one rejoin per
  cut), no viewer gap over 1.3 s, the recording one file.
  Every subscription is given back however its response ends, also when
  it never ran (`_SubscriberResponse`, and `_PreviewResponse` for the web
  guide's preview): a browser that dropped the preview while the channel
  opened left a "ghost" pulling the one provider connection for 25 min
  (27 Oct 2026). The net under that: a client with bytes waiting and none
  taken for 60 s (`_IDLE_SUBSCRIBER_SECONDS`) is let go and logged, and
  the pull closes when it was the last.
- The HLS worker (`hls_to_mpegts()`) classifies statuses with the same
  `_raw_retryable()`: a 5xx on a playlist, a re-resolve or a segment is
  waited out (a 5xx segment is fetched again, not skipped). An expired token
  (401/403/404/407/410 from a token URL) re-resolves the channel URL, at most
  `_MAX_RERESOLVE` times in a row; a 407 from the channel URL itself (an
  ended session while the token is renewed) does not count for a recording
  and is waited out on the refusal cap. A 401/403 from the channel URL on a
  recording does not count either for `_DENIED_GRACE` (180 s) of unbroken
  refusal (`_denied_grace()`, #646), then counts as before; the token URL's
  401/403 that starts each re-resolve always counts. Viewers keep the three
  tries.
- Channel ids are the provider's `stream_id` (stable across changes), used
  as `GuideNumber`; Jellyfin keys the channel, its timers and favourites on
  `hdhr_<GuideNumber>`, so it must never change. M3U channels have no
  provider id: `stream_id` is the hash of the first name + URL seen, and
  `m3u_key` the hash of the current ones, which a sync matches by. A URL
  change (rotated token, new host) moves `m3u_key` only (#259). The name
  is everything after the first comma outside the quoted attributes; a row
  an older build stored under the text after the name's last comma moves
  its `m3u_key` to the whole name the same way (#525).
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
  run, kept or dropped), or the insert hits `uq_epg_program` (#467),
  except an id another provider's channel uses as its guide that this sync
  doesn't store again: programmes are shared by guide id across providers
  (#516).
- One schedule per guide id (`xmltv.one_schedule`, before the insert): a
  provider's feed can merge several guide sources under one id (its
  `MLBNetwork.us` came as four blocks that disagree), and Jellyfin drew
  them on top of each other. The feed's first word on a time wins; a later
  programme keeps the largest free stretch of its time when that is at
  least half of it, else it is left out (the log says how many).
- A tvg-id matches a feed id exactly first, else ignoring case when that
  names one feed id (never a guess between ids that differ only in case);
  the feed's spelling goes in `epg_name_match` and `epg_match` still says
  "tvg-id" (#523).
- A programme's categories are stored in `epg_programs.category` joined by
  U+001F (`xmltv.CATEGORY_SEP`, which XML can't carry) and served one
  `<category>` each, in feed order: Jellyfin flags sports/news/kids/movie
  when any of them is in its lists (#521).
- A group is unique on (provider, name) with one Xtream `category_id`, but
  Xtream category names aren't unique: `_sync_groups` gives each category
  its own group (the one already on it, else its name, first listed wins,
  else `NAME (id)`). Matching by name alone crashed the sync or moved an
  enabled group to the other category.
- A failed guide download keeps the old guide, so it is retried
  (`run_guide_top_up`, every 5 min, #709): a failed sync (nightly, manual or
  a retry: 403, timeout, a bad or partial feed) again after 5, 15, 30 min,
  then hourly until one works, one download each, longer when the provider
  sends Retry-After (6 h at most), hourly while provider health finds its
  server refusing. A guide that worked is synced again when it (the median
  enabled channel's last programme, not the latest) ends within
  12 h (at most every 6 h). The record (`livetv_guide_top_up_<pid>`) survives
  a restart. It waits for VOD syncs, the nightly job until its guide step,
  and protected recordings. A guide failing 6 h or run out is an admin alert
  (`admin_alerts._seen_guides`) and a line on the dashboard's provider
  banner. Feeds often reach only a day ahead, and a provider that refuses the
  account for a while after the nightly VOD sync's burst refused the 07:00
  UTC download night after night.
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
  dashboard adds providers only in Sources → IPTV providers, and one row serves
  VOD and Live TV: `POST /api/providers/{id}/test` sets `live_tv_enabled`
  when an Xtream account has live categories, or an M3U playlist live
  entries. `epg_url` has a field for M3U providers only; `user_agent` is API only.
- M3U providers (`services/m3u_source.py`): a read classifies every entry
  (`m3u_parser.classify`: tvg-type, panel path, file extension) so Live TV
  takes channels and VOD films/episodes; a playlist whose links are mostly
  one Xtream panel login (`panel_login`) stores it in the provider's
  `server_url`/`username`/`password` (as Xtream does): live opens `.ts` with
  `.m3u8` fallback (`_m3u_open_urls`), the panel's films get `/api/vod`
  links (`M3UClient._link`, `_resolve` rebuilds the same URL), the test reads
  player_api for expiry/max connections, `_account_key` groups rows on one
  account. The header's `url-tvg`/`x-tvg-url` is kept in
  `livetv_m3u_guide_url_{id}` (guide: `epg_url`, then that, then the panel's
  xmltv.php). The SSRF LAN exception for an M3U URL is the playlist's own
  origin (`m3u_source.guard_origin`). Nightly discovery re-reads the
  playlist. (The one-provider
  `/api/live/provider` routes are gone: Live TV is a switch per card.)
- Several Live TV providers (#704): each account carries its own streams
  at once (`Provider.live_max_streams`, else `livetv_max_concurrent_streams`,
  default 6; `provider_stream_limit`), the overall ceiling and the tuner
  count are their sum (`overall_stream_limit`). `_StreamSlots.acquire_lease`
  takes `provider_id` / `provider_limit`: a full account takes a slot only
  from its own pulls and never holds up another provider's. Recording
  protection stays global. Channel numbers: the GuideNumber is
  `guide_number(ch, offsets)`, the stream_id plus the provider's
  `channel_number_offset` (the first Live TV provider 0, every other one a
  round number above all ids, given once by `ensure_channel_offsets` and
  kept); Jellyfin's `hdhr_<n>` from timers and reservations maps back with
  `channels_for_guide_numbers`. On Tentacle's own M3U tuners (the lineup,
  and the per-person tuners of before) a timer says `m3u_` + MD5(tuner URL)
  + MD5(stream URL): `_tentacle_m3u_ids` makes those ids from the SAVED
  tuner URLs (`livetv_all_tuner_url`, `LiveUserLineup.tuner_url`) and each
  lineup entry's stream path (and old keys' paths), mapped to the GuideNumber
  of the channel the entry streams now (#760; before, every lineup recording
  was a viewer). A viewer whose slot a recording takes (or recording
  protection stops) is stopped through Jellyfin's Sessions API and sent the
  message "Stopped for a recording" (`_tell_stopped_for_recording`; web and
  the TV app both act on Stop and DisplayMessage), instead of freezing. A number two channels would share is left
  out of the lineup, XMLTV and M3U (logged), never handed out twice.
- Recording phases (#300): the timer lookup's answer (`_RecordingAnswer`)
  carries each recorded GuideNumber's phase, `pre` / `programme` (StartDate
  to EndDate, 10 s grace) / `post`, and the programme's end;
  `sync_recordings(..., phases=)` puts them on the leases (`lease.phase`,
  `programme_end`; a reservation, or no answer, is `programme`). At the
  limit, a recording not in `post` takes the slot of the `post` recording
  whose programme ended first (`_padding_victim`), only on an answer issued
  after it began to open (`fresh_since`), after any viewer or film; it waits
  up to 20 s for that answer when a running recording's programme is just
  ending. `pre` and `programme` are never taken by a recording. #184's rival
  test ranks `post` at 5 (`_rank`). `/api/live/streams` shows
  `recording_phase`, `/api/live/capacity` `padding_handovers`.
- Several addresses per provider (#704, `services/provider_addresses.py`,
  table `provider_addresses`): the address in use is `server_url`, the rest
  backups. `add_from_text` picks addresses out of a pasted message
  (scheme://host[:port] only). `test_address`: DNS, Cloudflare, login
  (status, connections), first bytes of a live channel and a film, one
  connection at a time, streams (Test all only) skipped while anyone uses
  the account (`_someone_watching` or the account's `active_cons`). The
  hourly check records its verdict on the address in use (`record_in_use`)
  and logs in to the others (`check_backups`, `streams=False`) only after
  a confirmed failure that isn't the account's, never while
  `something_busy`. Nothing switches by itself (Lucas, 25 Oct 2026; the
  old `address_auto_switch` column is dropped by `_drop_retired_columns`):
  `offer_for` picks the best other address whose latest test worked within
  `OFFER_FRESH` (2 h) and wasn't kept, into the health state's `offer`
  (provider_health `_offer`: Activity and one Pushover per offered
  address; `_save_state` publishes `health`, so open dashboards redraw).
  The Health banner shows it with Switch (`POST .../addresses/{id}/use`,
  then `address_switched` clears the failure, the offer and `kept`) and
  Keep (`POST .../addresses/{id}/keep`, `keep_address`: added to `kept`
  until the state is ok again; a new failure asks again). `switch_to`
  is the save path (`rewrite_xtream_channel_urls`, `provider_relink`) plus a
  probation: marks from the old address's listings are cleared and the sync
  marks and prunes nothing until a full sync on the new address read
  everything and served 95% of the known titles (`may_prune`,
  `end_probation`, setting `provider_address_probation_<pid>`).

### Multi-view (`services/multiview.py`, `routers/multiview.py`)

The TV app's multi-view: 2-4 channels as one 1920x1080 MPEG-TS picture
(2 side by side, centred; 3-4 a 2x2 grid), one AAC track per tile in tile
order, so the player switches sound by switching tracks.

- **Tiles are tuner clients.** Each tile calls `stream_proxy` in-process
  (no HTTP, no port): a channel someone already watches or records attaches
  to its `_SharedUpstream` and costs no connection; a new one takes a
  "live" lease like a viewer, so a recording at capacity preempts it. One
  task per tile reads the subscriber body into the tile's buffer (`buf`)
  for the session's whole life (cancelling an `__anext__` would close the
  subscription).
- **Before the first decoder starts** each tile's first ~2 MB are read and
  ffprobed: no audio = silence for its track (track N stays tile N);
  8-bit 4:2:0 H.264/HEVC/MPEG-2 = the GPU decodes it.
- **Why not one ffmpeg (v1.35.0 did that).** A one-connection account
  (one household's Xtream account) cuts all but one of the open
  channels in the same second every 10-15 s; each tile 4-5 times a minute.
  The shared upstream rejoins the provider's replay where it stopped
  (nothing lost) but its bytes pause 2-9 s, and one ffmpeg's `xstack` waits
  for every input: measured 600 s with 4 tiles, only 406 s of picture came
  out, a player rebuffered 34 times.
- **Pipeline now:** per tile a decoder ffmpeg (`decoder_args`: TS on stdin,
  raw NV12 960x540 at 29.97 and s16 stereo 48 kHz on two pipes, both from
  t=0: `fps=...:start_time=0`, `aresample=...:first_pts=0`; VAAPI decode,
  `deinterlace_vaapi`, `scale_vaapi` and download when the codec allows,
  else `yadif` + `scale`). A clock thread (`_Clock`) takes one frame and
  one tick of sound of every tile 29.97 times a second (reading up to 4
  frames ahead) and writes them non-blocking to the encoder (`ffmpeg_args`:
  raw inputs with `-nofind_stream_info`, or ffmpeg's one-by-one probing
  deadlocks against the clock; `xstack`, upload, `h264_vaapi` 8 Mb/s,
  GOP 30, one AAC track per tile; libx264 veryfast without the GPU;
  `encoder()` decides once with a 3-frame test). A tile with no frame
  ready shows its last frame and is silent for that tick (`held`,
  `stalls`); its sound is taken with its frames, so they stay together.
  Reading one frame a tick paces each decoder (it blocks on its full
  pipe), so the channel's bytes wait in `buf`: the provider's burst on
  every (re)connect fills it, and a tile that paused is that much further
  behind, which is the buffer for the next cut. Behind = how long the bytes
  the decoder gets next have waited (by time: a re-dial the proxy couldn't
  join resends seconds already sent, many bytes the decoder gets through at
  once). More than 45 s: `drop_to` what arrived in the last 15 s, from a
  keyframe, decoder restarted. The picture starts
  1.5 s ahead (the player's buffer) once every tile has a frame (8 s at
  most). It runs in a thread so a busy event loop doesn't make it late
  (`late_ticks` counts ticks > 0.25 s late).
- **Monitor** (every 0.5 s): a decoder that stopped is started again (from
  a keyframe; on the CPU if the GPU gave up on it within 15 s); an ended
  channel is opened again after 20 s, then 40, 80 ... up to 5 min while it
  won't (each try is a new connection: it cuts another channel on a
  one-connection account; measured 25 Oct, the provider refused every
  re-dial of 3 of 4 channels for 2 min and the stream proxy gave up); a **stuck feed** moves to another.
  Stuck = 85 % of the last 450 frames (15 s) had been shown already within
  them (a still or a looping clip: one broken 4K feed had 34 different frames in
  30 s, 92-100 %; a live cartoon channel 0 %). `other_feeds` ranks the
  network's other switched-on variants as the network does (`_Scorer`);
  the place keeps its name, key and item id; `mark_stuck` remembers the
  feed for 3 h, so the next multi-view starts on another (`first_feed`),
  and `/api/live/net/<nid>` plays another variant meanwhile
  (`routers/livetv_catalog._not_stuck`): full screen from the grid gets the
  feed the tile has, at no extra connection. No other feed: it stays
  (`gave_up`). `describe()` shows `buffer_seconds`, `stalls`,
  `held_seconds` per place (carried over when it moves or reopens),
  `late_ticks` and `moved`. The clock reads frames into a ring of 12
  buffers per place (`os.readv`) and writes them as memoryviews: no copies
  of the ~90 MB/s of raw tiles in Python.
- **Connections:** `free_connections` = overall limit minus active leases
  minus recordings starting within 30 min (Jellyfin timers), then each
  provider's own limit; channels already streaming need none. Refused
  with a plain message (409). The TV's button asks `GET /api/live/multiview`
  first (`can_start`, `max_tiles`).
- **Lifetime:** one session per person (a new one replaces theirs after
  it has its channels, so shared channels never close in between). `PUT`
  changes it in place: removed tiles leave first (a swap at capacity
  works), new ones open (a place that moved to another feed keeps it:
  tiles match by key), the picture restarts with the new layout. Closed by
  DELETE, by Tentacle stopping, or by the reaper when nobody reads the
  picture and no keepalive came for 45 s. Full screen is the channel tuned
  through Jellyfin as usual: its upstream is open (a tile), so the tuner
  attaches at no cost; the app sends keepalives meanwhile, and the mosaic
  keeps running so Back is instant.
- **Readers** start in the last 3 s of the picture on a keyframe with
  sound (`ts_start.AvStart`); a slow reader loses its oldest pieces.

## Recording rules (`services/recording_rules/`, `routers/recordings.py`, dashboard `static/js/recordings.js`)

Follow a team or record by words, as Jellyfin one-off timers (#770). No league
schedule or score service: games are found in the guide Tentacle serves.

- `leagues.json`: the built-in leagues and teams (full/short name, nicknames,
  code, city, French name, venues; per league the context words, the shortest
  listing that is a game, padding, playoff words; motor racing as `series`
  with terms). Edited by hand. Per league also: `needs_context` (EuroLeague,
  FIFA, IIHF: no name is enough alone; the league's words, or its sport /
  a `weak_context` word plus a second team of it), `exclude` (words of
  another sport or competition: never this league's game, checked in the
  listing and its category, not the channel name; the football leagues
  exclude basketball), `others` (replaces the global other-leagues list),
  `match_abbr: false` (country codes are display only).
- `match.py`: fold (NFKD, casefold, `&` = and, `Live:`/`New:` off), whole-word
  phrases. A team's full name is enough; any other name needs the league
  (a context word in the listing, its category or the channel's raw name) or a
  second team of that league; a city alone needs the league itself (not only
  "hockey"), and other teams' full names that contain the city ("Toronto
  Raptors") are taken out first. A listing naming a league not in the list
  (AHL, NCAA, ...) isn't the team's. Venue = home; "X at/@ Y" = Y at home.
  The same name in another league ("Canada" in FIFA and IIHF) is never a
  foreign name. A team rule's "Leave out" words apply too (`plan._hit`).
- `guide.py`: listings per lineup entry (pool key), mapped exactly like
  `hdhr_xmltv` (epg ids, folded networks' guides, `event_programmes()` for
  event channels; not stored anywhere).
- `plan.py` (pure): per rule, matched listings (plus the listing after a
  matching pregame show, plus same-time listings from an opponent's arena for
  a game an event slot names), grouped into events (team/series: starts within
  90 min; words: same title+description within 45 min), best option first
  (regular channel, `Live:`, earliest), later same programme = replay. `fit()`
  gives connections by priority then start, against each provider's
  `provider_stream_limit` and the windows of timers Tentacle didn't make;
  a first showing that can't fit takes its replay; else `waiting` with why.
  Reruns (`plan.rerun`, unless the rule records replays): a team listing is
  checked against the team's real games (`schedule.py`, no key, per team
  every 6 h, kept in `DATA_DIR/team_logos/schedule-*.json`; ESPN's team
  schedule for NHL/NBA/WNBA/NFL/MLB, every game; ESPN's `soccer/all` team
  schedule, every competition merged (league, cups, Europe, Concacaf,
  qualifiers, Nations League, friendlies), for MLS, the big five and FIFA
  national teams, team ids from the leagues' (and second divisions',
  friendlies', qualifiers') team lists; EuroLeague's own
  `api-live.euroleague.net/v2/.../games?teamCode=`, play-in/playoffs/Final
  Four once set, unconfirmed dates left out): an opponent it played earlier
  (soccer 21 days back, EuroLeague 4: domestic games aren't in it) and no
  game against them within 4 h, another opponent than the one played then,
  or (every-game leagues only, inside the schedule's dates) no game then at
  all is a rerun. A team or series listing the feed marks
  `<previously-shown>` (stored as `epg_programs.previously_shown`;
  `<new>`/`<premiere>` = False) is one too unless its title says `Live:`.
  Recorded matchup (`plan._same_matchup`, every league, schedule or not):
  the same two teams (`Option.opponents`) within 4 days (`MATCHUP_WINDOW`)
  after a game the rule recorded or booked (`book.recorded`:
  recording_bookings booked/recording/ended/covered, a preview rule's
  previews; titles re-matched, nothing stored) or an earlier game in this
  plan is a rerun; not for playoffs (`playoff_words`), `Live` titles, a game
  the schedule has then (`schedule.confirms`), or MLB (series). Walked in
  time order, so a rerun never counts as the game; an event channel's slot
  (its date is a guess) never makes a regular channel's listing a rerun. Reruns go first in the
  near misses with the reason; a `booked` one is cancelled by the scan.
  Source unreachable: the last copy, else no schedule (plans as before);
  CFL and IIHF have none (the recorded matchup covers them).
- `book.py`: the only writer of Jellyfin timers, one scan at a time (`_lock`):
  every 30 min (`recording_rules` job), 3 s after a rule change
  (`request_scan`), 10 min after a guide refresh (`jellyfin_guide`). Booking =
  `/LiveTv/Programs` at the start (±2 min) on the entry's Jellyfin channel
  id (`m3u_channel_item_id`), Defaults, padding, POST, then the id is read
  back (POST has no body); Defaults' 500 for a programme already on gets a
  hand-made DTO. A journal row (`recording_booking_events`) is committed
  before every write. Reconcile by timer id; a missing id is re-found by
  (channel, start) (moved) or the row is `lost` and booked again; after the
  window `ended`. A timer someone else set on the same channel and start
  makes the row `covered` (never touched). Rules gone, off or back in preview
  cancel their own `booked` timers that haven't started. A guide that lost a
  listing never cancels a booking. A booking holds its whole game (any of
  its listings within the team window), also after its own listing ended.
  Event-channel slots (a time, no date) are booked only within
  `EVENT_SLOT_AHEAD_HOURS` (6) of their start, and never when the same
  channel and name were booked a day earlier (a played game's name rolls
  over to the next day).
- Tables (plain columns): `recording_rules` (+ `recording_rule_terms`,
  `recording_rule_channels`; `keep_forever`/`keep_last` = what the space
  limit never deletes), `recording_bookings` (unique rule, pool key,
  start; states preview/booked/recording/waiting/covered/failed/lost/ended/
  skipped), `recording_booking_events`.
- Who: `can_record` = admin, or Jellyfin's `EnableLiveTvManagement` (read
  once a minute), never a kids profile. Everyone signed in reads rules,
  Upcoming, Recorded and marks; owners and admins edit.
- Live updates: topic `recordings` to everyone (dashboards) and the clients
  (plugin Notify), after every change and scan that changed something.
- `GET /api/live/recordings/busy`: Jellyfin recording or a padded start
  within 20 min; the maintainer's deploy gate checks the same.

## Space limit for recordings (`services/recording_space.py`, #868)

Keep recordings under N GB (oldest first) and optionally none older than N
days. Settings `recordings_limit_gb`, `recordings_limit_days`; empty = off,
the default (an upgrade deletes nothing). Recordings page: the total and
the limit (`#rec-space`), the admin's dialog with a dry-run preview.

- `recording_space` job every 15 min (first 7 min after start), off or on:
  reads every recording and rewrites `recording_sizes` (plain columns: item,
  name, recorded_at = DateCreated, size = MediaSources[0].Size, rule_id,
  kept), which the page and `?preview=` read; nothing asks Jellyfin per page
  load. A recording = a video in a library over the recordings folder
  (`recordings_libraries(any_library=True)`) whose Path is under one of
  `/System/Configuration/livetv`'s folders (a library over a parent folder
  holds other media: never counted).
- `kept` (never deleted, still counted in the total): anyone's favourite
  (the item or a parent folder; `/Items?userId=&Filters=IsFavorite` per
  Jellyfin user), part-way through (`IsResumable`), playing now
  (`/Sessions`), recording now (timer InProgress or inside its padded window
  with the same name, booking `recording`, or DateCreated < 10 min ago), a
  rule's `keep_forever` or its newest `keep_last` (item → booking by
  `recording_art.job._booking_of`).
- Anything unreadable (any of those calls, an item without Path, size or
  DateCreated) skips the pass, nothing deleted; the reason shows on the page.
- `plan()` (pure): older than days, then oldest until total <= limit.
  Each delete re-reads the item (gone: skipped; out of the recordings
  folder: stop), `DELETE /Items/{id}` (file and item), Deletion Log kind
  `recordings-limit`, reason `auto`. A failed delete ends the pass.
- Jellyfin's per-series "keep up to" is left as it is.

## Pictures for recordings (`services/recording_art/`, #815)

Two JPEGs per finished Live TV recording (`POST /Items/{id}/Images/{type}`,
base64 body), as Jellyfin has them for films: a 2:3 poster (800x1200) as
Primary for poster cards and grids, and a 16:9 picture (1280x720) as Thumb for
wide cards (the TV app's Recordings screen, Continue Watching on wide rows,
Jellyfin Web's thumb cards). Until #839 both were the 16:9 picture, which a
poster row cut down to its middle third. `primary_shape` names the layout
that drew a row (`job.POSTER`, now `poster-safe`); `upgrade_posters` draws
rows of an older layout again (empty: #827's 16:9; `poster`: #839's, whose
channel sat under the TV's progress bar), `POSTERS_PER_PASS` (8) a pass
(and as many title folders), only while both tags are still Tentacle's:
the poster, plus the 16:9 when Tentacle drew it (matchup, league, title).
Bump `POSTER` when the drawn layout changes. Safe area (`cards.py`):
nothing drawn in the bottom `SAFE_BOTTOM` (12 %) of a poster or
`WIDE_SAFE_BOTTOM` (8 %) of a 16:9 card, where clients draw progress bars;
`poster_footer_box()` / `footer_box()` place the footer above it. Setting `recording_art_enabled` (default on);
GET/PUT `/api/live/recordings/artwork`; Sources → Live TV → More.

- `job.py`: `recording_art` job every 10 min (first 5 min after start),
  `PER_PASS` (4) recordings, newest first, one at a time. Recordings = items
  of the libraries whose locations hold Jellyfin's recordings folder
  (`/System/Configuration/livetv`); a library with `SaveLocalMetadata`
  is skipped with the reason on the settings page (the recordings folder is
  never written to). A recording waits until `DateCreated + RunTimeTicks +
  10 min`, and while a timer of the same name records (or its booking is
  `recording`).
- Sources in order (`make_picture`): game → matchup card, league event →
  league card (`cards.py`); show/film → TMDB backdrop through
  `TMDBService` at `tmdb_match_threshold`, `strict=True` (TMDB down = next
  source); the EPG programme's `icon_url` when about 16:9 (`is_safe_url`, no
  redirects, 8 MB cap); a frame (`frames.py`); a title card. Posters: the
  cards have poster layouts (`matchup_poster`, `league_poster`,
  `title_poster`), TMDB's `poster_path` (w780, `to_2x3`); a picture with no
  poster (guide, frame, TMDB without one) goes on a poster with the title
  over a blurred copy of itself (`poster_from_wide`).
- Team logos (`logos.py`): `matchup_card`/`matchup_poster` draw both
  logos when `team_logos(subject)` has both in the cache (all or none: else
  the code badges). Sources per team: ESPN's keyless
  `site.api.espn.com/apis/site/v2/sports/<path>/teams` list (`ESPN` map; the
  `dark` variant; soccer also the league below for relegated clubs), ESPN's
  country flags by code for `fifa`/`iihf`; `api-live.euroleague.net` clubs
  (crest); TheSportsDB free key `123` `searchteams.php` filtered by sport
  (429 → `TooMany`, rest of the pass skipped, no miss written); logo.dev
  `/name/<team>` only with `logodev_api_key` (never logged: the address holds
  it). Matching `match()`: full name, unique code, other names, unique
  prefix. Cache `DATA_DIR/team_logos/<league>-<team>.png` (trimmed RGBA, ≤400
  px) + `index.json` (source, `plate`, misses retried after a day) + the
  sources' team lists (30 days). Drawing never fetches: the scheduled pass
  (`run_pass(fetch_team_logos=True)`; tests don't) fetches up to
  `FETCHES_PER_PASS` (8). `plate`: EuroLeague crests and mostly-dark logos
  get a light square. `recording_artwork.team_logos` = 1 when drawn with
  logos; `upgrade_logos` redraws `source=matchup` rows without it (both
  shapes, only while both tags are still Tentacle's), 8 a pass.
- What it is (`what.py`, pure): the booking (rule kind/league/team, its
  `why` quotes the listing that named the opponent) or the title,
  sub-title and description with `leagues.json` and `recording_rules.match`
  (folded names, codes, arenas = home, "A at/@ B" = B home).
  Recording ↔ booking: same folded title, `DateCreated` inside the padded
  window. Recording ↔ EPG programme: same title, starting 0–45 min after
  `DateCreated`.
- While it records (`live_pass`, job `recording_art_live` every
  `LIVE_MINUTES` (1), first 90 s after start; skips while `run_pass` holds
  `_lock`): `GET /LiveTv/Recordings?IsInProgress=true&UserId=<admin>` (no
  user: an empty list; the admin with the fewest BlockedTags, kept 1 h).
  One call when nothing records. A recording in progress is an item of
  Type `Recording` (by id, not in the library's `/Items` listing, no
  RunTimeTicks); only those under the recordings libraries' roots (Path).
  New: `give_picture(live_at=now, ctx=live_context())`, same order, the
  frame from `_live_frame` (none until `FRAME_AFTER` (4 min) of the
  programme is written); row `while_recording` 1, `path`, and a copy of
  both JPEGs in `DATA_DIR/recording_art_live/` (`_keep`). A frame/title row
  is tried again every `FRAME_EVERY` (15 min) / `FRAME_RETRY` (5 min)
  (`refresh_frame`, only while both tags are still Tentacle's).
  `live_context`: DateCreated of an item in progress is its NFO's local
  time labelled UTC (4 h off in Toronto), so how much is written comes from
  the file: `frames.written_seconds` (ffprobe of 1 MB at each end, last
  minus first video PTS, wrap at 2^33; fallback the clock since
  DateCreated), and `created` = now - written (booking/guide matching).
  Finished (`follow_finished`, live pass, when a `while_recording` 1 row's
  item is no longer in progress): 10.11.8 keeps the item and its id (Type
  Recording → Movie, pictures kept): `while_recording` None for
  `KEPT_SOURCES`, 2 for frame/title. Should a Jellyfin make a new item of
  the file instead (another id), it is found by Path in the library, the
  kept copy uploaded and the row moved to it; no item after
  `FOLLOW_CHECKS` (30) passes, or the new one has pictures of its own: the
  row goes. `reconcile` never drops a `while_recording` 1
  row. `run_pass`: `while_recording` 2 and `_finished` → `take_over`:
  made again from the finished file (`make_picture`, may `Defer`; after
  `MAX_ATTEMPTS` failures the moved picture stays). The growing file:
  `GET /Videos/{id}/stream?static=true` ignores `Range` on it (200 from
  byte 0, no length: a ProgressiveFileStream); `GET /Items/{id}/File`
  honours it, `Content-Range` gives the size so far (`file_size`,
  `read_file`). Offsets `frames.live_offsets`: `LIVE_FRAMES` (8) spread
  over the last `LIVE_SPAN` (14 min), 20 s from the end, 30 s past the
  pre-padding at least (bytes ∝ time).
- Frames: MPEG-TS only; `GET /Videos/{id}/stream?static=true` with a
  `Range` of `READ_BYTES` (6 MB) at 30/50/70 % of the programme after the
  pre-padding (byte offset ∝ time); ffmpeg `-skip_frame nokey` from stdin,
  `signalstats` (black: YAVG < 26, flat: YHIGH-YLOW < 22), the most
  edges wins; `nice 19`, 30 s timeout. Ad-looking frames rank after the
  rest (the cheap signals of the ad-breaks research): silent (a second
  ffmpeg `volumedetect` on the same piece, mean < `SILENT_DB` -50 dB; no
  sound track = unknown), or `logo_missing` (3+ frames: the edge pixels,
  320x180 `FIND_EDGES`, in the corners (30 % wide, 25 % high), that 35 % of
  the frames share; a frame with < 0.4x the best frame's share of them;
  fewer than 20 shared = no logo, says nothing). Measured on a golf
  recording: frames with the bug 0.31-0.77, full-screen ads 0.01-0.15. Not while Jellyfin records or a recording starts within 20 min
  (`busy_from_timers`): the item waits (the finished file only; the live
  frame reads the growing one).
- Safety (`recording_artwork`, plain columns: item id, kind recording/folder,
  state, source, the Primary/Thumb tags Jellyfin gave): an item with any
  picture Tentacle didn't set → `theirs`, never touched; a `set` row whose
  tags no longer match → `left`, never set again (also checked when turning
  off). Off: deletes only images whose tag is still the stored one.
  Title folders (Jellyfin makes a Folder for 2+ files): the newest child's
  picture, or the league card when it is a game; follows the newest while
  still Tentacle's. The key is only ever in the `X-Emby-Token` header.

## After a recording (`services/recording_check.py`, #869)

A re-dial that isn't joined (`_ReplaySplicer`: "sent as it came") puts the
provider's new clock in a recording: nothing lost, but ffprobe and Jellyfin
take the length from the first and last timestamps (Rob: 3 h 45 min shown
as 21.7 h; the Canadiens game, 3 h 39 min as 21 h). Fixed after the
recording, never on live bytes (Lucas, 30 Oct 2026).

- `recording_check` job every 15 min: each finished recording (items of
  `recording_art.job.recordings_libraries(any_library=True)`, newest first,
  6 a pass) once. Not while its timer or booking records or in its first
  10 min; no check while Jellyfin records anything or a recording starts
  within 20 min (`_busy`: a copy reads and writes the whole file).
- The plugin does the file work (`POST /Tentacle/Recordings/{id}/Retime`,
  plugin.md "RecordingRetime"): the files are Jellyfin's
  (`/config/data/livetv/recordings` by default), not in Tentacle's
  container. 409 = still recording: asked next pass. 404 without a state =
  an older plugin: the pass stops, nothing stored.
- Then `POST .../Premiere?date=`: PremiereDate = the programme's start
  (booking, else guide programme as in `context()`, else DateCreated),
  only when Jellyfin has none.
- `recording_checks` (plain columns: item id, state ok/retimed/not_fixed/
  failed/no_window/no_space/not_a_recording, duration/wall/after seconds,
  premiere_set, attempts). `failed`/`no_space` are tried again after 6 h,
  3 times. Activity: `livetv_recording_retimed` ("Recording fixed"),
  `livetv_recording_not_fixed` (a problem).

## YouTube per person (`services/youtube/follows.py`, `routers/my_youtube.py`, dashboard `static/js/myyoutube.js`)

- `youtube_follows` (channel_fk, jellyfin_user_id without dashes, added_by =
  the admin who added it for them, else None). Everyone adds their own on
  My YouTube (paste a link; a channel someone already has is followed, never
  fetched twice); admins add for anyone on Sources → YouTube ("For", and
  "Seen by" chips per channel; `PUT /api/youtube/channels/<id>/followers`).
  Kids can't change theirs. The last follower leaving (also a person
  forgotten by `gone_people`) = `routers/youtube.remove_channel`, the full
  removal with its files. "Remove for everyone" is the old DELETE.
- Who sees what is BlockedTags: each channel's videos carry `yt:<slug>`
  (NFO since the start), its Live TV channel and programmes
  `tentacle-youtube-<id>` (plugin at creation from the stream address,
  `livetv_tuners.tag_lineup` after each guide refresh), its Studio item
  `yt:<slug>` (`tag_studios`: search lists studios). Everyone blocks both
  tags of every channel they don't follow, always sorted last after their
  own and Live TV's (`follows.with_blocked` and `livetv_tuners.blocked_tags`
  write the same list, under `follows.policy_lock`). Set at once on a
  follow change (`apply_person`), for everyone Jellyfin has hourly and 30 s
  after start (`main._youtube_follows`), at an invite, and by the plugin for
  accounts made in Jellyfin. Tentacle reads YouTube items without a user
  (an admin who doesn't follow a channel would hide it from Tentacle).
- Playlists, the Playlists page, the refill and My Channels list only a
  person's own channels; a YouTube live channel a kid follows counts as a
  kids channel (`_kids_keys`). A channel added for a kid with no rating is
  rated G (`rate_for_kids`: NFOs and the Jellyfin items), since kids
  profiles hide unrated films.
- Migration (`database._migrate_youtube_follows`, once, setting
  `youtube_follows_migrated`): every channel there was becomes the admin's
  only (the owner admin, else the first admin; waits for one).

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
  nothing. That later run reads the album once (no waiting; an owed add that
  has just landed gets a first try that waits), leaves an album unmonitored in
  Lidarr since alone, removes the row of an album Lidarr no longer has (404),
  and otherwise tries again at the next check. The same album requested twice
  at once reuses the row the other request wrote, and is searched once
  (`finish_request` skips a row that was already there and is no longer
  pending; a row it has to write again is still owed).
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

# Tentacle plugin (`tentacle-plugin/`): internals

Reference for coding agents; every route and its auth:
[plugin-api.md](plugin-api.md). Where this and the code disagree, the code
wins.

One plugin instead of five: Tentacle's home screen (hero + playlist rows),
navbar, Discover and Activity tabs, unified TMDB search, an item detail
overlay, the media bar, MDBList and TMDB ratings, Live TV tweaks and
download notifications, all in Jellyfin's web UI. .NET 9, Jellyfin 10.11
(`Jellyfin.Controller`), Harmony for the injection.

## Layout

```
Plugin.cs, PluginServiceRegistrator.cs   entry point; services (HomeScreenManager, PlaylistManager, LibraryDeleteHandler), the MVC filter and the Harmony patch
Configuration/                          PluginConfiguration (TentacleUrl), configPage.html
Api/                                    controllers (see plugin-api.md); CallerIdentity (who may act as which user)
Patching/                               HarmonyInit, IndexHtmlPatch (injects the CSS/JS into index.html), TransformedFileInfo
Services/LiveTvIsolation.cs             Live TV per person: Harmony prefixes on GuideManager.GetChannel (a Tentacle tuner's channel is born with its tag) and GetProgram (+ postfix: programmes get the channel's tag), LiveTvNewUser (a new user blocks every person's own tuner and every YouTube channel's tags, 5 s after the event); a channel whose stream is /api/youtube/live/<id>/stream.ts is born with tentacle-youtube-<id>, and programmes copy it like the places' tags
Services/DeletedUserPlaylists.cs        On UserDeletedEventArgs: removes data/playlists folders whose playlist.xml still names the deleted user and that are no library item (Jellyfin's RemovePlaylistsAsync leaves them; a scan revives them for everyone)
Services/LiveTvProbePatch.cs            Harmony prefix on LiveStreamHelper.AddMediaInfoWithProbe: a Tentacle channel's first open skips Jellyfin's fixed 3 s wait before the probe (jellyfin-notes.md "How long a live channel takes to start")
Services/VodSubtitleExtractionPatch.cs  Harmony prefix on SubtitleEncoder.ExtractSubtitlesForFile, Tentacle VOD inputs only: the same ffmpeg writes <n>.partial.<ext>, renamed into Jellyfin's subtitle cache only after exit 0 with no input error ("Stream ends prematurely"); a stream Tentacle closed (nobody watching) leaves nothing cached (jellyfin-notes.md "Who reads a .strm besides the player")
Services/VodProbeOncePatch.cs           Harmony prefix on MediaSourceManager.GetPlaybackMediaSources: a Tentacle VOD item that already has a video stream is not re-probed at every PlaybackInfo (Jellyfin probes every .strm at every play; ~3.4 s per provider film start)
HomeScreen/                             HomeScreenManager (per-user home config from the server, awaited and shared per user)
Playlists/, Tasks/                      PlaylistManager, SmartListConfig, PlaylistRefreshTask
Services/                               LibraryDeleteHandler (Jellyfin ItemRemoved → server, 2 s debounce), MdbListCacheService,
                                        VodNoVideoCopyFilter (Tentacle VOD as HLS: re-encode the video, never copy it),
                                        PublicUsersFilter (/Users/Public is empty outside Jellyfin's local network),
                                        OneLiveStreamPerDeviceFilter (a device opening a channel closes its previous live stream first)
Inject/                                 the injected tentacle-*.js/.css and the logo (embedded resources)
Web/                                    per-user settings page
Assets/                                 rating source icons (served by AssetsController, file names sanitized)
```

The only setting is `TentacleUrl`: the plugin gets everything else (home
config, discover, activity) from the Tentacle server, calling it with the
user's id forwarded (`GetUserIdParam()`/`AppendUserId()` in
`DiscoverController`) so the server can check permissions.

## Rules learned the hard way

- **Config page**: Jellyfin loads plugin pages as fragments; only what's
  inside the `data-role="page"` div runs. Keep `<script>` inside it.
- **Assembly version**: the `.csproj` must not set
  `GenerateAssemblyInfo=false` (and no hand-written `AssemblyInfo.cs`), or
  the release workflow's `/p:Version` is ignored (versions stuck at 2.0.0.0).
- **Proxying errors**: proxy endpoints (AddToRadarr, AddToSonarr, ...) must
  return `new ContentResult { StatusCode = (int)response.StatusCode, ... }`;
  `Content(...)` always answers 200 and hides the server's errors.
- **Injection order** (IndexHtmlPatch): CSS into `<head>`; JS before
  `</body>`, search after discover (it needs `window.TentacleDiscover`).
  Assets get a cache-buster query.
- **Navbar and tabs**: `tentacle-navbar.js` replaces Jellyfin's
  `skinHeader` but keeps page tabs (Live TV guide, Music); `.sectionTabs`
  shows only under `[data-tentacle-tab-ancestor]`, which the JS sets on
  those pages, never on home. With tabs shown the JS also sets
  `body.tentacle-page-tabs`, and the CSS moves Jellyfin's tab bar and the
  page below Tentacle's header (`--tentacle-tabs-top`), so they never overlap.
- **Live TV guide** (`tentacle-livetv.js`): replaces `#/livetv` with no tab,
  tab 0 and tab 1 (Programs, Guide); Channels, Recordings, Schedule and
  Series stay native. Favorites / All is saved in Jellyfin display
  preferences (`tentacle-livetv`, client `tentacle`, `CustomPrefs.guideView`
  = `favorites`/`all`, the TV app can share it). Date strip = today to the
  day of the furthest programme end (`POST LiveTv/Programs`, StartDate
  descending, Limit 1; Favorites: their channel ids). One `LiveTv/Channels`
  request for the list; rows are virtualized; programmes per day for the
  visible rows, 100 channels per `POST LiveTv/Programs`. Programme cells use
  `overflow: clip`, not hidden (hidden stops the sticky title); programmes
  that overlap anyway are drawn side by side (a later one starts where the
  last ended). Keeps your place: leaving the guide records the view, day,
  top row (by channel id), the time at the left edge (or "now") and the
  channel played (`keepPlace`); coming back draws the list it had at once
  there (`restorePlace`), then re-reads the channels. Channel up/down in the
  player: the guide asks `Sessions?DeviceId=` (this browser) every 3 s on
  the player page, so it comes back on the channel you ended on.
  Live updates: every Jellyfin web tab listens on `ApiClient._webSocket`
  (re-attached after a reconnect). UserDataChanged (a favourite changed
  anywhere, Tentacle included: it sets Jellyfin's favourite) flips the star
  and the Favorites rows in place; LibraryChanged (Jellyfin's, or Tentacle's
  `tentacle-livetv` push) re-reads `LiveTv/Channels` (~60 ms, 1,700
  channels) and redraws only the rows that differ, keeping the scroll.
  Jellyfin sends a browser's messages to ONE socket only (the newest tab's),
  so the tab that gets one relays it on `BroadcastChannel('tentacle-livetv')`.
  No socket: the list is re-read every 10 s; back to the tab: at once.
  A first page load can open the guide before Jellyfin web knows the user
  (`getCurrentUserId()` empty); `learnUser()` fills it in later.
- **Favorites tab** (`tentacle-favorites.js`): replaces Jellyfin's home
  tab 1 with home-style rows (home's `.mh-row`/`.mh-card` markup and CSS):
  Movies, Shows, Live TV, YouTube, Recordings, then Seasons, Episodes,
  Collections, Playlists, Artists, Albums, Songs, Videos, Books, Photos;
  empty rows are not drawn. Three reads, each drawn as it arrives: every
  favourite (`Users/{id}/Items?Filters=IsFavorite&Recursive=true`, paged,
  channels excluded), the favourite channels (`LiveTv/Channels?IsFavorite=true
  &AddCurrentProgram=true`, lineup order, re-read every minute for what's on),
  and which favourites are recordings (`LiveTv/Recordings/Folders`, then the
  favourites under each). YouTube = videos tagged `yt:<slug>` (never the bare
  `youtube` tag: TMDB puts that keyword on ordinary films) and channels tagged
  `tentacle-youtube-<id>`. All with the person's own token, so kids' limits
  and BlockedTags are Jellyfin's. Live updates: listens on
  `ApiClient._webSocket` in every tab and relays on
  `BroadcastChannel('tentacle-favorites')` (also hears the guide's
  `tentacle-livetv` relay); a UserDataChanged of this person with an
  IsFavorite re-reads after 150 ms, LibraryChanged after 1.5 s; rows are
  redrawn only when their cards changed (scroll kept). No socket: every 10 s.
  Measured (Jellyfin 10.11.8, a real install): first row 0.2-0.3 s after the tab opens, 0.9 s
  on a full reload; a star changed in Tentacle shows in 1.3 s in two tabs.
- **Libraries list**: a fixed element on `<body>` (the pill's
  backdrop-filter breaks `position: fixed` inside it), so it must be given a
  top/left on every width, phones included, or it opens far below the
  screen. Hover opens it only for `pointerType === 'mouse'`: a tap also sends
  mouseenter, and the click after it would close the list again.
- **Discover tab**: the JS re-checks `TentacleDiscover/Config` on every home
  visit (no permanent client cache), so turning Discover off removes it.
- **Activity**: the JS polls `TentacleDiscover/Activity` every 3 s while the
  Discover tab is open; the server nudges Radarr/Sonarr's
  `RefreshMonitoredDownloads` (throttled) so progress moves.
- **Home config reads are awaited, never blocked on**: every home endpoint
  (Sections, each row, Hero, HeroConfig, Toolbar) needs the user's home
  config. `HomeScreenManager.GetHomeConfigResultAsync` shares one in-flight
  fetch per user and token and callers `await` it. The old sync-over-async
  fetch behind a per-user `lock` starved Jellyfin's thread pool on a burst of
  home loads and stalled all of Jellyfin (#256). Never add
  `.GetAwaiter().GetResult()` or `.Result` on a request path.
- **Names on the login screen only at home** (`PublicUsersFilter`, a
  global MVC action filter like the one below): `GET /Users/Public`
  (`User.GetPublicUsers`; Jellyfin Web asks for `/users/public`) answers
  `[]` to a caller outside Jellyfin's local network
  (`INetworkManager.IsInLocalNetwork(GetNormalizedRemoteIP())`, so its
  LAN and known-proxy settings count) or one a CF-Connecting-IP /
  True-Client-IP / X-Real-IP / X-Forwarded-For / Forwarded header names
  outside it (a tunnel connects from inside the home, and Known proxies is
  usually empty), so every client shows its name form.
  Before the startup wizard is done it leaves Jellyfin alone. The login
  page's look is `tentacle-login.css/js` (scoped to
  `#loginPage.tentacle-login`; words and decoration only, signing in stays
  Jellyfin's; same per-name colours as the dashboard's `LOGIN_COLORS`).
- **Tentacle VOD as HLS re-encodes the video** (`VodNoVideoCopyFilter`,
  a global MVC action filter, `Configure<MvcOptions>` in the registrator):
  on `POST /Items/{id}/PlaybackInfo` and the HLS video requests
  (master.m3u8, main.m3u8, segments) of a .strm whose address is
  Tentacle's signed `/api/vod/` link, it sets `allowVideoStreamCopy=false`.
  With the video copied, a resumed play's segments are cut at the
  source's keyframes (~2.7 s) while the playlist says 6 s each, so
  Jellyfin Web runs past what was written and plays the next episode
  (jellyfin/jellyfin#17966). Jellyfin then writes the flag into the
  TranscodingUrl itself; direct play (the TV) is decided as before, and
  library files, Live TV, .strm files pointing straight at a provider and
  users without video transcoding permission (their remux is all they can
  play) are untouched. Turning off the user policy "Allow video playback that
  requires conversion without re-encoding" does not stop the copy.
- **One Live TV stream per device** (`OneLiveStreamPerDeviceFilter`, a
  global async MVC action filter): before `PlaybackInfo` or
  `LiveStreams/Open` opens a Live TV channel for a device (the
  `Jellyfin-DeviceId` claim of the caller's token), it kills that device's
  transcodes of the live streams it opened before and closes those streams
  (through `ISessionManager.CloseLiveStreamIfNeededAsync`, so a later stop
  report doesn't close them twice); after Jellyfin answers it remembers the
  new `LiveStreamId`. A request naming a `liveStreamId` (Jellyfin Web's
  retry with other settings) reuses its stream and closes nothing. Why:
  Jellyfin opens the new stream first and closes the old one only when the
  client reports the old play stopped; on a one-connection IPTV account the
  overlap makes the provider cut the new channel, and a player that never
  reports (still retrying, another tab) kept the old one open. Devices are
  separate (two people, or one on two devices, keep a stream each); tabs of
  one browser share a device id, the newest channel wins.
- **The guide plays in its own tab** (`tentacle-livetv.js`): Jellyfin Web
  10.11 has no global `playbackManager`, and `sendPlayCommand` makes the
  server send Play to the browser's newest websocket, which may be another
  tab (a hidden player with sound). The guide triggers Jellyfin Web's own
  websocket message handler locally instead (`Events.trigger(ApiClient,
  'message', [{MessageType: 'Play', ...}])`) and sets that as
  `window.playbackManager` for the other Tentacle pages. It also stops a
  live video still playing off `#/video` (not picture-in-picture): a
  local Playstate Stop, then pause and drop its source.
- **Home sections**: when Tentacle's home is on, the server turns
  Jellyfin's own home sections off per user (see server.md).
- **Brand**: the csproj links the server's `tentacle/static/brand/*.svg`
  (one source, drawn by `scripts/brand.py`), served as
  `/Tentacle/Assets/brand-<name>.svg`. `tentacle-home.css` swaps Jellyfin's
  logo for `brand-lockup.svg` (header, admin drawer, splash, home title);
  the login page shows it over "Who's watching?"; the navbar has the small
  mark on wide screens. `/Tentacle/logo.png` (a PNG of the lockup) stays
  for anything that still asks for it.
- **Releasing**: only through a `plugin-vX.Y.Z` tag ([releasing.md](releasing.md)); users'
  Jellyfin reads the catalog from `tentacle-plugin/manifest.json` on main,
  and picks up a new version after a Jellyfin restart.

## Caches

All cleared by `POST /Tentacle/Refresh` (admin: the server calls it with
its API key after the nightly sync and settings changes). With `?userId=`
(a save of one user's home or toolbar) only that user's home config and
home-row entries go. A home-config fetch that started before a clear does
not cache its answer (a generation counter in HomeScreenManager), so an old
config can't be served for 5 s after a save.

| What | Where | Lifetime |
|---|---|---|
| Per-user home config | HomeScreenManager | 5 s |
| Discover items | DiscoverController | 30 min |
| Activity | DiscoverController | none (always fresh) |
| Feature config (masked keys) | ConfigController | 5 min |
| MDBList ratings | MdbListController / MdbListCacheService | 7 days |
| TMDB episode/season ratings | TmdbRatingsController | 24 h |

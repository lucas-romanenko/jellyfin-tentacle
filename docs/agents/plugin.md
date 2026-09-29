# Tentacle plugin (`tentacle-plugin/`): internals

Reference for coding agents; every route and its auth:
[plugin-api.md](plugin-api.md). Merged from Lucas's notes on 2026-09-28
and checked against the code then; the code wins.

One plugin instead of five: Tentacle's home screen (hero + playlist rows),
navbar, Discover and Activity tabs, unified TMDB search, an item detail
overlay, the media bar, MDBList and TMDB ratings, Live TV tweaks and
download notifications, all in Jellyfin's web UI. .NET 9, Jellyfin 10.11
(`Jellyfin.Controller`), Harmony for the injection.

## Layout

```
Plugin.cs, PluginServiceRegistrator.cs   entry point; services (HomeScreenManager, PlaylistManager, LibraryDeleteHandler) and the Harmony patch
Configuration/                          PluginConfiguration (TentacleUrl), configPage.html
Api/                                    controllers (see plugin-api.md); CallerIdentity (who may act as which user)
Patching/                               HarmonyInit, IndexHtmlPatch (injects the CSS/JS into index.html), TransformedFileInfo
HomeScreen/                             HomeScreenManager (per-user home config from the server, awaited and shared per user)
Playlists/, Tasks/                      PlaylistManager, SmartListConfig, PlaylistRefreshTask
Services/                               LibraryDeleteHandler (Jellyfin ItemRemoved → server, 2 s debounce), MdbListCacheService
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
  those pages, never on home.
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
- **Home sections**: when Tentacle's home is on, the server turns
  Jellyfin's own home sections off per user (see server.md).
- **Logo**: served at `/Tentacle/logo.png`; CSS in `tentacle-home.css`
  swaps Jellyfin's logo (header, admin drawer, splash, home title).
- **Releasing**: only through a `plugin-vX.Y.Z` tag (CLAUDE.md); users'
  Jellyfin reads the catalog from `tentacle-plugin/manifest.json` on main,
  and picks up a new version after a Jellyfin restart.

## Caches

All cleared by `POST /Tentacle/Refresh` (admin: the server calls it with
its API key after the nightly sync and settings changes).

| What | Where | Lifetime |
|---|---|---|
| Per-user home config | HomeScreenManager | 5 s |
| Discover items | DiscoverController | 30 min |
| Activity | DiscoverController | none (always fresh) |
| Feature config (masked keys) | ConfigController | 5 min |
| MDBList ratings | MdbListController / MdbListCacheService | 7 days |
| TMDB episode/season ratings | TmdbRatingsController | 24 h |

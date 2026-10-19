# Tentacle plugin: HTTP API

The Jellyfin plugin (`tentacle-plugin/`, C#) adds these routes to the
Jellyfin server. Clients: the web UI the plugin injects (`home.js`,
`navbar.js`, ...), the Android TV app (jellyfin-tentacle-androidtv, "TV
client" column), and the Tentacle server (`/Tentacle/Refresh` after a sync).

Source of truth: the controllers in `tentacle-plugin/Api/*.cs`. List the
current routes with

```
grep -n -E '\[(Route|Http(Get|Post|Put|Delete))' tentacle-plugin/Api/*.cs
```

and update this table when you add, remove or change one (generated
2026-09-28, 93 routes).

## Auth

- **user**: `[Authorize]`, any signed-in Jellyfin user. Clients send the
  user's own Jellyfin access token (the TV client as `?api_key=`, the web UI
  as the usual Jellyfin auth header) plus `?userId=`.
  `CallerIdentity.ResolveAsync` then checks that the token's user *is* that
  `userId` (a server API key, with no user behind it, may act as anyone),
  and that the user may read the playlist asked for. `[Authorize]` alone only
  proves someone is signed in: every user-scoped endpoint must go through
  `CallerIdentity`, or one account could read or change another's data.
- **admin**: `[Authorize(Policy = "RequiresElevation")]`: a Jellyfin
  administrator or the server API key (the Tentacle server calls
  `/Tentacle/Refresh` with it).
- **none**: no attribute, anonymous on purpose: the injected web assets,
  `/Tentacle/Boot` (must work from any page state), and the image proxy.

Gotchas:

- Jellyfin user ids come with dashes from the SDK; the Tentacle server
  stores them without. Normalize before comparing.
- The TV client's `refreshPluginCache()` calls `/Tentacle/Refresh`, which is
  admin-only, with the user's token (a non-admin gets 403, which it only
  logs). Nothing calls that function today.
- When the plugin cannot read the user's home config from the server
  (timeout, refused, 5xx), `/TentacleHome/Sections` and `/HeroConfig`
  answer **503**, never the `enabled: false` that means "home turned off"
  (#257): clients keep the rows and hero they show. `/TentacleHome/Toolbar`
  still answers 200 with the default buttons, marked `fallback: true`, so a
  client that already has the user's own toolbar keeps it.
- `POST /Tentacle/Playlists/PruneDead` with `"Async": true` answers 202
  `{runId}` at once and prunes in the background (one prune at a time); the
  server polls `GET .../PruneDead/{runId}` (running / done with the summary /
  failed; 404 after a Jellyfin restart) and holds its playlist lock until
  then (#181). Without the flag (an older server) it answers the summary
  when done, as before.

## Routes

| Method | Path | Auth | Handler | TV client |
|---|---|---|---|---|
| GET | `/Tentacle/Assets/{fileName}` | none | GetAsset |  |
| GET | `/Tentacle/Boot` | none | GetBoot |  |
| GET | `/Tentacle/Config` | user | GetConfig |  |
| POST | `/Tentacle/Deletions/{mediaType}/{tmdbId}/Confirm` | admin | ConfirmDeletion |  |
| GET | `/Tentacle/HomeConfig` | admin | GetHomeConfig |  |
| GET | `/Tentacle/MdbList/Ratings` | user | GetRatings |  |
| GET | `/Tentacle/Playlists/Ownerless` | admin | GetOwnerlessPlaylists |  |
| POST | `/Tentacle/Playlists/PruneDead` | admin | PruneDeadPlaylistEntries |  |
| GET | `/Tentacle/Playlists/PruneDead/{runId}` | admin | GetPruneRun |  |
| POST | `/Tentacle/Playlists/{playlistId}/Items/{entryId}/Move/{newIndex}` | user | MovePlaylistItem |  |
| POST | `/Tentacle/Refresh` | admin | Refresh | yes |
| GET | `/Tentacle/TestConnection` | admin | TestConnection |  |
| GET | `/Tentacle/Tmdb/EpisodeRating` | user | GetEpisodeRating |  |
| GET | `/Tentacle/Tmdb/SeasonRatings` | user | GetSeasonRatings |  |
| GET | `/Tentacle/details.css` | none | GetDetailsCss |  |
| GET | `/Tentacle/details.js` | none | GetDetailsJs |  |
| GET | `/Tentacle/discover.css` | none | GetDiscoverCss |  |
| GET | `/Tentacle/discover.js` | none | GetDiscoverJs |  |
| GET | `/Tentacle/favorites.css` | none | GetFavoritesCss |  |
| GET | `/Tentacle/favorites.js` | none | GetFavoritesJs |  |
| GET | `/Tentacle/home.css` | none | GetHomeCss |  |
| GET | `/Tentacle/home.js` | none | GetHomeJs |  |
| GET | `/Tentacle/livetv.css` | none | GetLiveTvCss |  |
| GET | `/Tentacle/livetv.js` | none | GetLiveTvJs |  |
| GET | `/Tentacle/logo.png` | none | GetLogo |  |
| GET | `/Tentacle/mdblist.css` | none | GetMdbListCss |  |
| GET | `/Tentacle/mdblist.js` | none | GetMdbListJs |  |
| GET | `/Tentacle/mediabar.css` | none | GetMediaBarCss |  |
| GET | `/Tentacle/mediabar.js` | none | GetMediaBarJs |  |
| GET | `/Tentacle/navbar.css` | none | GetNavbarCss |  |
| GET | `/Tentacle/navbar.js` | none | GetNavbarJs |  |
| GET | `/Tentacle/notifications.css` | none | GetNotificationsCss |  |
| GET | `/Tentacle/notifications.js` | none | GetNotificationsJs |  |
| GET | `/Tentacle/search.css` | none | GetSearchCss |  |
| GET | `/Tentacle/search.js` | none | GetSearchJs |  |
| GET | `/Tentacle/tmdb.js` | none | GetTmdbJs |  |
| GET | `/TentacleDiscover/Activity` | user | GetActivity | yes |
| POST | `/TentacleDiscover/AddToRadarr` | user | AddToRadarr | yes |
| POST | `/TentacleDiscover/AddToSonarr` | user | AddToSonarr | yes |
| POST | `/TentacleDiscover/ArrCheck` | user | ArrCheck | yes |
| POST | `/TentacleDiscover/ArrGrab` | user | ArrGrab | yes |
| POST | `/TentacleDiscover/ArrRemove` | user | ArrRemove | yes |
| POST | `/TentacleDiscover/ArrSearch` | user | ArrSearch | yes |
| POST | `/TentacleDiscover/ArrStopMissing` | user | ArrStopMissing | yes |
| GET | `/TentacleDiscover/Config` | user | GetDiscoverConfig |  |
| GET | `/TentacleDiscover/Detail/{mediaType}/{tmdbId}` | user | GetDetail | yes |
| GET | `/TentacleDiscover/DetailTvdb/{tvdbId}` | user | GetDetailTvdb | yes |
| POST | `/TentacleDiscover/FixMatch/movie/{tmdbId}` | user | FixMatch | yes |
| GET | `/TentacleDiscover/FixMatch/movie/{tmdbId}/Frames` | user | FixMatchFrames | yes |
| GET | `/TentacleDiscover/FixMatch/movie/{tmdbId}/Suggestions` | user | FixMatchSuggestions | yes |
| POST | `/TentacleDiscover/Follow/{tmdbId}` | user | ToggleFollow | yes |
| GET | `/TentacleDiscover/Genre` | user | GetByGenre | yes |
| GET | `/TentacleDiscover/Genres` | user | GetGenres | yes |
| GET | `/TentacleDiscover/ImageProxy/{cacheKey}` | none | ImageProxy |  |
| GET | `/TentacleDiscover/Items` | user | GetDiscoverItems | yes |
| DELETE | `/TentacleDiscover/LibraryItem/{mediaType}/{tmdbId}` | user | DeleteLibraryItem | yes |
| GET | `/TentacleDiscover/ListMissing` | user | GetListMissing | yes |
| GET | `/TentacleDiscover/Lists` | user | GetLists | yes |
| POST | `/TentacleDiscover/ManageEpisodes` | user | ManageEpisodes | yes |
| GET | `/TentacleDiscover/Notifications` | user | GetNotifications | yes |
| POST | `/TentacleDiscover/Notifications/DismissAll` | user | DismissAllNotifications |  |
| POST | `/TentacleDiscover/Notifications/Toggle` | user | ToggleNotifications |  |
| POST | `/TentacleDiscover/Notifications/{notificationId}/Dismiss` | user | DismissNotification | yes |
| GET | `/TentacleDiscover/Providers` | user | GetStreamingProviders | yes |
| GET | `/TentacleDiscover/RadarrFolders` | user | GetRadarrFolders |  |
| GET | `/TentacleDiscover/RadarrProfiles` | user | GetRadarrProfiles | yes |
| POST | `/TentacleDiscover/ReplaceCopy/{mediaType}/{tmdbId}` | user | ReplaceCopy | yes |
| GET | `/TentacleDiscover/Search` | user | SearchDiscover | yes |
| GET | `/TentacleDiscover/Season/{tmdbId}/{seasonNumber}` | user | GetSeasonEpisodes | yes |
| GET | `/TentacleDiscover/SeasonTvdb/{tvdbId}/{seasonNumber}` | user | GetSeasonEpisodesTvdb | yes |
| GET | `/TentacleDiscover/Seasons/{tmdbId}` | user | GetSeasons | yes |
| GET | `/TentacleDiscover/SeasonsTvdb/{tvdbId}` | user | GetSeasonsTvdb | yes |
| GET | `/TentacleDiscover/SonarrEpisodes/{tmdbId}` | user | GetSonarrEpisodes | yes |
| GET | `/TentacleDiscover/SonarrFolders` | user | GetSonarrFolders |  |
| GET | `/TentacleDiscover/SonarrProfiles` | user | GetSonarrProfiles | yes |
| GET | `/TentacleDiscover/Streaming` | user | GetNewOnStreaming | yes |
| GET | `/TentacleDiscover/VodEpisodes/{tmdbId}` | user | GetVodEpisodes | yes |
| POST | `/TentacleDiscover/WrongMatch/movie/{tmdbId}` | user | ReportWrongMatch | yes |
| GET | `/TentacleHome/Hero` | user | GetHeroItems | yes |
| POST | `/TentacleHome/Hero` | user | SetHero | yes |
| GET | `/TentacleHome/HeroConfig` | user | GetHeroConfig | yes |
| GET | `/TentacleHome/Playlists` | user | GetPlaylists | yes |
| POST | `/TentacleHome/Reorder` | user | ReorderSections | yes |
| GET | `/TentacleHome/Section/{playlistId}` | user | GetSectionItems | yes |
| GET | `/TentacleHome/Sections` | user | GetSections | yes |
| GET | `/TentacleHome/Toolbar` | user | GetToolbar | yes |
| GET | `/TentacleHome/UserSettings` | user | GetUserSettings |  |
| POST | `/TentacleHome/UserSettings` | user | SaveUserSettings |  |
| GET | `/TentacleHome/Version` | user | GetPlaylistVersion |  |
| GET | `/TentacleMusic/Album/{id}` | user | Album |  |
| GET | `/TentacleMusic/Artist/{id}` | user | Artist |  |
| GET | `/TentacleMusic/Config` | user | GetConfig |  |
| POST | `/TentacleMusic/Request` | user | RequestAlbum |  |
| GET | `/TentacleMusic/Search` | user | Search |  |
| GET | `/TentacleMusic/Song` | user | Song |  |

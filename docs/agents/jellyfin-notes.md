# Jellyfin, Radarr and Sonarr: hard-won notes

What their APIs actually do (Jellyfin 10.11), found building Tentacle.
The code wins where they differ.

## Jellyfin

- **Tags on downloaded files**: NFO `<tag>`s work for `.strm` only; `.mkv`
  and other real video files need the API (`services/jellyfin.py`
  `set_item_tags`):
  1. GET the item user-scoped: `/Users/{user_id}/Items/{item_id}` (the
     global `GET /Items/{id}` answers 400);
  2. build a *minimal* body: `Id, Name, OriginalTitle, Overview, Genres,
     Tags, Studios, People, ProviderIds, ProductionYear, PremiereDate,
     CommunityRating, OfficialRating, Taglines`;
  3. POST it to the global `/Items/{item_id}` (the full DTO → 500, Jellyfin
     #10724; `/Items/{id}/Tags/Add` doesn't exist).
- **ItemUpdate on a Series/Season copies its parental rating** onto every
  season and episode (every update, a tags-only one included).
  `_post_cascading_update` lists the children first and writes their own
  ratings back afterwards (`pending_rating_restores.json` keeps what a
  failure left). If the listing fails (after one retry in the run), the
  series is *not* updated that push: with no snapshot nothing could restore
  an episode rated above the series. Only after `DEFER_SERIES_MAX_ATTEMPTS`
  (3) failed listings in a row is it updated anyway, with a warning and a
  `rating_cascade_unprotected` Activity entry (#161). The count lives in
  `deferred_series_updates.json` in the data dir.
- **Refreshing metadata** with `ReplaceAllMetadata=true` wipes the tags
  (TMDB has none): always `ReplaceAllMetadata=false`.
- **NFO for downloads**: named exactly like the video
  (`Alien (1979) Bluray-1080p.nfo`); Jellyfin reads its metadata, not its tags.
- **Finding items by TMDB id**: `GET /Items?ProviderIds=Tmdb=…` is ignored
  (returns everything). Load all movies once
  (`/Users/{user_id}/Items?IncludeItemTypes=Movie&Recursive=true&Fields=ProviderIds,Tags`)
  and index by TMDB id; fall back to title + year with "(1979)" stripped and
  colons/hyphens normalized (new downloads can show "Alien (1979)" with no
  year until identified, so also try without a year).
- **The `Tags` filter** (`/Items?Tags=`) does work server-side.
- **Playlist items**: `POST /Playlists/{id}/Items?EntryIds=…` hits 414 on
  big lists: send `{"Ids": [...]}` as the body with `?UserId=`, in chunks of
  50; removals as EntryIds in chunks of 50.
- **XMLTV listing provider**: re-POSTing it with the same id doesn't remap
  new channels; delete it and POST a new one.
- **Library settings** that Tentacle needs: the TheMovieDb metadata
  downloader and image fetcher on for the movie library (downloads get
  identified and get artwork); "Automatically refresh metadata from the
  internet" on Never (Tentacle refreshes items itself).
- **API key**: Jellyfin → Dashboard → API Keys; Tentacle checks it at
  startup and logs a warning if it's invalid. Invalid key = no playlists.

## Radarr and Sonarr

- **Paths differ per container**: the paths Tentacle sees
  (`/media/movies`, `/media/shows`, `/media/vod/...`) aren't Radarr's or
  Sonarr's. When calling their APIs, use *their* root folders (Radarr's is
  `/data/movies` in the common setup); Tentacle's own media paths are fixed
  (see [server.md](server.md#stack)) and mapped by the compose volumes.
- **Hybrid series** need a Sonarr root folder on the VOD shows directory
  (the same host folder as Tentacle's `/media/vod/shows`); Tentacle finds
  it by "vod" in the path.
- **Activity shows nothing while downloading**: almost always the download
  client category in Radarr/Sonarr doesn't exist in the client (SABnzbd,
  qBittorrent): the client downloads but the *arr can't track it, so its
  queue (and Tentacle's Activity) is empty. Check the *arr's own queue first.
- **Webhooks** must use an address the *arr can reach inside the network
  (`http://<tentacle-host>:8888/api/radarr/webhook`, `.../sonarr/webhook`),
  not a public tunnel URL. Triggers: On File Import, On Movie/Series Added,
  On Delete, On File Delete. Set a `webhook_secret` in Tentacle and add
  `?secret=` to the URLs: without it Tentacle accepts forged events (and
  logs a warning on each). The Settings page tests webhooks through the
  server (`POST /api/settings/test-webhook`) to avoid mixed-content errors.
- **IMDb lists** from the *arr APIs often carry only `ImdbId`: resolve with
  TMDB `/find/{imdb_id}` (`TMDBService.find_by_imdb_id()`) before enriching.
- **Settings for Radarr/Sonarr** in Tentacle: use the Docker network address
  when both run in Docker (`http://radarr:7878`), never `localhost`.

## IPTV providers

- Some providers block unknown players: set the provider's user agent
  (a TiviMate one works).
- Category names look like `AMAZON MOVIES`, `NETFLIX SERIES`; the English
  filter is `is_likely_english()` in `routers/providers.py`; title prefixes
  are stripped by `clean_title()` (`services/cleaner.py`).

## Setup requirements

Minimum: a Jellyfin URL and API key (TMDB works with the built-in key).
Optional: Radarr, Sonarr, Lidarr, IPTV providers, Trakt, Logo.dev, MDBList,
YouTube. The setup wizard needs only Jellyfin; when it finishes, Radarr and
Sonarr are scanned in the background. Home screen not updating after a
plugin update: restart Jellyfin.

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
- **Two files of one film in one folder** are one item with two versions.
  The item's Path is its main version; the other is an owned item (`OwnerId`
  set, its id made from its path as a Video) that recursive `/Items` listings
  leave out: only `?Ids=` or the item's `MediaSources` name it. Users'
  data is on the main item whichever version they played. Delete the main
  version's file and the next scan makes a new item (new id) for the other
  file with nobody's data on it (Jellyfin's own re-attach by provider-id key
  doesn't catch it), #333.
- **Refreshing metadata** with `ReplaceAllMetadata=true` wipes the tags
  (TMDB has none): always `ReplaceAllMetadata=false`.
- **`POST /Items/{id}/Refresh`** defaults `MetadataRefreshMode` and
  `ImageRefreshMode` to `None`: new items under it are created from folder
  and file names only (no audio tags, no MusicBrainz ids) and still marked
  refreshed, so later Default scans skip them. Always pass
  `MetadataRefreshMode=Default&ImageRefreshMode=Default` (#677).
- **A library created on an empty folder** has no folder item under it
  until a full `POST /Library/Refresh`: refreshing the library item finds
  nothing. The music rescan does a full scan while the library lists
  nothing (#677).
- **NFO for downloads**: named exactly like the video
  (`Alien (1979) Bluray-1080p.nfo`); Jellyfin reads its metadata, not its tags.
- **Finding items by TMDB id**: `GET /Items?ProviderIds=Tmdb=…` is ignored
  (returns everything). Load all movies once
  (`/Users/{user_id}/Items?IncludeItemTypes=Movie&Recursive=true&Fields=ProviderIds,Tags`)
  and index by TMDB id; fall back to title + year with "(1979)" stripped and
  colons/hyphens normalized (new downloads can show "Alien (1979)" with no
  year until identified, so also try without a year).
- **The `Tags` filter** (`/Items?Tags=`) does work server-side.
- **Deleting a user** (`DELETE /Users/{id}`, 10.11): revokes their tokens,
  then `RemovePlaylistsAsync` hands a shared playlist to the first share,
  keeps an open-access one, and deletes the rest from the library with
  `DeleteFileLocation = false`: the folder and its `playlist.xml` stay in
  `data/playlists/`, and the next scan of the playlists folder resolves them
  again. Seen on 10.11.8 after a refresh of the playlists folder: it rewrote
  their `playlist.xml` with no owner and no entries, open access, so every
  user saw them (empty "Drama", "Ann's Downloads1"). `DELETE /Items/{id}`
  removes the folder too. The plugin removes the leftover folders on
  `UserDeletedEventArgs` (plugin.md).
- **Playlist items**: `POST /Playlists/{id}/Items?EntryIds=…` hits 414 on
  big lists: send `{"Ids": [...]}` as the body with `?UserId=`, in chunks of
  50; removals as EntryIds in chunks of 50.
- **XMLTV listing provider**: re-POSTing it with the same id doesn't remap
  new channels; delete it and POST a new one.
- **Library settings** that Tentacle needs: the TheMovieDb metadata
  downloader and image fetcher on for the movie library (downloads get
  identified and get artwork); "Automatically refresh metadata from the
  internet" on Never (Tentacle refreshes items itself).
- **HLS with the video copied** (10.11, jellyfin/jellyfin#17966): a
  transcode for the audio only (`-codec:v:0 copy`, after a resume also
  `-ss … -noaccurate_seek`) cuts segments at the source's keyframes while
  the playlist promises `SegmentLength` each; the player runs ahead of
  the real media and ends the item early. `allowVideoStreamCopy=false`
  (query on PlaybackInfo and on the HLS URLs) re-encodes and the segments
  match the playlist; the plugin sets it for Tentacle VOD. The user policy
  EnablePlaybackRemuxing does not stop the copy.
- **Who reads a .strm besides the player** (10.11.8, read in the source and
  measured Oct 2026): an embedded text subtitle delivered External, Hls or
  burnt in (Encode) is first extracted by `SubtitleEncoder` with one ffmpeg
  over the WHOLE file (`-map 0:N … -c:s copy` per text track). It is not
  cancelled with the request or the playback (the token is `None`; only a
  30 min timeout), so it reads on after the player stopped; it is cached
  per media source id in `data/subtitles/xx/<id>/<index>.srt` for good,
  and judged only by the files being non-empty: an ffmpeg that saw its
  input end early exits 0 with part of the cues and is logged "extraction
  completed". After a body that ends short, ffmpeg (7.1) asks once for the
  rest (`Range: bytes=<where it was>-`); an error answer ends it. The web
  player waits for that request (no subtitles until
  the whole file was read). Embed delivery (direct play into a container
  that carries them, the TV app with SRT) extracts nothing. Every
  PlaybackInfo of a `.strm` probes it (a full metadata refresh with
  `EnableRemoteContentProbe`, a short read before the stream starts):
  `MediaSourceManager.GetPlaybackMediaSources` checks the `.strm` path
  alone, not whether the streams are known (they are saved by the first
  probe). For a provider film that was ~3.4 s of range requests before
  PlaybackInfo answered; the plugin (`VodProbeOncePatch`) passes
  `allowMediaProbe=false` for a Tentacle VOD item that has a video stream
  probed at the address its `.strm` holds now (remembered in memory since
  Jellyfin started; a `.strm` Tentacle re-pointed is probed again, #775:
  a scan updates `ShortcutPath` but never re-probes a shortcut).
  Trickplay, chapter images and the keyframe task skip shortcuts; a
  library scan does not probe them. Jellyfin Web in Chrome direct-plays a
  remote mkv itself: the browser fetches the .strm address from its own
  address while the extraction comes from Jellyfin's.
- **API key**: Jellyfin → Dashboard → API Keys; Tentacle checks it at
  startup and logs a warning if it's invalid. Invalid key = no playlists.
- **Plugin updates** (10.11's source): every installed plugin's
  `meta.json` has `autoUpdate` (default true; no API sets it, `/Plugins`
  doesn't show it), and the scheduled task "Update Plugins" (at start-up and
  every 24 h) installs the newest compatible version from the catalogues in
  `/Repositories` for each plugin with it on and not disabled. The new
  version is listed with Status `Restart` and runs only after Jellyfin
  restarts. A plugin installed by hand (no catalogue listed) is never updated.

### Live TV per person (proven on 10.11.8, Oct 2026)

What was proven for per-person tuners (v1.11-1.12); since 21 Oct 2026 the
same tags hide non-kids channels from kids on one lineup.

- M3U channel id: `m3u_` + MD5(tuner URL) + MD5(stream URL) (`M3UTunerHost`,
  `M3uParser.cs:104`); `string.GetMD5()` is MD5 of the UTF-16LE text read
  as a .NET Guid. Item id = MD5("MediaBrowser.Controller.LiveTv.LiveTvChannel"
  + lower("Emby" + external id + "4")). The number is only `tvg-chno`:
  renumbering keeps favourites and timers. HDHomeRun keys on `hdhr_<n>`.
- M3U stream lines must be http(s)/rtsp: a file path is skipped.
- XMLTV programme ids get `_<channel id>` appended (`ListingsManager`), so
  one guide serves any number of tuners mapping the same feed channel.
- BlockedTags: channels are filtered in the database query (own tags).
  Programmes are not (they don't carry the channel's tag), but every list
  goes through `DtoService.GetBaseItemDtos` → `IsVisible` (inherited tags
  walk up to the channel), so nobody sees them; pages come back short and
  TotalRecordCount counts everyone's. The plugin copies the tag onto the
  programme (Harmony on `GuideManager.GetProgram`) to fix that. Programme
  ids of another person's channel: 0 items. `/Items/{id}` of another's
  channel: 404. `/Items?ids=` *without* userId is unfiltered.
- `GuideManager.GetChannel` keeps a channel's tags when the tuner gives
  none (M3U never does) and only adds "Kids". Live TV items raise no
  `ItemAdded` (`SourceType.LiveTV` is skipped).
- Timers and series timers have no owner: `/LiveTv/Timers` lists all to
  everyone with Live TV. Recording needs `EnableLiveTvManagement`.
  Recordings are one library; a tag on a recording item hides it like a
  channel (survives scans; recording NFOs are locked).
- A recording in progress is an item of Type `Recording` from its first
  seconds: `/Items?Ids=` finds it, the library's recursive `/Items`
  listing doesn't, `/LiveTv/Recordings?IsInProgress=true` lists it only
  with a `UserId` (none: empty). No RunTimeTicks; MediaSources Size as
  first probed; its DateCreated is the recording NFO's `dateadded`, local
  time read as UTC (Toronto: 4 h early). Pictures uploaded to it show on
  "Recording now" cards. When it finishes, the same item (same id) becomes
  Type Movie with a length and keeps its pictures (production and a
  throwaway Jellyfin, 30 Oct 2026). `/Videos/{id}/stream?static=true` ignores `Range` on the
  growing file (200 from byte 0, no Content-Length, follows the file);
  `/Items/{id}/File` (admin key) answers 206 with `Content-Range: bytes
  a-b/<size so far>` (10.11.8, 30 Oct 2026).
- Saving a tuner or listing provider cancels a running RefreshGuide and
  queues a new one. XMLTV programmes have no etag: every refresh re-saves
  every programme.
- Changing a user's policy inside the `UserCreatedEventArgs` consumer makes
  `POST /Users/New` fail (DbUpdateConcurrencyException, 500): do it later.
- Favourite (`POST /UserFavoriteItems/{id}?userId=`) on a channel the user
  can't see: 404.
- **How long a live channel takes to start (measured 21 Oct 2026, 10.11.8,
  Jellyfin Web).** PlaybackInfo with AutoOpenLiveStream opens the tuner
  stream (SharedHttpStream: Tentacle + the provider's redirect, 0.5-0.9 s),
  then `LiveStreamHelper.AddMediaInfoWithProbe`: cached per channel in
  `/cache/mediainfo` (key = the open token, so per channel item and stream
  URL), else a fixed **3 s sleep** and ffprobe (~0.1 s on a provider that
  sends its buffer at once). Then ffmpeg remuxes to HLS (`-hls_time 3`); the
  tuner's **Read at native frame rate** adds `-re`, so the first segment
  needs 3 s of real time and more (11 s on a 60 fps channel with odd audio
  timestamps). Before: 4.9-6.8 s, 15.9 s on Sportsnet One. Tentacle switches
  `-re` off on its tuner (once) and the plugin drops the 3 s sleep for
  Tentacle's channels (`LiveTvProbePatch`): about 1.5 s. The M3U channel
  list is cached per tuner (`BaseTunerHost`), read once after a restart.
- **Many M3U tuners make the guide refresh quadratic (measured Oct 2026,
  10.11.8).** `BaseTunerHost.GetChannels` builds every M3U tuner's channel
  list and de-duplicates them against each other with a list scan, and
  `GuideManager` calls it once per channel while refreshing. One tuner of
  5,669 channels: 2 m 10 s (20 ms/channel). 7 tuners × 1,734 channels (one
  per person): 358 ms/channel, ≈73 min per refresh (stopped at 20 min);
  a Harmony prefix with a hash-set merge brought that to 10.5 min (67 ms/
  channel, DB 212 MB), still 5× one lineup, and it would be a patch on
  Jellyfin internals. So Tentacle has **one M3U tuner** (the household's
  lineup) and per-person tuners are gone (Decisions, 21 Oct 2026). The
  prototype patch is not shipped.
- One tuner, two tags: lineup channels carry `tentacle-livetv-all`, the
  household's kids channels `tentacle-livetv-kids`; kids profiles block
  `-all`. The plugin tags a new channel with its tuner's tag (`-all`) in
  `GuideManager.GetChannel`, and keeps `-kids` on a channel that has it
  (`WithTag(..., keepKids: true)`), so a kid never sees a channel before an
  admin makes it a kids channel; programmes always take their channel's
  tag. Only one `tentacle-livetv-*` tag per channel survives the plugin, so
  people's places have their own prefix: `tentacle-notlocal-<place>` on a
  channel people at <place> don't see (they block it); the plugin keeps
  them on channels (it strips only `tentacle-livetv-*`) and copies them
  onto the programmes with the tuner tag (`ProgramTags`).
- Everything a person has on a channel (favourite, timers) hangs on the
  channel item id, so on the tuner URL and the stream URL: a channel whose
  stream address changes (a pool key `c123` folded into network `n7`:
  `/api/live/stream/123` → `/api/live/net/7`) is a new channel to Jellyfin.
  The guide refresh (`GuideManager.CleanDatabase`) deletes the channels a
  tuner no longer lists, and with them what `/LiveTv/Channels?isFavorite=true`
  can report, so the sync reads favourites *before* the refresh and sets
  them on the new channel after it; schedules are made again from the new
  channel's guide (exact title) and the old ones cancelled. The same moves
  people off per-person tuners of before (v1.11-1.12) and the old
  HDHomeRun tuner: favourites read while they still see the old channels,
  then their BlockedTags change, then the favourites are set on the
  lineup's channels (the 404 above).
- A guide refresh renames an existing channel to the M3U's name, but sets
  its Primary image from `tvg-logo` only while it has none
  (`GuideManager.GetChannelAsync`: `if (!item.HasImage(ImageType.Primary))`,
  10.11 source). A new logo reaches Jellyfin only after
  `DELETE /Items/{id}/Images/Primary` (admin key) and the next refresh.

## Radarr and Sonarr

- **Paths differ per container**: the paths Tentacle sees
  (`/media/movies`, `/media/shows`, `/media/vod/...`) aren't Radarr's or
  Sonarr's. When calling their APIs, use *their* root folders (Radarr's is
  `/data/movies` in the common setup); Tentacle's own media paths are fixed
  (see [server.md](server.md#stack)) and mapped by the compose volumes.
  The other way round, a Radarr/Sonarr path becomes Tentacle's with
  `media_files.local_arr_folder` (`/data/movies` → `/media/movies`, else
  the folder by name under the mount; Windows paths too). Never
  `str.replace` or `Path()` on their paths: other roots and `D:\...` break.
- **One series = one folder** (checked on Jellyfin 10.11.8 and Sonarr 4
  with the production images, #889): a show's folders under two library
  paths are two series even with the same TMDB/TVDB ids
  (EnableAutomaticSeriesGrouping is off); "Season 5" and "Season 05" in one
  show folder are one season; a `.strm` and a download of one episode in one
  show are two episode entries; a `*.strm.parked` file is ignored by
  Jellyfin and by Sonarr's disk scan (Sonarr 4 lists `.strm` as an episode
  file, quality SDTV). Users' data does not follow a file that moves to
  another folder: the new item is made while the old one still exists, and
  the old one's data is detached (10.11 keeps it under a placeholder item),
  not re-attached by key. Jellyfin skips (removes nothing from) a library
  folder that became empty. Sonarr's `PUT /series/{id}?moveFiles=true`
  answers 202 and moves in the background: into a missing folder it renames
  the series folder; into an existing one it moves file by file,
  OVERWRITING same-named files, then deletes the emptied source folder; on
  an error it reverts the series path, leaving moved files where they went.
- **Hybrid series** need a Sonarr root folder on the VOD shows directory
  (the same host folder as Tentacle's `/media/vod/shows`); Tentacle finds
  it by "vod" in the path.
- **Activity shows nothing while downloading**: almost always the download
  client category in Radarr/Sonarr doesn't exist in the client (SABnzbd,
  qBittorrent): the client downloads but the *arr can't track it, so its
  queue (and Tentacle's Activity) is empty. Check the *arr's own queue first.
- **Deleting a file** (`DELETE moviefile/{id}`, `episodefile/{id}`) answers
  only when it is done. With a recycle bin on another drive they copy the
  file there first, which can take minutes, and the title keeps `hasFile`
  until then: a timed-out delete usually still happens, so read `hasFile`
  before calling it failed (`services/bad_copy.py`). A manual delete also
  unmonitors the title when "Unmonitor Deleted Movies/Episodes" is on, so
  a monitor call has to come after the delete, not before. Bad copy always
  monitors the episode again, but monitors a movie only when it was
  unmonitored before the press: with "Unmonitor Deleted Movies" on, a
  monitored movie ends unmonitored.
- **Adding a series** (`POST /series`): Sonarr answers before it has set
  the series up. A queued refresh creates the episodes, then the scan
  applies `addOptions.monitor` (a write of the whole series row), queues
  the post-add search and clears `addOptions`; on a busy Sonarr (three
  command threads) that is minutes later. Until then `GET /series/{id}`
  still carries `addOptions`. A series PUT before it is overwritten, an
  episode monitor PUT is undone by the add's `monitor`, and an unmonitored
  series makes the post-add search reject every release. So presets leave
  the series monitored, and Pick Episodes waits for `addOptions` to clear
  (`SonarrService._await_series_setup`, `SETUP_WAIT_SECONDS`).
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

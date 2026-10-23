# VOD Library

Tentacle syncs on-demand movies and TV series from your IPTV streaming provider into Jellyfin, complete with full metadata from TMDB — posters, plots, ratings, genres, and cast information.

## How It Works

1. You add a streaming provider with your credentials
2. Tentacle fetches available categories (Netflix Movies, Amazon Series, HBO MAX Movies, etc.)
3. You choose which categories to sync
4. Tentacle matches each title against TMDB for metadata
5. `.strm` files (streaming URLs) and `.nfo` files (metadata) are written to disk
6. Jellyfin picks up the files and displays them with full artwork and info

The result: your provider's content appears in Jellyfin as if it were a native library, organized and searchable with proper metadata.

## Adding a Provider

Go to **Sources → IPTV providers** in Tentacle and click **+ Add Provider**. Enter what your provider gave you: an Xtream Codes login, a playlist link (M3U URL) or a playlist file. All three work the same: the same provider also serves [Live TV](live-tv.md) when it has live channels, and several people can watch at once. Paste a link into any box: Tentacle splits an Xtream link (`get.php?username=…&password=…`) into server, username and password, and moves a playlist link to the playlist box.

### Xtream API (Recommended)

Most IPTV providers support the Xtream Codes API:

- **Name** — A label for this provider (e.g., "My IPTV")
- **Server URL** — Your provider's server address
- **Username** — Your account username
- **Password** — Your account password

### Playlist link (M3U URL)

If your provider gives you a playlist link instead:

- **Name** — A label for this provider
- **Playlist link** — The full URL to the M3U playlist
- **Guide link** (optional) — Leave it empty: Tentacle uses the guide the playlist names (`url-tvg`), or the provider's own

The playlist is read whole: its live channels go to Live TV, its films and episodes to the library. When its links are an Xtream panel's (most provider playlists are `get.php`), Tentacle also reads the account's expiry and connection limit, opens live channels as one continuous stream like an Xtream provider, and plays films through Tentacle so a dropped connection resumes. Episodes need a season and episode number in their names (`S01E02`).

### Playlist file (M3U)

For a playlist file on the server:

- **Name** — A label for this provider
- **Playlist file** — Its path **inside Tentacle's container**: put the file in Tentacle's data folder and enter `/data/<file name>`, or mount its folder into the container. Tentacle says so when it saves the provider if it can't read the file. It is read again at every sync.

!!! tip "Test first"
    Click the **Test** button on the provider's card in Sources → IPTV providers to verify the connection before syncing. This only tests connectivity — it doesn't download any content.

## Selecting Categories

After adding a provider, switch on **Use for Movies & Series** on its card (Sources → IPTV providers) and click **Choose** next to **Categories**. Tentacle fetches the provider's categories the first time.

Categories are grouped by type — movies and series. Each category shows its content count. Toggle on the categories you want to sync.

!!! example "Common categories"
    - `NETFLIX MOVIES`, `NETFLIX SERIES`
    - `AMAZON MOVIES`, `AMAZON SERIES`
    - `HBO MAX MOVIES`, `HBO MAX SERIES`
    - `DISNEY+ MOVIES`, `DISNEY+ SERIES`

### Category Whitelisting

Only whitelisted (enabled) categories are synced. This lets you control exactly what content appears in your Jellyfin library.

### English Filtering

Tentacle automatically filters for English-language content using smart detection that checks country prefixes and language indicators in category/title names. Content with non-English country prefixes (like `FR -`, `DE -`, `BR -`) is excluded.

## TMDB Matching

Every title from your provider is matched against TMDB (The Movie Database) to get official metadata:

- **Official title** — The proper TMDB title replaces provider naming
- **Poster and backdrop images** — High-quality artwork
- **Plot summary, rating, genres** — Full metadata
- **Year, runtime, cast** — Complete information

!!! info "TMDB as gatekeeper"
    By default, content that can't be matched to TMDB is skipped. This ensures only real, identifiable content makes it into your library.

    If you want to include unmatched content (with the provider's title as-is), you can disable "Require TMDB Match" in the provider settings.

### Title Cleaning

Provider titles often include prefixes like `NF -`, `AMZ -`, `HBO -`. Tentacle automatically strips these before TMDB matching to improve match accuracy.

## What Gets Created

For each matched title, Tentacle creates:

### Movies
```
/media/vod/movies/
  Inception (2010)/
    Inception (2010).strm      ← streaming URL
    Inception (2010).nfo       ← full metadata (title, plot, cast, tags, poster URLs)
```

### TV Series
```
/media/vod/shows/
  Breaking Bad (2008)/
    Season 01/
      Breaking Bad S01E01.strm
      Breaking Bad S01E01.nfo
    Season 02/
      Breaking Bad S02E01.strm
      ...
```

The `.strm` file contains the streaming URL — Jellyfin reads it and plays the content directly from your provider. The `.nfo` file contains all the metadata and tags that Jellyfin reads to display proper info.

## Tagging

Every synced title is automatically tagged for playlist generation:

- **Source tag** — e.g., `"Netflix Movies"`, `"Amazon TV"` (based on category)
- **Recently Added** — Rolling 30-day window, refreshed on every sync

These tags are written into the NFO files and are what power the [auto playlists](playlists.md).

## Syncing

### Manual Sync

Click **Sync now** in Settings → Schedule & notifications to run the sync immediately. A progress indicator shows the current status.

### Scheduled Sync

Tentacle runs a full sync automatically every night at 3 AM. This:

1. Pulls fresh content from all active providers
2. Matches new titles against TMDB
3. Writes/updates `.strm` and `.nfo` files
4. Refreshes recently added tags
5. Triggers a Jellyfin library scan
6. Updates all playlists

### What Happens During Sync

| Step | What it does |
|------|-------------|
| Fetch catalog | Downloads the provider's content list |
| TMDB match | Matches each title to TMDB metadata |
| Write files | Creates `.strm` + `.nfo` for new content |
| Tag content | Applies source tags and recently added tags |
| Jellyfin scan | Triggers Jellyfin to pick up new files |
| Playlist refresh | Updates all smart playlists with new content |

## Deleting a Provider

When you delete a provider, Tentacle:

1. Removes the `.strm` and `.nfo` files it wrote from disk
2. Deletes all database records (movies, series, categories)
3. Automatically rebuilds playlists (orphaned source playlists are removed)
4. Updates home screen configs (validates hero spotlight still exists)

Only files Tentacle created are removed. If you run a merged setup — the VOD
folder and your Radarr/Sonarr downloads folder pointing at the same place —
downloaded episodes, subtitles and artwork in those folders are left untouched,
and empty folders are tidied up afterwards.

## Safety Guards on Removal

Content disappearing from a provider's catalog, or files disappearing from
disk, is treated as suspicious rather than authoritative — a provider glitch or
a storage hiccup must never be able to wipe a library:

- **A category that returns nothing** but held titles last time is treated as a
  failed fetch. Nothing is pruned from that sync, and the count is reset so a
  category that genuinely emptied is accepted on the next run.
- **Two runs must agree.** A title missing for the first time is only marked;
  it is removed on the next run that still doesn't see it. Anything that
  reappears has the mark cleared.
- **Blast radius is capped.** No single run removes more than 5% of a
  provider's titles (minimum 50). Anything larger is logged loudly and skipped
  — check the provider before assuming the catalog really shrank.
- **The nightly orphan sweep probes the mount first.** If `/media/vod/movies`
  or `/media/vod/shows` is missing or empty, or holds none of the titles
  Tentacle recorded there, the sweep is skipped entirely rather than
  concluding that every title was deleted. This matters on mergerfs, unionfs,
  NFS, SMB and rclone, where a branch dropping out makes every file report as
  missing while the mount itself stays up, and in a merged setup, where a
  Radarr or Sonarr import can land on the bare mount point of a share that
  isn't mounted.
- **The sync refuses an unmounted folder.** In the same cases the provider
  sync fails with "the storage looks unmounted" and writes nothing. Mount the
  share and sync again. If the folder really is new (a new disk), put an empty
  file named `.tentacle-library` in it and sync again.

Blocked removals are recorded in the deletion log (Health → What happened → Removals) as
"refused": nothing is deleted.

## Opting a Title Out of `.strm` Management

Sometimes a provider's stream for one title is broken and you switch that title
to downloaded copies. Open the title from the Library page and turn off
**Manage .strm files**: it stays in the catalog, playlists and Discover, but
the sync stops writing or repairing its `.strm` files. You can also delete the
existing ones at the same time — downloaded episodes in the same folder are
left alone.

## Stale File Detection

If you're migrating from another tool (like xtream-sync) and Tentacle detects existing `.strm` files but has an empty database, it will show a banner on the dashboard offering to:

- **Delete and start fresh** — Removes old files so Tentacle can create properly tagged ones
- **Dismiss** — Keep old files (not recommended — they won't have proper metadata or tags)

## Changing Your Provider's Address or Login

When your provider gives you a new server URL (or a new username or
password) for the same account, edit the provider in **Sources → IPTV providers**
and save. The films and episodes already in your library are updated to the
new address in the background, Jellyfin rescans, and an entry in Activity
says how many moved. Live TV channels follow at once. Every sync also
repairs any title still on the old address before it asks the provider for
anything, so this works even when the provider refuses the sync, and a
restart before the update ran doesn't lose it.

For an M3U provider, save the new playlist address: the next full sync points
every film and episode it lists at the new playlist's addresses.

### Provider refusing (HTTP 403)

A provider that blocks your server (an IP block, often after many requests,
or a blocked account) answers every request with 403. After three refusals
in a row Tentacle stops asking for the rest of that sync, so the block isn't
made worse, and the run says "the provider refuses this server". Nothing is
removed from your library on such a night. Tentacle can't lift the block:
check whether the same link works in another app on the same network (for
example TiviMate), and if it doesn't, ask the provider to unblock your IP or
account. A new link from them doesn't help while the IP or account is
blocked. Once **Check now** on the dashboard banner passes, sync again.

Titles the new address no longer lists are removed by the sync, like any title
a provider drops: marked by one sync, removed by the next one that still
doesn't list them (see [Safety Guards on Removal](#safety-guards-on-removal)).

If the new link is a different service (another panel, other stream ids),
that is a new provider: add it and use Migrate (below).

## Provider Migration

When you switch to another provider, **Sources → IPTV providers → ⇄ Move to another provider**
moves your films to it without re-syncing everything:

1. Add the new provider and choose its movie categories first. Migrate refuses
   while it has none.
2. In the dialog pick the old and the new provider. **Preview** shows how many
   films move and how many stay; **Migrate** does it.

A film moves when the new provider lists it in one of its chosen movie
categories under the same title and year: its `.strm` is rewritten to play
from the new provider, and the new provider's sync looks after it from then on.
Everything else stays with the old provider, with its files untouched:

- films the new provider doesn't list (or lists only in a category you didn't
  choose, or under another name),
- films on a stream you marked **Wrong movie** or re-matched on the new
  provider,
- a film that shares its title and year with another film in your library
  (on any provider, or downloaded),
- every series.

The old provider is switched off, so its sync no longer runs; what stayed with
it keeps playing from it for as long as that account works.

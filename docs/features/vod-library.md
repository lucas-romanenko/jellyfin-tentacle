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

Go to the **VOD** page in Tentacle and click **Add Provider**.

### Xtream API (Recommended)

Most IPTV providers support the Xtream Codes API:

- **Name** — A label for this provider (e.g., "My IPTV")
- **Server URL** — Your provider's server address
- **Username** — Your account username
- **Password** — Your account password

### M3U URL

If your provider gives you an M3U playlist URL instead:

- **Name** — A label for this provider
- **M3U URL** — The full URL to the M3U playlist

### M3U File

For locally hosted M3U files:

- **Name** — A label for this provider
- **M3U File** — Path to the M3U file

!!! tip "Test first"
    Click the **Test** button on a provider card to verify the connection before syncing. This only tests connectivity — it doesn't download any content.

## Selecting Categories

After adding a provider, go to the **Categories** tab. Tentacle will automatically fetch available categories on your first visit.

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

Click **Sync** on a provider card to trigger a sync immediately. A progress indicator shows the current status.

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

1. Removes all `.strm` and `.nfo` files from disk
2. Deletes all database records (movies, series, categories)
3. Automatically rebuilds playlists (orphaned source playlists are removed)
4. Updates home screen configs (validates hero spotlight still exists)

This is a clean operation — nothing is left behind.

## Stale File Detection

If you're migrating from another tool (like xtream-sync) and Tentacle detects existing `.strm` files but has an empty database, it will show a banner on the dashboard offering to:

- **Delete and start fresh** — Removes old files so Tentacle can create properly tagged ones
- **Dismiss** — Keep old files (not recommended — they won't have proper metadata or tags)

## Provider Migration

If your provider changes their server URL, Tentacle can rewrite all existing `.strm` files with the new URL without re-syncing everything. This is available in Settings.

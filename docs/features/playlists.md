# Playlists

Tentacle creates and manages Jellyfin playlists automatically. Content is tagged behind the scenes, and playlists are built from those tags — you just toggle what you want and Tentacle handles the rest.

## The Playlists Page

Go to **Jellyfin → Playlists** to manage your playlists. There are two types:

## Auto Playlists

Auto playlists are generated automatically from your synced content. They appear as toggle switches — turn on the ones you want created in Jellyfin.

### Source Playlists

One playlist per streaming provider source. If you have Netflix and Amazon categories synced, you'll see:

- Netflix Movies
- Netflix TV
- Amazon Movies
- Amazon TV
- HBO MAX Movies
- *(etc.)*

### List Playlists

One playlist per active [list subscription](discover-and-lists.md#lists). If you've subscribed to the IMDb Top 250 list, you'll see an "IMDB TOP 250" auto playlist.

### Built-in Playlists

Always available:

- **Recently Added Movies** — Movies added in the last 30 days
- **Recently Added TV** — Series with episodes added in the last 30 days
- **Downloaded Movies** — All content from Radarr

### Toggling

Click the toggle switch to enable or disable a playlist. Changes sync to Jellyfin automatically — no manual action needed. Enabled playlists are created in Jellyfin as private playlists owned by your user account.

## Custom Playlists

Create your own playlists with custom filters. Click **+ Create** in the Custom Playlists section.

### Friendly Builder

The default creation view offers simple controls:

- **Name** — What to call the playlist
- **Content type** — Movies, TV, or both
- **Content source** — All content, from a specific provider, from a list, or downloaded only
- **Genre** — Filter by genre (Comedy, Action, Drama, etc.)
- **Minimum rating** — TMDB community rating threshold
- **Year after** — Only content released after this year

!!! example "Example: Recent Sci-Fi Movies"
    - Content type: Movies
    - Content source: All
    - Genre: Science Fiction
    - Minimum rating: 7.0
    - Year after: 2020

### Advanced Mode

Click **Show advanced filters** to reveal the full condition builder. This lets you create complex rules with multiple field/operator/value conditions:

| Field | What it filters |
|-------|----------------|
| Genre | Movie/show genre (queries Jellyfin directly) |
| Rating | TMDB community rating (queries Jellyfin directly) |
| Year | Release year (queries Jellyfin directly) |
| Source | Streaming provider source tag |
| Source Tag | Specific source category tag |
| Runtime | Duration in minutes |

!!! info "Native vs. tag-based queries"
    **Genre, Rating, and Year** filters query Jellyfin directly using its own metadata. A "Comedy" playlist will find ALL comedies in your library — not just VOD content.

    **Source, Source Tag, and Runtime** filters use Tentacle's tag system because Jellyfin doesn't have this data natively.

    If a custom playlist mixes both types, it falls back to tag-based filtering for all conditions.

### Editing

Clicking an existing custom playlist opens it in advanced mode for editing. Changes auto-sync to Jellyfin when you save.

### Deleting

Delete a custom playlist and it's removed from Jellyfin automatically. If it was on your home screen, the row is also removed.

## Sort Order

Each playlist can have its own sort order. Click the sort button on any playlist to choose:

- **Release Date** — By premiere date
- **Name** — Alphabetical
- **Rating** — By TMDB rating
- **Recently Added** — By when Tentacle added it
- **Random** — Shuffled order

Changing the sort clears and re-populates the Jellyfin playlist in the new order.

## Playlist Artwork

Tentacle auto-generates playlist artwork using Logo.dev — provider logos are fetched and set as the playlist image in Jellyfin. This happens during the nightly sync.

## How Tags Work (Behind the Scenes)

You don't need to understand tags to use playlists, but here's what happens internally:

1. **VOD content** — Tagged via `<tag>` elements in NFO files. Jellyfin reads these automatically for `.strm` files.
2. **Radarr content** — Tagged via the Jellyfin API. Jellyfin ignores NFO tags for downloaded `.mkv` files.
3. **Tag format** — Source tags include the media type suffix: `"Netflix Movies"`, `"Netflix TV"`, `"Downloaded Movies"`.
4. **Playlist query** — Auto playlists query Jellyfin for items with matching tags. Custom playlists with native conditions (genre/rating/year) query Jellyfin directly.

## Per-User Playlists

Every playlist is per-user:

- Each user has their own set of auto playlist toggles
- Each user can create their own custom playlists
- Jellyfin playlists are created as private (`IsPublic=false`), owned by each user's Jellyfin account
- Playlist configs are stored at `/data/smartlists/{jellyfin_user_id}/`

## Auto-Sync Behavior

You never need to manually sync playlists. Changes are pushed to Jellyfin automatically when you:

- Toggle an auto playlist on/off
- Create, edit, or delete a custom playlist
- Change a playlist's sort order
- Run a VOD sync (new content triggers playlist refresh)
- Radarr/Sonarr webhook fires (new downloads are added to relevant playlists)

The nightly 3 AM sync also refreshes all playlists for all users.

# Discover & Lists

Tentacle brings content discovery to both the dashboard and Jellyfin itself — browse trending titles, subscribe to curated lists, and add missing content to your library with one click.

## Discover

Go to **Jellyfin → Discover** in the Tentacle dashboard to browse content from TMDB.

### Sections

The Discover page shows several sections:

- **Trending** — What's trending on TMDB right now
- **Popular** — Most popular movies and TV shows
- **Upcoming** — Titles not yet released
- **Missing from Lists** — Content from your list subscriptions that's not in your library yet

### Content Cards

Each title shows:

- Poster artwork
- Title, year, and rating
- Status badge:
    - :material-check-circle:{ style="color: green" } **In Library** — Already in your Jellyfin library
    - :material-download:{ style="color: purple" } **Downloading X%** — Currently being downloaded by Radarr/Sonarr
    - :material-plus-circle: **Add** — Not in your library, click to add

### Adding Content

Click a title to see its full details (plot, cast, genres, rating). From the detail view:

**Movies:**

- Click **Add to Radarr** to add it to your Radarr download queue
- Select a quality profile before adding (the button cycles through available profiles)
- Your last selected profile is remembered

**TV Series:**

- Click **Add to Sonarr** with options for which episodes to monitor:
    - All Episodes (including future)
    - First Season
    - Last Season
    - Pilot
    - Pick Episodes (opens the episode picker)
- Select a quality profile before adding

### Search

Use the search bar to find specific titles on TMDB. Results show the same status badges and add functionality.

## Discover in Jellyfin

Tentacle can inject a **Discover tab** directly into Jellyfin's web interface via the companion plugin. This gives you the same browsing experience without leaving Jellyfin.

### Enable/Disable

On the Discover page in Tentacle, toggle **Show in Jellyfin**. This saves immediately and the plugin picks up the change.

!!! info "How it works"
    The Tentacle plugin injects JavaScript that adds a "Discover" tab to Jellyfin's home page. The JS re-checks the enabled state on every page visit, so toggling takes effect immediately (just refresh Jellyfin).

### What It Looks Like

In Jellyfin, you'll see tabs at the top of the home page. The Discover tab shows the same trending/popular/upcoming grid with add buttons and status badges.

An **Activity tab** also appears showing real-time download progress — see [Activity Tracking](activity.md) for details.

## Lists

Subscribe to curated content lists from IMDb, Trakt, and Letterboxd. Lists automatically generate playlists and help you discover what's missing from your library.

### Supported Sources

| Source | What you need |
|--------|--------------|
| **IMDb** | The list URL (e.g., `https://www.imdb.com/list/ls062911411/`) |
| **Trakt** | The list URL (e.g., `https://trakt.tv/lists/...`) |
| **Letterboxd** | The list URL (e.g., `https://letterboxd.com/dave/list/...`) |

!!! info "Trakt API key"
    Trakt lists require a Trakt API key configured in Settings. IMDb and Letterboxd lists work without any API key.

### Adding a List

1. Go to the **Discover** page and scroll to the Lists section
2. Click **Add List**
3. Enter the list URL and a name
4. Tentacle fetches the list items and matches them to TMDB

### List Coverage

Each list card shows how much of the list content you have in your library. Click a list to see the full coverage view — every title with its status:

- :material-check-circle:{ style="color: green" } **In Library** — You have it
- :material-close-circle:{ style="color: red" } **Missing** — Not in your library
- :material-download:{ style="color: purple" } **Downloading** — Currently being downloaded

### Add Missing Content

From the coverage view, you can:

- **Add all missing movies** to Radarr in bulk
- **Add all missing series** to Sonarr in bulk
- **Add individual titles** one at a time

### List Playlists

Each list subscription can generate a Jellyfin playlist. On the Playlists page, list playlists appear in the Auto Playlists section. Toggle them on to create a Jellyfin playlist containing all matching library items.

For example, subscribing to the "IMDB TOP 250" list and enabling its playlist creates a Jellyfin playlist with all Top 250 movies you have in your library.

### Refreshing Lists

Lists are refreshed automatically during the nightly 3 AM sync. You can also click **Refresh** on a list card to fetch the latest items manually.

!!! tip "Lists stay current"
    IMDb, Trakt, and Letterboxd lists change over time. Tentacle re-fetches list contents on every refresh, so your coverage view and playlists always reflect the current list.

## Episode Picker

When adding a TV series to Sonarr via "Pick Episodes", Tentacle shows an episode picker that combines multiple data sources:

### Download More Episodes (VOD Series)

For series that already exist as VOD content:

- **VOD episodes** show a green "VOD" badge (already available as streams)
- **Downloaded episodes** show a purple "DL" badge (already downloaded via Sonarr)
- Both VOD and downloaded episodes are pre-checked and disabled (can't be unselected)
- Only missing episodes can be selected for download

Season headers show coverage counts:

- **5/8** (orange) — Partial coverage
- **8/8** (green) — Full season
- **7/7 +1 upcoming** — All aired episodes present, one hasn't aired yet

### Manage Episodes (Existing Sonarr Series)

For series already in Sonarr, you can change which episodes are monitored:

1. Click **Manage Episodes** in the series detail view
2. The picker shows current monitoring state from Sonarr
3. Adjust your selection
4. Apply — Sonarr will search for newly-monitored episodes automatically

### Unaired Episodes

Episodes that haven't aired yet are shown at 50% opacity with an air date badge (e.g., "Apr 7" or "TBA"). They're excluded from coverage counts to avoid misleading numbers like "7/8" when the 8th episode just hasn't aired yet.

### Auto-Download New Episodes

When adding a series via "Download More Episodes", an "Auto-download new episodes" checkbox (on by default) sets the series to be [followed](library.md#following) — Sonarr will automatically download new episodes as they air.

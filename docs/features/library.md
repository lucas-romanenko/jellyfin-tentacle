# Library

The Library page gives you a unified view of all content Tentacle knows about — VOD titles from your streaming provider, Radarr movies, and Sonarr series — all in one place.

## Browsing

The Library page shows content cards with posters, titles, years, and ratings. You can:

- **Filter by type** — Movies, TV Series, or both
- **Search** — Find specific titles by name
- **Sort** — By title, year, rating, or recently added

### Content Badges

Each card shows where the content comes from:

- **Source badge** — The streaming provider source (Netflix, Amazon, etc.)
- **DL badge** (green) — Downloaded via Radarr or Sonarr
- **Both** — Some content may appear with both a source badge and DL badge (hybrid content)

## Detail View

Click any title to open its full detail modal with:

- Poster and backdrop artwork
- Full plot summary
- Cast, genres, rating, runtime
- Release year and status
- Source information
- Action buttons (add to Radarr/Sonarr, manage episodes, follow)

### TV Series Details

For TV series, the detail view also shows:

- **Season/episode breakdown** — Expandable seasons with episode lists
- **Episode status** — VOD (green), Downloaded (purple), or missing
- **Unaired episodes** — Shown at 50% opacity with air date badges
- **Download More Episodes** — Add missing episodes to Sonarr
- **Manage Episodes** — Change monitoring on existing Sonarr series

## Following

Following a series tells Sonarr to automatically download new episodes as they air. This is integrated with Sonarr's `monitorNewItems` feature.

### How to Follow

- Click the **Follow** button in a series' detail view
- Or check **"Auto-download new episodes"** when adding a series to Sonarr

### Following Tab

The Library page has a **Following** tab that shows all series you're currently following. This excludes ended or canceled series since there won't be new episodes.

### How It Works

- **Following ON** → Sonarr's `monitorNewItems` is set to `"all"` — new episodes auto-download
- **Following OFF** → `monitorNewItems` is cleared, but the series stays monitored in Sonarr (existing episode monitoring is preserved)
- **Bidirectional sync** — If you change monitoring in Sonarr directly, Tentacle picks up the change during the next scan

!!! info "Not shown for ended series"
    The Follow button is hidden for series with a status of "Ended" or "Canceled" since there won't be new episodes to follow.

## Duplicates

When content exists in both VOD (from your streaming provider) and your downloaded library (from Radarr), Tentacle detects the overlap and flags it as a duplicate.

### Viewing Duplicates

The dashboard shows a duplicate count if any are detected. Click through to see the list with:

- The title and which sources have it
- The VOD file path
- Options to resolve

### Resolving

You can resolve duplicates individually or all at once. Resolving removes the duplicate record from the database — it doesn't delete any files. You decide which version to keep by managing it in your provider categories or Radarr.

!!! info "Hybrid series are not duplicates"
    If you use "Download More Episodes" to add missing episodes to a VOD series, that's an intentional hybrid — not a duplicate. Tentacle tracks this via the `sonarr_path` field and won't flag it.

## Real-Time Updates

The Library page supports real-time updates via Server-Sent Events (SSE). When new content is synced or downloaded, it appears automatically without refreshing the page.

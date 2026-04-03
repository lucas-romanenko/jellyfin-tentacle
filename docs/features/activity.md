# Activity Tracking

Tentacle provides real-time visibility into what's currently downloading and what content you're waiting for.

## What It Shows

### Active Downloads

Content currently being downloaded by Radarr or Sonarr:

- **Poster** — Movie/show artwork
- **Title and year**
- **Progress bar** — Download percentage
- **Status** — Downloading, importing, queued, etc.
- **Quality** — The quality profile being downloaded (1080p, 4K, etc.)
- **ETA** — Estimated time remaining

### Unreleased Content

Movies and series you're monitoring that haven't been released yet:

- **Poster** — Artwork from TMDB
- **Title and year**
- **Countdown badge** — Days until release (e.g., "12 days")

## Where Activity Appears

### Tentacle Dashboard

The dashboard home page shows activity status with counts of active downloads and unreleased titles.

### Jellyfin Discover Tab

If you have the [Tentacle plugin](../integrations/jellyfin-plugin.md) installed and the Discover tab enabled, an **Activity** tab appears in Jellyfin's home page. It shows the same download progress and unreleased content.

The Activity tab has a purple highlight badge showing the count of active items.

### Jellyfin Discover Cards

Individual content cards in the Discover tab show a purple **"Downloading X%"** badge when that title is actively being downloaded. This replaces the usual "Add" button.

### Android TV App

The [Tentacle Android TV app](../integrations/android-tv.md) has a dedicated Activity tab in the top toolbar. It shows download progress cards with progress bars and unreleased countdown cards.

## How It Works

### Polling

Activity data is always fresh — there's no caching on the activity endpoint.

- **Jellyfin plugin** — Polls every 3 seconds when the Discover tab is open. Badge count updates in the background even when viewing other tabs.
- **Android TV app** — Polls every 3 seconds when the Activity tab is active.
- **Dashboard** — Updates on page load.

### Download Client Refresh

To ensure progress percentages are current, Tentacle triggers `RefreshMonitoredDownloads` on Radarr and Sonarr every 5 seconds (throttled). This forces them to query the download client (SABnzbd, qBittorrent, etc.) for the latest progress.

### Poster Fallback

Activity posters come from multiple sources in priority order:

1. Local database (Tentacle's cached TMDB artwork)
2. Radarr/Sonarr images array
3. TMDB fallback

!!! warning "Activity empty while downloading?"
    If Activity shows nothing while content is downloading in your download client, the issue is almost always a **category mismatch** between Radarr/Sonarr and the download client.

    Check Radarr/Sonarr → Activity → Queue first. If their queue is also empty, the download client category doesn't match what Radarr/Sonarr expects. Fix the category in the download client settings. See [Troubleshooting](../troubleshooting.md) for more details.

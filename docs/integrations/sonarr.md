# Sonarr Integration

Tentacle integrates with Sonarr for TV series — tracking downloads, managing episode monitoring, enabling the Following system, and supporting hybrid VOD + downloaded series.

## Setup

### Connect Sonarr

1. Go to **Settings** in Tentacle
2. Enter your **Sonarr URL** (e.g., `http://192.168.1.100:8989` or `http://sonarr:8989` on Docker)
3. Enter your **Sonarr API Key** (found in Sonarr → Settings → General)
4. Click **Test** to verify the connection

### Configure Webhooks

In Sonarr, go to **Settings → Connect → Add → Webhook**:

- **Name:** Tentacle
- **URL:** `http://<tentacle-ip>:8888/api/sonarr/webhook`
- **Triggers:** Enable all of these:
    - On File Import
    - On Series Add
    - On Series Delete
    - On Episode File Delete

!!! important "Use internal IP"
    The webhook URL must use the internal network IP, not an external URL. Sonarr needs to reach Tentacle directly on the local network.

### VOD Root Folder (For Hybrid Series)

If you plan to use "Download More Episodes" to fill in missing episodes on VOD series, Sonarr needs access to the VOD shows folder:

1. Add a volume mount in your `docker-compose.yml` for Sonarr:
   ```yaml
   sonarr:
     volumes:
       - /path/to/vod/shows:/data/vod/tv
   ```
2. In Sonarr, go to **Settings → Media Management → Root Folders**
3. Add `/data/vod/tv` as a root folder

This lets Sonarr download new episodes directly into existing VOD series folders, creating a unified view in Jellyfin.

## What Happens When Episodes Download

1. Sonarr fires a `Download` webhook → Tentacle receives it
2. Tentacle runs a Sonarr library scan
3. The series is matched to TMDB for metadata
4. NFO files are written for each episode
5. Tags are applied via Jellyfin API
6. Playlists are updated

## What Happens When a Series is Deleted from Sonarr

The behavior depends on the type of series:

- **Hybrid series** (has both VOD and downloaded episodes): Tentacle keeps the database record but clears the Sonarr tracking fields. VOD content remains available.
- **Sonarr-only series**: The database record is deleted entirely.

## Following

Following is Tentacle's system for tracking series and automatically getting new episodes. It maps directly to Sonarr's `monitorNewItems` feature.

### What Following Does

- **Following ON** → Sonarr's `monitorNewItems` is set to `"all"`, meaning Sonarr will automatically search for and download new episodes when they air
- **Following OFF** → `monitorNewItems` is cleared, but existing episode monitoring stays intact

### How to Follow/Unfollow

- Click **Follow** in a series' detail modal in the Library
- Check **"Auto-download new episodes"** when adding a series via Download More Episodes

### Bidirectional Sync

Following state is synced both ways:

- **Tentacle → Sonarr**: When you toggle Follow in Tentacle, it updates Sonarr's `monitorNewItems`
- **Sonarr → Tentacle**: During every library scan, Tentacle reads `monitorNewItems` for all series and updates its database

### Following Tab

The Library page has a Following tab showing all series you're currently following, excluding series with a status of "Ended" or "Canceled" (since there won't be new episodes).

## Download More Episodes

For VOD series that have gaps in their episode coverage, you can download missing episodes via Sonarr:

1. Open the series detail modal in Library or Discover
2. Click **Download More Episodes**
3. The episode picker shows:
    - **VOD episodes** — Green "VOD" badge (already available as streams)
    - **Downloaded episodes** — Purple "DL" badge (already on disk)
    - **Missing episodes** — Selectable checkboxes
    - **Unaired episodes** — Shown at 50% opacity with air date
4. Select the episodes you want
5. Choose a quality profile
6. Click **Add** — Sonarr downloads them into the same folder

!!! info "Hybrid series"
    Downloaded episodes land in the same Jellyfin series entry as VOD episodes. Jellyfin sees one unified series with a mix of `.strm` (VOD) and `.mkv` (downloaded) files.

### Episode Monitoring Options

When adding a new series to Sonarr:

| Option | What it monitors |
|--------|-----------------|
| All Episodes (including future) | Every episode, past and future |
| First Season | Only Season 1 |
| Last Season | Only the most recent season |
| Pilot | Only S01E01 |
| Pick Episodes | Opens the episode picker for manual selection |

## Manage Episodes

For series already in Sonarr, you can change which episodes are monitored:

1. Open the series detail modal
2. Click **Manage Episodes**
3. Adjust checkboxes — downloaded episodes are pre-checked and disabled
4. Apply — newly-monitored episodes are automatically searched in Sonarr

## Quality Profiles

When adding a series via Discover or a list:

- Available quality profiles are fetched from Sonarr's API
- Click the quality button to cycle through profiles
- Your last selection is remembered (stored separately from Radarr profiles)

## Sonarr Library Scan

Tentacle scans Sonarr's library:

- **Automatically** — During the nightly 3 AM sync
- **Post-setup** — After the setup wizard if Sonarr is configured
- **What it does**: Fetches all series, matches TMDB metadata, writes NFO files, applies tags, and syncs Following state for every series in the database

## Volume Mapping

```yaml
tentacle:
  volumes:
    - /path/to/shows:/media/shows              # Sonarr TV library
    - /path/to/vod/shows:/media/vod/shows      # VOD series (for hybrid detection)
```

## Download Client Prerequisites

Same as Radarr — the download client category must match what Sonarr expects. If Sonarr's Activity → Queue is empty while downloads are running, the category is misconfigured. See [Troubleshooting](../troubleshooting.md) for details.

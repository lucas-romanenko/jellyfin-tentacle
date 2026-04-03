# Radarr Integration

Tentacle integrates with Radarr to track downloaded movies, apply tags, generate NFO files, and trigger playlist updates. New downloads appear on your home screen automatically.

## Setup

### Connect Radarr

1. Go to **Settings** in Tentacle
2. Enter your **Radarr URL** (e.g., `http://192.168.1.100:7878` or `http://radarr:7878` on Docker)
3. Enter your **Radarr API Key** (found in Radarr → Settings → General)
4. Click **Test** to verify the connection

### Configure Webhooks

Webhooks let Tentacle react to Radarr events in real time. Without webhooks, Tentacle only picks up changes during the nightly scan.

In Radarr, go to **Settings → Connect → Add → Webhook**:

- **Name:** Tentacle
- **URL:** `http://<tentacle-ip>:8888/api/radarr/webhook`
- **Triggers:** Enable all of these:
    - On File Import
    - On Movie Added
    - On Movie Delete
    - On Movie File Delete

!!! important "Use internal IP"
    The webhook URL must use the internal network IP (e.g., `http://192.168.1.100:8888`), not an external/Cloudflare tunnel URL. Radarr needs to reach Tentacle directly.

!!! tip "Test the webhook"
    You can test webhooks from Tentacle at **Settings → Connections**. The test is proxied through the backend to avoid browser mixed-content issues.

## What Happens When a Movie Downloads

1. Radarr fires a `Download` webhook → Tentacle receives it
2. Tentacle runs a Radarr library scan — fetches TMDB metadata, saves to database
3. Tags are applied:
    - `"Downloaded Movies"` (always)
    - `"Recently Added Movies"` (30-day rolling window)
    - List tags (e.g., `"IMDB TOP 250"`) if the movie matches a subscribed list
4. An NFO file is written with the same filename as the video (e.g., `Alien (1979) Bluray-1080p.nfo`)
5. Tags are pushed to Jellyfin via API (Jellyfin ignores NFO tags for `.mkv` files)
6. A Jellyfin item refresh is triggered for proper metadata display
7. Playlists are updated immediately

## What Happens When a Movie is Deleted

1. Radarr fires a `MovieDelete` webhook
2. Tentacle removes the movie from its database
3. The movie shows as "Missing" in the Library with an add button

## Radarr Library Scan

Tentacle can scan your entire Radarr library at any time:

- **Automatic** — Runs during the nightly 3 AM sync
- **Manual** — Click **Scan** on the Settings page
- **Post-setup** — Runs automatically after the setup wizard if Radarr is configured

The scan fetches all movies from Radarr, matches TMDB metadata, writes NFO files, and applies tags.

## Quality Profiles

When adding a movie via Discover or a list, you can select a Radarr quality profile:

- Available profiles are fetched from Radarr's API
- Click the quality button to cycle through profiles
- Your last selection is remembered

## NFO Files

Tentacle writes NFO files for Radarr movies to provide metadata:

- **Filename** — Must match the video filename exactly (e.g., `Alien (1979) Bluray-1080p.nfo`)
- **Content** — Full metadata including title, plot, rating, cast, poster URLs, and tags
- **Purpose** — Jellyfin reads NFO for metadata display, but NOT for tags on `.mkv` files (tags are pushed via API separately)

## Duplicate Detection

If a movie exists in both VOD (from your streaming provider) and Radarr, Tentacle flags it as a duplicate. See [Library → Duplicates](../features/library.md#duplicates) for details.

## Volume Mapping

Tentacle needs access to the same movie folder that Radarr writes to:

```yaml
# In your docker-compose.yml
tentacle:
  volumes:
    - /path/to/movies:/media/movies    # Same folder Radarr downloads to
```

Radarr sees this folder at `/data/movies` (inside the Radarr container). Tentacle sees it at `/media/movies`. The path mapping is handled automatically.

## Download Client Prerequisites

For [Activity tracking](../features/activity.md) to show download progress, the full chain must be configured:

1. **Download client** (SABnzbd, qBittorrent, etc.) must be added in Radarr → Settings → Download Clients
2. **Categories must match** — the category in Radarr's download client config (e.g., `radarr`) must exist in the download client
3. If categories don't match, Radarr can't track downloads and Activity will show nothing

!!! warning "Common issue"
    Activity showing empty while movies are downloading? Check Radarr → Activity → Queue. If Radarr's own queue is empty, the download client category doesn't match. Fix it in the download client settings.

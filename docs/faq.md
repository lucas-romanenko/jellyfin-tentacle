# FAQ

## General

### What is Tentacle?

Tentacle is a unified media library manager for Jellyfin. It syncs VOD content from IPTV providers, tracks Radarr/Sonarr downloads, auto-generates playlists, and provides a Netflix-style home screen — all from a single Docker container.

### Do I need Jellyfin?

Yes. Tentacle is built specifically for Jellyfin. It manages content, playlists, and the home screen experience within Jellyfin.

### Does Tentacle replace Jellyfin?

No. Tentacle works alongside Jellyfin — it manages content organization, playlists, and the home screen, while Jellyfin handles playback, transcoding, and user management.

### Is an IPTV provider required?

No. Tentacle works without a streaming provider. You can use it just for managing Radarr/Sonarr content, playlists, home screen customization, and content discovery.

### What are the system requirements?

Tentacle is lightweight — a single Docker container with SQLite. It runs well on low-powered hardware. The main resource usage comes from TMDB API calls during syncs and the Jellyfin API for playlist management.

---

## Setup

### What's the minimum I need to get started?

Just a Jellyfin URL and API key. TMDB metadata works automatically with the built-in key. Everything else (providers, Radarr, Sonarr) is optional.

### How do I get a Jellyfin API key?

In Jellyfin: Dashboard → API Keys → Create. Give it a name (e.g., "Tentacle") and copy the key.

### Can I use Tentacle with multiple Jellyfin servers?

Currently, Tentacle connects to one Jellyfin server. For multiple servers, you'd run separate Tentacle instances.

### Do I need to set up TMDB?

No. Tentacle includes a built-in TMDB API key that works out of the box. You can optionally use your own key in Settings.

---

## Content & Syncing

### How does VOD content appear in Jellyfin?

Tentacle creates `.strm` files (streaming URLs) and `.nfo` files (metadata) in folders that Jellyfin monitors. Jellyfin reads these and displays the content with full artwork and metadata, just like any other media file.

### What happens during the nightly sync?

At 3 AM, Tentacle:

1. Refreshes all list subscriptions
2. Syncs VOD content from active providers
3. Scans Radarr and Sonarr libraries
4. Refreshes tags and playlists
5. Triggers a Jellyfin library scan
6. Updates home screen configs for all users
7. Syncs playlist artwork

### Can I trigger a sync manually?

Yes. On the VOD page, click the Sync button on any provider card. For Radarr/Sonarr, you can trigger scans from Settings. Webhook events also trigger immediate syncs.

### Why was some content skipped during sync?

By default, content that can't be matched to TMDB is skipped. This filters out placeholder entries and garbled titles. You can disable "Require TMDB Match" in the provider settings to include everything.

### What's the difference between VOD and downloaded content?

- **VOD** — Streamed directly from your IPTV provider via `.strm` files. Quality depends on the provider.
- **Downloaded** — Actual video files (`.mkv`) downloaded by Radarr/Sonarr. Full quality, stored on disk.

Both appear identically in Jellyfin. The difference is in how they're played and where the content lives.

---

## Playlists & Home Screen

### What's the difference between auto playlists and custom playlists?

- **Auto playlists** — Generated automatically from your content sources. One per provider category, one per list subscription, plus built-ins. Toggle them on/off.
- **Custom playlists** — Created by you with custom filters (genre, rating, year, source). More flexible.

### Do playlists sync automatically?

Yes. Any change (toggle, create, edit, delete, sort) syncs to Jellyfin automatically. The nightly sync also refreshes everything.

### Can different users have different playlists?

Yes. Playlists are per-user. Each user has their own auto playlist toggles, custom playlists, and home screen layout. Playlists are created as private in Jellyfin.

### Do I need the plugin for the home screen?

Yes, the custom home screen (hero spotlight + playlist rows) requires the [Tentacle plugin](integrations/jellyfin-plugin.md). Without the plugin, playlists still exist in Jellyfin's regular playlist section.

### What's the hero spotlight?

A large cinematic banner at the top of the Jellyfin home page. It cycles through content from a playlist you choose, with Ken Burns zoom and crossfade transitions. Each user can pick their own hero playlist.

---

## Integrations

### Do I need Radarr and Sonarr?

No. Radarr and Sonarr are optional. Without them, you can still use VOD content, playlists, and home screen customization. You just won't have the download management and activity tracking features.

### What do webhooks do?

Webhooks let Radarr/Sonarr notify Tentacle immediately when content is downloaded, added, or deleted. Without webhooks, Tentacle only picks up changes during the nightly sync.

### What does the Jellyfin plugin do?

It adds three things to Jellyfin's web interface:

1. Custom home screen (hero spotlight + playlist rows)
2. Discover tab (browse TMDB content, add to Radarr/Sonarr)
3. Activity tab (download progress, unreleased countdown)

### Is the Android TV app required?

No. The Android TV app is optional. The standard Jellyfin app works fine — you just won't have the custom Tentacle features (hero, Discover, Activity). You can also use the Jellyfin web interface which gets the plugin features.

### Can I use Tentacle with Plex instead of Jellyfin?

No. Tentacle is built specifically for Jellyfin's API and plugin system.

---

## Live TV

### Does Tentacle replace Threadfin?

Yes. Tentacle includes a built-in HDHomeRun emulator that serves the same purpose — presenting your IPTV channels to Jellyfin as a native tuner device.

### Can I use the same provider for VOD and Live TV?

Yes, but they're configured separately. Add the provider on the VOD page for on-demand content, and on the Live TV page for channels. They can use the same credentials.

### Why are some channels missing EPG?

Not all channels from your provider have guide data. The EPG badge in Tentacle ("Has EPG" / "No EPG") reflects actual program data. If a channel shows "No EPG" after a full EPG sync, the provider doesn't supply guide data for it.

---

## Hybrid Series (VOD + Downloaded)

### What's a hybrid series?

A series that has some episodes as VOD streams (`.strm`) and others as downloaded files (`.mkv`) in the same Jellyfin entry. For example, a show where seasons 1-3 are available from your VOD provider and you download season 4 via Sonarr.

### How do I set up hybrid series?

1. Mount the VOD shows folder in Sonarr (e.g., `/data/vod/tv`)
2. Add it as a root folder in Sonarr
3. Use "Download More Episodes" in Tentacle — it automatically uses the VOD folder path

### Won't this create duplicates?

No. Tentacle tracks the `sonarr_path` when a VOD series is intentionally added to Sonarr, so it's not flagged as a duplicate during scans.

---

## Data & Privacy

### Where is my data stored?

Everything is local — SQLite database and config files in your data volume (`/data`). No data is sent to external servers except API calls to TMDB (for metadata), your IPTV provider (for content), and Radarr/Sonarr (on your network).

### Can I back up my configuration?

Yes. Back up the entire data volume (`./tentacle-data` or wherever you mounted `/data`). This includes the database, TMDB cache, playlist configs, and home screen configs.

### What happens if I lose the database?

You'll need to go through the setup wizard again and re-add your providers. VOD content will need to be re-synced. Radarr/Sonarr content will be picked up on the next scan. Playlists and home screen layouts will need to be reconfigured.

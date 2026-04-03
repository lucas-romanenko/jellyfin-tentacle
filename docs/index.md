# Tentacle

**Turn Jellyfin into a full-featured streaming platform.**

<p align="center">
  <img src="images/banner.png" alt="Tentacle" width="600">
</p>

Tentacle is a unified media library manager for Jellyfin — a dashboard and companion plugin that brings together live TV, VOD content from your streaming provider, and your personal media library into one organized, tagged system.

Content from every source is automatically matched to TMDB metadata, tagged, and surfaced through smart playlists and a Netflix-style home screen with a hero spotlight banner. One Docker container, no external databases, no build steps.

---

## What Can Tentacle Do?

### :material-television: Live TV
Add your IPTV provider (Xtream, M3U URL, or M3U file) and get live channels with EPG in Jellyfin — no Threadfin needed. Tentacle emulates an HDHomeRun tuner so Jellyfin sees it as a native device.

### :material-movie-open: VOD Library
Your provider's movies and series appear in Jellyfin with full metadata — posters, plots, ratings, genres. Every title is matched against TMDB and tagged by source automatically.

### :material-playlist-play: Smart Playlists
Auto-generated playlists from your content sources (Netflix, Amazon, HBO, etc.), imported lists (IMDb, Trakt, Letterboxd), and custom filters. Each playlist becomes a row on your home screen.

### :material-home: Netflix-Style Home Screen
A hero spotlight banner with Ken Burns zoom, drag-and-drop playlist rows, and native Jellyfin sections — all customizable per user.

### :material-compass: Content Discovery
Browse trending, popular, and upcoming titles from TMDB. Subscribe to curated lists. Add missing content to Radarr or Sonarr with one click. A Discover tab is injected directly into Jellyfin via the companion plugin.

### :material-download: Activity Tracking
Real-time progress for content being downloaded. Unreleased titles you're following shown with countdown badges. Polls every 3 seconds for live updates.

### :material-account-group: Multi-User
Each Jellyfin user gets their own home screen layout, playlists, list subscriptions, and content preferences. Netflix-style user picker on login.

### :material-download-box: Radarr & Sonarr
New downloads are automatically tagged, organized into playlists, and surfaced on the home screen. VOD episodes and downloaded episodes merge into one unified series view.

---

## How It Works

```
┌─────────────────────────────────────────────────┐
│                   Jellyfin                       │
│  ┌─────────────────────────────────────────┐    │
│  │  Tentacle Plugin                        │    │
│  │  • Custom home screen rows & hero       │    │
│  │  • Discover tab (trending/popular)      │    │
│  │  • Activity tab (progress/upcoming)     │    │
│  └──────────────────┬──────────────────────┘    │
│                     │ fetches config via API      │
└─────────────────────┼───────────────────────────┘
                      │
            Jellyfin API (tags, playlists,
             library scan, HDHomeRun tuner)
                      │
┌─────────────────────┴───────────────────────────┐
│              Tentacle Dashboard                  │
│  • VOD sync & NFO generation                    │
│  • Live TV with HDHomeRun emulation             │
│  • Smart playlists & tag engine                 │
│  • Content discovery (TMDB)                     │
│  • Radarr & Sonarr webhook integration          │
└──────┬──────────┬──────────┬───────────┬────────┘
       │          │          │           │
  Streaming    TMDB       Radarr      Sonarr
   Provider    API         API         API
```

The **Dashboard** is a FastAPI web app that runs as a single Docker container. It syncs content, manages playlists, and serves the admin UI.

The **Plugin** is a C# Jellyfin plugin that injects a custom home screen, Discover tab, and Activity tab into Jellyfin's web interface. It communicates with the Dashboard via API.

Both work together, but the Dashboard is fully functional on its own — the plugin just enhances the Jellyfin experience.

---

## Quick Start

```yaml
tentacle:
  image: ghcr.io/lucas-romanenko/jellyfin-tentacle:latest
  container_name: tentacle
  ports:
    - 8888:8888
  volumes:
    - ./tentacle-data:/data
  restart: unless-stopped
```

```bash
docker compose up -d
```

Open `http://localhost:8888` and the setup wizard will walk you through connecting to Jellyfin.

[:material-arrow-right: Full installation guide](getting-started/installation.md){ .md-button .md-button--primary }
[:material-puzzle: Install the plugin](integrations/jellyfin-plugin.md){ .md-button }

---

## Components

| Component | What it is | Where it runs |
|-----------|-----------|---------------|
| **Dashboard** | FastAPI web app (Python) | Docker container on port 8888 |
| **Plugin** | Jellyfin plugin (C#) | Inside Jellyfin's plugin directory |
| **Android TV App** | Custom Jellyfin client (Kotlin) | Android TV / Fire TV devices |

The Dashboard works standalone. The Plugin adds the custom home screen, hero spotlight, Discover tab, and Activity tab inside Jellyfin's web UI. The Android TV app brings the same experience to your TV.

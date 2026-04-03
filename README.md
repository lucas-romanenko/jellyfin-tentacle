# Jellyfin Tentacle

**Turn Jellyfin into a full-featured streaming platform.**

<p align="center">
  <img src="docs/images/banner.png" alt="Jellyfin Tentacle" width="800">
</p>

[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://ghcr.io/lucas-romanenko/jellyfin-tentacle)
[![Jellyfin](https://img.shields.io/badge/Jellyfin-10.11+-00A4DC?logo=jellyfin&logoColor=white)](https://jellyfin.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## What is Tentacle?

Tentacle is a unified media library manager for Jellyfin — a dashboard and Jellyfin plugin that brings together IPTV live TV, VOD content from your IPTV provider, and your personal media library into one organized, tagged system. Content from every source is automatically tagged and matched to TMDB metadata, then surfaced through smart playlists and a Netflix-style home screen with a hero spotlight banner. One Docker container, no external databases, no build steps.

Add your IPTV provider and your Jellyfin home screen fills with curated rows, a hero banner with Ken Burns crossfade, and a Discover tab showing what's trending. Connect Radarr and Sonarr, and new content is automatically tagged, organized into playlists, and surfaced on the home screen. Follow series to track new episodes as they release. Multiple Jellyfin users get their own independent home screen layout, playlists, and content preferences.

---

## Features

### 📺 Live TV & IPTV
- Add your IPTV provider (Xtream, M3U URL, or M3U file) — replaces Threadfin
- Browse and enable channel groups — only sync what you want
- Full EPG program guide with automatic refresh and auto-chaining
- Built-in HDHomeRun emulation — Jellyfin sees Tentacle as a native tuner
- Stream proxy handles provider redirects and HLS-to-MPEG-TS conversion

### 🎬 VOD Library from IPTV
- Your IPTV provider's movies and series appear in Jellyfin with full metadata — posters, plots, ratings, genres
- Automatic TMDB matching and tagging — every title is identified, tagged by source, and ready for playlists
- English-language filtering with smart country/prefix detection

### 🏷️ Smart Playlists
- All content is tagged by source (Netflix, Amazon, HBO, etc.), type, and list membership — playlists are built from these tags
- Auto playlists generated from your content — toggle on the ones you want in Jellyfin
- Create custom playlists with filters: genre, rating, year, runtime, source
- Native Jellyfin queries for genre/rating playlists — sees your entire library, not just IPTV content
- Per-playlist sort order (release date, name, rating, recently added, random)
- Auto-generated playlist artwork via Logo.dev
- Playlists become rows on your Netflix-style home screen — drag to reorder, mix with native Jellyfin sections

### 🔍 Content Discovery
- Trending, popular, and upcoming titles from TMDB
- Subscribe to IMDb, Trakt, and Letterboxd lists — auto-generate playlists from them
- See what's missing from your library and add to Radarr/Sonarr with quality profile selection
- Live status badges — "In Library", progress percentage, or "Add" for missing content
- Discover tab injected directly into Jellyfin via the companion plugin

### 📊 Activity & Tracking
- Real-time progress tracking for content being added to your library
- Unreleased titles you're following shown with countdown badges
- Activity tab injected into Jellyfin via the companion plugin
- Polls every 3 seconds for live updates

### 🏠 Netflix-Style Home Screen
- Drag-and-drop row ordering for the Jellyfin home page
- Hero spotlight banner with Ken Burns zoom and smooth crossfade transitions
- Mix playlist rows with native Jellyfin sections (Continue Watching, Next Up, etc.)
- Independent sort options per row and for the hero carousel
- Per-user — each Jellyfin user customizes their own home screen independently

### 👥 Multi-User Support
- Netflix-style Jellyfin user picker on login
- Per-user playlists, auto-playlist toggles, list subscriptions, and home screen layout
- Admin / non-admin roles synced from Jellyfin — non-admins see Library + Jellyfin pages only
- Owner protection — the first user can't have admin removed

### 📥 Radarr & Sonarr Integration
- New content automatically tagged, organized into playlists, and surfaced on the home screen
- Webhook-driven — library updates in real time as content arrives
- Duplicate detection when content exists in both VOD and your personal library
- **Unified series view** — VOD episodes and personal library episodes merge into one Jellyfin entry. Fill in missing episodes from a VOD series with per-episode selection.
- **Following** — track series and automatically get new episodes as they release. Following tab in Library shows everything you're tracking.
- **Episode management** — per-episode control over what's monitored, with smart counts that distinguish aired episodes from upcoming ones

### ⚡ Lightweight
- Single Docker container — FastAPI + SQLite + APScheduler
- No external databases, message queues, or caches required
- Vanilla HTML/JS frontend — no build step, no framework bloat
- Built-in TMDB API key — zero-config metadata out of the box

---

## Quick Start

Add Tentacle to your existing `docker-compose.yml` — replace the paths on the left side of each volume mount with your own:

```yaml
tentacle:
  image: ghcr.io/lucas-romanenko/jellyfin-tentacle:latest
  container_name: tentacle
  ports:
    - 8888:8888
  volumes:
    - ./tentacle-data:/data                        # Required — database and config
    - /your/movies:/media/movies                   # Radarr movies library
    - /your/shows:/media/shows                     # Sonarr TV library
    - /your/vod-movies:/media/vod/movies           # IPTV VOD movies
    - /your/vod-shows:/media/vod/shows             # IPTV VOD series
  restart: unless-stopped
```

```bash
docker compose up -d
```

Open `http://localhost:8888` — the setup wizard will guide you through connecting Jellyfin. Only a Jellyfin URL and API key are required. TMDB metadata works out of the box with a built-in key.

> **Volume notes:**
> - `./tentacle-data:/data` is the only required volume. The other four are optional depending on which features you use.
> - Only mount what you need — skip `/media/movies` and `/media/shows` if you don't use Radarr/Sonarr, skip `/media/vod/...` if you don't have an IPTV provider.
> - The right side of each mount (`/data`, `/media/...`) is fixed — don't change these. Only change the left side to match your host paths.
> - Settings → Library Paths shows which mounts Tentacle can see. Green = mounted, red = missing.

### After Starting

1. **Jellyfin** — URL + API key (Dashboard → API Keys → Create) — *required*
2. **TMDB** — works automatically with built-in key, or override with your own from [themoviedb.org](https://www.themoviedb.org/settings/api)
3. **IPTV Provider** — add via the VOD page (optional)
4. **Radarr / Sonarr** — URL + API key in Settings → Connections (optional). Set up webhooks in Radarr/Sonarr pointing to `http://<tentacle-ip>:8888/api/radarr/webhook` and `/api/sonarr/webhook` for real-time updates.
5. **Check paths** — Settings → Library Paths to verify your volume mounts are correct
6. **Playlists** — go to Jellyfin → Playlists tab to enable auto playlists from your synced content
7. **Home Screen** — go to Jellyfin → Home Screen tab to set up the hero spotlight and playlist rows

> **First-time users with existing VOD files:** If Tentacle detects `.strm` files in your VOD folders from a previous tool (xtream-sync, etc.), it will offer to clean them up so you can start fresh with proper metadata and tagging.

---

## Components

Tentacle is a monorepo with two components:

| Component | What it is | Where it runs |
|-----------|-----------|---------------|
| **Dashboard** | FastAPI web app (Python) | Docker container (port 8888) |
| **Plugin** | Jellyfin plugin (C#) | Inside Jellyfin's plugin directory |

The dashboard works standalone. The plugin adds the custom home screen, hero spotlight, Discover tab, and Activity tab inside Jellyfin's web UI.

### Install the Plugin

1. In Jellyfin, go to **Dashboard → Plugins → Repositories → Add**
2. Paste the repository URL:
   ```
   https://raw.githubusercontent.com/lucas-romanenko/jellyfin-tentacle/main/tentacle-plugin/manifest.json
   ```
3. Go to **Catalog**, find **Tentacle**, click **Install**
4. Restart Jellyfin
5. Configure: **Dashboard → Plugins → Tentacle**
   - Set **Tentacle URL** to your dashboard address (e.g. `http://192.168.1.100:8888`)

### Android TV App

Get the Tentacle Android TV client from [jellyfin-tentacle-androidtv](https://github.com/lucas-romanenko/jellyfin-tentacle-androidtv). It brings the same hero spotlight, Discover tab, and Activity tracking to Android TV and Fire TV devices. Download the latest APK from [Releases](https://github.com/lucas-romanenko/jellyfin-tentacle-androidtv/releases) and sideload it — coexists with the official Jellyfin app.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   Jellyfin                       │
│  ┌─────────────────────────────────────────┐    │
│  │  Tentacle Plugin                        │    │
│  │  • Custom home screen rows & hero       │    │
│  │  • Discover tab (trending/popular)      │    │
│  │  • Activity tab (progress/upcoming)     │    │
│  │  • Injects CSS/JS via Harmony patch     │    │
│  └──────────────────┬──────────────────────┘    │
│                     │ fetches config via API      │
└─────────────────────┼───────────────────────────┘
                      │
            Jellyfin API (tags, playlists,
             library scan, HDHomeRun tuner)
                      │
┌─────────────────────┴───────────────────────────┐
│              Tentacle Dashboard                  │
│  • Multi-user auth (per-user playlists & home)  │
│  • VOD sync & NFO generation                    │
│  • Live TV with HDHomeRun emulation             │
│  • Smart playlists & tag engine                 │
│  • Content discovery (TMDB)                     │
│  • Activity tracking & progress monitoring       │
│  • Series following & episode management        │
│  • Radarr & Sonarr webhook integration          │
└──────┬──────────┬──────────┬───────────┬────────┘
       │          │          │           │
    IPTV       TMDB       Radarr      Sonarr
   Provider    API         API         API
```

---

## Contributing

Contributions are welcome! Please open an issue first to discuss what you'd like to change.

1. Fork the repo
2. Create your branch (`git checkout -b feature/my-feature`)
3. Commit your changes
4. Push and open a PR

---

## License

[MIT](LICENSE)

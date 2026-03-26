# Jellyfin Tentacle

**The all-in-one media manager for Jellyfin.**

<!-- Banner coming soon -->

[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Jellyfin](https://img.shields.io/badge/Jellyfin-10.10+-00A4DC?logo=jellyfin&logoColor=white)](https://jellyfin.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## What is Tentacle?

Tentacle is a unified dashboard and Jellyfin plugin that brings IPTV, VOD, smart playlists, content discovery, and home screen customization into a single app. It replaces Threadfin, manual tagging scripts, and fragmented tools — giving you one place to manage everything that shows up in Jellyfin.

<!-- Screenshots coming soon -->

---

## Features

### 📺 Live TV & IPTV
- Add your IPTV provider (Xtream, M3U URL, or M3U file)
- Browse and enable channel groups — only sync what you want
- Full EPG program guide with automatic refresh
- Built-in HDHomeRun emulation — Jellyfin sees Tentacle as a native tuner
- Stream proxy handles provider redirects and HLS-to-MPEG-TS conversion

### 🎬 VOD & Library
- Provider movies and series appear in Jellyfin with posters, metadata, and proper titles
- TMDB matching ensures clean, accurate library entries
- `.strm` + `.nfo` files written automatically — no manual work
- English-language filtering with smart country prefix detection

### 🏷️ Smart Playlists & Tags
- Auto-tag content by source: "Netflix Movies", "HBO MAX Series", etc.
- Create custom playlists with filters: genre, rating, year, runtime, source
- Native Jellyfin queries for genre/rating playlists — sees your entire library, not just IPTV content
- Auto-generated playlist artwork via Logo.dev

### 🔍 Content Discovery
- Trending, popular, and upcoming titles from TMDB
- Subscribe to IMDb, Trakt, and Letterboxd lists
- See what's missing from your library and add to Radarr/Sonarr in one click
- Discover tab injected directly into Jellyfin via the companion plugin

### 🏠 Custom Home Screen
- Drag-and-drop row ordering for the Jellyfin home page
- Hero spotlight with backdrop and logo images
- Mix playlist rows with native Jellyfin sections (Continue Watching, Next Up, etc.)
- Independent sort options per row and for the hero

### 📥 Radarr & Sonarr Integration
- Automatic library scanning via webhooks — new downloads tagged and organized instantly
- NFO files written for all downloaded content
- Tags pushed to Jellyfin via API for `.mkv` files (Jellyfin ignores NFO tags for real video files)
- Duplicate detection when Radarr downloads content that already exists as VOD

### ⚡ Lightweight
- Single Docker container — FastAPI + SQLite + APScheduler
- No external databases, message queues, or caches required
- Vanilla HTML/JS frontend — no build step, no framework bloat

---

## Quick Start

```yaml
# docker-compose.yml
services:
  tentacle:
    image: ghcr.io/lucas-romanenko/jellyfin-tentacle:latest
    container_name: tentacle
    ports:
      - 8888:8888
    volumes:
      - ./data:/data                                          # Database, config, cache
      - /path/to/jellyfin/smartlists:/mnt/jellyfin/smartlists # Jellyfin smart playlists
      - /path/to/media/vod:/mnt/media/vod                    # IPTV VOD content
      - /path/to/media/movies:/mnt/media/movies              # Radarr library
      - /path/to/media/tv:/mnt/media/tv                      # Sonarr library
    restart: unless-stopped
```

```bash
docker compose up -d
```

Open `http://localhost:8888` and configure your connections in **Settings**:
1. **Jellyfin** — URL and API key
2. **TMDB** — Bearer token ([get one here](https://www.themoviedb.org/settings/api))
3. **IPTV Provider** — server URL, username, password
4. **Radarr / Sonarr** — URL and API key (optional)

---

## Plugin Installation

Tentacle is a monorepo with two components:

| Component | What it is | Where it runs |
|-----------|-----------|---------------|
| **Dashboard** | FastAPI web app | Docker container (port 8888) |
| **Plugin** | Jellyfin plugin (C#) | Inside Jellyfin's plugin directory |

The dashboard works standalone, but the plugin is needed for the custom home screen, hero spotlight, and Discover tab inside Jellyfin.

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

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   Jellyfin                       │
│  ┌─────────────────────────────────────────┐    │
│  │  Tentacle Plugin                        │    │
│  │  • Custom home screen rows & hero       │    │
│  │  • Discover tab (trending/popular)      │    │
│  │  • Smart playlist refresh               │    │
│  └──────────────────┬──────────────────────┘    │
│                     │ reads tentacle-home.json   │
└─────────────────────┼───────────────────────────┘
                      │
            Jellyfin API (tags, playlists,
             library scan, HDHomeRun tuner)
                      │
┌─────────────────────┴───────────────────────────┐
│              Tentacle Dashboard                  │
│  • VOD sync & NFO generation                    │
│  • Live TV / IPTV management                    │
│  • Smart playlists & tag engine                 │
│  • Content discovery (TMDB)                     │
│  • Radarr & Sonarr integration                  │
└──────┬──────────┬──────────┬───────────┬────────┘
       │          │          │           │
    IPTV       TMDB      Radarr      Sonarr
   Provider    API       /Sonarr      API
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

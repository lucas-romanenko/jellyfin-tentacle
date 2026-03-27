# Jellyfin Tentacle

**Turn Jellyfin into a full-featured streaming platform.**

<!-- Banner coming soon -->

[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Jellyfin](https://img.shields.io/badge/Jellyfin-10.10+-00A4DC?logo=jellyfin&logoColor=white)](https://jellyfin.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## What is Tentacle?

Tentacle is a dashboard + Jellyfin plugin that adds IPTV live TV, VOD libraries from your IPTV provider, smart playlists, a Netflix-style home screen with hero spotlight, and content discovery — all from a single Docker container. No databases, no message queues, no build steps.

Add your IPTV provider and your Jellyfin home screen fills with curated rows, a hero banner, and a Discover tab showing what's trending. Connect Radarr and Sonarr, and downloads are automatically tagged, organized into playlists, and surfaced on the home screen.

<!-- Screenshots coming soon -->

---

## Features

### 📺 Live TV & IPTV
- Add your IPTV provider (Xtream, M3U URL, or M3U file) — replaces Threadfin
- Browse and enable channel groups — only sync what you want
- Full EPG program guide with automatic refresh
- Built-in HDHomeRun emulation — Jellyfin sees Tentacle as a native tuner
- Stream proxy handles provider redirects and HLS-to-MPEG-TS conversion

### 🎬 VOD Library from IPTV
- Your IPTV provider's movies and series appear in Jellyfin with posters, plots, and correct titles
- Automatic TMDB matching — no manual metadata cleanup
- `.strm` + `.nfo` files generated for each title
- English-language filtering with smart country/prefix detection

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

### 🏠 Netflix-Style Home Screen
- Drag-and-drop row ordering for the Jellyfin home page
- Hero spotlight banner with backdrop and logo images
- Mix playlist rows with native Jellyfin sections (Continue Watching, Next Up, etc.)
- Independent sort options per row and for the hero carousel

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

### Minimum Setup (Jellyfin only)

If you just want to get Tentacle running and explore the UI:

```yaml
# docker-compose.yml
services:
  tentacle:
    image: ghcr.io/lucas-romanenko/jellyfin-tentacle:latest
    container_name: tentacle
    ports:
      - 8888:8888
    volumes:
      - ./tentacle-data:/data    # Database, config, TMDB cache
    restart: unless-stopped
```

```bash
docker compose up -d
```

Open `http://localhost:8888` — the setup wizard will guide you through connecting Jellyfin. Only a Jellyfin URL and API key are required. TMDB metadata works out of the box with a built-in key.

### Full Setup (VOD + Radarr/Sonarr)

To use IPTV VOD sync, Radarr/Sonarr integration, or smart playlists, mount the media paths:

```yaml
services:
  tentacle:
    image: ghcr.io/lucas-romanenko/jellyfin-tentacle:latest
    container_name: tentacle
    ports:
      - 8888:8888
    volumes:
      - ./tentacle-data:/data                      # Database, config, TMDB cache, smartlists (required)
      - /path/to/media/vod:/mnt/media/vod          # Where VOD .strm files will be created
      - /path/to/media/movies:/mnt/media/movies    # Same folder Radarr downloads to
      - /path/to/media/tv:/mnt/media/tv            # Same folder Sonarr downloads to
    restart: unless-stopped
```

The Tentacle plugin communicates with the dashboard via its API — no shared volumes needed between Jellyfin and Tentacle containers. Just configure the Tentacle URL in the plugin settings.

> **Volume notes:**
> - `./tentacle-data:/data` is the only required volume. Everything else is optional depending on which features you use.
> - `/mnt/media/vod` — only needed if you add an IPTV provider for VOD content. Point this to a folder inside a Jellyfin library.
> - `/mnt/media/movies` and `/mnt/media/tv` — mount the same paths your Radarr/Sonarr containers use, so Tentacle can write NFO files alongside downloads.
> - Paths are hardcoded defaults inside the container — just make sure your docker-compose volume mounts point to the right places.

### After Starting

1. **Jellyfin** — URL + API key (Dashboard → API Keys → Create) — *required*
2. **TMDB** — works automatically with built-in key, or override with your own from [themoviedb.org](https://www.themoviedb.org/settings/api)
3. **IPTV Provider** — add via the VOD page (optional)
4. **Radarr / Sonarr** — URL + API key in Settings → Connections (optional)

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
│  • VOD sync & NFO generation                    │
│  • Live TV with HDHomeRun emulation             │
│  • Smart playlists & tag engine                 │
│  • Content discovery (TMDB)                     │
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

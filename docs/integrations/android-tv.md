# Android TV App

Tentacle has a custom Android TV client that brings the full Tentacle experience to your TV — hero spotlight, Discover tab, Activity tracking, and quality profile selection.

## Installation

1. Download the latest APK from [GitHub Releases](https://github.com/lucas-romanenko/jellyfin-tentacle-androidtv/releases)
2. Sideload it onto your Android TV or Fire TV device
3. The app appears as "Tentacle" in your launcher

!!! info "Coexists with official app"
    The Tentacle app uses a different package ID (`org.jellyfin.tentacle`) so it can be installed alongside the official Jellyfin Android TV app.

## Features

### Hero Spotlight

A cinematic banner at the top of the home screen that cycles through featured content:

- Auto-rotates every 8 seconds
- D-pad navigation between hero items
- Ken Burns zoom effect on backdrop images
- Smooth crossfade transitions

### Home Screen

Your Tentacle-configured home rows appear below the hero:

- All your enabled playlists as scrollable rows
- Native Jellyfin sections (Continue Watching, Next Up, etc.)
- Parallel loading — all playlist items pre-fetched concurrently for fast display

### Discover Tab

Browse TMDB content directly from your TV:

- Trending, popular, and upcoming sections
- Search functionality
- Full detail views with metadata
- Add to Radarr/Sonarr with quality profile selection
- Cycling quality profile button (persists last selection)

### Activity Tab

A dedicated tab in the top toolbar:

- **Download cards** — Poster, status badge, progress bar, ETA, quality
- **Unreleased cards** — Poster with countdown badge (days until release)
- Polls every 3 seconds for live updates
- Purple accent colors throughout

### Episode Picker

When adding series to Sonarr, the same episode picker available in the web dashboard:

- VOD episodes (green badge) and downloaded episodes (purple badge)
- Season coverage counts
- Selective episode monitoring

## Setup

1. Launch the Tentacle app
2. Enter your Jellyfin server address
3. Log in with your Jellyfin account

The app automatically connects to the Tentacle plugin running inside your Jellyfin server. No separate Tentacle URL configuration needed — the app communicates through the plugin endpoints.

## Graceful Fallback

If the Tentacle plugin is unavailable on your Jellyfin server, the app falls back to the standard Jellyfin Android TV home experience. You'll see the default home sections instead of Tentacle's custom layout.

## Requirements

- Android TV or Fire TV device
- Jellyfin 10.11 or later
- Tentacle plugin installed in Jellyfin

## Building from Source

The Android TV app is in a separate repository: [jellyfin-tentacle-androidtv](https://github.com/lucas-romanenko/jellyfin-tentacle-androidtv)

Releases are automated via GitHub Actions:

```bash
git tag v1.0.0
git push origin v1.0.0
```

This triggers a build that creates a release APK attached to a GitHub Release.

### Tech Stack

- Kotlin + Jetpack Compose + Leanback
- Jellyfin SDK 1.8.6
- Media3/ExoPlayer 1.9.3
- Koin for dependency injection

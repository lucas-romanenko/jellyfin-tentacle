# Jellyfin Plugin

The Tentacle plugin enhances Jellyfin's web interface with a custom home screen, hero spotlight banner, Discover tab, and Activity tab. It communicates with the Tentacle dashboard via API to render per-user content.

## Installation

1. In Jellyfin, go to **Dashboard → Plugins → Repositories**
2. Click **Add** and paste:
   ```
   https://raw.githubusercontent.com/lucas-romanenko/jellyfin-tentacle/main/tentacle-plugin/manifest.json
   ```
3. Go to **Catalog**, find **Tentacle**, click **Install**
4. **Restart Jellyfin**
5. Go to **Dashboard → Plugins → Tentacle** and set the **Tentacle URL** (e.g., `http://192.168.1.100:8888`)

!!! warning "Restart required"
    The plugin won't be active until you restart Jellyfin. After restart, refresh your browser to see the changes.

## What the Plugin Adds

### Custom Home Screen

The plugin replaces the default Jellyfin home page with your Tentacle-configured layout:

- **Hero spotlight banner** — Large cinematic banner at the top that cycles through featured content with Ken Burns zoom and crossfade transitions
- **Playlist rows** — Your enabled playlists shown as horizontal scrollable rows
- **Native sections** — Continue Watching, Next Up, and other Jellyfin sections mixed in

The layout is fetched from Tentacle's API per user, so each Jellyfin user sees their own configured home screen.

### Discover Tab

A browsing tab injected into the Jellyfin home page:

- **Trending** — What's popular on TMDB right now
- **Popular** — Most popular movies and series
- **Upcoming** — Titles not yet released
- **Search** — Find anything on TMDB
- Status badges (In Library, Downloading, Add)
- One-click add to Radarr/Sonarr with quality profile selection

### Activity Tab

A real-time status tab showing:

- **Active downloads** — Progress bars, ETA, quality info
- **Unreleased content** — Countdown badges for titles you're waiting for
- Purple badge count in the tab header

The Activity tab polls every 3 seconds for live updates.

## Configuration

The only required setting is the **Tentacle URL** — the address where your Tentacle dashboard is running.

1. Go to **Jellyfin Dashboard → Plugins → Tentacle**
2. Set **Tentacle URL** (e.g., `http://192.168.1.100:8888`)
3. Save

!!! tip "Use internal addresses"
    If Jellyfin and Tentacle are on the same network, use the internal IP address. Don't use a Cloudflare tunnel or external URL for the plugin connection — it should be local network traffic.

## How It Works

### Harmony Patching

The plugin uses Harmony (a .NET patching library) to inject custom JavaScript and CSS into Jellyfin's `index.html`. This is how the home screen, Discover tab, and Activity tab are added without modifying Jellyfin's source code.

### API Communication

The plugin acts as a proxy between the Jellyfin web frontend and the Tentacle backend:

```
Browser JS → Plugin Endpoints → Tentacle API
```

Plugin API endpoints:

| Endpoint | Purpose |
|----------|---------|
| `GET /TentacleHome/Sections` | Home screen rows + built-in sections |
| `GET /TentacleHome/Hero` | Hero spotlight items |
| `GET /TentacleHome/HeroConfig` | Hero configuration |
| `GET /TentacleDiscover/Items` | Trending/popular/upcoming from TMDB |
| `GET /TentacleDiscover/Search` | TMDB search |
| `GET /TentacleDiscover/Activity` | Real-time download queue |
| `POST /TentacleDiscover/AddToRadarr` | Add movie to Radarr |
| `POST /TentacleDiscover/AddToSonarr` | Add series to Sonarr |
| `POST /Tentacle/Refresh` | Clear all plugin caches |

### Caching

- **Home config** — Cached for 5 seconds per user
- **Discover data** — Trending/popular cached for 30 minutes
- **Discover config** — Cached for 5 minutes
- **Activity** — Never cached (always fresh)

Caches are cleared when `POST /Tentacle/Refresh` is called (triggered automatically by Tentacle after settings changes).

### Per-User Content

The plugin forwards the Jellyfin user ID with every request to the Tentacle API. This is how each user gets their own home screen, playlists, and content.

## Updating

Plugin updates are automatic:

1. Tag a new release in the Tentacle repo: `git tag plugin-v2.x.x && git push origin plugin-v2.x.x`
2. GitHub Action builds the plugin, creates a release, and updates the manifest
3. Jellyfin auto-discovers the update from the manifest URL
4. Restart Jellyfin to install the new version

You can also manually check for updates in Jellyfin → Dashboard → Plugins → Catalog.

## Graceful Fallback

If the Tentacle dashboard is unreachable, the plugin falls back gracefully to the standard Jellyfin home screen. No error pages — you just see the default Jellyfin layout until the connection is restored.

## Troubleshooting

!!! warning "Home screen not showing"
    1. Verify the plugin is installed: Dashboard → Plugins → My Plugins → Tentacle
    2. Check the Tentacle URL is correct in plugin settings
    3. Restart Jellyfin after installing or updating
    4. Hard refresh your browser (Ctrl+Shift+R) to clear cached JS/CSS

!!! warning "Discover tab not appearing"
    The Discover tab must be enabled in Tentacle: go to Jellyfin → Discover and toggle "Show in Jellyfin". The plugin re-checks this setting on every page visit.

!!! warning "Add to Radarr/Sonarr fails in Jellyfin"
    The plugin passes the Jellyfin user ID to Tentacle for admin verification. Make sure the logged-in Jellyfin user has admin permissions in Tentacle.

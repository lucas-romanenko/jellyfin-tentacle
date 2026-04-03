# Setup Wizard

When you first open Tentacle, the setup wizard guides you through the essential configuration. Only one thing is truly required — connecting to Jellyfin. Everything else is optional and can be configured later.

## Step 1: Connect to Jellyfin

Enter your Jellyfin server details:

- **Jellyfin URL** — The address of your Jellyfin server (e.g., `http://192.168.1.100:8096`)
- **API Key** — Generate one in Jellyfin → Dashboard → API Keys → Create

Click **Test Connection** to verify. You should see a green "Connected" status.

!!! tip "Docker networking"
    If Jellyfin and Tentacle are on the same Docker network, you can use the container name: `http://jellyfin:8096`

## Step 2: TMDB (Automatic)

Tentacle ships with a built-in TMDB API key — metadata works out of the box with zero configuration. You'll see a green checkmark next to TMDB automatically.

If you prefer to use your own TMDB key, you can override it in Settings later. Get one free at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api).

## Step 3: Radarr & Sonarr (Optional)

If you use Radarr and/or Sonarr for downloading content:

- **URL** — Your Radarr/Sonarr address (e.g., `http://192.168.1.100:7878`)
- **API Key** — Found in Radarr/Sonarr → Settings → General → API Key

Click **Test** to verify each connection.

!!! info "Post-setup scan"
    When you finish the wizard with Radarr or Sonarr configured, Tentacle automatically scans their libraries in the background. Your existing content will appear in the Library page within a few minutes.

## Step 4: Check Library Paths

After completing the wizard, go to **Settings → Library Paths** to verify your volume mounts:

| Path | Status | What it means |
|------|--------|---------------|
| `/data` | :material-check-circle:{ .green } Green | Database and config volume mounted correctly |
| `/media/movies` | :material-check-circle:{ .green } Green | Radarr library accessible |
| `/media/shows` | :material-check-circle:{ .green } Green | Sonarr library accessible |
| `/media/vod/movies` | :material-close-circle:{ .red } Red | VOD movies path not mounted (OK if you don't use a provider) |

Red paths mean the volume isn't mounted in your Docker Compose. This is fine if you don't use that feature.

## What Happens After Setup

Once Jellyfin is connected, Tentacle:

1. **Creates your user account** — the first Jellyfin admin to log in becomes the Tentacle owner
2. **Scans Radarr/Sonarr** (if configured) — imports your existing library
3. **Shows the dashboard** — you're ready to add providers, configure playlists, and customize your home screen

## Next Steps

After the wizard, here's the recommended order:

1. **Add a streaming provider** — Go to the VOD page to add an IPTV provider for on-demand content, or the Live TV page for channels
2. **Enable playlists** — Go to Jellyfin → Playlists to toggle on auto-generated playlists
3. **Customize your home screen** — Go to Jellyfin → Home Screen to set up hero spotlight and playlist rows
4. **Install the plugin** — [Install the Tentacle Jellyfin plugin](../integrations/jellyfin-plugin.md) to see the custom home screen, Discover tab, and Activity tab inside Jellyfin
5. **Set up webhooks** — Configure [Radarr](../integrations/radarr.md) and [Sonarr](../integrations/sonarr.md) webhooks for real-time library updates

---

Next: [Docker Compose Examples →](docker-compose.md)

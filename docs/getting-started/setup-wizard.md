# Setup Wizard

When you first open Tentacle, the setup wizard guides you through the essential configuration in six steps. Steps 1 and 2 are required: connecting to Jellyfin and signing in with a Jellyfin administrator account. Everything after that is optional and can be configured later in Settings.

## Step 1: Connect Jellyfin

Enter your Jellyfin server details:

- **Jellyfin URL** — The address of your Jellyfin server (e.g., `http://192.168.1.100:8096`)
- **API Key** — Generate one in Jellyfin → Dashboard → API Keys → +

Click **Test Connection** to verify. You should see "Jellyfin 10.11.x connected" (your Jellyfin version). Click **Next**.

!!! tip "Docker networking"
    If Jellyfin and Tentacle are on the same Docker network, you can use the container name: `http://jellyfin:8096`

## Step 2: Login to Jellyfin

Sign in with your Jellyfin **username** and **password**. This account becomes the Tentacle owner, so it must be a Jellyfin administrator: a non-admin account is refused here. Other Jellyfin users sign in later with their own accounts (see [Multi-User](../features/multi-user.md)).

Click **Login & Continue**.

## Step 3: Radarr & Sonarr (Optional)

If you use Radarr and/or Sonarr for downloading content:

- **URL** — Your Radarr/Sonarr address (e.g., `http://192.168.1.100:7878`)
- **API Key** — Found in Radarr/Sonarr → Settings → General → API Key

Click **Test Radarr** / **Test Sonarr** to verify each connection. Once connected, pick a **Default quality profile**: every request made from Tentacle uses it. You can change it later in Settings → Connections.

Don't use Radarr or Sonarr? Click **Skip — I don't use Radarr/Sonarr** to go straight to step 5.

!!! info "Post-setup scan"
    When you continue with Radarr or Sonarr configured, Tentacle scans their libraries in the background. Your existing content appears on the Library page within a few minutes.

## Step 4: Webhook Setup

Shown only when you configured Radarr or Sonarr. Enter the **Tentacle URL**, the address Radarr and Sonarr can reach Tentacle at (e.g., `http://192.168.1.100:8888`). The wizard shows the webhook URL for each app with a **Copy** button:

- Radarr: `http://<tentacle-ip>:8888/api/radarr/webhook`
- Sonarr: `http://<tentacle-ip>:8888/api/sonarr/webhook`

Add each one in Radarr/Sonarr under **Settings → Connect → + → Webhook**, with the triggers the wizard lists. Details: [Radarr](../integrations/radarr.md), [Sonarr](../integrations/sonarr.md).

## Step 5: Optional Services

All three are optional:

- **TMDB Bearer Token** — Leave it empty: Tentacle ships with a built-in TMDB key, so metadata works out of the box. Use your own from [themoviedb.org](https://www.themoviedb.org/settings/api) only if you want to (Settings → Connections later).
- **Trakt Client ID** — For subscribing to Trakt lists (Settings → Integrations later).
- **Logo.dev API Key** — For auto-generated playlist artwork (Settings → Integrations later).

Click **Get Started** to save and go to step 6, or **Skip everything — I'll configure later** to close the wizard right away.

## Step 6: Install the Jellyfin Plugin

The wizard shows the plugin's manifest URL with a **Copy** button and the steps to install it. The plugin adds the custom home screen, Discover tab and Activity tab to Jellyfin. It's optional but recommended: [Jellyfin Plugin](../integrations/jellyfin-plugin.md) has the full instructions.

Click **Done**, or **Skip — I'll install later**.

## Check Library Paths

Library Paths is not part of the wizard. After it, go to **Settings → Library Paths** to verify your volume mounts:

| Path | Status | What it means |
|------|--------|---------------|
| `/data` | :material-check-circle:{ .green } Green | Database and config volume mounted correctly |
| `/media/movies` | :material-check-circle:{ .green } Green | Radarr library accessible |
| `/media/shows` | :material-check-circle:{ .green } Green | Sonarr library accessible |
| `/media/vod/movies` | :material-close-circle:{ .red } Red | VOD movies path not mounted (OK if you don't use a provider) |

Red paths mean the volume isn't mounted in your Docker Compose. This is fine if you don't use that feature.

## Next Steps

After the wizard, here's the recommended order:

1. **Add a streaming provider** — Go to **Settings → Providers** and click **+ Add Provider**. One provider serves both VOD and Live TV: when its account has live channels, Live TV is switched on for it automatically (see [Live TV](../features/live-tv.md))
2. **Enable playlists** — Go to Jellyfin → Playlists to toggle on auto-generated playlists
3. **Customize your home screen** — Go to Jellyfin → Home Screen to set up hero spotlight and playlist rows
4. **Install the plugin** — If you skipped step 6, [install the Tentacle Jellyfin plugin](../integrations/jellyfin-plugin.md)

---

Next: [Docker Compose Examples →](docker-compose.md)

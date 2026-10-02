# Setup Wizard

When you first open Tentacle, the setup wizard guides you through the essential configuration. Two steps are required: connecting to Jellyfin and signing in with your Jellyfin administrator account. Everything else is optional and can be configured later.

The wizard has six steps.

## Step 1: Connect Jellyfin

Enter your Jellyfin server details:

- **Jellyfin URL**: the address of your Jellyfin server (e.g., `http://192.168.1.100:8096`)
- **API Key**: generate one in Jellyfin → Dashboard → API Keys → Create

Click **Test Connection** to verify. You should see your Jellyfin version followed by "connected" (e.g., "Jellyfin 10.11.8 connected"). Then click **Next**.

!!! tip "Docker networking"
    If Jellyfin and Tentacle are on the same Docker network, you can use the container name: `http://jellyfin:8096`

## Step 2: Login to Jellyfin

Sign in with your main Jellyfin **administrator** account (**Jellyfin Username** and **Jellyfin Password**), then click **Login & Continue**.

The first account to sign in becomes the Tentacle **owner**, so use an administrator here. Other Jellyfin users can sign in later; see [Multi-User](../features/multi-user.md).

## Step 3: Radarr & Sonarr (Optional)

If you use Radarr and/or Sonarr for downloading content:

- **URL**: your Radarr/Sonarr address (e.g., `http://192.168.1.100:7878`)
- **API Key**: found in Radarr/Sonarr → Settings → General → API Key

Click **Test Radarr** / **Test Sonarr** to verify each connection. Once a test passes, a **Default quality profile** list appears: pick the profile Tentacle uses when it sends a title to Radarr or Sonarr.

Don't use them? Click **Skip — I don't use Radarr/Sonarr** to go straight to step 5.

!!! info "Library scan"
    When you click **Next** with Radarr or Sonarr filled in, Tentacle starts scanning their libraries in the background. Your existing content will appear in the Library page within a few minutes.

## Step 4: Webhook Setup

Shown only when you entered Radarr or Sonarr in step 3. Webhooks let Radarr and Sonarr tell Tentacle when something new has downloaded.

**Tentacle URL** is filled in with the address you opened the dashboard on. Change it if Radarr and Sonarr reach Tentacle at a different address. The wizard then shows the **Radarr Webhook URL** and/or **Sonarr Webhook URL** with a **Copy** button. Add each one in that app under **Settings → Connect → Webhook**. More detail: [Radarr](../integrations/radarr.md), [Sonarr](../integrations/sonarr.md).

## Step 5: Optional Services

All optional:

- **TMDB Bearer Token**: Tentacle ships with a built-in TMDB key, so metadata works out of the box. Enter your own token only if you prefer it. Get one free at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api).
- **Trakt Client ID**
- **Logo.dev API Key**

Click **Get Started** to save and finish the setup. Or click **Skip everything — I'll configure later**, which closes the wizard right away (without step 6).

## Step 6: Install the Jellyfin Plugin

The plugin adds a custom home screen and a Discover tab to Jellyfin. It's optional but recommended. The step shows the **Plugin Manifest URL** with a **Copy** button and the install steps. They are the same as in [Jellyfin Plugin](../integrations/jellyfin-plugin.md). Click **Done** (or **Skip — I'll install later**) to open the dashboard.

## After Setup

Go to **Settings → Library Paths** to verify your volume mounts:

| Path | Status | What it means |
|------|--------|---------------|
| `/data` | :material-check-circle:{ .green } Green | Database and config volume mounted correctly |
| `/media/movies` | :material-check-circle:{ .green } Green | Radarr library accessible |
| `/media/shows` | :material-check-circle:{ .green } Green | Sonarr library accessible |
| `/media/vod/movies` | :material-close-circle:{ .red } Red | VOD movies path not mounted (OK if you don't use a provider) |

Red paths mean the volume isn't mounted in your Docker Compose. This is fine if you don't use that feature.

Everything from the wizard can be changed later: Jellyfin, Radarr, Sonarr and TMDB in **Settings → Connections**; Trakt, Logo.dev and the webhook URLs in **Settings → Integrations**.

## Next Steps

After the wizard, here's the recommended order:

1. **Add a streaming provider**: go to **Settings → Providers** and click **+ Add Provider**. One provider serves both VOD and [Live TV](../features/live-tv.md): pick its categories on the **VOD** page and its channel groups on the **Live TV** page
2. **Enable playlists**: go to Jellyfin → Playlists to toggle on auto-generated playlists
3. **Customize your home screen**: go to Jellyfin → Home Screen to set up hero spotlight and playlist rows
4. **Install the plugin** (if you skipped step 6): [Install the Tentacle Jellyfin plugin](../integrations/jellyfin-plugin.md) to see the custom home screen, Discover tab, and Activity tab inside Jellyfin

---

Next: [Docker Compose Examples →](docker-compose.md)

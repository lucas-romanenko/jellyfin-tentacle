# Troubleshooting

Common issues and how to resolve them.

## Connection Issues

### "Cannot connect to Jellyfin" on login

- Verify the Jellyfin URL in Settings is correct and reachable
- Check that the Jellyfin API key is valid (Dashboard → API Keys)
- If using Docker networking, make sure the container name is correct
- The Settings page shows connection status badges — green means connected, red means unreachable

### Settings page shows red connection badges

Each service (Jellyfin, Radarr, Sonarr, TMDB) shows a green/red status:

- **Red** — URL is wrong, API key is invalid, or the service is unreachable
- **Green** — Connected successfully

Double-check the URL and API key for any red service. If using Docker container names, ensure they're on the same Docker network.

---

## Activity & Downloads

### Activity shows nothing while content is downloading

This is almost always a **category mismatch** between Radarr/Sonarr and your download client.

**Diagnosis:**

1. Open Radarr/Sonarr → Activity → Queue
2. If their queue is also empty, the download client category doesn't match

**Fix:**

1. In Radarr/Sonarr → Settings → Download Clients, note the category (e.g., `radarr`, `sonarr`)
2. In your download client (SABnzbd, qBittorrent, etc.), make sure that category exists
3. For SABnzbd: Settings → Categories → Add the category
4. For qBittorrent: The category is created automatically on first use

!!! tip "Quick check"
    If Radarr/Sonarr's own Activity → Queue shows the download with progress, but Tentacle doesn't — that's a different issue (likely Tentacle can't reach Radarr/Sonarr). Check the connection in Settings.

### Download progress stuck at 0%

Tentacle triggers `RefreshMonitoredDownloads` on Radarr/Sonarr every 5 seconds to get fresh progress. If progress stays at 0%, the download client may not be reporting progress correctly, or the download just started.

---

## Playlists

### Playlists not appearing in Jellyfin

1. Check that the Jellyfin API key is valid (Settings → green "Connected" badge)
2. Verify the playlist is toggled on (Jellyfin → Playlists page)
3. Check if the playlist has any matching content (empty playlists may not be created)
4. An invalid API key means playlists can't be created via the Jellyfin API

### Playlist content is wrong or outdated

Playlists are refreshed automatically but if something seems off:

1. Go to Jellyfin → Playlists
2. Toggle the playlist off and back on
3. This triggers a full re-sync of that playlist

### Custom playlist doesn't find all content

- **Genre/Rating/Year filters** query Jellyfin directly — they see all library content
- **Source/Source Tag filters** use Tentacle's tag system — they only see content Tentacle has tagged
- If you mixed both types, the query falls back to tag-based (which may be more restrictive)

---

## Home Screen

### Home screen not showing custom layout in Jellyfin

1. Verify the [Tentacle plugin](integrations/jellyfin-plugin.md) is installed
2. Check the Tentacle URL in plugin settings (Dashboard → Plugins → Tentacle)
3. Restart Jellyfin after installing the plugin
4. Hard refresh your browser (Ctrl+Shift+R)

### Home screen not updating after changes

The plugin caches home config for 5 seconds per user. After making changes:

1. Wait a few seconds
2. Refresh the Jellyfin page
3. If still not updated, clear plugin cache by visiting any page then returning to home

### Rows disappeared after a sync

If playlists were recreated during a sync (new Jellyfin playlist IDs), Tentacle remaps rows by name. If a playlist was renamed, the row may be lost. Re-add it from the Home Screen page.

---

## VOD / Streaming Provider

### Sync completes but no content appears in Jellyfin

1. Check that VOD volume mounts are correct: Settings → Library Paths should show green for `/media/vod/movies` and `/media/vod/shows`
2. Verify Jellyfin has a library pointing to the same folders
3. Run a Jellyfin library scan after VOD sync
4. Check if "Require TMDB Match" is enabled — unmatched content is skipped by default

### "No categories found" on provider

Click the Categories tab — Tentacle auto-fetches categories on first visit. If nothing loads:

1. Test the provider connection (click Test on the provider card)
2. Verify credentials are correct
3. Some providers may be temporarily unavailable

### VOD content missing metadata

If titles appear without posters or proper names:

- The TMDB match may have failed — check the provider's title naming
- Provider prefixes (NF -, AMZ -, etc.) are stripped automatically, but unusual prefixes may not be recognized
- Disable "Require TMDB Match" to see unmatched titles, then verify they exist on TMDB

---

## Live TV

### Channels not appearing in Jellyfin

1. Verify the HDHomeRun tuner is added in Jellyfin (Dashboard → Live TV → Tuner Devices)
2. Make sure channels are enabled in Tentacle (not just the groups)
3. Click **Refresh Guide** in Tentacle — this recreates the XMLTV listing provider in Jellyfin, forcing a full channel remap
4. Simply refreshing guide data alone won't pick up new channels

### No EPG data for channels

1. Run an EPG sync in Tentacle (usually auto-chains after channel sync)
2. In Jellyfin, refresh the TV guide
3. EPG badges in Tentacle show "Has EPG" / "No EPG" based on actual data — if "No EPG", the provider may not have guide data for that channel

### Streams not playing

1. Check if your provider requires a specific User-Agent — set it in the provider settings
2. Tentacle proxies streams and handles HLS-to-MPEG-TS conversion, but some streams may have compatibility issues
3. Try playing the stream URL directly in VLC to isolate the issue

---

## Plugin

### Discover tab not appearing in Jellyfin

1. Enable it in Tentacle: Jellyfin → Discover → toggle "Show in Jellyfin"
2. The plugin re-checks this setting on every page visit — just refresh Jellyfin
3. Verify the plugin is installed and Tentacle URL is configured

### "Add to Radarr/Sonarr" fails from Jellyfin Discover tab

The plugin passes the Jellyfin user ID for admin verification:

1. Make sure you're logged in as an admin user in Jellyfin
2. Check that the user has admin permissions in Tentacle (Settings → Users)
3. Verify Radarr/Sonarr connections in Tentacle Settings

### Plugin shows old version

1. Check for updates in Jellyfin → Dashboard → Plugins → Catalog
2. Restart Jellyfin to apply the update
3. Hard refresh browser (Ctrl+Shift+R) to clear cached JS/CSS

---

## Webhooks

### Webhooks not triggering

1. Verify the webhook URL uses the internal IP: `http://<tentacle-ip>:8888/api/radarr/webhook`
2. Don't use external/Cloudflare tunnel URLs — webhooks must reach Tentacle directly
3. Check that all required triggers are enabled in Radarr/Sonarr
4. Use the webhook test in Settings → Connections to verify

### Webhook test fails from browser

The test is proxied through the Tentacle backend to avoid mixed-content issues (HTTPS page → HTTP webhook). If the test still fails, Tentacle can't reach the webhook URL configured in Radarr/Sonarr.

---

## Docker & General

### Container won't start

Check logs:

```bash
docker logs tentacle
```

Common causes:

- Port 8888 already in use
- Volume mount paths don't exist on the host
- Permission issues on the data directory

### Database issues after update

The SQLite database at `/data/tentacle.db` is migrated automatically. If you encounter issues after an update:

1. Check logs for migration errors
2. As a last resort, back up and delete the database for a fresh start

### Stale files banner on dashboard

If Tentacle detects `.strm` files in VOD folders but has an empty database (e.g., migrating from another tool), it shows a banner. Either:

- **Delete and start fresh** — Recommended, lets Tentacle create properly tagged files
- **Dismiss** — Keeps old files (they won't have proper metadata/tags)

---

## Viewing Logs

```bash
# Follow logs in real time
docker logs -f tentacle

# Last 100 lines
docker logs --tail 100 tentacle

# Logs from the last hour
docker logs --since 1h tentacle
```

Look for `ERROR` or `WARNING` level messages for clues about issues.

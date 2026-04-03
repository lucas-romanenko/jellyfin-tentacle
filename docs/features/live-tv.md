# Live TV

Tentacle replaces tools like Threadfin as an HDHomeRun tuner emulator for Jellyfin. Add your IPTV provider and get live channels with full EPG guide data inside Jellyfin.

## How It Works

Tentacle emulates an HDHomeRun network tuner device. When Jellyfin discovers the tuner, it sees Tentacle as a native TV device — no third-party tools needed.

1. You add a Live TV provider in Tentacle
2. Tentacle fetches channel groups and their channels
3. You enable the groups you want
4. Tentacle serves channel data via HDHomeRun endpoints
5. Jellyfin discovers the tuner and loads your channels with EPG

```
Jellyfin  ←→  HDHomeRun API  ←→  Tentacle  ←→  IPTV Provider
              (discover.json)      (stream
               lineup.json)        proxy)
```

## Adding a Live TV Provider

Go to the **Live TV** page and add a provider. Live TV providers are separate from VOD providers — they use the same credentials format but are configured independently.

### Provider Types

=== "Xtream API"
    - **Server URL** — Your provider's server address
    - **Username** — Your account username
    - **Password** — Your account password

=== "M3U URL"
    - **M3U URL** — Direct link to your provider's M3U playlist

=== "M3U File"
    - **M3U File** — Path to a local M3U file

!!! info "Same provider, different configs"
    You can use the same provider for both VOD and Live TV — just add it separately on each page. VOD and Live TV use different provider entries even if the credentials are the same.

### User-Agent

Some providers require a specific User-Agent header to allow streaming. If your provider blocks connections, try setting the User-Agent in the provider settings (e.g., `TiviMate/4.7.0`).

## Channel Groups

After adding a provider, click **Sync** to fetch channel groups. Groups are organized by your provider (Sports, Entertainment, News, etc.) and show the channel count for each.

### Enabling Groups

Toggle on the groups you want. Only channels from enabled groups are served to Jellyfin. This lets you keep your channel list manageable.

!!! tip "Two-phase sync"
    Phase 1 (Sync Groups) fetches just the group list with channel counts — this is fast. Phase 2 (Sync Channels) fetches the actual channels for enabled groups only, then automatically chains into an EPG sync.

### Bulk Actions

You can enable or disable multiple groups at once, or filter groups by keyword to quickly find what you need.

## Channels

After syncing channels, you can:

- **Enable/disable** individual channels
- **Edit** channel names and numbers
- **See EPG status** — "Has EPG" or "No EPG" badges based on actual program data

### Stable Channel IDs

Tentacle uses your provider's stream ID as the channel number, not sequential numbering. This means channel numbers stay consistent even when you add or remove channels — your Jellyfin recordings and favorites won't break.

## EPG (Electronic Program Guide)

Tentacle downloads XMLTV guide data from your provider and stores it in the database. EPG data covers ALL provider channels (not just enabled ones), so when you enable a new channel, it already has EPG data available.

### EPG Refresh

- **Automatic** — EPG syncs automatically after channel sync (auto-chaining)
- **Manual** — Click "Sync EPG" to refresh guide data
- **Cache** — EPG data is cached on disk for 8 hours to avoid repeated downloads

### Shared EPG IDs

Multiple channels can share the same EPG data (e.g., an HD channel and its backup). Tentacle handles this automatically — program data is duplicated for all matching channels in the XMLTV output.

## Connecting to Jellyfin

### Add the Tuner

1. In Jellyfin, go to **Dashboard → Live TV → Tuner Devices → Add**
2. Select **HD Homerun** as the tuner type
3. Enter the Tentacle address: `http://<tentacle-ip>:8888`
4. Jellyfin will discover the device automatically

### Add the Guide Provider

1. In Jellyfin, go to **Dashboard → Live TV → TV Guide Data Providers → Add**
2. Select **XMLTV**
3. Enter the guide URL: `http://<tentacle-ip>:8888/api/live/xmltv.xml`
4. Save and click **Refresh Guide**

!!! tip "Automatic guide refresh"
    When you click "Refresh Guide" in Tentacle, it automatically deletes and recreates the XMLTV listing provider in Jellyfin (which forces a full channel remap), then triggers a guide refresh. This ensures new channels get properly mapped to EPG data.

### Verify

After setup, you should see your enabled channels in Jellyfin's Live TV section with program guide data. If channels are missing EPG, sync EPG in Tentacle and refresh the guide in Jellyfin.

## Stream Proxy

Tentacle proxies live streams between Jellyfin and your provider. This handles:

- **Provider redirects** — Follows redirect chains automatically
- **HLS to MPEG-TS conversion** — Converts HLS streams to MPEG-TS format for Jellyfin compatibility
- **User-Agent forwarding** — Uses the configured User-Agent when connecting to the provider

## HDHomeRun Endpoints

These endpoints are served automatically — you don't need to configure them manually:

| Endpoint | Purpose |
|----------|---------|
| `/hdhr/discover.json` | Device discovery |
| `/hdhr/lineup.json` | Channel lineup |
| `/hdhr/lineup_status.json` | Lineup scan status |
| `/hdhr/device.xml` | Device description |
| `/api/live/xmltv.xml` | XMLTV guide data |

## Troubleshooting

!!! warning "Channels missing after enable"
    If you enable new channel groups but they don't appear in Jellyfin, click "Refresh Guide" in Tentacle. This forces Jellyfin to re-discover all channels. Simply refreshing the guide data alone won't pick up new channels — the XMLTV listing provider needs to be recreated.

!!! warning "No EPG data"
    EPG badges in Tentacle ("Has EPG" / "No EPG") reflect actual program data in the database. If channels show "No EPG" after a fresh setup, run an EPG sync first. The auto-chain after channel sync usually handles this automatically.

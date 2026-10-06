# Live TV

Tentacle replaces tools like Threadfin as an HDHomeRun tuner emulator for Jellyfin. Add your IPTV provider and get live channels with full EPG guide data inside Jellyfin.

## How It Works

Tentacle emulates an HDHomeRun network tuner device. When Jellyfin discovers the tuner, it sees Tentacle as a native TV device — no third-party tools needed.

1. You add your IPTV provider in Tentacle (Settings → Providers)
2. Tentacle fetches the channel groups
3. You enable the groups you want
4. Tentacle serves channel data via HDHomeRun endpoints
5. Jellyfin discovers the tuner and loads your channels with EPG

```
Jellyfin  ←→  HDHomeRun API  ←→  Tentacle  ←→  IPTV Provider
              (discover.json)      (stream
               lineup.json)        proxy)
```

## Adding a Provider

There is no separate Live TV provider. Go to **Settings → Providers**, click **+ Add Provider** and choose **Xtream Codes** (server URL, username, password). The same provider serves VOD and Live TV.

When you save it, Tentacle tests the connection. If the account has live channels, Live TV is switched on for that provider and its channel groups are fetched automatically. The Live TV page then shows them; until a provider is added it says "No IPTV provider configured. Add a provider in Settings first."

!!! info "M3U providers"
    An M3U URL or M3U file provider added in Settings → Providers is used for VOD only. Live TV needs an Xtream Codes provider.

### User-Agent

Tentacle connects to your provider with a TiviMate User-Agent (`TiviMate/4.7.0 (Linux; Android 12)`), which most providers accept. The dashboard has no setting to change it.

## Channel Groups

Groups are fetched automatically when the provider is added. They are organized by your provider (Sports, Entertainment, News, etc.) and show the channel count for each. On the Live TV page's **Groups** tab, **Refresh** fetches the group list again (with no groups yet the button is **Sync Groups**).

### Enabling Groups

Toggle on the groups you want, then click **Save & Sync Channels**. Only channels from enabled groups are served to Jellyfin. This lets you keep your channel list manageable.

!!! tip "Two-phase sync"
    Phase 1 (fetching groups) gets just the group list with channel counts — this is fast. Phase 2 (**Save & Sync Channels**) fetches the actual channels for enabled groups only, then automatically chains into an EPG sync.

### Bulk Actions

You can enable or disable multiple groups at once, or filter groups by keyword to quickly find what you need.

## Channels

After syncing channels, the **Channels** tab lets you:

- **Enable/disable** individual channels (then click **Save Channels**)
- **Rename** a channel as Jellyfin's guide shows it
- **See EPG status** — "Has EPG" or "No EPG" badges based on actual program data; click a badge to set the guide id (XMLTV channel id) a channel takes its guide from

### Stable Channel IDs

Tentacle uses your provider's stream ID as the channel number, not sequential numbering. This means channel numbers stay consistent even when you add or remove channels — your Jellyfin recordings and favorites won't break.

## EPG (Electronic Program Guide)

Tentacle downloads XMLTV guide data from your provider and stores it in the database. EPG data covers ALL provider channels (not just enabled ones), so when you enable a new channel, it already has EPG data available.

### EPG Refresh

- **Automatic** — EPG syncs automatically after channel sync (auto-chaining)
- **Manual** — Click **Sync EPG** on the Channels tab to refresh guide data
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
    Save Tentacle's address on the Live TV page's **Jellyfin Setup** tab. Then, after every EPG sync, Tentacle refreshes Jellyfin's guide by itself: it re-creates the XMLTV listing provider in Jellyfin (which forces a full channel remap) and triggers a guide refresh. This ensures new channels get properly mapped to EPG data.

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
    If you enable new channel groups but they don't appear in Jellyfin, check that Tentacle's address is saved on the **Jellyfin Setup** tab, then click **Sync EPG** on the Channels tab: when the sync finishes, Tentacle re-creates the XMLTV listing provider in Jellyfin, which makes it re-discover all channels. Simply clicking "Refresh Guide" in Jellyfin alone won't pick up new channels.

!!! warning "No EPG data"
    EPG badges in Tentacle ("Has EPG" / "No EPG") reflect actual program data in the database. If channels show "No EPG" after a fresh setup, run an EPG sync first. The auto-chain after channel sync usually handles this automatically.

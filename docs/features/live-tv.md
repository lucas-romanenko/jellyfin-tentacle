# Live TV

Tentacle replaces tools like Threadfin as an HDHomeRun tuner emulator for Jellyfin. Add your IPTV provider and get live channels with full EPG guide data inside Jellyfin.

## How It Works

Tentacle emulates an HDHomeRun network tuner device. When Jellyfin discovers the tuner, it sees Tentacle as a native TV device — no third-party tools needed.

1. You add your IPTV provider in Tentacle (Sources → IPTV providers)
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

There is no separate Live TV provider. Go to **Sources → IPTV providers**, click **+ Add Provider** and choose **Xtream Codes** (server URL, username, password). The same provider serves VOD and Live TV.

When you save it, Tentacle tests the connection. If the account has live channels, Live TV is switched on for that provider and its channel groups are fetched automatically. The provider's card (Sources → IPTV providers) then holds them under **Channel groups**, and a **Live TV** tab appears under Sources. Until a provider is used for Live TV, that tab says no provider is used for Live TV.

!!! info "M3U providers"
    An M3U URL or M3U file provider added in Sources → IPTV providers is used for VOD only. Live TV needs an Xtream Codes provider.

### Several providers

Any number of providers can be used for Live TV: switch **Use for Live TV** on for each. Under it, each card has:

- **Streams at once**: how many channels that provider's account can play or record at the same time (its "connections"). Live TV, recordings and films played through Tentacle share them. The card shows what the account says; empty means 6. Jellyfin gets as many tuners as all providers' streams together.
- **Channel numbers +**: what is added to the provider's own channel numbers, so two providers never give Jellyfin the same number. The first Live TV provider keeps its own numbers (0); every other one is given its own range once and keeps it. Change it only if you have a reason: Jellyfin then sees that provider's channels under new numbers, and recordings set up on the old ones need setting up again.

### Several addresses

Providers often have more than one address and send a list of others when one stops working (an internet provider blocking one address in the evening, for example). On the provider's card, **Address → Addresses** keeps them:

1. Paste the provider's whole message: Tentacle picks out the addresses (a login or a path in them is dropped) and adds them as backups.
2. **Test all** checks each address from the server, one at a time: whether the name resolves, whether it is behind Cloudflare, whether the login works (account status, connections in use), and how long a live channel and a film take to start. Stream tests are skipped while anyone is watching, so a one-connection account is never cut off. The list is ranked best first, with how often each worked over the last day.
3. The hourly provider check tests the backups too, so the ranking covers the evening.
4. **Use this one** switches the provider to an address. When the address in use fails its checks, Tentacle switches to the best backup by itself and says so in Activity (and on your phone, with Pushover). You can choose **switch, and come back when it works again**, or **only tell me**.

A switch keeps everything: nothing is imported again or deleted, channels keep their numbers, the guide and recordings stay, and a stream that is playing finishes on the old address. Until a sync on the new address has read the whole catalogue, titles it does not list are not treated as gone.

### User-Agent

Tentacle connects to your provider with a TiviMate User-Agent (`TiviMate/4.7.0 (Linux; Android 12)`), which most providers accept. The dashboard has no setting to change it.

## Channel Groups

Groups are fetched automatically when the provider is added. They are organized by your provider (Sports, Entertainment, News, etc.) and show the channel count for each. On the provider's card, **Channel groups → Choose** lists them; **Refresh from provider** fetches the list again, and **Save & update channels** fetches the channels of the groups you switched on.

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

Tentacle uses your provider's stream ID as the channel number (plus the provider's offset when several providers are used for Live TV), not sequential numbering. This means channel numbers stay consistent even when you add or remove channels — your Jellyfin recordings and favorites won't break.

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
    Save Tentacle's address in **Settings → Services** (the **Jellyfin setup** tab under Sources → Live TV shows the tuner and guide addresses made from it). Then, after every TV guide update, Tentacle refreshes Jellyfin's guide by itself: it re-creates the XMLTV listing provider in Jellyfin (which forces a full channel remap) and triggers a guide refresh. This ensures new channels get properly mapped to EPG data.

### Verify

After setup, you should see your enabled channels in Jellyfin's Live TV section with program guide data. If channels are missing EPG, sync EPG in Tentacle and refresh the guide in Jellyfin.

## Stream Proxy

Tentacle proxies live streams between Jellyfin and your provider. This handles:

- **Provider redirects** — Follows redirect chains automatically
- **HLS to MPEG-TS conversion** — Converts HLS streams to MPEG-TS format for Jellyfin compatibility
- **User-Agent forwarding** — Uses the configured User-Agent when connecting to the provider

Xtream channels are pulled as one continuous MPEG-TS stream each, which is what lets several channels run at once on an account with a connection limit. If your provider does not serve a channel that way, Tentacle opens it as HLS instead, automatically.

When a provider drops a raw MPEG-TS stream, Tentacle reconnects. Many providers start the new connection with the last 20 seconds or so again; Tentacle recognises those seconds and skips them, so the recording goes on where it stopped, without repeated seconds. An interruption covered that way is not counted as damage.

A recording also waits out a provider refusing it (HTTP 401/403, for example a short IP block or flood protection) for up to three minutes before it ends; a viewer's stream still ends at once.

When the provider interrupts a recording, or serves a placeholder (a black "channel unavailable" video) instead of the channel, Tentacle notes it under **Health → Recent Activity**: a recording that may be missing content shows as **Recording damaged**, a placeholder as **Channel placeholder**. The guide sync, stream checks and nightly sweeps report there too, and anything that failed is shown in red.

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
    If you enable new channel groups but they don't appear in Jellyfin, check that Tentacle's address is saved in **Settings → Services**, then click **Update TV guide** on the Live TV tab's Channels: when the sync finishes, Tentacle re-creates the XMLTV listing provider in Jellyfin, which makes it re-discover all channels. Simply clicking "Refresh Guide" in Jellyfin alone won't pick up new channels.

!!! warning "No EPG data"
    EPG badges in Tentacle ("Has EPG" / "No EPG") reflect actual program data in the database. If channels show "No EPG" after a fresh setup, run an EPG sync first. The auto-chain after channel sync usually handles this automatically.

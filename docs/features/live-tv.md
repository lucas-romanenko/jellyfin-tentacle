# Live TV

Tentacle is the tuner for Jellyfin's Live TV: add your IPTV provider and get live channels with a full TV guide inside Jellyfin, without tools like Threadfin. Everyone in the household gets their own channel list.

## How It Works

1. An admin adds the IPTV provider in Tentacle (**Sources → IPTV providers**) and switches on the **channel groups** the household should have. Those channels are what everyone can watch.
2. Everyone else sees channels only, never the provider's groups. Under **My Channels** in Tentacle (signed in with their Jellyfin login), each person picks **All channels** or **My own list**: the channels they want, in their order, with their own favourites.
3. Tentacle sets Live TV up in Jellyfin itself: one tuner for everyone on All channels, one per person with a list of their own, and the TV guide for all of them. Nobody adds a tuner by hand.
4. In every Jellyfin app (the web, the TV app, phones) each person sees only their own channels: the guide, the channel list, search, "On now" and their numbers (1, 2, 3... in the order they chose).

```
Jellyfin  ←→  one M3U tuner per list  ←→  Tentacle  ←→  IPTV Provider
              + Tentacle's TV guide        (stream proxy)
```

Two people watching the same channel use one connection to the provider. Recordings and recording schedules are the household's, like a family recorder: everyone sees them.

## My Channels

Everyone (admins too) has **My Channels** in Tentacle's sidebar once the household has Live TV channels.

- **All channels** (where everyone starts): everything the household has, in the admin's order. Star the channels you watch most: they come first in Jellyfin's guide.
- **My own list**: search and **+ Add** the channels you watch, drag them (or use the arrows) into the order you like (that is their numbers), star your favourites, and **✕** removes one. Switching to your own list the first time starts it with your favourites. Switching back to All channels keeps your list for later.

Changes save by themselves, and your TV and apps show them about a minute later. Favourites carry over when you switch between the two.

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

### Stable channels

Jellyfin knows each channel by the tuner's address and the channel's stream address in Tentacle, never by its number. So numbers can change freely (each person's list is numbered 1, 2, 3... in their order) while favourites and recordings stay with the channel.

## EPG (Electronic Program Guide)

Tentacle downloads XMLTV guide data from your provider and stores it in the database. EPG data covers ALL provider channels (not just enabled ones), so when you enable a new channel, it already has EPG data available.

### EPG Refresh

- **Automatic** — EPG syncs automatically after channel sync (auto-chaining)
- **Manual** — Click **Sync EPG** on the Channels tab to refresh guide data
- **Cache** — EPG data is cached on disk for 8 hours to avoid repeated downloads

### Shared EPG IDs

Multiple channels can share the same EPG data (e.g., an HD channel and its backup). Tentacle handles this automatically — program data is duplicated for all matching channels in the XMLTV output.

## Connecting to Jellyfin

There is nothing to set up in Jellyfin. Once Tentacle is connected to Jellyfin (the setup wizard, or **Settings → Services**) and a provider has Live TV channels, Tentacle adds the tuners and the TV guide to Jellyfin itself within a few minutes, and keeps them in line: whenever someone changes their channels, when the admin switches groups or channels on or off, and every hour (which also catches people added in Jellyfin). **Sources → Live TV → In Jellyfin** shows when it last did so, and **Set up again now** runs it at once. **People** on the same page shows who uses All channels and who has a list of their own.

Each person's channels are tagged in Jellyfin and everyone else's are blocked for them (Jellyfin's per-user "blocked tags"). Tentacle manages only its own tags: blocked tags an admin set by hand are kept.

!!! info "Coming from an earlier version"
    An install that used Tentacle as an HDHomeRun tuner (or an M3U tuner on `/api/live/playlist.m3u`) is moved by itself: everyone's favourite channels and the recording schedules go to the new tuner, then the old tuner is removed from Jellyfin. Channel numbers become 1, 2, 3... Before any change, Tentacle saves what Jellyfin had (tuners, favourites, schedules, blocked tags) to `livetv-backups/` in its data folder.

!!! info "Tentacle's address"
    Jellyfin knows every channel by the tuner's address, so Tentacle chooses it once (the address Jellyfin already reached Tentacle at, else the plugin's Tentacle address, else Settings → Services) and keeps it.

## Stream Proxy

Tentacle proxies live streams between Jellyfin and your provider. This handles:

- **Provider redirects** — Follows redirect chains automatically
- **HLS to MPEG-TS conversion** — Converts HLS streams to MPEG-TS format for Jellyfin compatibility
- **User-Agent forwarding** — Uses the configured User-Agent when connecting to the provider

Xtream channels are pulled as one continuous MPEG-TS stream each, which is what lets several channels run at once on an account with a connection limit. If your provider does not serve a channel that way, Tentacle opens it as HLS instead, automatically.

When a provider drops a raw MPEG-TS stream, Tentacle reconnects. Many providers start the new connection with the last 20 seconds or so again; Tentacle recognises those seconds and skips them, so the recording goes on where it stopped, without repeated seconds. An interruption covered that way is not counted as damage.

A recording also waits out a provider refusing it (HTTP 401/403, for example a short IP block or flood protection) for up to three minutes before it ends; a viewer's stream still ends at once.

When the provider interrupts a recording, or serves a placeholder (a black "channel unavailable" video) instead of the channel, Tentacle notes it under **Health → Recent Activity**: a recording that may be missing content shows as **Recording damaged**, a placeholder as **Channel placeholder**. The guide sync, stream checks and nightly sweeps report there too, and anything that failed is shown in red.

## Tuner Endpoints

Tentacle gives these to Jellyfin itself:

| Endpoint | Purpose |
|----------|---------|
| `/api/live/tuner/all.m3u` | The shared tuner: every channel |
| `/api/live/users/<jellyfin user id>/tuner.m3u` | One person's own list |
| `/api/live/xmltv.xml` | XMLTV guide data |

The HDHomeRun endpoints (`/hdhr/discover.json`, `/hdhr/lineup.json`, `/hdhr/device.xml`) and `/api/live/playlist.m3u` are still served for anything set up by hand, but carry every channel and know nothing of people.

## Troubleshooting

!!! warning "Channels missing after enable"
    Tentacle refreshes Jellyfin's guide a few seconds after you switch groups or channels on; Jellyfin then takes a few minutes for a large lineup. If they still don't appear, open **Sources → Live TV → In Jellyfin**: it says why the last try didn't work (Jellyfin not reachable, for example). **Set up again now** tries again.

!!! warning "No EPG data"
    EPG badges in Tentacle ("Has EPG" / "No EPG") reflect actual program data in the database. If channels show "No EPG" after a fresh setup, run an EPG sync first. The auto-chain after channel sync usually handles this automatically.

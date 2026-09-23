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

### A title sits under "Searching" for a long time

**Searching** lists titles Radarr/Sonarr are monitoring and that are already released, but that no download has started for yet. It shows how long each one has been waiting. Minutes is normal. Days usually means your indexers have no release that matches the quality profile. A movie still before its release date shows under **Upcoming Releases**, not here.

Open the title from Activity (on the TV, select the card) to act on it:

- **Search again** asks Radarr/Sonarr to search right now (for a series, only the missing aired episodes).
- **Stop looking** (shows only) tells Sonarr to stop searching for the missing episodes and changes nothing else. Episodes you already have stay in Sonarr and Jellyfin. If you follow the show, new episodes are still grabbed as they air. When several episodes are missing, **Choose** lets you pick which ones to give up on, so Sonarr keeps looking for the rest. To undo it, open the show's **Manage Episodes** and tick the episodes again.
- **Remove from Radarr/Sonarr** deletes the title from Radarr/Sonarr together with its folder. It asks for a second press first. For a show with episodes already downloaded, the button says **Delete whole show** with the number of episodes on disk, because those files are deleted too. **Stop looking** is usually what you want there. A series that lives in your VOD folder is removed from Sonarr, but its VOD files are kept.

Admins can do this for any title; everyone else only for titles they requested.

### Discover or Activity says "Tentacle is busy" or "Can't reach Tentacle"

The Jellyfin plugin couldn't get an answer from the Tentacle dashboard, so it says why instead of showing an empty page:

| Message | What to check |
|---|---|
| Tentacle isn't set up yet | Set the Tentacle URL in the Jellyfin plugin's settings |
| Tentacle is busy and didn't answer in time | Usually temporary (for example, right after sign-in or during a sync). Try again in a moment |
| Tentacle didn't accept this account | Open the Tentacle dashboard once so setup finishes, then check that the account exists in Jellyfin |
| Can't reach Tentacle | Check that the Tentacle container is running and the plugin's Tentacle URL is correct |

### A title plays a completely different movie

Your IPTV provider labelled that stream wrong: its catalogue says one film, but the stream is another. Tentacle can only go by the provider's label, so the library shows the right poster and "In Library" while Play gives you something else. It also hides the real film: if you requested it, Radarr's search looks already satisfied.

Fix it from the title itself (admins only):

- **Jellyfin web:** open the title, **⋯ → Wrong movie? Fix it**
- **Android TV:** the **Wrong movie? Fix it** button on the title's page
- **Dashboard:** open the title in Library, **Wrong movie? Fix it**

Tentacle asks **which movie it really is** and suggests candidates. Once the title has been played, Jellyfin knows the stream's real length, and films of that length are listed first (a mislabelled stream is usually a similarly named film). Pick the right one and the copy moves there: new folder, the right metadata, same stream. Tentacle remembers the fix so the nightly sync keeps it. On the web and the dashboard you can also search for the title yourself.

**Not sure which film it is?** Two more clues help:

- **Audio language.** Jellyfin also records the language of the stream's audio. When the stream has a single audio language, films originally in that language get a **same language** badge and move up the list. A stream with several audio tracks (an original plus dubs) says little about the film, so it isn't used.
- **Pictures from the stream.** **Not sure? Show pictures** grabs three stills from the stream, spread across its length, so you can see what it actually is before you pick. The pictures come straight from your provider, so they take a few seconds. If the provider only allows one stream at a time and someone is watching, try again later. They are cached for two weeks.

If you still can't tell, **Leave it for now** closes the panel and changes nothing. The title stays flagged under **Library → Possible wrong movies**, so you can come back to it.

If it's none of them, **None of these — remove it** removes the copy and blocks that provider stream so it is never re-added. Either way, if you had requested the film on the label, Radarr keeps searching for it. Blocked streams are listed under **Library → Possible wrong movies**, where you can unblock one.

Tentacle also flags likely cases itself: each night it compares the real length of every IPTV movie that has been played with the length the film should have. Titles that are far off appear in **Library → Possible wrong movies** with **Fix it** / **It's fine** buttons. Titles nobody has played yet can't be checked: Jellyfin only measures a stream the first time it plays.

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

The failure message says why — read it first, it names the actual cause
("Radarr refused it: …", "Sonarr could not find this show in its TVDB metadata
source", "Could not read Radarr's root folders…"). The common ones:

| Message | What to do |
|---------|-----------|
| Could not read Radarr's/Sonarr's root folders | The *arr was busy or down. Nothing was added — retry in a moment. |
| Sonarr could not find this show in its TVDB metadata source | The show isn't on TheTVDB yet (usually very new). Nothing you can fix in Tentacle. |
| the root folder Tentacle used does not exist | Add a root folder in the *arr (Settings → Media Management). |
| the selected quality profile no longer exists | Pick a different quality profile. |
| another series/movie is already using that folder | A folder collision in the *arr — rename or remove the existing one. |

If it's an authorization problem instead, the plugin passes the Jellyfin user ID
for admin verification:

1. Make sure you're logged in as an admin user in Jellyfin
2. Check that the user has admin permissions in Tentacle (Settings → Users)
3. Verify Radarr/Sonarr connections in Tentacle Settings

!!! tip "A slow *arr is no longer reported as a failure"
    Radarr and Sonarr do a metadata refresh, artwork download and disk scan
    before answering an add, which on a large library can take well over a
    minute. Tentacle waits up to 90 seconds and, if it still times out, checks
    whether the title landed anyway before reporting anything.

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

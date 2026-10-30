# Library

The Library page gives you a unified view of all content Tentacle knows about — VOD titles from your streaming provider, Radarr movies, and Sonarr series — all in one place.

## Browsing

The Library page shows content cards with posters, titles, years, and ratings. You can:

- **Filter by type** — Movies, TV Series, or both
- **Search** — Find specific titles by name
- **Sort** — By title, year, rating, or recently added

### Content Badges

Each card shows where the content comes from:

- **Source badge** — The streaming provider source (Netflix, Amazon, etc.)
- **DL badge** (green) — Downloaded via Radarr or Sonarr
- **Both** — Some content may appear with both a source badge and DL badge (hybrid content)

## Detail View

Click any title to open its full detail modal with:

- Poster and backdrop artwork
- Full plot summary
- Cast, genres, rating, runtime
- Release year and status
- Source information
- Action buttons (add to Radarr/Sonarr, manage episodes, follow)

### TV Series Details

For TV series, the detail view also shows:

- **Season/episode breakdown** — Expandable seasons with episode lists
- **Episode status** — VOD (green), Downloaded (purple), or missing
- **Unaired episodes** — Shown at 50% opacity with air date badges
- **Download More Episodes** — Add missing episodes to Sonarr
- **Manage Episodes** — Change monitoring on existing Sonarr series

## Following

Following a series tells Sonarr to automatically download new episodes as they air. This is integrated with Sonarr's `monitorNewItems` feature.

### How to Follow

- Click the **Follow** button in a series' detail view
- Or check **"Auto-download new episodes"** when adding a series to Sonarr

### Following Tab

The Library page has a **Following** tab that shows all series you're currently following. This excludes ended or canceled series since there won't be new episodes.

### How It Works

- **Following ON** → Sonarr's `monitorNewItems` is set to `"all"` — new episodes auto-download
- **Following OFF** → `monitorNewItems` is cleared, but the series stays monitored in Sonarr (existing episode monitoring is preserved)
- **Bidirectional sync** — If you change monitoring in Sonarr directly, Tentacle picks up the change during the next scan

!!! info "Not shown for ended series"
    The Follow button is hidden for series with a status of "Ended" or "Canceled" since there won't be new episodes to follow.

## Duplicates

When content exists in both VOD (from your streaming provider) and your downloaded library (from Radarr), Tentacle detects the overlap and flags it as a duplicate.

### Viewing Duplicates

The dashboard shows a duplicate count if any are detected. Click through to see the list with:

- The title and which sources have it
- The VOD file path
- Options to resolve

### Resolving

You can resolve duplicates individually or all at once:

- **Keep Downloaded** deletes the VOD copy (its `.strm` and `.nfo` files) and keeps the download. Tentacle stops importing that title from your provider. For a show, Tentacle first checks with Sonarr that every episode the VOD copy has really was downloaded, and refuses otherwise (deleting nothing), so episodes only your provider has aren't lost; use Keep Both, or download the rest first.
- **Keep VOD** deletes the downloaded files through Radarr or Sonarr and removes the title there. When the download shares its folder with the VOD copy, only the downloaded video files are deleted; the `.strm` and `.nfo` files stay. Tentacle first checks that the VOD copy is on disk (a film's `.strm`, at least one episode `.strm` of a show) and refuses otherwise, so the download is never deleted when it is the only copy left, for example a film you downloaded before your provider offered it.
- **Keep Both** changes nothing.

A duplicate is resolved only once. If it was already resolved meanwhile (in another browser tab, or by Resolve All), the second choice is refused, nothing is deleted, and the list reloads to show what happened.

When the two copies are separate items in Jellyfin, each user's watched state on the copy that is removed (played, play count, resume point, favourite) is carried over to the copy that stays before anything is deleted. If Jellyfin can't be reached, or the copy you keep isn't in Jellyfin yet while users have watched state on the other one, nothing is deleted and the duplicate stays pending: scan the library in Jellyfin and try again.

!!! info "Sonarr and `.strm` files"
    Sonarr counts `.strm` files as episodes. When Sonarr holds a show in the same folder as its VOD copy, Sonarr lists Tentacle's `.strm` files as downloaded episodes. Tentacle only calls a show a duplicate when Sonarr has at least one episode that is not a `.strm`, and dismisses older duplicates that have none.

### Automatic rule

Instead of resolving each duplicate by hand, Tentacle can resolve them for you. It's **off** until you turn it on, under **Health → Duplicates**. Pick a mode:

- **Download first** (the default when you turn it on): the download stays and the stream's `.strm` goes, the same as **Keep Downloaded**. Your download doesn't depend on the provider.
- **Stream first**: the stream stays and the download is deleted through Radarr or Sonarr, the same as **Keep VOD**. Set a recycle bin in Radarr/Sonarr (Media Management) so a deleted download can be recovered; the card warns when there isn't one. The download stays when:
    - the title is on the **Prefer the download** list (switch it on in the title's details; useful for shows whose streams are unreliable),
    - its stream is on the **Streams that don't play** list,
    - it came from a **protected indexer** (names as Radarr/Sonarr show them).

  These are left for you to decide:

    - the provider isn't answering, or hasn't been checked yet,
    - the torrent is still seeding (connect qBittorrent on the card; Tentacle only reads it, and you can limit it to torrents with one tag),
    - nobody has watched the download yet, so a better copy isn't removed before it's been watched,
    - the download has episodes the stream doesn't have.

The rule runs every 15 minutes. It never opens a provider stream: whether a stream plays comes from what Tentacle already knows (stream checks and provider status). A show with episodes that exist only as streams never loses them, in either mode.

**Safety cap:** a bulk change in Radarr or Sonarr can turn many titles back into duplicates at once. When one run would change more titles than the cap (10 by default), it changes nothing. It shows the list and waits until you click **Apply** or raise the cap. **Preview** shows what a mode would do right now, without changing anything. Every change goes into the Deletion Log ("Duplicates rule", with the reason), and the duplicate shows as resolved "(rule)".

!!! info "Hybrid series are not duplicates"
    If you use "Download More Episodes" to add missing episodes to a VOD series, that's an intentional hybrid — not a duplicate. Tentacle tracks this via the `sonarr_path` field and won't flag it.

## Real-Time Updates

The Library page supports real-time updates via Server-Sent Events (SSE). When new content is synced or downloaded, it appears automatically without refreshing the page.

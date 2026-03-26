# Tentacle Live TV / IPTV Module

## GOAL

Add universal Live TV / IPTV capabilities to Tentacle so anyone can set up their IPTV provider and watch live TV through Jellyfin. This completely replaces Threadfin. Tentacle becomes the single tool — VOD sync, library management, AND live TV — all in one.

## CURRENT STATUS

Live TV is **fully working**:
- ✅ Channel guide data (EPG) displays in the UI and Jellyfin web client
- ✅ Channel logos/images show correctly
- ✅ HDHomeRun discover.json and lineup.json endpoints work
- ✅ Streams play in Jellyfin via HLS-to-MPEG-TS proxy
- ✅ Provider API (player_api.php) works — account active, channels sync
- ✅ EPG sync: full XMLTV download, disk-cached 8h, stream-parsed for ALL provider channels (not just enabled)
- ✅ EPG dedup within batch (UNIQUE constraint safe)
- ✅ discover.json uses correct BaseURL (from Host header or `hdhr_base_url` setting)
- ✅ VOD and Live TV providers fully separated (same table, `live_tv_enabled` flag)
- ✅ EPG status badges in UI (Has EPG / No EPG) — based on actual program data in DB (`EPGProgram` table), with filter dropdown
- ✅ Channel sync auto-chains into EPG sync — badges are accurate immediately when viewing channels
- ✅ Toggle controls fixed (read state from DOM, not baked-in value)
- ✅ Bulk Enable All / Disable All respects group, search, and EPG filters
- ✅ Stable channel numbering via stream_id (no shifting when channels added/removed)
- ✅ Auto Jellyfin guide refresh after EPG sync (delete+recreate listing provider + RefreshGuide)
- ✅ One-click "Refresh Jellyfin" — auto-detects missing EPG for newly-enabled channels and syncs inline
- ✅ Adding new channels: enable → Refresh Jellyfin → done (EPG data already in DB from first sync)
- ✅ Shared EPG IDs: multiple channels with same `epg_channel_id` all get programs in XMLTV (one-to-many mapping)
- ✅ Stream proxy uses `async with` for httpx clients — no connection leaks
- ✅ Thread-safe sync status with `threading.Lock()`
- ✅ Server address persistence with locked/edit toggle in Jellyfin Setup tab
- ✅ Channel counts shown per group during group sync (before channel sync)
- ✅ EPG timezone offset parsing (converts to UTC correctly)

## HOW STREAMING WORKS

The provider (currently `cf.iptbears.shop`) is behind Cloudflare. `.ts` streams are blocked (302 → `cloudflare-terms-of-service-abuse.com`). `.m3u8` requests get a valid 302 redirect to a tokenized URL on the real streaming server.

**The redirect chain:**
1. `http://PROVIDER/live/USER/PASS/STREAM_ID.m3u8` → 302 redirect
2. `http://RANDOM_SERVER.us9.ip2-st15.me:80/live/play/BASE64_TOKEN/STREAM_ID` → 200 OK
3. Returns HLS playlist (`#EXTM3U`) with relative `.ts` chunk paths like `/hls/HASH/STREAM_ID_407.ts`

**Stream proxy strategy (what we do):**
1. `live_stream_url()` in `xtream_client.py` uses `.m3u8` extension (not `.ts`)
2. Stream proxy at `/api/live/stream/{channel_id}` follows the 302 chain with TiviMate UA
3. Probes the tokenized URL — checks content type
4. If raw TS/binary: pipes bytes directly to Jellyfin
5. If HLS: fetches m3u8 playlist, downloads `.ts` chunks continuously, pipes as raw MPEG-TS
6. Jellyfin's HDHomeRun tuner expects continuous MPEG-TS bytes — it cannot handle HLS playlists

**Why this matters:** Jellyfin's HDHomeRun tuner implementation speaks MPEG-TS only. Returning an m3u8 playlist (even with rewritten absolute URLs) doesn't work — Jellyfin doesn't know how to consume HLS through the HDHomeRun interface. The proxy must consume HLS and output raw TS bytes.

**BaseURL fix:** `discover.json`, `lineup.json`, and `device.xml` derive BaseURL from the `hdhr_base_url` setting if configured, otherwise from the request's `Host` header (not `request.base_url` which returns `localhost` inside Docker).

**EPG fix:** EPG sync deduplicates entries within each batch using a `seen_epg` set keyed on `(channel_id, start)` to prevent UNIQUE constraint crashes.

## CHANNEL NUMBERING

**Uses provider's `stream_id` as stable channel number.** This ensures channel IDs never shift when channels are added/removed.

- In lineup.json: `GuideNumber` = `stream_id` (or fallback to channel DB `id`)
- In XMLTV: channel `id` attribute = same `stream_id`
- Jellyfin auto-maps channels to XMLTV by matching GuideNumber to channel id
- Old approach (sequential 1, 2, 3...) caused stale cache issues — adding channels shifted all numbers, Jellyfin showed wrong logos from cached data

## EPG SYNC FLOW

1. Get ALL provider channels (not just enabled) with `epg_channel_id` values
2. Determine XMLTV URL (provider's `epg_url` override, or Xtream `get_xmltv_url()`)
3. Clear old EPG data for ALL provider channels (by provider_id, not just enabled EPG IDs)
4. Stream-parse XMLTV (memory-efficient `ET.iterparse()`):
   - Download full XMLTV from provider (8-hour disk cache at `/data/xmltv_cache/`)
   - Keep programs matching ALL provider channels' `epg_channel_id` (not just enabled)
   - Handle gzip decompression automatically
   - Progress callback updates UI
5. Batch insert programs into `EPGProgram` table (chunks of 5000)
6. Auto-trigger Jellyfin guide refresh if server address configured

**Why all channels?** Storing EPG for all channels means newly-enabled channels already have guide data. Users can enable more channels and click "Refresh Jellyfin" without needing a separate EPG sync.

**Shared EPG IDs:** Multiple channels can share the same `epg_channel_id` (e.g. "CP24 HD" and "CP24 HD BACKUP" both use `Cp24.ca`). The XMLTV endpoint uses a one-to-many mapping (`epg_id_to_guide_numbers: dict[str, list[str]]`) to duplicate programs for all channels sharing an EPG ID.

## JELLYFIN GUIDE REFRESH (POST /api/live/refresh-guide)

One-click operation that handles everything needed for new channels to appear in Jellyfin:

1. **Pre-check for missing EPG data:**
   - Queries all enabled channels with EPG IDs
   - Checks which ones have zero programs in the `EPGProgram` table
   - If any are missing, runs EPG sync **inline** (synchronously) before proceeding
   - This is a safety net for channels enabled after the last EPG sync
2. **Delete + Recreate XMLTV listing provider:**
   - GET `/System/Configuration/livetv` to find existing XMLTV provider
   - DELETE it by ID
   - POST as new (without ID) — forces Jellyfin to remap ALL channels
   - **Critical:** Re-POSTing with the same ID does NOT remap new channels. Must delete+recreate.
3. **Trigger RefreshGuide scheduled task:**
   - GET `/ScheduledTasks` → find Key="RefreshGuide"
   - POST `/ScheduledTasks/Running/{taskId}`
   - This fetches lineup from tuner + refreshes EPG data

**Frontend trigger:** After EPG sync completes, `pages.js` automatically calls `POST /api/live/refresh-guide` if `live_setup_host` is configured.

## ADDING NEW CHANNELS WORKFLOW

The intended flow for users adding channels incrementally:

1. Initial setup: enable groups/channels → sync channels → sync EPG → set up Jellyfin tuner
2. Later: go back, enable more channels
3. Click "Refresh Jellyfin" — this single button:
   - Detects the new channels have no EPG → auto-syncs EPG inline
   - Deletes+recreates listing provider → Jellyfin remaps all channels
   - Triggers RefreshGuide → Jellyfin picks up new lineup + EPG
4. New channels appear in Jellyfin with logos and guide data

**Why it works:** EPG sync stores data for ALL provider channels. If the first sync already ran, most new channels already have EPG in the DB. The refresh-guide pre-check catches any edge cases.

## DATABASE MODELS

### LiveChannel
```
id, provider_id (FK), name, channel_number (nullable), stream_id (unique per provider)
stream_url, logo_url, group_title, epg_channel_id (for EPG matching)
enabled (default False), sort_order, created_at, updated_at
```

### LiveChannelGroup
```
id, provider_id (FK), name, category_id, enabled (default False), channel_count
Unique: (provider_id, name)
```

### EPGProgram
```
id, channel_id (matches epg_channel_id), title, description
start, stop, category, icon_url
Unique: (channel_id, start)
```

### Provider (Live TV fields)
```
live_tv_enabled, provider_type (xtream/m3u_url/m3u_file)
m3u_url, epg_url, user_agent, last_live_sync, has_live
require_tmdb_match (per-provider, for VOD only)
```

## SETTINGS

| Key | Default | Purpose |
|-----|---------|---------|
| hdhr_tuner_count | "3" | Virtual tuners reported to Jellyfin |
| hdhr_device_id | "TENTACLE1" | HDHomeRun device ID |
| hdhr_base_url | "" | Explicit base URL (falls back to Host header) |
| live_setup_host | "" | Server IP for Jellyfin Setup tab |
| live_setup_port | "" | Server port (default 8888 in UI) |

## API ENDPOINTS

```
GET/POST /api/live/provider              — Get/save live TV provider config
POST     /api/live/provider/test         — Test connection
POST     /api/live/sync/{provider_id}    — Phase 1: fetch groups with channel counts
POST     /api/live/sync-channels/{id}    — Phase 2: fetch channels for enabled groups
POST     /api/live/sync-epg/{id}         — Fetch EPG data (XMLTV, cached 8h)
GET      /api/live/sync-status           — Sync progress polling
GET      /api/live/channels              — List channels (filter: group, enabled, search, has_epg)
PUT      /api/live/channels/{id}         — Update channel
POST     /api/live/channels/bulk         — Bulk enable/disable by ID list
POST     /api/live/channels/bulk-filter  — Bulk enable/disable by filter
GET      /api/live/groups                — List groups
PUT      /api/live/groups/{id}           — Enable/disable group + its channels
PUT      /api/live/groups/bulk           — Bulk enable/disable groups
GET      /api/live/status                — Counts: total/enabled channels, groups, EPG programs
POST     /api/live/refresh-guide         — Delete+recreate listing provider + RefreshGuide
GET      /api/live/stream/{channel_id}   — Stream proxy (redirect-follow, UA spoof, HLS→MPEG-TS)
HEAD     /api/live/stream/{channel_id}   — Stream validation (returns 200 + video/mp2t)
GET      /api/live/playlist.m3u          — M3U playlist for enabled channels
GET      /api/live/xmltv.xml             — XMLTV guide data (alias: /hdhr/xmltv.xml)
GET      /hdhr/discover.json             — HDHomeRun device discovery
GET      /hdhr/lineup.json               — Channel lineup (enabled channels)
GET      /hdhr/lineup_status.json        — Scan status
POST     /hdhr/lineup.post               — Scan trigger (no-op)
GET      /hdhr/device.xml                — UPnP device descriptor
```

## UI TABS

### Groups tab
- Sync groups button fetches categories + channel counts per category
- Toggle switches to enable/disable groups
- "Save & Sync Channels" fetches channels for enabled groups

### Channels tab
- Paginated list (100/page) with logos, EPG badges, toggle switches
- EPG badge = "Has EPG" if actual program data exists in the DB for that channel's `epg_channel_id` (accurate after first EPG sync, which auto-chains from channel sync)
- Filters: search, group dropdown, EPG status dropdown
- Shift-click for range selection
- "Sync EPG" button downloads guide data and auto-refreshes Jellyfin

### Jellyfin Setup tab
- Server address input with locked/edit toggle (persisted to settings)
- Tuner URL and XMLTV URL displayed with copy buttons
- Instructions for initial Jellyfin tuner setup

## SYNC FLOW

### Xtream Provider (2-phase + auto EPG)
1. **Phase 1: Groups** — `get_live_categories()` + `get_live_streams()` for channel counts per category. Fast, no channel details.
2. **Phase 2: Channels** — For each enabled group: `get_live_streams(category_id)`. Upserts `LiveChannel` records by `stream_id`.
3. **Auto EPG chain** — After channel sync completes, background thread automatically runs EPG sync (`_run_epg_sync_background()`). This ensures EPG badges are accurate when the user views the channels tab. The UI sync poll handles the channel→EPG phase transition.

### M3U Provider (1-phase)
- Download M3U (cached 8h for URLs), parse all channels, upsert by stable ID (sha256 hash of name+URL). Auto-create groups.
- Upsert preserves user customizations (enabled, sort_order, channel_number) — no delete-all/re-insert.

### Background tracking
- Global `_sync_status[provider_id]` dict with phase, progress, message, status
- Thread-safe: protected by `threading.Lock()` via `_set_sync_status()` / `_get_sync_status()`
- UI polls `/api/live/sync-status` every 1.5-2s

## XMLTV TIMEZONE PARSING

`_parse_xmltv_time()` in `services/xmltv.py` correctly handles timezone offsets:
- Input: `20260324060000 +0100`
- Parses datetime portion: `2026-03-24 06:00:00`
- Parses offset: `+0100` → 1 hour ahead of UTC
- Subtracts offset to convert to UTC: `2026-03-24 05:00:00`
- Stored in DB as UTC, displayed by Jellyfin in user's local timezone

## KNOWN GOTCHAS

1. **`.ts` streams blocked, `.m3u8` works** — Cloudflare-fronted providers block raw TS but allow HLS
2. **HLS chunk URLs are relative** — Must resolve against redirected server, not original provider
3. **Tokenized redirect URLs are temporary** — Don't cache redirect URLs
4. **Provider blocks `get.php` but allows `player_api.php`** — Always prefer JSON API
5. **User-Agent required** — TiviMate UA or provider rejects
6. **Jellyfin `Favorite` field** — Must be boolean, not number (Jellyfin crashes on numbers)
7. **Jellyfin listing provider remap** — Must DELETE+recreate, not re-POST with same ID
8. **EPG sync must use upsert** — Plain INSERT crashes on duplicate (channel_id, start)
9. **discover.json BaseURL** — Must not be localhost; use real IP or Docker hostname
10. **46K+ channels** — Some providers have massive channel lists, handle efficiently
11. **Sequential channel numbers caused cache issues** — Fixed by using stream_id as GuideNumber
12. **Stream proxy connection leaks** — httpx.AsyncClient must use `async with` context manager; raw TS path needs dedicated client owned by generator
13. **EPG cleanup must scope by provider** — Delete by all provider channel EPG IDs, not just the ones being synced, to prevent unbounded growth
14. **M3U channel IDs must be stable** — Array indices shift when channels are added/removed; use sha256(name+URL) hash instead
15. **Shared EPG IDs** — Multiple channels can share one `epg_channel_id`. XMLTV must map programs to ALL matching channels, not just the last one (use list, not dict)
16. **EPG auto-chain DB session** — Channel sync background thread must close its DB session before chaining EPG sync, otherwise EPG sync gets a stale session. Set `db = None` after `db.close()` before calling `_run_epg_sync_background()`

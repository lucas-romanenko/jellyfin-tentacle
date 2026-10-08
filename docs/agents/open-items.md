# Open items

Known gaps worth fixing (each is a feature: it waits for the maintainer's go).

- Title prefixes: `STRIP_PREFIXES` in `services/cleaner.py` is a fixed list
  (NF, AMZ, HBO, ...) and misses unknown codes (e.g. `NF-DO`); a generic rule
  for short uppercase codes before ` - `, maybe reviewable per category.
- Playlist refresh efficiency (diffs, DB-computed tag playlists, parallel
  users): [pipelines.md](pipelines.md#known-inefficiencies-improvement-ideas).
- Provider Migrate (`services/migration.py`) moves films only, matched by
  title and year; series and films the new provider lists under another name
  stay with the old (switched-off) provider. Options (#460): rank the old
  provider below the new one so the new one's sync takes over by TMDB id
  (the #154 takeover), or a series branch (`get_series_info` per show).
- The unmounted-root guard (`_vod_root_unavailable()`, #439, #440) looks
  only at whether the VOD root is missing or empty. In a merged setup (VOD
  and Radarr/Sonarr in one folder), an *arr import onto the bare mount point
  makes it non-empty and the sync and sweep treat it as mounted. A stricter
  test (none of the rows' `.strm` files exist) would cover it, but then a
  library emptied on purpose would never be written again without a way out.
- The Radarr/Sonarr webhooks accept unsigned events unless `webhook_secret`
  is set: consider making it required, as the music webhook's is.

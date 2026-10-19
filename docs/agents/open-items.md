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
- The Radarr/Sonarr webhooks accept unsigned events unless `webhook_secret`
  is set: consider making it required, as the music webhook's is.
- The Radarr and Sonarr scans set a hybrid row's (VOD title also
  downloaded) tags with the bare `source_tag` ("Netflix") where the VOD
  sync writes the suffixed one ("Netflix TV"): `tags.append(...source_tag)`
  in their NFO stage. Check whether the VOD playlist keeps such a title;
  releasing a download (#378, #617) keeps what the scan left.
- A raw MPEG-TS recording still ends at once on a 404/410 from the channel
  URL at re-dial, where an HLS recording waits it out for `_GONE_GRACE`
  (180 s; a panel blip answers 404 for seconds). 401/403 got their own
  grace (#646, `_DENIED_GRACE`); 404 on raw TS could take the same shape.

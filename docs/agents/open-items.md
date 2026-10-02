# Open items

Known gaps worth fixing (each is a feature: it waits for the maintainer's go).

- Title prefixes: `STRIP_PREFIXES` in `services/cleaner.py` is a fixed list
  (NF, AMZ, HBO, ...) and misses unknown codes (e.g. `NF-DO`); a generic rule
  for short uppercase codes before ` - `, maybe reviewable per category.
- Playlist refresh efficiency (diffs, DB-computed tag playlists, parallel
  users): [pipelines.md](pipelines.md#known-inefficiencies-improvement-ideas).
- The Radarr/Sonarr webhooks accept unsigned events unless `webhook_secret`
  is set: consider making it required, as the music webhook's is.

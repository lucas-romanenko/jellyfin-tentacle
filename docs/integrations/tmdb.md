# TMDB Integration

Tentacle uses [The Movie Database (TMDB)](https://www.themoviedb.org/) for all metadata — matching titles, fetching artwork, getting ratings, genres, cast information, and powering the Discover section.

## Zero Configuration

Tentacle ships with a built-in TMDB API key. Metadata works out of the box — no setup required.

You can optionally override with your own key in **Settings** if you prefer, but it's not necessary.

## What TMDB Provides

### Content Matching

When Tentacle syncs VOD content from your provider, each title is matched against TMDB:

- **Official title** — Replaces the provider's often-inconsistent naming
- **TMDB ID** — Unique identifier for reliable cross-referencing
- **Media type** — Movie vs. TV series classification

### Metadata

For every matched title:

- **Plot summary** — Full description
- **Poster and backdrop images** — High-quality artwork
- **Rating** — Community rating (0-10)
- **Genres** — Action, Comedy, Drama, etc.
- **Release year** — Original release date
- **Runtime** — Duration in minutes
- **Cast** — Actors and characters
- **Studios** — Production companies
- **Content rating** — PG-13, R, TV-MA, etc.

### Discover

TMDB powers the entire Discover section:

- **Trending** — What's trending today/this week
- **Popular** — Most popular titles
- **Upcoming** — Movies and shows not yet released
- **Search** — Find any title in TMDB's database
- **Detail views** — Full metadata for any title

### Series Information

For TV series, TMDB also provides:

- **Season/episode structure** — How many seasons, episodes per season
- **Episode details** — Title, plot, air date for each episode
- **Series status** — Returning, Ended, Canceled, In Production, Planned

### IMDb ID Resolution

When importing lists (especially IMDb lists), items often only have an IMDb ID. Tentacle uses TMDB's `/find` endpoint to resolve IMDb IDs to TMDB IDs for proper metadata enrichment.

## Caching

TMDB responses are cached locally to reduce API calls and improve performance:

| Content | Cache Duration |
|---------|---------------|
| Movie metadata | 30 days |
| Series metadata | 7 days |
| Trending/Popular | 30 minutes (plugin), refreshed nightly (dashboard) |

Cache files are stored in `/data/tmdb_cache/` inside the Tentacle container. Expired entries are cleaned during the nightly sync.

## TMDB as Gatekeeper

By default, content that can't be matched to TMDB is **skipped** during VOD sync. This ensures only real, identifiable titles make it into your Jellyfin library — filtering out placeholder entries, test content, or titles with garbled names from your provider.

### Disabling the Requirement

If you want to include unmatched content (using the provider's title as-is), you can disable **"Require TMDB Match"** in the provider settings. Unmatched titles get a negative TMDB ID in the database and appear with the provider's original title.

## Fuzzy Matching

TMDB matching isn't just exact title lookup. Tentacle uses fuzzy matching that:

- Strips provider prefixes (NF -, AMZ -, HBO -, etc.)
- Normalizes punctuation and special characters
- Accounts for title variations (colons, hyphens, articles)
- Considers the release year for disambiguation
- Falls back to progressively looser matching if strict matching fails

## Using Your Own API Key

If you'd rather use your own TMDB API key:

1. Create a free account at [themoviedb.org](https://www.themoviedb.org/)
2. Go to [Settings → API](https://www.themoviedb.org/settings/api) and request an API key
3. In Tentacle Settings, enter your TMDB Bearer Token (the long `eyJ...` token, not the short API key)

The built-in key is a project-registered key with standard rate limits. For heavy usage, your own key may perform better.

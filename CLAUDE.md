# Jellyfin Tentacle

A companion server for Jellyfin, a Jellyfin plugin, and the docs site. Other
people run all three, and this repo is public: never put credentials,
tokens, internal IPs or server paths in it. The maintainer's own install is
documented privately (~/homelab/docs/tentacle.md, Lucas's server, private).

| Part | Where | What |
|---|---|---|
| Server | `tentacle/` | Python 3.11 / FastAPI, one uvicorn worker on port 8888; shipped as the Docker image `ghcr.io/lucas-romanenko/jellyfin-tentacle` |
| Plugin | `tentacle-plugin/` | C#, .NET 9, Jellyfin 10.11 (`Jellyfin.Controller`); installed from the catalog in `tentacle-plugin/manifest.json` |
| Docs site | `docs/`, `mkdocs.yml` | MkDocs Material on GitHub Pages; `docs/agents/` is excluded from it |

The Android TV clients live in jellyfin-tentacle-androidtv (current) and
jellyfin-tentacle-androidtv-legacy.

What it does: syncs VOD from IPTV providers (Xtream, M3U) as `.strm` + NFO,
tracks Radarr/Sonarr downloads, tags everything (NFO tags for `.strm`, the
Jellyfin API for real video files), and turns tags, lists and rules into
per-user Jellyfin playlists and a custom Jellyfin home screen (hero + rows),
plus Discover (TMDB), Activity (downloads), Live TV (an HDHomeRun tuner for
Jellyfin), YouTube and an optional music module (Lidarr).

## Reference (read the one you need before changing that area)

| File | What |
|---|---|
| [docs/agents/server.md](docs/agents/server.md) | server internals: layout, auth and users, media paths, playlists, home config, deletes, DB, logs, Live TV, music, UI words |
| [docs/agents/pipelines.md](docs/agents/pipelines.md) | what happens on each event (webhooks, edits, nightly sync), how changes reach the clients, known inefficiencies |
| [docs/agents/plugin.md](docs/agents/plugin.md) | plugin internals, rules learned the hard way, caches |
| [docs/agents/jellyfin-notes.md](docs/agents/jellyfin-notes.md) | Jellyfin/Radarr/Sonarr API behavior that isn't documented |
| [docs/agents/server-api.md](docs/agents/server-api.md) | every server route and its auth |
| [docs/agents/plugin-api.md](docs/agents/plugin-api.md) | every plugin route, its auth, what the TV app calls |
| [docs/agents/triage.md](docs/agents/triage.md) | GitHub issues and pull requests: what to ask, labels, issue forms |
| [docs/agents/releasing.md](docs/agents/releasing.md) | CI workflows and what each publishes, release notes, plugin and server releases, checking a release |
| [docs/agents/open-items.md](docs/agents/open-items.md) | known gaps worth fixing |

## Layout

- Server: `tentacle/main.py` (app, routers, scheduler, `/api/health`,
  `/api/version`), `routers/` (one file per area), `services/` (the work),
  `models/database.py` (SQLAlchemy), `static/` (the dashboard). Data in
  `/data` in the container (SQLite `tentacle.db` plus caches).
- Plugin: `Api/` (HTTP endpoints), `Inject/` and `Web/` (the web UI it
  injects into Jellyfin), `HomeScreen/`, `Playlists/`, `Services/`,
  `Tasks/`, `Patching/`, `Configuration/`. Its only setting is
  `TentacleUrl`; all JS/CSS goes into Jellyfin's `index.html` through
  Harmony (no files modified).

## Rules that bite

Server (details in server.md, pipelines.md, jellyfin-notes.md):

- Credentials (Jellyfin, *arr, TMDB, ... keys, IPTV logins) live only in the
  database (`settings`, `providers`), set in the dashboard. Never in files,
  env or logs. A new credential in a URL needs a secret-like parameter name
  or a rule in `services/log_redaction.py`.
- Auth: `get_user_from_request` takes a session cookie or a *verified*
  `?api_key=`; `userId` is only a claim. Jellyfin ids have dashes, Tentacle
  stores them without: normalize (`.replace("-", "")`) before comparing.
  Admin-only routers declare `dependencies=[Depends(require_admin)]`.
- Tags on `.mkv` only through the Jellyfin API with a minimal body; refresh
  items with `ReplaceAllMetadata=false` (true wipes the tags).
- Source tags carry a type suffix ("Netflix Movies"); playlist expressions
  must use it.
- Playlist refreshes hold `_playlist_refresh_lock` (concurrent refreshes
  corrupt Jellyfin's playlist folders); add/remove items in chunks of 50.
- Every home-config or playlist mutation calls `bump_playlist_version()`,
  writes the home config and notifies the plugin, or the clients won't see it.
- `write_home_config()` remaps rows by `display_name` when playlists get new
  ids; keep that, or rows vanish after a sync.
- Functions used from `onclick=""` in `index.html` must be exported in
  `exposeGlobals()` at the bottom of `static/js/pages.js`.
- UI says "Playlist", never "Tag" or "Collection".
- Lidarr: one request at a time, pageSize 50 or less, no `include*`;
  MusicBrainz 1 request/s.

Plugin (details in plugin.md, plugin-api.md):

- A user-scoped endpoint checks the caller with `CallerIdentity`
  (`[Authorize]` alone only proves someone is signed in); admin-only ones
  use `[Authorize(Policy = "RequiresElevation")]`.
- Proxy endpoints forward the server's status code (`ContentResult`, never
  `Content()`); config-page scripts stay inside the `data-role="page"` div;
  never set `GenerateAssemblyInfo=false`.
- Never edit `tentacle-plugin/manifest.json` by hand: users' Jellyfin reads
  the catalog from it on main, and only the Plugin Release workflow writes it.

## Test

```
make check       # = scripts/check: the full unit suite (about 4 min), as CI (tests.yml) runs it on every push and PR; venv cached in ~/.cache/jellyfin-tentacle
```

- Hermetic (`tests/hermetic.py`: no DNS, loopback only, no proxy): mock the
  service in the test (TMDB: `no_tmdb(self)`). A test that truly needs the
  network is `@live` and runs only with `TENTACLE_LIVE_TESTS=1`, never in
  `make check`.
- The suite runs with its own TMPDIR and fails if anything is left in it:
  scratch dirs come from `temp_dir(self)` (`tests/tmp_dirs.py`), never a bare
  `tempfile.mkdtemp()`; no test writes into the source tree.
- Plugin builds: `dotnet build --disable-build-servers` (parallel sessions
  share a build lock; a lingering MSBuild/Roslyn build server holds it for
  ever; `dotnet build-server shutdown` frees it).

## Merge

main moves only through a pull request whose `unit` check passed; pull
requests merge by squash only, merged branches are deleted automatically.
Merging publishes nothing (CI publishes only on a tag: releasing.md).

## Deploy (the maintainer's server)

After main moves, run main on Lucas's server. In a Claude session make is
sandboxed: `make deploy` builds the plugin, then prints one exact `ops ...`
line; run that line as a command of its own (600000 ms timeout).

```
make deploy                     # builds the plugin, prints: ops tentacle deploy main <sha> --plugin (installs it only if tentacle-plugin/ changed)
ops tentacle verify             # healthy, /api/version = the deployed commit; a deployed plugin is Active (make verify prints this line)
ops tentacle status             # what runs, and "Jellyfin playing now: N"
make deploy REF=vX.Y.Z          # a release tag instead of main
make deploy-release             # back to the latest public release (image latest, released plugin)
ops tentacle stage <pr>         # a pull request (or main) on the contained staging instance, never production
```

- Production runs main or a release tag, never a branch or a pull request:
  those go to staging (`ops tentacle stage <pr>`). Contributed code never
  runs on the workbench; its tests come from CI.
- A plugin deploy restarts Jellyfin: `ops tentacle status` must show nothing
  playing first (the deploy refuses otherwise).
- The make targets call a private script (`TENTACLE_DEPLOY` in the
  Makefile); nothing goes to a registry or a release, other installs are
  untouched. Mechanics, staging and the plugin's private version scheme:
  ~/homelab/docs/tentacle.md (Lucas's server, private).

## Releases

Releases are the maintainer's (approval rules: ~/homelab/workflow.md
"Approval"). A session that changed something user-visible ends with draft
release notes and "ready to release vX.Y.Z" (plus "and plugin-vX.Y.Z" when
`tentacle-plugin/` changed): format in
[docs/agents/releasing.md](docs/agents/releasing.md).

# Jellyfin Tentacle

A companion server for Jellyfin, a Jellyfin plugin, and the docs site. Other
people run all three, and this repo is public: never put credentials,
tokens, internal IPs or server paths in it (Lucas's own install is
documented privately in his homelab manual).

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

Reference for agents (read the one you need before changing that area):

| File | What |
|---|---|
| [docs/agents/server.md](docs/agents/server.md) | server internals: auth and users, playlists, home config, deletes, DB, Live TV, music, UI words |
| [docs/agents/pipelines.md](docs/agents/pipelines.md) | what happens on each event (webhooks, edits, nightly sync) and how changes reach the clients |
| [docs/agents/plugin.md](docs/agents/plugin.md) | plugin internals, rules learned the hard way, caches |
| [docs/agents/jellyfin-notes.md](docs/agents/jellyfin-notes.md) | Jellyfin/Radarr/Sonarr API behavior that isn't documented |
| [docs/agents/server-api.md](docs/agents/server-api.md) | every server route and its auth |
| [docs/agents/plugin-api.md](docs/agents/plugin-api.md) | every plugin route, its auth, what the TV app calls |

## Server (`tentacle/`)

- `main.py`: the FastAPI app, routers, `/api/health` (container
  healthcheck) and `/api/version` (unauthenticated: commit, build date,
  `code.matches` = the running files match the image's fingerprint).
- `routers/`: one file per area (auth, discover, sonarr, radarr, sync, vod,
  youtube, music, smartlists, activity, ...); `services/`: the work behind
  them; `models/database.py`: SQLAlchemy models; `static/`: the dashboard.
- Data: `/data` in the container (SQLite `tentacle.db` plus caches).
  Credentials (Jellyfin, Sonarr, Radarr, Lidarr, TMDB, ... keys) are set in
  the dashboard and stored in the database: the `settings` table
  (key/value, e.g. `jellyfin_api_key`, `sonarr_api_key`, `tmdb_bearer_token`)
  and `providers` (IPTV logins). Never in files or env.
- Auth (`routers/auth.py`, `get_user_from_request`): a dashboard session
  cookie, or a *verified* `?api_key=` (a Jellyfin access token, resolved to
  its owner through Jellyfin). `userId` is only a claim. Jellyfin sends user
  ids with dashes; Tentacle stores them without: normalize
  (`.replace("-", "")`) before comparing. Admin-only routers declare
  `dependencies=[Depends(require_admin)]`; non-admins may delete only
  downloads they requested (`DownloadRequest`).
- Media paths inside the container are fixed (users map host folders with
  volumes; Settings → Library Paths checks them): `/data` (DB, caches,
  per-user `smartlists/` and `home-configs/`), `/media/movies` (Radarr),
  `/media/shows` (Sonarr), `/media/vod/movies`, `/media/vod/shows` (VOD
  `.strm`), `/media/youtube`.
- Nightly sync: cron setting `sync_schedule` (default `0 3 * * *`),
  `run_scheduled_sync()` in `main.py`.

Rules that bite (details in the docs above):

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
- Lidarr: one request at a time, pageSize ≤ 50, no `include*`; MusicBrainz 1
  request/s.

## Plugin (`tentacle-plugin/`)

`Api/` (the HTTP endpoints: `TentacleHome`, `TentacleDiscover`, `Tentacle`,
`TentacleMusic`, ...), `Inject/` and `Web/` (the web UI it injects into
Jellyfin), `HomeScreen/`, `Playlists/`, `Services/`, `Tasks/`, `Patching/`,
`Configuration/`.

Every endpoint, its auth level and which the TV app calls:
[docs/agents/plugin-api.md](docs/agents/plugin-api.md). Rule: a user-scoped
endpoint checks the caller with `CallerIdentity` (`[Authorize]` alone only
proves someone is signed in); admin-only ones use
`[Authorize(Policy = "RequiresElevation")]`. Its only setting is
`TentacleUrl`; all JS/CSS is injected into Jellyfin's `index.html` through
Harmony (no files modified). Proxy endpoints forward the server's status
code (`ContentResult`, never `Content()`); config-page scripts stay inside
the `data-role="page"` div; never set `GenerateAssemblyInfo=false`.

Repo settings: pull requests merge by squash only; merged branches are
deleted automatically.

## Work on it

Clone at `/code/jellyfin-tentacle` on the workbench; work in a worktree on a
branch, merge to main, push (the workbench's workflow). Tests:

```
make check       # = scripts/check: the full unit suite, as CI runs it (a venv cached in ~/.cache/jellyfin-tentacle)
```

The workbench's pre-push hook runs the check before main moves (about
4 min). CI (`tests.yml`) runs the same script on every push and pull request.
The suite runs with its own TMPDIR and the check fails if anything is left in
it: a test's scratch dirs come from `temp_dir(self)` (`tests/tmp_dirs.py`),
never a bare `tempfile.mkdtemp()`, and no test writes into the source tree.
Merging to main publishes nothing (see below).

Every coding task ends, after main is pushed, with:

```
make deploy      # run the current origin/main on Lucas's server (app; the plugin only if tentacle-plugin/ changed)
make verify      # healthy, /api/version = the deployed commit; a deployed plugin is Active in Jellyfin
```

Both must pass; report their output. To confirm a fix on Lucas's server
before merging it (triage below), push the branch and run
`make deploy REF=<branch>` and `make verify REF=<branch>` (verify then also
fails unless the branch's head is what runs); after the merge, `make deploy`
and `make verify` put main back. This is private: the image is built
on the server itself, nothing goes to a registry or a release, and other
installs are untouched. `make deploy-release` switches Lucas's server back
to the latest public release (image `latest`, and the released plugin if a
private build is installed). The targets call a private script that knows
the server (outside this repo; `TENTACLE_DEPLOY` in the Makefile names it).
A private plugin build is versioned `<newest release>.<commit count>` (e.g.
`2.270.0.1189`), so the next real plugin release supersedes it.

Plugin builds on the workbench run under a lock shared by parallel
sessions: build with `dotnet build --disable-build-servers`, or the
MSBuild/Roslyn build server outlives the build, inherits the lock's file
descriptor and holds it for ever (every later build waits; `dotnet
build-server shutdown` frees it).

Logs: `services/log_redaction.py` strips credentials from every log record
(uvicorn's access log included): Xtream paths and any query parameter
named like a secret (`*secret*`, `*token*`, `*password*`, `*api_key*`,
`key`, ...). A new credential in a URL needs such a name, or a rule there.

## Triage (GitHub issues and pull requests)

Sessions follow the workbench's github-triage skill (a gatekeeper: sort
every item, reproduce bugs on Lucas's install before changing code, never
build features without his `approved` label). Tentacle specifics:

- "Lucas's install" = the commit his server reports at `/api/version` and
  the plugin version his Jellyfin reports (how to read them:
  his private manual). Reproduce against that commit, not main.
- Not reproduced: label `needs-info` and ask for Tentacle server version
  (`/api/version` commit), plugin version (Dashboard → Plugins), Jellyfin
  version, the client (web, Android TV app and its version, other) and any
  local patches or modifications. The bug report template asks the same.
- Reproduced: failing test first, fix on `fix/<n>-<slug>`, push the
  branch, `make deploy REF=fix/<n>-<slug>` + `make verify REF=...`, confirm
  on Lucas's install, merge to main (pull requests: squash only), `make
  deploy` + `make verify`, comment Cause and Change, close.
- A plugin change restarts Jellyfin on deploy: first check that nobody is
  watching (how: the private manual).
- Labels: `needs-info`, `needs-lucas` (a feature or idea waiting for
  Lucas), `approved` (only Lucas adds it: a feature may be built),
  plus GitHub's defaults.
- Issue forms: `.github/ISSUE_TEMPLATE/` (bug report, feature request;
  `config.yml` keeps blank issues for questions and links the docs,
  troubleshooting page and Discussions).

## Releasing is Lucas's decision

Releasing publishes to other people's servers and Jellyfin installs, so
only Lucas tags or creates releases. Agents never tag or release (the
workbench's guard asks before any tag push, `gh release` change or `gh
workflow run`, and before a push that changes
`tentacle-plugin/manifest.json`). A session ends with
draft release notes (user-visible changes by area, issue numbers in
brackets, upgrade notes; `git log vA.B.C..main`) and "ready to release
vX.Y.Z", plus "and plugin-vX.Y.Z" when `tentacle-plugin/` changed since the
last plugin tag (`git diff --stat plugin-vA.B.C origin/main --
tentacle-plugin`).

| Trigger | Workflow | Publishes |
|---|---|---|
| push to any branch, pull request | Tests | nothing (unit tests) |
| docs change on main | Deploy Documentation | nothing (build only, `mkdocs --strict`) |
| tag `vX.Y.Z`, or a GitHub release on it | Docker Publish | image tags `X.Y.Z`, `X.Y` and `latest` (a pre-release like `v2.0.0-rc.1` gets only its own tag, never `latest`) |
| tag `vX.Y.Z`, or a GitHub release on it | Deploy Documentation | the docs site |
| Run workflow (by hand) | Deploy Documentation | the docs site, from the chosen branch |
| tag `plugin-vX.Y.Z` | Plugin Release | a GitHub release with the plugin zip, and a new entry in `tentacle-plugin/manifest.json` on main |

Users' Jellyfin reads the plugin catalog from `tentacle-plugin/manifest.json`
on main (raw.githubusercontent.com), so that file is a release too: only the
Plugin Release workflow edits it; never edit it by hand.

### Server release (Lucas)

1. Main is green: the Tests run for the commit passed
   (`gh run list -R lucas-romanenko/jellyfin-tentacle --branch main -L 3`).
2. Tag that commit and push the tag (or create the release in the GitHub UI
   on a new tag `vX.Y.Z` targeting main, which does the same):
   ```
   git -C /code/jellyfin-tentacle pull --ff-only
   git -C /code/jellyfin-tentacle tag -a v1.9.0 -m "v1.9.0"
   git -C /code/jellyfin-tentacle push origin v1.9.0
   ```
   The last release is v1.9.0 (2026-09-29, with plugin-v2.271.0), so the
   next is 1.9.1 or 1.10.0. Semver: `vX.Y.Z`, pre-releases `vX.Y.Z-rc.N`.
   The docs deploy on a tag needs the `github-pages` environment to allow
   it: its deployment rules allow the branch `main` and tags `v*` (added
   2026-09-29, after the v1.9.0 docs deploy was refused).
3. Optional: `gh release create v1.9.0 --verify-tag --notes-file notes.md`
   for release notes on GitHub (it fires Docker Publish again for the same
   tag; the concurrency group runs them in turn and the result is the same).

### Check it worked

```
gh run list -R lucas-romanenko/jellyfin-tentacle --branch v1.9.0      # Docker Publish, Deploy Documentation, Tests: success
gh run view <id> -R lucas-romanenko/jellyfin-tentacle --json jobs --jq '.jobs[] | "\(.name) \(.conclusion)"'
```

The image, anonymously (it's public): the tag exists and `latest` points at
the same digest.

```
tok=$(curl -s "https://ghcr.io/token?scope=repository:lucas-romanenko/jellyfin-tentacle:pull" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $tok" https://ghcr.io/v2/lucas-romanenko/jellyfin-tentacle/tags/list
for t in 1.9.0 latest; do curl -sI -H "Authorization: Bearer $tok" -H "Accept: application/vnd.oci.image.index.v1+json" \
  https://ghcr.io/v2/lucas-romanenko/jellyfin-tentacle/manifests/$t | grep -i docker-content-digest; done
```

A running install says what it is: `GET /api/version` on it shows `commit`
(= `git rev-list -n1 v1.9.0`) and `code.matches: true`. Installs that follow
`latest` (Lucas's included) only get new code from a release; how Lucas
deploys and checks his own is in his homelab manual.

### Plugin release (Lucas)

Tag `plugin-vX.Y.Z` on main and push it (with a server release on the same
commit, tag the plugin first). Check: the GitHub release has
`tentacle-plugin-vX.Y.Z.zip`, and main has the bot's "Update plugin manifest
for vX.Y.Z" commit. Notes: `gh release edit plugin-vX.Y.Z --notes-file
notes.md --latest=false`, so the server release stays "latest".

## Open items

- Title prefixes: `STRIP_PREFIXES` in `services/cleaner.py` is a fixed list
  (NF, AMZ, HBO, ...) and misses unknown codes (e.g. `NF-DO`); a generic rule
  for short uppercase codes before ` - `, maybe reviewable per category.
- Playlist refresh efficiency (diffs, DB-computed tag playlists, parallel
  users): [docs/agents/pipelines.md](docs/agents/pipelines.md).
- The Radarr/Sonarr webhooks accept unsigned events unless `webhook_secret`
  is set: consider making it required, as the music webhook's is.

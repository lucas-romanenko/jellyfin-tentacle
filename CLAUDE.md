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
  (`.replace("-", "")`) before comparing.

## Plugin (`tentacle-plugin/`)

`Api/` (the HTTP endpoints: `TentacleHome`, `TentacleDiscover`, `Tentacle`,
`TentacleMusic`, ...), `Inject/` and `Web/` (the web UI it injects into
Jellyfin), `HomeScreen/`, `Playlists/`, `Services/`, `Tasks/`, `Patching/`,
`Configuration/`.

Every endpoint, its auth level and which the TV app calls:
[docs/agents/plugin-api.md](docs/agents/plugin-api.md). Rule: a user-scoped
endpoint checks the caller with `CallerIdentity` (`[Authorize]` alone only
proves someone is signed in); admin-only ones use
`[Authorize(Policy = "RequiresElevation")]`.

## Work on it

Clone at `/code/jellyfin-tentacle` on the workbench; work in a worktree on a
branch, merge to main, push (the workbench's workflow). Tests:

```
scripts/check    # the full unit suite, as CI runs it (a venv cached in ~/.cache/jellyfin-tentacle)
```

The workbench's pre-push hook runs `scripts/check` before main moves (about
4 min). CI (`tests.yml`) runs the same suite on every push and pull request.
Merging to main publishes nothing (see below).

## Releasing is Lucas's decision

Releasing publishes to other people's servers and Jellyfin installs, so
only Lucas tags or creates releases. A Claude session may prepare release
notes (what changed since the last tag: `git log vA.B.C..main`) and say
"ready to release"; it never creates a tag or a release itself.

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
   The last published image is 1.8.8 (its git tag no longer exists), so the
   next is 1.9.0 or later. Semver: `vX.Y.Z`, pre-releases `vX.Y.Z-rc.N`.
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

Tag `plugin-vX.Y.Z` on main and push it. Check: the GitHub release has
`tentacle-plugin-vX.Y.Z.zip`, and main has the bot's "Update plugin manifest
for vX.Y.Z" commit.

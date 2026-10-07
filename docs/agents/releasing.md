# Releasing

Releases publish to other people's servers and Jellyfin installs, so only
the maintainer tags or creates releases. Release tags (`v*`, `plugin-v*`)
are protected by a GitHub ruleset; the maintainer makes them with a private
tool (`tentacle-tag`, documented in ~/homelab/docs/tentacle.md "Rulesets and
releases", Lucas's server, private). The GitHub UI can't create one.

## What CI publishes

| Trigger | Workflow | Publishes |
|---|---|---|
| push to any branch, pull request | Tests | nothing (unit tests; the `unit` check) |
| docs change on main | Deploy Documentation | nothing (build only, `mkdocs --strict`) |
| tag `vX.Y.Z`, or a GitHub release on it | Docker Publish | image tags `X.Y.Z`, `X.Y` and `latest` (a pre-release like `v2.0.0-rc.1` gets only its own tag, never `latest`) |
| tag `vX.Y.Z`, or a GitHub release on it | Deploy Documentation | the docs site |
| Run workflow (by hand) | Deploy Documentation | the docs site, from the chosen branch |
| tag `plugin-vX.Y.Z` | Plugin Release | a GitHub release with the plugin zip, and a new entry in `tentacle-plugin/manifest.json` on main |

- The docs deploy on a tag needs the `github-pages` environment to allow it:
  its deployment rules allow the branch `main` and tags `v*`.
- Users' Jellyfin reads the plugin catalog from `tentacle-plugin/manifest.json`
  on main (raw.githubusercontent.com), so that file is a release too: only
  Plugin Release edits it. It pushes the manifest commit to main with its own
  deploy key (secret `MANIFEST_DEPLOY_KEY` of environment `plugin-manifest`,
  which only `plugin-v*` tags may use), which also runs Tests on it.

## Release notes (what a session prepares)

User-visible changes by area, issue numbers in brackets, upgrade notes,
from `git log vA.B.C..main`. End with "ready to release vX.Y.Z", plus "and
plugin-vX.Y.Z" when `tentacle-plugin/` changed since the last plugin tag:

```
git diff --stat plugin-vA.B.C origin/main -- tentacle-plugin
```

Versions: semver `vX.Y.Z`, pre-releases `vX.Y.Z-rc.N`. Find the last
release with `gh release list -R lucas-romanenko/jellyfin-tentacle -L 5`.

## Order and notes

- Main must be green: `gh run list -R lucas-romanenko/jellyfin-tentacle --branch main -L 3`.
- Server and plugin on the same commit: tag `plugin-vX.Y.Z` first, then
  `vX.Y.Z` on the same sha (the manifest commit moves main's tip).
- Server release notes go on the existing tag afterwards
  (`gh release create vX.Y.Z --verify-tag --notes-file notes.md`, or the
  GitHub UI). It fires Docker Publish again for the same tag; the
  concurrency group runs them in turn and the result is the same.
- Plugin release notes: `gh release edit plugin-vX.Y.Z --notes-file notes.md
  --latest=false`, so the server release stays "latest".

## Check a release

```
gh run list -R lucas-romanenko/jellyfin-tentacle --branch vX.Y.Z      # Docker Publish, Deploy Documentation, Tests: success
gh run view <id> -R lucas-romanenko/jellyfin-tentacle --json jobs --jq '.jobs[] | "\(.name) \(.conclusion)"'
```

The image, anonymously (it's public): the tag exists and `latest` points at
the same digest.

```
tok=$(curl -s "https://ghcr.io/token?scope=repository:lucas-romanenko/jellyfin-tentacle:pull" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $tok" https://ghcr.io/v2/lucas-romanenko/jellyfin-tentacle/tags/list
for t in X.Y.Z latest; do curl -sI -H "Authorization: Bearer $tok" -H "Accept: application/vnd.oci.image.index.v1+json" \
  https://ghcr.io/v2/lucas-romanenko/jellyfin-tentacle/manifests/$t | grep -i docker-content-digest; done
```

Plugin: the GitHub release has `tentacle-plugin-vX.Y.Z.zip`, and main has
the bot's "Update plugin manifest for vX.Y.Z" commit (with a Tests run).

A running install says what it is: `GET /api/version` shows `commit`
(= `git rev-list -n1 vX.Y.Z`) and `code.matches: true`. Installs that
follow `latest` get new code only from a release.

# Triage: GitHub issues and pull requests

The maintainer's sessions follow the workbench's github-triage skill (sort
every item, reproduce bugs before changing code, never build a feature
without the `approved` label). Tentacle specifics:

- Reproduce against what the maintainer's install runs (the server commit
  at `/api/version`, the plugin version Jellyfin reports; how to read them:
  ~/homelab/docs/tentacle.md, private), not main.
- Not reproduced: label `needs-info` and ask for the Tentacle server version
  (`/api/version` commit), plugin version (Dashboard → Plugins), Jellyfin
  version, the client (web, Android TV app and its version, other) and any
  local patches. The bug report template asks the same.
- Reproduced: failing test first, fix on `fix/<n>-<slug>`, push the branch,
  open a pull request and confirm it on staging where staging can show it,
  merge (squash), deploy and verify (CLAUDE.md "Deploy"), confirm on the
  maintainer's install, comment Cause and Change, close.
- Pull requests from others: read the diff and CI's result; never check
  them out to run or build on the workbench. One that changes
  `tentacle/Dockerfile`, `requirements.txt` or `.dockerignore` can't be
  staged (its build steps would run on the server): the maintainer reviews it.
- Labels: `needs-info`, `needs-lucas` (a feature or idea waiting for the
  maintainer), `approved` (only the maintainer adds it: a feature may be
  built), `security`, plus GitHub's defaults.
- Issue forms: `.github/ISSUE_TEMPLATE/` (bug report, feature request;
  `config.yml` keeps blank issues for questions and links the docs,
  troubleshooting page and Discussions).

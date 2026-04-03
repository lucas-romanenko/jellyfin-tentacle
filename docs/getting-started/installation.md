# Installation

Tentacle runs as a single Docker container alongside your existing Jellyfin setup. No external databases, message queues, or build steps required.

## Requirements

- **Jellyfin** 10.11 or later
- **Docker** and **Docker Compose**
- A Jellyfin API key (generated in Jellyfin Dashboard → API Keys)

## Docker Compose

Add Tentacle to your existing `docker-compose.yml`:

```yaml
tentacle:
  image: ghcr.io/lucas-romanenko/jellyfin-tentacle:latest
  container_name: tentacle
  ports:
    - 8888:8888
  volumes:
    - ./tentacle-data:/data                        # Required — database and config
    - /your/movies:/media/movies                   # Radarr movies library
    - /your/shows:/media/shows                     # Sonarr TV library
    - /your/vod-movies:/media/vod/movies           # VOD movies (streaming provider)
    - /your/vod-shows:/media/vod/shows             # VOD series (streaming provider)
  restart: unless-stopped
```

Then start it:

```bash
docker compose up -d
```

!!! info "Volume mounts"
    - `./tentacle-data:/data` is the **only required** volume — it stores the SQLite database, config, TMDB cache, and playlist configs.
    - The other four volumes are optional depending on which features you use.
    - **Only change the left side** of each mount to match your host paths. The right side (`/data`, `/media/...`) is fixed.
    - Skip `/media/movies` and `/media/shows` if you don't use Radarr/Sonarr.
    - Skip `/media/vod/...` if you don't have a streaming provider.

!!! tip "Verify your mounts"
    After starting, go to **Settings → Library Paths** in the Tentacle dashboard. Each path shows green if mounted correctly or red if missing.

## What Gets Created

Inside your data volume (`./tentacle-data`), Tentacle creates:

| Path | Purpose |
|------|---------|
| `tentacle.db` | SQLite database — all settings, providers, content metadata |
| `tmdb_cache/` | Cached TMDB metadata (30-day movies, 7-day series) |
| `smartlists/` | Per-user playlist configuration files |
| `home-configs/` | Per-user home screen layout files |

## Accessing the Dashboard

Open your browser to `http://<your-server-ip>:8888`.

On first launch, you'll see the [setup wizard](setup-wizard.md) which guides you through connecting to Jellyfin.

## Updating

Tentacle publishes Docker images to GitHub Container Registry automatically on every push to `main`.

```bash
docker compose pull tentacle
docker compose up -d tentacle
```

!!! warning "Wait for the build"
    After pushing code changes to GitHub, the container image takes ~2 minutes to build. If you pull immediately, you'll get the old image. Wait for the GitHub Action to complete first.

## Fresh Install / Reset

To wipe all settings and start over:

```bash
docker stop tentacle && docker rm tentacle
rm -rf ./tentacle-data
docker compose up -d tentacle
```

This removes the database, all configs, and cached data. You'll go through the setup wizard again on next visit.

---

Next: [Setup Wizard →](setup-wizard.md)

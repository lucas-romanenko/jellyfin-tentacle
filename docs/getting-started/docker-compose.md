# Docker Compose Examples

## Minimal Setup (Jellyfin Only)

If you just want to manage playlists and your home screen — no VOD, no Radarr/Sonarr:

```yaml
tentacle:
  image: ghcr.io/lucas-romanenko/jellyfin-tentacle:latest
  container_name: tentacle
  ports:
    - 8888:8888
  volumes:
    - ./tentacle-data:/data
  restart: unless-stopped
```

## Full Setup (All Features)

VOD from a streaming provider, Radarr movies, Sonarr TV shows, and Live TV:

```yaml
tentacle:
  image: ghcr.io/lucas-romanenko/jellyfin-tentacle:latest
  container_name: tentacle
  ports:
    - 8888:8888
  volumes:
    - ./tentacle-data:/data
    - /mnt/media/movies:/media/movies          # Radarr library
    - /mnt/media/tv:/media/shows               # Sonarr library
    - /mnt/media/vod/movies:/media/vod/movies  # VOD movies
    - /mnt/media/vod/shows:/media/vod/shows    # VOD series
  restart: unless-stopped
```

## Alongside Jellyfin, Radarr, and Sonarr

A complete stack example:

```yaml
services:
  jellyfin:
    image: jellyfin/jellyfin:latest
    container_name: jellyfin
    ports:
      - 8096:8096
    volumes:
      - ./jellyfin-config:/config
      - /mnt/media:/media
    restart: unless-stopped

  radarr:
    image: linuxserver/radarr:latest
    container_name: radarr
    ports:
      - 7878:7878
    volumes:
      - ./radarr-config:/config
      - /mnt/media:/data
    restart: unless-stopped

  sonarr:
    image: linuxserver/sonarr:latest
    container_name: sonarr
    ports:
      - 8989:8989
    volumes:
      - ./sonarr-config:/config
      - /mnt/media:/data
    restart: unless-stopped

  tentacle:
    image: ghcr.io/lucas-romanenko/jellyfin-tentacle:latest
    container_name: tentacle
    ports:
      - 8888:8888
    volumes:
      - ./tentacle-data:/data
      - /mnt/media/movies:/media/movies
      - /mnt/media/tv:/media/shows
      - /mnt/media/vod/movies:/media/vod/movies
      - /mnt/media/vod/shows:/media/vod/shows
    restart: unless-stopped
```

## Volume Mount Reference

| Container Path | Purpose | Required? |
|---------------|---------|-----------|
| `/data` | Database, config, TMDB cache, playlist configs | **Yes** |
| `/media/movies` | Radarr movie library (where `.mkv` files live) | Only for Radarr |
| `/media/shows` | Sonarr TV library (where downloaded episodes live) | Only for Sonarr |
| `/media/vod/movies` | VOD movie `.strm` and `.nfo` files | Only for VOD providers |
| `/media/vod/shows` | VOD series `.strm` and `.nfo` files | Only for VOD providers |

!!! important "Path matching"
    The paths inside the container (right side of `:`) are fixed — don't change them. Only change the left side to match where your media lives on the host.

    The VOD paths must point to folders that Jellyfin also has access to. Tentacle creates `.strm` and `.nfo` files here, and Jellyfin needs to read them.

## Hybrid Series (VOD + Downloaded Episodes)

If you want to use "Download More Episodes" to fill in missing episodes from a VOD series via Sonarr, both Tentacle and Sonarr need access to the VOD shows folder:

```yaml
sonarr:
  volumes:
    - /mnt/media:/data
    - /mnt/media/vod/shows:/data/vod/tv    # Additional mount for hybrid series

tentacle:
  volumes:
    - /mnt/media/vod/shows:/media/vod/shows
```

Then add `/data/vod/tv` as a root folder in Sonarr (Settings → Media Management → Root Folders). This lets Sonarr download new episodes directly into existing VOD series folders, creating a unified view in Jellyfin.

## Network Considerations

!!! tip "Use internal IPs for webhooks"
    When configuring Radarr/Sonarr webhooks, use the internal Docker network IP or container name — not an external/tunnel URL. For example: `http://tentacle:8888/api/radarr/webhook` if on the same Docker network, or `http://192.168.x.x:8888/api/radarr/webhook` if using host networking.

If Tentacle, Radarr, and Sonarr are all on the same Docker network, you can use container names instead of IPs in the Tentacle settings:

- Radarr URL: `http://radarr:7878`
- Sonarr URL: `http://sonarr:8989`
- Jellyfin URL: `http://jellyfin:8096`

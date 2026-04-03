# Multi-User Support

Tentacle supports multiple Jellyfin users with role-based access. Each user gets their own playlists, home screen layout, and content preferences.

## Login

Tentacle uses a Netflix-style user picker for login:

1. The login page shows all Jellyfin users with their profile images
2. Click your account
3. Enter your Jellyfin password if required
4. You're logged in with a 30-day session

!!! info "Jellyfin authentication"
    Tentacle authenticates through Jellyfin's API — it doesn't store passwords. Your Jellyfin credentials are used directly.

## User Roles

There are two roles:

### Admin

Full access to everything:

- **Dashboard** — Sync status, statistics, activity
- **VOD** — Add/manage streaming providers, categories, sync
- **Live TV** — Manage channels, groups, EPG
- **Library** — Browse all content, follow series, manage episodes
- **Jellyfin** — Playlists, home screen, Discover tab
- **Settings** — All configuration, user management

### Non-Admin

Limited access:

- **Library** — Browse content, follow series
- **Jellyfin** — Their own playlists, home screen, Discover tab

Non-admins don't see the Dashboard, VOD, Live TV, or Settings pages.

### Admin Sync

Admin status is synced from Jellyfin on every login. If a user is a Jellyfin administrator, they're automatically an admin in Tentacle too.

### Owner

The first user to log into Tentacle becomes the **owner**. The owner:

- Is always an admin
- Cannot have admin status removed by anyone
- Is the fallback for legacy data migration

## What's Per-User vs. Shared

### Per-User (Independent)

Each user has their own:

- **Playlists** — Jellyfin playlists created per-user (`IsPublic=false`)
- **Auto playlist toggles** — Which auto playlists are enabled
- **Custom playlists** — Their own tag rules and filters
- **List subscriptions** — Which lists they're subscribed to
- **Home screen layout** — Hero spotlight, row order, row selection
- **Following** — Which series they follow

### Shared (Global)

These are shared across all users:

- **Providers** — VOD and Live TV providers
- **VOD sync** — Synced content is available to everyone
- **Radarr/Sonarr scans** — Library content is global
- **Tag application** — Tags applied to content are shared
- **Live TV channels** — Channel list and EPG data
- **Settings** — Server connections, API keys, paths

!!! example "How it works in practice"
    Admin Alice adds a Netflix provider and syncs it. The content appears in the library for everyone. Non-admin Bob goes to Jellyfin → Playlists and enables "Netflix Movies" — a private playlist is created in his Jellyfin account. Alice does the same — a separate private playlist is created in her account. Each can customize their home screen independently.

## Managing Users

Admins can manage users at **Settings → Users**:

- See all Jellyfin users who have logged into Tentacle
- Toggle admin status for other users (updates both Tentacle and Jellyfin's policy)
- The owner cannot be modified

## Session Management

- Sessions use HMAC-SHA256 signed cookies (30-day TTL)
- Sessions are HTTPOnly and SameSite=Lax for security
- Logging out clears the session cookie
- Switching users triggers a full page reload to clear all SPA state

## First Login Migration

When a user logs in for the first time, Tentacle automatically migrates any orphaned data (from before multi-user support) to their account. This includes auto playlist toggles, tag rules, and list subscriptions that weren't associated with a user.

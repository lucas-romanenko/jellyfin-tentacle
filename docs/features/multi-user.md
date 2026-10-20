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
- **People and Requests** — Accounts, kids profiles, request approvals
- **Settings** — All configuration

### Non-Admin

Limited access:

- **Library** — Browse content, follow series
- **Jellyfin** — Their own playlists, home screen, Discover tab

Non-admins don't see the Dashboard, VOD, Live TV, or Settings pages.

### Admin Sync

Admin status is synced from Jellyfin on every login. If a user is a Jellyfin administrator, they're automatically an admin in Tentacle too.

### Owner

The first user to log into Tentacle becomes the **owner**. That first sign-in must be a Jellyfin administrator: a non-admin account is refused until an admin has signed in. The owner:

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

## Your household (People)

Tentacle is built for one person running a home server for their family. Admins manage everyone at **People** (in the sidebar):

- **Invite someone:** type a name and Tentacle makes the Jellyfin account with an easy password (two words and two digits), starts them on the household's defaults, and shows an invite card to pass on: their name, password, the server address and how to set up the TV.
- **New people start with:** pick once whose home screen and whose Live TV channel list new people copy (or Tentacle's standard rows and all channels). They change their own afterwards.
- **Age limit (kids profiles):** one choice per person: *Young kids* (up to TV-Y7), *Kids* (up to PG) or *Teens* (up to PG-13 and TV-14). It uses Jellyfin's own parental controls, so every Jellyfin app follows it. Movies and shows without an age rating are hidden from kids too. In Live TV kids see only the **kids channels** you pick on People (none picked: no Live TV for kids). Switching it off puts back exactly what the person had before.
- **Their requests:** *Ask me first* (the default for everyone who isn't an admin, and always for kids) or *Straight away*.
- **Admin:** make someone an admin (both in Tentacle and in Jellyfin). The owner stays an admin.
- **Sign in a TV:** on the TV, open Tentacle, choose *Use Quick Connect* and type the code it shows into Tentacle. Everyone can do this for their own TV (sidebar → *Sign in a TV*); admins can do it for anyone. If Quick Connect is off in Jellyfin, People offers to turn it on.
- **New password:** gives someone a fresh password when they lost theirs.
- Each person's row shows which Tentacle TV app version their TV runs, and when a newer one is waiting.

## Requests with approval

When someone whose requests need approval asks for a movie, show or album (in Tentacle, in Jellyfin's Discover or on the TV), nothing downloads yet: they see "Sent to (admin) to approve", and every admin gets a message in Jellyfin (and on their phone with Pushover). Admins answer at **Requests**: *Approve* sends it to Radarr, Sonarr or Lidarr as if that person had asked (their Downloads playlist and "ready to watch" message work as usual), *Decline* takes an optional reason. The person gets a message either way and sees their requests in Discover → Activity, where they can take one back while it waits. Admins' own requests go straight through.

## Problems go to the admin

Every 10 minutes Tentacle checks what can break for the household: an IPTV provider failing, Radarr or Sonarr unreachable, a disk nearly full, downloads stuck, Live TV not set up in Jellyfin, Jellyfin down. Admins get one message when a problem starts and one when it's over. The family never sees error details, only a plain line where it matters, such as "Live TV is unavailable right now. (admin) knows." (Tentacle's pages, Jellyfin's Live TV page and the TV app).

## The TV app

The Tentacle Android TV app isn't in an app store. On each Android TV or Fire TV, install the free **Downloader** app, open it and type your Tentacle address followed by `/tv` (for example `http://192.168.1.10:8888/tv`): it downloads the newest release. After that the app offers every new version itself: *Update now*, then *Install*.

## Session Management

- Sessions use HMAC-SHA256 signed cookies (30-day TTL)
- Sessions are HTTPOnly and SameSite=Lax for security
- Logging out clears the session cookie
- Switching users triggers a full page reload to clear all SPA state

## First Login Migration

When a user logs in for the first time, Tentacle automatically migrates any orphaned data (from before multi-user support) to their account. This includes auto playlist toggles, tag rules, and list subscriptions that weren't associated with a user.

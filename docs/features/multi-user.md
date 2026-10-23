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
- **Live TV** — The channels people pick from (groups, folded networks, types), EPG, who has how many channels
- **Library** — Browse all content, follow series, manage episodes
- **Jellyfin** — Playlists, home screen, Discover tab
- **People and Requests** — Accounts, kids profiles, request approvals
- **Settings** — All configuration

### Non-Admin

Limited access:

- **Library** — Browse content, follow series
- **Jellyfin** — Their own playlists, home screen, Discover tab
- **My Channels** — Their favourite Live TV channels

Non-admins don't see the Dashboard, VOD, Live TV admin, or Settings pages.

### Live TV: everyone's own favourites

The household has one Live TV lineup, the channels the admin switched on (Sources → Live TV). Under **My Channels** each person stars their favourites, a whole type at once (Sports, News, Local…) or one channel at a time; favourites are Jellyfin's own, per person, so every app shows them. Kids profiles see only the kids channels. Details: [Live TV → My Channels](live-tv.md#my-channels).

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

- **Add someone**, in three steps:
    1. **Who:** their name, grown-up or kid (with an age limit), where they watch Live TV from, and a password made from their name (their name in lowercase, five digits and "!", like `cam12457!`, easy to type on a TV remote; change it if you like).
    2. **What they get:** Live TV or not, how their requests work, and the home screen and favourite channels they start with (the household's choice below, or another for just this person).
    3. **The message:** a ready-to-send message with **Copy** and **Share**. It explains Tentacle first (where they pick their channels, set up their home screen and ask for movies and shows), gives the sign-in (the same for everything), then where to watch (Jellyfin's address, in the browser on a phone, laptop or computer, the website on an iPhone or iPad), and last how to put the TV app on an Android TV or Google TV. The addresses are your own (Settings → Services); while they only work on your home network, Add someone asks once whether there are internet ones.
- **New grown-ups start with:** pick once whose home screen and whose Live TV favourites new grown-ups copy (or the default home and no favourites). They change their own afterwards under My Channels (a few quick picks give a short list of favourites in under a minute).
- **New kids start with:** a ready-made kids setup unless you pick something else: a kids home (Continue Watching, Kids Movies and Kids Shows, which are cartoons and family films and shows) and the kids channels as their favourites, all within each kid's age limit.
- **Their first sign-in:** Tentacle welcomes them once, in five short steps they can skip: keep their password or pick their own, what Tentacle is, their channels (quick picks), their home screen, how to ask for movies and shows, and where to watch. Kids get the same with only what kids have.
- **Wrong passwords:** after 5 wrong passwords in a row Jellyfin locks the account (everyone but admins, including accounts made in Jellyfin's dashboard). People shows them as *Locked* with **Unlock**.
- **Age limit (kids profiles):** one choice per person: *Young kids* (up to TV-Y7), *Kids* (up to PG) or *Teens* (up to PG-13 and TV-14). It uses Jellyfin's own parental controls, so every Jellyfin app follows it. Movies and shows without an age rating are hidden from kids too. In Live TV kids see only the **kids channels** you pick on People (none picked: no Live TV for kids); they can star them but not change them. Switching it off puts back exactly what the person had before.
- **Live TV from:** where someone watches from, for their local channels: someone set to Vancouver gets CTV, Global, CBC and City from Vancouver and NBC, ABC and FOX from Seattle (and the West feeds of channels that have one) instead of yours; the rest of the lineup is the same for everyone. The list is the cities your provider's channels are named after. Everyone starts at yours (also in Add someone, where you can change it), and the owner's place is the household's. Changing it moves their favourites to their new place's channels at once (Jellyfin reads the new channels first when nobody in the household watched from there yet: about a minute).
- **Their requests:** *Ask me first* (the default for everyone who isn't an admin, and always for kids) or *Straight away*.
- **On sign-in screen:** whether someone is shown on the "Who's watching?" screen (Tentacle's and Jellyfin's) to pick instead of typing their name. People you add are; accounts made in Jellyfin's own dashboard start hidden (Jellyfin's default) until you switch this on. Names are only ever shown on your home network: from the internet everyone types their name, or signs in with a code from a device where they're already signed in.
- **Admin:** make someone an admin (both in Tentacle and in Jellyfin). The owner stays an admin.
- **Sign in a TV:** on the TV, open Tentacle, choose *Use Quick Connect* and type the code it shows into Tentacle. Everyone can do this for their own TV (sidebar → *Sign in a TV*); admins can do it for anyone. If Quick Connect is off in Jellyfin, People offers to turn it on.
- **New password:** gives someone a fresh password (made from their name) when they lost theirs, with the message to send them.
- **More** (next to each person who isn't an admin) holds the Jellyfin settings a household needs, read from Jellyfin every time so they never show a stale value, and saved there straight away:
    - **Live TV:** whether they have Live TV at all (a kid's is the kids channels).
    - **Can record shows:** schedule and cancel Live TV recordings (shared by everyone). Jellyfin makes new accounts without it; people you add get it, kids don't.
    - **Can watch away from home:** off means only on your home network. Behind a reverse proxy, Jellyfin needs its *Known proxies* set (Dashboard → Networking) to tell home from away.
    - **Libraries:** all (including ones added later) or only the ones you tick. Tentacle's Library then lists only what they can open.
    - **Watching hours:** any time, or only between two times every day (it may run past midnight), by the server's clock. Outside them Jellyfin won't let them sign in or play.
    - **Turn account off:** they can't sign in or watch until you turn it on again; nothing is deleted. *Turn on* also clears Jellyfin's count of wrong passwords.
- The row shows what differs from the usual at a glance: *Home only*, the number of libraries, the watching hours, *Turned off*.

**Settings → People & sign-in** has the two server-wide ones: **TV sign-in codes** (Jellyfin's Quick Connect, recommended on) and **Quality away from home** (a cap for everyone watching outside your home network, for a slow upload; no limit by default).
- Each person's row shows which Tentacle TV app version their TV runs, and when a newer one is waiting.
- **Removing someone:** delete their account in Jellyfin (Dashboard → Users). Jellyfin signs them out everywhere and removes their playlists (Tentacle deletes the copies a library scan brings back); Tentacle forgets the rest (their home screen, playlist settings, requests, notifications, Live TV lists) the next time People is opened, or in the nightly sync.

## Requests with approval

When someone whose requests need approval asks for a movie, show or album (in Tentacle, in Jellyfin's Discover or on the TV), nothing downloads yet: they see "Sent to (admin) to approve", and every admin gets a message in Jellyfin (and on their phone with Pushover). Admins answer at **Requests**: *Approve* sends it to Radarr, Sonarr or Lidarr as if that person had asked (their Downloads playlist and "ready to watch" message work as usual), *Decline* takes an optional reason. The person gets a message either way and sees their requests in Discover → Activity, where they can take one back while it waits. Admins' own requests go straight through.

Everything about a request shows up on open screens within a second or two, without a reload: the admins' Requests list and badge when someone asks, the person's Activity badge, their requests and Discover's "Asked" / "Declined" when an admin answers, download progress and "ready to watch" (in Tentacle, in Jellyfin's Tentacle pages and messages, and on the TV app).

### Other people's downloads (admins)

Admins see everyone's downloads in their Activity, each marked who it's for ("for Sam"), and get the "ready to watch" message for them too. To see only your own, turn off **Show other people's downloads in my Activity** under My Home Screen → Download notifications. Requests waiting for your answer stay in Requests either way. Everyone else only ever sees their own.

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

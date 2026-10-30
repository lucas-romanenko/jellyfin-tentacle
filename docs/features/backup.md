# Backup & Restore

Tentacle can save what you set up by hand — and put it back, on the same
server or on another one. **Settings → Backup & restore** (admins).

## What's in a backup

You pick the parts:

| Part | What |
|---|---|
| VOD categories | the categories you ticked, and their source names (per provider) |
| Live TV channels | the channel groups you switched on, the channels you switched on or off, your channel names, numbers, logos and guide ids (per provider) |
| Home rows | everyone's home screen rows and spotlight, and the default home |
| Playlists | everyone's ready-made playlists that are on, and their own playlists |
| Lists | everyone's Trakt, Letterboxd and IMDb lists |
| Recording rules & teams | the teams people follow and the keyword rules |

You can also pick whose home rows, playlists and lists go in.

**Never in the file:** provider logins and addresses, API keys, tokens and
passwords. On a new server, add your providers and keys first, then
restore.

## Saved backups

Tentacle saves a backup of everything every night at 02:30 in its data
folder (`/data/backups`) and keeps the last 14. **Back up now** saves one
straight away; **Download file** gives you a file of the parts you ticked.

## Restoring

Pick **Restore from a file…** or **Restore…** on a saved backup. Nothing
changes until you press **Restore**: first you see, for each part, what
would be added, changed, removed and what isn't found on this server.

- Everything is matched **by name**: "Movies | 4K", "Sports", a channel's
  name, a person's name. A provider that renumbered its categories still
  lines up.
- **Another provider?** If this server has no provider with the name in the
  file, choose which of your providers the categories and channels go to —
  Tentacle shows how many of them match by name — or skip them. The other
  parts restore either way.
- **Add & update** (the default) never removes anything. **Replace** also
  takes away what the file doesn't have, for that part only.
- Your setup as it was is saved first (“before a restore” in the list), so
  a restore can be undone by restoring that.

Playlists and home rows finish building in Jellyfin in the background a
minute or two after a restore.

# Home Screen

Tentacle gives you a Netflix-style home screen in Jellyfin with a hero spotlight banner and customizable playlist rows. Each Jellyfin user has their own independent home screen layout.

!!! note "Requires the Tentacle plugin"
    The custom home screen is rendered by the [Tentacle Jellyfin plugin](../integrations/jellyfin-plugin.md). Without the plugin installed, playlists still work but the hero banner and custom layout won't appear in Jellyfin.

## Setting Up Your Home Screen

Go to **My Home Screen** in the Tentacle dashboard.

## The default home

Everyone starts on the **default home**, so a fresh install looks right with no setup: Continue Watching (with the next episodes of your shows), On Now, Recently Added Movies, Recently Added TV and your biggest sources, with recently added movies in the spotlight.

- **Admins** change it with **My Home Screen → Default for everyone**: the same rows, order, poster or wide cards and spotlight as your own home, or **Copy my home here** to make your own layout the default (your personal lists are left out; nobody else has them). Everyone on the default sees a change within seconds.
- **Everyone else** keeps the default until they change something on their own Home Screen; from then on the layout is theirs and later changes to the default leave it alone. **Reset to default** goes back.
- **Kids** get the same rows, showing only what their age limit allows.

## Hero Spotlight

The hero spotlight is a large banner at the top of the Jellyfin home page that cycles through featured content with a Ken Burns zoom effect and smooth crossfade transitions.

### Configuring the Hero

1. **Enable/disable** — Toggle the hero on or off
2. **Pick a playlist** — Choose which playlist's content appears in the hero rotation (e.g., "Recently Added Movies")
3. **Sort order** — How items are ordered in the rotation:
    - Random (default)
    - Release date
    - Name
    - Rating
    - Recently added
4. **Title image only** — When enabled (default), only shows items that have both a backdrop image AND a logo/title image. This ensures a polished look. Disable to include items without logos.

The hero sort order is independent from the playlist's own sort — you can have a playlist sorted by rating but the hero cycling randomly.

## Playlist Rows

Below the hero, your home screen consists of rows. Each row is either a Tentacle playlist or one of Tentacle's own rows.

### Adding Rows

Click the dropdown to add a row. Available options include:

**Tentacle playlists:**

- Any auto playlist you've enabled (Netflix Movies, Recently Added, etc.)
- Any custom playlist you've created

**Tentacle rows:**

- **Continue Watching**: what you stopped part-way. Its **Next episodes** switch adds the next episode of each show you're watching to the same row.
- **Next Up**: the next episodes on a row of their own. It is in the list only while Continue Watching's **Next episodes** is off; switching Next episodes back on takes the Next Up row away, so nothing shows twice.
- **On Now**: what's on now on your favourite channels.
- **Recordings**: your latest Live TV recordings.

Next episodes come most recently watched first. A show you had caught up on whose new season (or new episode) arrives later jumps to the front from the day it arrived, instead of waiting behind every show you watched since.

### Reordering Rows

Drag and drop rows to change their order. Changes are saved and pushed to Jellyfin automatically.

### Removing Rows

Click the remove button on any row to take it off your home screen. The underlying playlist still exists — it's just not shown as a home screen row.

### Max Items Per Row

Each row can have a limit on how many items it displays. This is useful for large playlists where you only want to show a curated subset on the home screen.

## Favorites

Jellyfin's **Favorites** tab (the heart in the top bar) shows your favourites in rows like the home screen, each scrolling sideways: **Movies**, **Shows**, **Live TV**, **YouTube**, **Recordings**, then episodes, collections, playlists, music and anything else you have favourited. Rows with nothing in them are left out.

- **Live TV** cards show the channel's logo and number, what's on now with how far in it is, and play the channel when you click them. Channels come in the guide's order.
- **YouTube** holds the videos and YouTube live channels you favourited (only the YouTube channels you follow).
- Kids see only what their profile allows, like everywhere else in Jellyfin.
- Favourite or unfavourite something anywhere (Tentacle's My Channels, the guide's star, a details page, another device) and the open Favorites page shows it within a second or two, no reload. On a computer, the heart on a card (when you point at it) takes it off the list.

## How It Works Internally

Your home screen configuration is stored as a JSON file at `/data/home-configs/{jellyfin_user_id}.json`. The Tentacle plugin fetches this config via API and renders the home screen accordingly.

```json
{
  "hero": {
    "enabled": true,
    "playlist_id": "abc123",
    "display_name": "Recently Added Movies",
    "sort_by": "random",
    "require_logo": true
  },
  "rows": [
    {"type": "playlist", "playlist_id": "abc123", "display_name": "Recently Added Movies", "order": 1},
    {"type": "builtin", "section_id": "resumevideo", "display_name": "Continue Watching", "order": 2},
    {"type": "playlist", "playlist_id": "def456", "display_name": "Netflix Movies", "order": 3}
  ]
}
```

### Playlist ID Remapping

When playlists are recreated during a sync (which can assign new Jellyfin IDs), Tentacle automatically remaps your home screen rows by playlist name to preserve your layout. You won't notice anything — rows just keep working.

### Validation

If you delete a playlist that was used as the hero spotlight, the hero automatically resets to the first available playlist (or disables itself if no playlists exist).

## Per-User Home Screens

Every Jellyfin user has a completely independent home screen:

- Their own hero spotlight configuration
- Their own set of rows in their own order
- Their own max items per row settings

This means one household member can have a Netflix-focused home screen while another has a "Recently Added" and "Continue Watching" focus.

## Pushing Changes

All changes to your home screen are pushed to Jellyfin automatically:

- Adding or removing rows
- Reordering rows
- Changing the hero playlist or settings
- Changing max items

No manual "push" or "sync" button is needed.

!!! tip "Cache delay"
    The Jellyfin plugin caches home config for 5 seconds per user. After making changes in Tentacle, you may need to refresh the Jellyfin page to see updates.

# Home Screen

Tentacle gives you a Netflix-style home screen in Jellyfin with a hero spotlight banner and customizable playlist rows. Each Jellyfin user has their own independent home screen layout.

!!! note "Requires the Tentacle plugin"
    The custom home screen is rendered by the [Tentacle Jellyfin plugin](../integrations/jellyfin-plugin.md). Without the plugin installed, playlists still work but the hero banner and custom layout won't appear in Jellyfin.

## Setting Up Your Home Screen

Go to **Jellyfin → Home Screen** in the Tentacle dashboard.

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

Below the hero, your home screen consists of rows. Each row is either a Tentacle playlist or a native Jellyfin section.

### Adding Rows

Click the dropdown to add a row. Available options include:

**Tentacle playlists:**

- Any auto playlist you've enabled (Netflix Movies, Recently Added, etc.)
- Any custom playlist you've created

**Native Jellyfin sections:**

- Continue Watching
- Next Up
- Latest Media
- *(other built-in Jellyfin sections)*

### Reordering Rows

Drag and drop rows to change their order. Changes are saved and pushed to Jellyfin automatically.

### Removing Rows

Click the remove button on any row to take it off your home screen. The underlying playlist still exists — it's just not shown as a home screen row.

### Max Items Per Row

Each row can have a limit on how many items it displays. This is useful for large playlists where you only want to show a curated subset on the home screen.

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

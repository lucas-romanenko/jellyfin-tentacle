# Music

Find music the way you find movies: browse or search, press Request, and the
album lands in your library. Tentacle always asks for the **original studio
album**, with its original tracklist: no deluxe editions, anniversary reissues,
live albums or compilations.

Tentacle finds and requests music; it never plays it or touches the files.
[Lidarr](https://lidarr.audio) downloads, tags and files every album. Your
player (Navidrome, or Jellyfin) plays it.

!!! warning "Off by default"
    Until you turn it on in **Settings → Music**, Tentacle shows no music
    anywhere and changes nothing.

## What you need

| | Required | What for |
|---|---|---|
| **Lidarr** | Yes | Downloads, tags and files the music |
| **MusicBrainz contact email** | Yes | MusicBrainz requires a contact in every request. It is sent only to MusicBrainz. |
| **Navidrome** | Optional | Artist pictures, a rescan after each import, "Open in Navidrome" links |
| **Jellyfin as a music player** | Optional | The same, in Jellyfin, plus music search inside Jellyfin's web app |

No other accounts or API keys are needed. Charts, genres and artist pictures
come from free, public sources.

## Setup

**1. Turn it on** in **Settings → Music** (tick *Enable the music module*).
A **Music** section appears at the bottom of **Settings → Connections**.

**2. Connect Lidarr** there: its URL and API key, then **Test**. Pick:

- **Root folder:** where Lidarr keeps your music
- **Quality profile:** the one you want albums downloaded in (for example a
  lossless-first profile)
- **Metadata profile:** usually *Standard*

Artists Tentacle adds to Lidarr get these, with only the album you asked for
monitored. Tentacle never falls back to Lidarr's first profile.

**3. Add your MusicBrainz contact email**, then **Test**.

**4. Optional: connect your player.** For Navidrome, tick it and enter its URL
and a user (an admin can also start rescans). For Jellyfin, tick *Jellyfin as
a music player* and pick or create its music library.

**5. Save Connections.** Tentacle then reads your Lidarr library once (about
ten minutes for a few hundred albums; MusicBrainz allows one request a second).
**Library → Music → Check library now** runs it again any time.

**6. Optional: add the Lidarr webhook** so new downloads show up in Tentacle
right away instead of at the daily check. **Settings → Music** shows the URL.
In Lidarr, go to *Settings → Connect → + → Webhook*, paste it, and tick
*On Artist Add*, *On Release Import* and *On Upgrade*. **Send test** confirms
it arrives.

## Discover → Music

Pick **Music** in Discover. The search box finds artists, albums and songs.
Without a search there are four tabs:

- **Trending:** the artists and songs on Apple Music's charts for your country
  right now. A song card opens the album it first came out on.
- **New releases:** albums out in the last eight weeks that are charting, with
  genre filters, plus **From your artists**: new and announced albums by
  artists already in your library.
- **Top of all time:** the most-listened studio albums on ListenBrainz, by
  genre. *Hide albums I have* narrows it to what's missing.
- **From Spotify:** your imported playlists (below).

The chart country is **Settings → Music → Chart country**. The lists are
built in the background, once a day for the charts and once a month for Top
of all time. The first all-time list takes about an hour to build, and genres
fill in as it goes.

Every card shows whether you have the album (*In library*, *Downloading*,
*Wanted*). The **+** button requests it.

## Requests

Whatever you request, Tentacle asks Lidarr for the original studio album:

- **An album:** its original release, meaning the tracklist most releases had
  in the year it first came out.
- **A song:** the studio album the song first appeared on. Songs that only
  came out on singles or soundtracks say so instead.
- **An artist:** their studio albums are listed on the artist page, and you
  pick which to request.

When the original is unclear (two tracklists equally common in the first year,
or an edition with a hidden track), the album goes to **Needs review** and you
choose. Tentacle never guesses.

## Spotify playlists

**Discover → Music → From Spotify → Import a playlist.** Either:

- **paste a public playlist link**, which reads the first 100 songs, or
- **upload an [Exportify](https://exportify.net) CSV**, for any size of
  playlist, private ones too.

No Spotify account or key is needed. Tentacle finds the original studio album
of every song and shows you the list, with the songs each album covers.
Nothing is requested until you tick albums and press **Request**. Songs it
can't place (singles, soundtracks, artists MusicBrainz doesn't know) are listed
with the reason.

A linked playlist can be **refreshed** later: new songs are looked up, and
known ones keep their result.

## Library → Music

Your monitored albums, by artist, with filters for *In library*,
*Downloading*, *Wanted* and *Needs review*.

**Fix library** (admins) is where the daily check puts albums that aren't on
their original release:

| Group | What Apply does |
|---|---|
| **Re-pin** | Your files already fit the original. Tentacle pins it, and Lidarr re-matches the files. |
| **Re-pin and remove extra tracks** | Pins the original, waits for Lidarr's rescan, then has Lidarr delete the tracks the original doesn't have (bonus and deluxe tracks). |
| **Re-pin and download** | Pins the original, then Lidarr searches for the missing tracks. |
| **Needs review** | You pick the tracklist. |
| **Right, but not locked** | Turns off Lidarr's "any release OK", so it can't import another edition later. |

Nothing changes until you press **Apply**, per album or per group. Each album
is checked again with fresh data first. Extra tracks are only removed when
Lidarr matched every original track and exactly the expected number is left
over; anything else stops with nothing deleted. Every deletion goes to the
Deletion log.

!!! tip "Set a recycle bin in Lidarr first"
    Removed tracks go to Lidarr's recycle bin (*Settings → Media Management*).
    Without one they are deleted for good, and the page says so.

**Settings → Music → Apply automatically** can apply the re-pin groups after
each daily check. All three are off by default.

## Artist pictures

With a player connected, Tentacle gives artists that show no picture a real
one: the artist's own `artist.jpg` if Tentacle can see your music folder
(optional setting), otherwise their photo from Deezer, matched by exact name.
Placeholders (grey stars, blank pictures) are never used. Pictures that
already work are left alone.

When the name is ambiguous (MusicBrainz knows two bands called *Crowbar*, say)
the artist goes to **Fix library → Artist pictures**, where you pick the right
photo or upload one.

## Settings reference

| Setting | Default | |
|---|---|---|
| Enable the music module | Off | |
| Chart country | United States | Whose Apple Music charts Trending and New releases follow |
| Preferred countries / formats | US, GB, CA, XW / CD, Digital Media | Break ties between releases with the original tracklist |
| Words that mark a special edition | deluxe, expanded, anniversary… | Releases with these are passed over when another has the same tracklist |
| Daily check at | 04:00 | |
| Apply automatically | All off | |
| Music folder as Tentacle sees it | Empty | Lidarr's root folder mounted read-only into Tentacle, to reuse `artist.jpg` files |
| Deezer as a source of artist pictures | On | |

# YouTube Channels

Add a YouTube channel and its videos appear in Jellyfin as ordinary Movie
items — streamed on demand, never downloaded.

Each video gets a `.strm` pointing back at Tentacle, which resolves the stream
with yt-dlp and proxies it as seekable HLS. Every Jellyfin client works,
because Jellyfin only ever sees a normal HLS source.

!!! warning "Off by default"
    The feature needs its own media mount and its own Jellyfin library, so it
    never starts indexing unasked.

## Setup

**1. Mount a media directory** for YouTube content, separate from your VOD and
downloads folders:

```yaml
volumes:
  - /your/host/youtube:/media/youtube
```

It is deliberately separate. YouTube videos are kept out of the `movies` and
`series` tables entirely — those carry a unique TMDB id and the VOD sync runs
TMDB matching over them, so a video called "Frozen" would be imported as the
real film and share its folder.

**2. Add a Jellyfin library** pointing at that folder:

- Content type: **Movies**
- Turn **off** every internet metadata and image fetcher — Tentacle writes the
  NFO and there is nothing on TMDB to match
- Turn off real-time monitoring; Tentacle triggers the refresh

**3. Turn it on** from the YouTube page. It asks for one thing — the address
your Jellyfin server can reach Tentacle on — and suggests the address you
opened the dashboard with.

Tentacle then checks your channels for new uploads and live streams about
once an hour, on its own. See [How Tentacle talks to YouTube](#how-tentacle-talks-to-youtube)
for what that costs and what you can change.

!!! danger "youtube_base_url must be reachable by Jellyfin"
    Jellyfin's ffmpeg is what fetches the `.strm`'s contents, not your browser.
    Use the address the Jellyfin server can reach Tentacle on — usually the LAN
    URL, e.g. `http://192.168.2.75:8888`.

**4. Add a channel** on the YouTube page. Paste any of:

```
https://www.youtube.com/@channelname
https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx
https://www.youtube.com/playlist?list=PLxxxxxxxx
@channelname
```

A playlist keeps its newest videos by upload date, whether its owner adds new
videos at the bottom (YouTube's default) or at the top. Tentacle reads up to
1,000 entries of a playlist. With a YouTube Data API key the hourly check sees
a video added at the bottom; without one the daily full check finds it.

## Per-channel options

Nothing here downloads anything. Each video becomes a small pointer file
(`.strm`) plus an NFO; the video itself streams from YouTube when someone
presses play.

| Option | Default | What it does |
|--------|---------|--------------|
| Videos / Past live streams / Shorts | Videos only | Which of the channel's tabs to look at |

!!! tip "Channels that mostly livestream"
    A channel's `/videos` tab holds its real uploads; `/streams` holds its live
    and finished broadcasts. **Past live streams** controls whether finished
    broadcasts join the library — it is off by default, so a channel that
    mostly streams shows only its actual uploads in its home row while its live
    broadcasts appear under Live TV. Turn it on if you want the back catalogue
    of past streams in the library too.

    A Live TV channel always polls `/streams` regardless, since that is how
    Tentacle knows what is on air — but that alone never adds finished
    broadcasts to the library.

    A channel that only ever streams (or only posts Shorts) has no Videos tab
    on YouTube at all. It can be added like any other: its finished streams
    become its library, and its Shorts too when **Shorts** is ticked.
| Skip shorter than | 60s | Ignores Shorts-length clips |
| Start with | 30 | How many existing videos to pick up when you add the channel |
| Show newest | 200 | How many stay listed; older ones drop off the library |
| Stream up to | 1080p | Caps playback quality — nothing is stored either way |

## Parental controls

A channel can write a rating and extra tags into every NFO, which Jellyfin's
per-user policies then act on:

- **Rating** → `<mpaa>`, works with *Max parental rating*
- **Extra tags** → `<tag>`, works with *Allowed tags*

Set a child user's library access, allowed tags and max rating in Jellyfin and
they will see only the channels you approved.

## How Tentacle talks to YouTube

Google flags an address whose traffic looks scripted: requests at exactly the
same time every hour, bursts, and a new connection for every request. When that
happens, Google Search starts showing a captcha to everyone in the house. So
Tentacle keeps its YouTube traffic small and irregular without any setup:

- **A light check.** Each check reads the channel's public RSS feed: one small
  request, the kind every feed reader makes. The channel's pages are only
  loaded when the feed shows something new, and once a day as a safety net.
  A Live TV channel also peeks at the top of its streams tab on each check to
  see what's live or scheduled. A stream's full details are read only when it
  ends.
- **Never at a fixed beat.** Checks run about every hour, ±20%, at a random
  time after startup, and each check spreads the channels a minute or so
  apart.
- **A pause after a bot check.** If YouTube answers with a 429, a captcha,
  "confirm you're not a bot" or its session rate limit ("try again later"),
  every YouTube request stops:
  - the first pause lasts an hour, and each block in a row doubles it, up to a day;
  - it survives a restart;
  - videos that were already found keep playing.
- **Nothing twice.**
  - A stream Tentacle has found is reused until shortly before Google's link
    expires, and still after a restart. The video's playlist is reused too.
  - A video that can't be read (private, members-only) is retried later, not
    on every check.
  - A library video that fails to play twice for a clear reason (private,
    members-only, removed, age-restricted) leaves the library, and is read
    again later (6 h, doubling up to 48 h): if it plays by then, it comes back.
- **A count in the log.** Once an hour Tentacle logs one line with how many
  requests it made to YouTube and Google, split into background checks,
  playback and artwork.

On the YouTube page, **Advanced: how Tentacle talks to YouTube** has four
optional settings:

| Setting | Default | What it does |
|---|---|---|
| Background checks | On | Off means channels only refresh when you press Refresh. |
| Check about every | 60 min | At least 30 minutes, with ±20% jitter either way. |
| YouTube Data API key | Not set | New uploads, video details and live status come from Google's official API instead of YouTube's pages, so the background checks never load a YouTube page. The key is free from the Google Cloud console, and the daily quota is far more than this uses. |
| Proxy for YouTube traffic | None | An HTTP proxy for YouTube traffic only, e.g. `http://gluetun:8888` to route it through a VPN container. Watching YouTube videos goes through it too, because Google ties a video's stream link to the address that asked for it. If a saved proxy can't be used (a SOCKS address, a bad port), nothing is sent to YouTube until it is fixed or cleared, and the page says so: Tentacle never falls back to your own address. |

## How playback works

```
Jellyfin plays the .strm
  └─ GET /api/youtube/v/<id>/master.m3u8
       └─ yt-dlp resolves the video (reused until shortly before Google's link expires, restarts included)
       └─ the HLS playlist is rewritten so every URL points back at Tentacle
            └─ GET /api/youtube/v/<id>/r/<token>.ts  → bytes streamed through
```

Google's media URLs expire after about six hours and are signed to the IP that
requested them, so they can never be handed to a client — hence the proxy.

Extraction prefers YouTube's `visionos` player client, which returns MPEG-TS
segments. That matters: jellyfin-ffmpeg cannot seek HLS whose segments are
fMP4, and produces corrupt output if given them.

## Home rows

Each channel has a **Home row** toggle. Turning it on adds a row of that
channel's videos to your Jellyfin home screen, newest upload first.

Rows are per-user, like every other Tentacle playlist — one person subscribing
doesn't put the channel on everyone's home screen. The row is built from the
`yt:<slug>` tag already in every video's NFO, so no extra tagging pass runs,
and it appears in both Jellyfin web and the Android TV app.

## Live streams as a Live TV channel

Each channel also has a **Live TV** toggle. With it on, that channel becomes a
tuner channel: whatever it is streaming right now plays, and its live and
upcoming streams appear in the Jellyfin guide.

- Channels get guide numbers from **9000** up, clear of IPTV stream ids
- The guide is built only from real live and upcoming streams — there are no
  filler "nothing on" entries, which would otherwise flood Jellyfin's *On Now*
- Programme start times are frozen once written. Jellyfin identifies a
  programme by channel + start time, so moving a start would delete any DVR
  timer set against it; a stream that runs long has its end extended instead
- A live or upcoming stream is a guide entry only, never a library item — it
  has no duration yet, and Jellyfin would file it as a zero-length movie. Once
  the stream ends it becomes an ordinary video and gets its files on the next
  index

Turning Live TV on also kicks off an index, because videos indexed earlier were
fetched with live streams skipped. Once that finishes, refresh the guide in
Jellyfin (Live TV → Refresh Guide) so it picks up the new channel.

!!! note "Not every channel streams"
    Plenty of channels never go live — they have no `/streams` tab content at
    all. The channel list shows **● LIVE**, **N upcoming** or **nothing on** so
    you can tell before pressing play. A channel with nothing on returns "not
    streaming right now", which Jellyfin surfaces as a playback error.

If nothing is streaming, the channel returns a "not streaming right now"
response rather than an error, and Jellyfin retries later instead of dropping
the channel from the lineup.

The channel is served as a continuous MPEG-TS stream, not as HLS. Jellyfin's
tuner reads the response body as video, so a playlist would be copied as if it
were video data — which shows up as playback stopping at 0 ms. ffmpeg does the
muxing (stream copy, no re-encoding), because YouTube's HLS carries video and
audio as separate renditions.

## Troubleshooting

**"yt-dlp is not installed in this image"** — pull a current Tentacle image.

**Videos appear but won't play** — `youtube_base_url` is almost certainly set to
something Jellyfin can't reach. Check it from the Jellyfin host:
`curl -I <youtube_base_url>/api/youtube/status`.

**A Live TV channel says "not streaming right now"** — that is the expected
answer when the channel has no live stream. It reappears when one starts.

**A channel shows "backing off", or the YouTube page says requests are paused**
— YouTube asked Tentacle to prove it isn't a bot. Every YouTube request pauses:
for an hour, doubling on each block in a row, up to a day. Nothing is deleted,
and videos already found keep playing. It's likelier from a datacenter or VPN
exit address than a home one. If it keeps happening, a YouTube Data API key
takes the background checks off YouTube's pages. The hourly
`Requests to YouTube/Google` log line shows what Tentacle is sending.

**A channel added just before a restart** finishes its first index a minute
after Tentacle starts again, whether background checks are on or not.

**Nothing is ever deleted because a listing failed.** A failed or bot-checked
listing raises, and retention only runs after a listing that succeeded.

**YouTube breaks yt-dlp regularly.** If extraction starts failing across the
board, the yt-dlp version in the image is likely behind.

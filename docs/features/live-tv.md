# Live TV

Tentacle is the tuner for Jellyfin's Live TV: add your IPTV provider and get live channels with a full TV guide inside Jellyfin, without tools like Threadfin. The household has one lineup, chosen by the admin; everyone stars their own favourites.

## How It Works

1. An admin adds the IPTV provider in Tentacle (**Sources → IPTV providers**) and switches on the **channel groups** the household should have. Those channels are the household's lineup. The same network listed for many cities or qualities ("NBC Buffalo", "NBC Boston", "NBC HD"…) shows once, as "NBC" (see [The channels people pick from](#the-channels-people-pick-from)).
2. Everyone else sees channels only, never the provider's groups. Under **My Channels** in Tentacle (signed in with their Jellyfin login), each person stars their favourites, a whole type at once (Sports, News, Local…) or one channel at a time.
3. Tentacle sets Live TV up in Jellyfin itself: one tuner with the lineup (numbered 1, 2, 3… in the admin's order) and one TV guide. Nobody adds a tuner by hand.
4. Favourites are Jellyfin's own, per person, so every Jellyfin app (the web, the TV app, phones) shows each person theirs. Kids profiles see only the kids channels an admin picked.

```
Jellyfin  ←→  one M3U tuner (the lineup) ←→  Tentacle  ←→  IPTV Provider
              + Tentacle's TV guide          (stream proxy)
```

Two people watching the same channel use one connection to the provider. Recordings and recording schedules are the household's, like a family recorder: everyone sees them.

## Watching: the guide

In Jellyfin's web app, **Live TV** opens straight to Tentacle's guide:

- **Favorites / All** at the top: Favorites is the guide with only your favourite channels, All is every channel you can see. It remembers your choice (on every browser you sign in to). With no favourites yet it says how to star one and offers All.
- **☆** next to a channel stars it (or unstars it); **Find a channel** searches by name or number.
- **Dates** run from today to the last day the TV guide has programmes for, so there are no empty days to tap through (a guide with one day of data shows no dates at all).
- A line marks **now**; what's on now is highlighted with how far in it is. Tap a channel or what's on now to watch; tap a later programme to see it and record it. A red dot marks what will record (amber: waiting for a free connection; hollow: a team or rule in preview would). **Schedule** (next to the search) lists what will record, from your teams and rules and from Jellyfin's Record button; **Recordings** shows what is recorded, to play.

It stays quick with thousands of channels and works on a phone. (Jellyfin's TV layout keeps Jellyfin's own guide.)

## Multi-view (Android TV app)

In the Tentacle Android TV app's guide, **Multi-view** (next to Recordings) puts two to four channels on screen at once: tick them in the guide, then **Start**. Two sit side by side, three or four in a 2×2 grid. You hear one at a time: the highlighted one (move with the D-pad). OK shows that channel full screen, Back returns to the grid, Back again leaves multi-view. Hold OK on a tile to swap or remove its channel.

- Each channel uses one of your provider's connections (**Streams at once**), unless someone is already watching or recording it. Multi-view starts only when enough are free and never takes one a recording needs: a recording starting within 30 minutes keeps its connection, and one that starts later takes a multi-view channel's connection, like it would a viewer's. Every connection is given back when you leave.
- Tentacle makes the grid as one picture (1080p, every channel's sound as its own track), so the TV plays one stream. With an Intel CPU it is made on the GPU (Quick Sync) when the container has `/dev/dri` (see [Docker Compose](../getting-started/docker-compose.md#gpu-for-multi-view)); otherwise on the CPU (about one core).

## Recordings: my teams and rules

**Recordings** (in the sidebar, for everyone) records from the TV guide by itself.

- **My teams**: pick a league (NHL, NBA, WNBA, NFL, MLB, MLS, CFL, Premier League, LaLiga, Serie A, Bundesliga, Ligue 1, Champions League, EuroLeague, FIFA and IIHF national teams, Formula 1, NASCAR, IndyCar, MotoGP), then tap your team. Tentacle looks for its games in the guide by every name guides use: the full name, the short name and nicknames, the city, the code ("TOR"), the French name, and its home arena ("From Scotiabank Arena in Toronto" is a home game). It also reads event and pay-per-view channels ("Penguins@Maple Leafs @7", "NHL | 01 - 8pm Canadiens at Blackhawks"), and when the guide names only the pregame show ("Toronto Maple Leafs Pregame"), it records the game that follows it on that channel.
- **EuroLeague, FIFA and IIHF**: their names are everyday words ("Canada", "Real Madrid" the football club), so a listing needs the competition ("EuroLeague", "FIFA", "World Cup", "Nations League", "IIHF", "World Juniors"), or the sport and a second team ("Soccer: Canada vs. Mexico", "Hockey: Sweden vs. Finland"); another sport in the listing ("rugby", "basketball", "field hockey") never counts. A national team is all its sides: men, women and youth (U-17, U-20, World Juniors). To keep only one, add words under **Leave out listings with** in its editor ("Women's, U-17, U-20"). Names in English, French, Spanish and the country's own language (Greek too, as Greek guides write them). Long lists have a search box.
- **Rules**: words to record by, for anything else: a show, "Grand Prix", a tournament. Whole words; accents and capitals don't matter; "Live:" and "New:" are ignored. Any word or every word, words to leave out, the title only or the description too, a shortest length.
- **Preview first**: a new team or rule shows what it would record and books nothing. Its editor lists what it would record in the guide as it is now, which channel it would use and where else it is on, the replays it would skip, and near misses with the reason ("Toronto, but nothing says NHL"; "only 30 minutes: too short to be the game"). Switch on **Record their games** when it looks right.
- **One game, one recording**: the same game on several channels is recorded once, on a regular channel before an event channel's slot, and a later showing of it (a replay) is skipped. If no connection is free for the first showing, the replay is recorded instead.
- **Connections**: recordings use the provider's **Streams at once**, like watching. A game that would need one more connection than the account has waits, and Upcoming says why ("the provider's connection is in use then").
- **Padding**: sports start 5 minutes early and run on after the listed end (an hour for hockey, 45 minutes for basketball); playoff games get an hour more for overtime. Each team's or rule's editor changes them.
- **When every connection is in use**: a recording comes before anyone watching. A recording that is only in its padding after its programme has ended gives its connection to a recording whose programme is starting, so the next game starts on time; the finished one keeps everything up to its listed end. A recording that is still before its start or inside its programme is never stopped for another recording.
- **Upcoming** lists what will record, soonest first, with why and on which channel, including what people set to record in Jellyfin itself. **✕** skips one showing (it is never booked again; **Recorded** brings it back). **Recorded** lists what teams and rules recorded or skipped.

Recordings are made in Jellyfin as ordinary recordings, exactly like its Record button, so they play everywhere and keep recording if the rule is switched off. Tentacle only ever cancels the recordings it made, and never one that has started; what people set in Jellyfin themselves is left alone. Anyone whose account may record (People → Can record; admins always, kids never) can follow teams and make rules; they see everyone's, and change their own (admins change any). The list of leagues and teams is built in: Tentacle doesn't ask any league's schedule, so it finds the games your guide lists (usually a day or two ahead).

### Pictures for recordings

Every finished recording gets a picture in Jellyfin about 10 minutes after it ends, in both shapes a film has: a wide (16:9) one for wide cards (the TV app's Recordings, Continue Watching on wide rows, Jellyfin Web) and a poster (2:3) for poster rows and library grids. Tentacle uses the first of these that works:

1. **A game**: a card with the league, both teams' logos with "@" (or "VS") between them, their names and codes, the date and the channel. Tentacle knows the teams from the team that booked it, or from the title and description ("NHL: Toronto Maple Leafs - Montreal Canadiens", "Canadiens at Blackhawks", "TOR @ MTL"). A race or a league with no teams named gets the league's card.
2. **A show or film**: its picture from TMDB, only when the title matches well (and the year, when the title has one).
3. **The guide's own picture** for the programme, when it is wide.
4. **A frame from the recording**: three are taken (at 30%, 50% and 70% of the programme), black and blank ones are left out, and the one with the most in it wins. Never while Jellyfin is recording.
5. **A card with the title**, the channel and the date.

Games and title cards are drawn in both shapes; a show or film gets TMDB's poster too. A picture that only comes wide (the guide's, a frame) is put on a poster with the title above it and the date and channel below.

A folder of several recordings of one title gets the newest recording's picture, or the league's card for games.

**Team logos** need no account or key. Tentacle takes them from ESPN's public team pages (NHL, NBA, WNBA, NFL, MLB, MLS, the Premier League, LaLiga, Serie A, Bundesliga, Ligue 1, and national teams' flags for FIFA and IIHF games), EuroLeague's own club list, and TheSportsDB's free tier (the CFL, and any club the others don't list). If a logo.dev key is set for playlist pictures, Tentacle asks logo.dev for a team none of these have. Each logo is fetched once and kept on the server. Logos that would be hard to see on the dark card get a light square behind them. A game whose logos can't be had yet gets the teams' codes, and is drawn again with logos once Tentacle has them (unless someone changed the picture).

Tentacle only adds pictures: recordings, their files and what people have watched stay as they are. It never replaces a picture it didn't set (Jellyfin's own, or one someone chose), and a picture someone changed or removed is never set again. Switch it off under **Sources → Live TV → More → Pictures for recordings**: that removes only the pictures Tentacle added. If Jellyfin's Recordings library saves pictures into the media folders ("Save artwork into media folders"), Tentacle adds none and says so there.

## My Channels

Everyone (admins too) has **My Channels** in Tentacle's sidebar once the household has Live TV channels.

Everyone has the same channels; what you choose there is your favourites. Favourites are Jellyfin's, so every app shows them, and the guide can show just them. The page has three parts:

- **Quick picks**: Sports, News, Movies, Kids, Entertainment, Local, and the other types the channels have (Documentary, Lifestyle, Music, Religious). Each stars that type's best-known channels, at most 25 (folded networks, with a guide, that work, in your country first), and says how many; every other channel of the type is under **All channels** (its type filter). Once on it says "★ Sports · 25 starred"; tap it again to unstar them (a channel another pick that is on covers too stays starred). Sports + News + Local is about 70 favourites.
- **My favourites**: the channels you starred, in the lineup's order; **☆** unstars one.
- **All channels**: the whole lineup, with a search and a type filter; **☆** stars a channel. Each channel says whether it has a guide (**Guide ✓** / **No guide**) and whether it works (**✓ Works (checked 2 h ago)**, **✗ Not working**, **Not checked yet**: see [Stream checker](#stream-checker)); channels that don't work are at the bottom. Event and PPV channels show the event and its time ("▶ Leafs vs Bruins · Today 7:00 PM"); event slots with nothing on aren't in the lineup.

A new person can have a short list of favourites in under a minute: tap **Sports**, **News** and **Local**, unstar the few they don't want, done. They start with the favourites of the person the admin chose for new people (see [Multi-user](multi-user.md)), or none.

Kids profiles see only the kids channels an admin picked, and can star them.

**Local channels where you are.** An admin sets where each person watches from (People → *Live TV from*; the admin's own city by default). Someone in Vancouver sees "CTV · Vancouver", "Global · Vancouver", "NBC · Seattle" and the West feeds where the household's lineup has Toronto's, Buffalo's or the East feed, with the same numbers, their own guides and their own favourites; nobody sees another city's. Everything else is the same lineup for everyone. Buffalo, Detroit and Seattle stations count as their city even when the provider names one by its call sign only ("PBS 17 (WNED)" is Buffalo's).

On a phone, **My favourites** and **All channels** are two tabs under the quick picks.

Stars save by themselves and show in Jellyfin at once.

## Adding a Provider

There is no separate Live TV provider. Go to **Sources → IPTV providers**, click **+ Add Provider** and enter an Xtream Codes login, a playlist link (M3U URL) or a playlist file. The same provider serves VOD and Live TV.

When you save it, Tentacle tests the connection. If the account has live channels, Live TV is switched on for that provider and its channel groups are fetched automatically. The provider's card (Sources → IPTV providers) then holds them under **Channel groups**, and a **Live TV** tab appears under Sources. Until a provider is used for Live TV, that tab says no provider is used for Live TV.

!!! info "Playlist (M3U) providers"
    A playlist link or file works like an Xtream login. Its live channels (not its films and episodes) become the channel groups; the guide comes from the link the playlist names (`url-tvg` / `x-tvg-url`), or from the optional **Guide link** on the provider. When its links are an Xtream panel's, each channel is opened as one continuous `.ts` stream (`.m3u8` as the fallback) and viewers of the same channel share it, so several people can watch on an account with few connections. Other links are opened as listed. The playlist is read again every night (new groups, changed links). Backup addresses are for Xtream logins only.

### Several providers

Any number of providers can be used for Live TV: switch **Use for Live TV** on for each. On each card:

- **Streams at once**: how many channels that provider's account can play or record at the same time (its "connections"). Live TV, recordings and films played through Tentacle share them. The card shows what the account says; empty means 6. Jellyfin gets as many tuners as all providers' streams together.
- **Channel numbers +** (under **More** on the card): what is added to the provider's own channel numbers, so two providers never give Jellyfin the same number. The first Live TV provider keeps its own numbers (0); every other one is given its own range once and keeps it. Change it only if you have a reason: Jellyfin then sees that provider's channels under new numbers, and recordings set up on the old ones need setting up again.

### Several addresses

Providers often have more than one address and send a list of others when one stops working (an internet provider blocking one address in the evening, for example). On the provider's card, **More → Backup addresses → Addresses** keeps them:

1. Paste the provider's whole message: Tentacle picks out the addresses (a login or a path in them is dropped) and adds them as backups.
2. **Test all** checks each address from the server, one at a time: whether the name resolves, whether it is behind Cloudflare, whether the login works (account status, connections in use), and how long a live channel and a film take to start. Stream tests are skipped while anyone is watching, so a one-connection account is never cut off. The list is ranked best first, with how often each worked over the last day.
3. The hourly provider check tests the backups too, so the ranking covers the evening.
4. **Use this one** switches the provider to an address. When the address in use fails its checks, Tentacle switches to the best backup by itself and says so in Activity (and on your phone, with Pushover). You can choose **switch, and come back when it works again**, or **only tell me**.

A switch keeps everything: nothing is imported again or deleted, channels keep their numbers, the guide and recordings stay, and a stream that is playing finishes on the old address. Until a sync on the new address has read the whole catalogue, titles it does not list are not treated as gone.

### User-Agent

Tentacle connects to your provider with a TiviMate User-Agent (`TiviMate/4.7.0 (Linux; Android 12)`), which most providers accept. The dashboard has no setting to change it.

## Channel Groups

Groups are fetched automatically when the provider is added. They are organized by your provider (Sports, Entertainment, News, etc.) and show the channel count for each. On the provider's card, **Channel groups → Choose** lists them; **Refresh from provider** fetches the list again, and **Save & update channels** fetches the channels of the groups you switched on.

### Enabling Groups

Toggle on the groups you want, then click **Save & Sync Channels**. Only channels from enabled groups are served to Jellyfin. This lets you keep your channel list manageable.

!!! tip "Two-phase sync"
    Phase 1 (fetching groups) gets just the group list with channel counts — this is fast. Phase 2 (**Save & Sync Channels**) fetches the actual channels for enabled groups only, then automatically chains into an EPG sync.

### Bulk Actions

You can enable or disable multiple groups at once, or filter groups by keyword to quickly find what you need.

### New groups from your provider

Providers add groups all the time. **Sources → Live TV → New groups** lists the ones added since you last looked, with clean names (the provider's "ES| DAZN BALONCESTO ᴿᴬᵂ" shows as *DAZN Baloncesto*, with a Spain flag and a *Raw* label), how many channels each has, and a few of its channels with their logos.

- **Probably for you** comes first: new groups from the countries your channels already come from, your own country and your language.
- Everything else is folded by country or language, each with **Dismiss all**.
- **Dismiss everything not for you** clears the rest in one click. Every dismiss can be undone from its message, and the **Dismissed** list at the bottom brings any group back.
- Tick groups and **Show their channels** to pick channels: **Tick all**, **Only working** or **Only with a guide**, then **Add**. Channels you already have stay unticked.

## The channels people pick from

**Sources → Live TV → Household channels** shows the household's lineup: the channels of the groups switched on, with the same network in different places or qualities folded into one entry. Its number on top ("2,291 channels in your household's guide") is exactly what everyone gets in Jellyfin; under it, how many of them are checked and working. Nothing personal is on this page: each person's favourites, the admin's included, are under **My Channels**.

- **Networks.** "US: NBC 4 (WDIV) DETROIT", "NBC (KNBC) LOS ANGELES" and "NBC HD" are one network, **NBC**, shown as "NBC · from US: NBC 4 (WDIV) DETROIT · 16 versions". People only ever see "NBC". The dropdown on its row chooses which version feeds it; each version is listed with its guide and works marks, working ones first. Tentacle chooses one by itself (one that works, then your region, then one with a guide); changing it later keeps everyone's favourites and recordings on the channel. Different channels of a network (NBC Sports, NBC News, NBC Golf) stay separate.
- **Your region** (under **More**, e.g. `CA: Toronto, Ontario, Buffalo`) is what Tentacle prefers when it chooses a version by itself. Empty: the server's time zone.
- **Edit** on a row: correct the channel's types (the quick picks people see; **Automatic types** goes back to Tentacle's), and for a network see every version, **Split ticked ones off** (a wrong fold) or **Undo this fold**, and **Rename** it.
- **Merge selected**: tick two or more rows that are really one network (a missed fold) and merge them.
- The switch on a row takes a channel out of what people pick from (a single channel is switched off; switch it on again under **More → Every channel the provider lists**). A network taken out is listed under **Show: Taken out**, with **Put back**.
- **Guide ✓ / No guide** and **✓ Works / ✗ Not working / Not checked yet** are on every row; **Show** filters to channels folded from several, channels that don't work, channels without listings, or event slots with nothing on (left out of the list and the count, as people don't see them).

### Stream checker

The line under the channel count is the stream checker's: in the background Tentacle opens each channel for a few seconds to see whether it works, one channel at a time and only while nobody watches or records (it stops the moment anyone starts), spread over the day. It shows what it is doing and how many channels work, don't work, or haven't been checked yet.

### Channel logos

Providers often give a whole group one picture, so the guide shows the same image for MTV, TBS and PBS, or a logo address stops working. In the background Tentacle checks every channel logo it uses and keeps a copy. When a channel's own logo is missing, broken, or a picture many different channels share, it uses, in this order: the same channel's logo from another of your channels, the logo in your guide data, then the [iptv-org](https://github.com/iptv-org/database) open logo library. A logo that stops working is shown from Tentacle's copy. A logo you set yourself (Edit → Name and logo) always comes first. Jellyfin's guide picks up the new logos at the next Live TV sync, a few seconds after they change.

### Event and PPV channels

Many event channels have no guide of their own but carry the event in their name ("NHL: Toronto Maple Leafs @ 7:00 PM", "PPV 3: UFC 300 - 10/22 8PM ET"). Tentacle reads the title and time from the name and puts it in the TV guide, so Jellyfin's guide shows what's on. Event slots with no event in the name show **No event** and are left out of the lineup until the provider names an event in them.

## Every channel the provider lists

**Sources → Live TV → More → Every channel the provider lists** is every channel of the groups switched on, as the provider lists them. It lets you:

- **Enable/disable** individual channels (then click **Save Channels**)
- **Rename** a channel as Jellyfin's guide shows it
- **See EPG status** — "Has EPG" or "No EPG" badges based on actual program data; click a badge to set the guide id (XMLTV channel id) a channel takes its guide from

### Stable channels

Jellyfin knows each channel by the tuner's address and the channel's stream address in Tentacle, never by its number. So numbers can change freely (the lineup is numbered 1, 2, 3... in the admin's order) while favourites and recordings stay with the channel.

## EPG (Electronic Program Guide)

Tentacle downloads XMLTV guide data from your provider and stores it in the database. EPG data covers ALL provider channels (not just enabled ones), so when you enable a new channel, it already has EPG data available.

### EPG Refresh

- **Automatic** — EPG syncs automatically after channel sync (auto-chaining)
- **Manual** — Click **Update TV guide** under **More → Every channel the provider lists** to refresh guide data
- **Cache** — EPG data is cached on disk for 8 hours to avoid repeated downloads

### Shared EPG IDs

Multiple channels can share the same EPG data (e.g., an HD channel and its backup). Tentacle handles this automatically — program data is duplicated for all matching channels in the XMLTV output.

## Connecting to Jellyfin

There is nothing to set up in Jellyfin. Once Tentacle is connected to Jellyfin (the setup wizard, or **Settings → Services**) and a provider has Live TV channels, Tentacle adds the tuner and the TV guide to Jellyfin itself within a few minutes, and keeps them in line: when the admin switches groups or channels on or off, when the kids channels change, and every hour (which also catches people added in Jellyfin). **Sources → Live TV → More → Live TV in Jellyfin** shows when it last did so, and **Set up again now** runs it at once.

Kids channels are tagged in Jellyfin and every other channel is blocked for kids profiles (Jellyfin's per-user "blocked tags"); a channel new to the lineup is blocked for kids until an admin makes it a kids channel. Tentacle manages only its own tags: blocked tags an admin set by hand are kept.

!!! info "Why one lineup"
    Earlier versions could give each person their own tuner. Jellyfin's guide refresh slows down sharply with several M3U tuners (7 people × 1,734 channels: over an hour per refresh, against about 2 minutes for one lineup of 5,669), so there is one lineup and everyone's favourites. A server that had per-person tuners or the shared "All channels" tuner is moved by itself: everyone's favourites and recording schedules go to the lineup, then the per-person tuners are removed.

!!! info "Coming from an earlier version"
    An install that used Tentacle as an HDHomeRun tuner (or an M3U tuner on `/api/live/playlist.m3u`) is moved by itself: everyone's favourite channels and the recording schedules go to the new tuner, then the old tuner is removed from Jellyfin. Channel numbers become 1, 2, 3... Before any change, Tentacle saves what Jellyfin had (tuners, favourites, schedules, blocked tags) to `livetv-backups/` in its data folder.

!!! info "Tentacle's address"
    Jellyfin knows every channel by the tuner's address, so Tentacle chooses it once (the address Jellyfin already reached Tentacle at, else the plugin's Tentacle address, else Settings → Services) and keeps it.

## Stream Proxy

Tentacle proxies live streams between Jellyfin and your provider. This handles:

- **Provider redirects** — Follows redirect chains automatically
- **HLS to MPEG-TS conversion** — Converts HLS streams to MPEG-TS format for Jellyfin compatibility
- **User-Agent forwarding** — Uses the configured User-Agent when connecting to the provider

Xtream channels are pulled as one continuous MPEG-TS stream each, which is what lets several channels run at once on an account with a connection limit. If your provider does not serve a channel that way, Tentacle opens it as HLS instead, automatically.

When a provider drops a raw MPEG-TS stream, Tentacle reconnects. Many providers start the new connection with the last 20 seconds or so again; Tentacle recognises those seconds and skips them, so the recording goes on where it stopped, without repeated seconds. An interruption covered that way is not counted as damage.

A recording also waits out a provider refusing it (HTTP 401/403, for example a short IP block or flood protection) for up to three minutes before it ends; a viewer's stream still ends at once.

Channels start in about a second or two. An event or pay-per-view channel with nothing on shows **Nothing on right now** for half a minute, with the event and when it starts if Tentacle knows, instead of a long wait and an error; recordings of it are retried as before.

When the provider interrupts a recording, or serves a placeholder (a black "channel unavailable" video) instead of the channel, Tentacle notes it under **Health → Recent Activity**: a recording that may be missing content shows as **Recording damaged**, a placeholder as **Channel placeholder**. The guide sync, stream checks and nightly sweeps report there too, and anything that failed is shown in red.

## Tuner Endpoints

Tentacle gives these to Jellyfin itself:

| Endpoint | Purpose |
|----------|---------|
| `/api/live/tuner/all.m3u` | The household's lineup (the tuner) |
| `/api/live/xmltv.xml` | XMLTV guide data |

The HDHomeRun endpoints (`/hdhr/discover.json`, `/hdhr/lineup.json`, `/hdhr/device.xml`) and `/api/live/playlist.m3u` are still served for anything set up by hand, but carry every channel without folding and know nothing of kids profiles.

## Troubleshooting

!!! warning "Channels missing after enable"
    Tentacle refreshes Jellyfin's guide a few seconds after you switch groups or channels on; Jellyfin then takes a few minutes for a large lineup. If they still don't appear, open **Sources → Live TV → More**: it says why the last try didn't work (Jellyfin not reachable, for example). **Set up again now** tries again.

!!! warning "No EPG data"
    EPG badges in Tentacle ("Has EPG" / "No EPG") reflect actual program data in the database. If channels show "No EPG" after a fresh setup, run an EPG sync first. The auto-chain after channel sync usually handles this automatically.

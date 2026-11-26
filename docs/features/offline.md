# Offline (watch without internet)

Save movies and episodes on a phone or computer and watch them with no
internet: on a plane, in the car, at the cottage. It works for everything in
the library, downloaded files and your provider's streams alike.

## Jellyfin's own Download

In Jellyfin, open a movie or episode and tap **More → Download**.

- **Jellyfin's iPhone and Android apps** put it in their own **Downloads**
  tab, ready to play in airplane mode.
- **A browser on a computer** (or Jellyfin Desktop) saves a normal video
  file, such as `The Iron Giant (1999).mkv`.

A provider's title (a stream) downloads as the real video, through
Tentacle. While it downloads it uses one of your provider's connections, and
only one that is free: if someone wants to watch and every connection is in
use, their show starts at once and the download waits, then carries on from
where it stopped.

The first version sends the provider's original file, full size (often
2–5 GB for a film).

## The Offline page

Tentacle's **Offline** page (in the sidebar, next to Library and Discover)
keeps titles inside Tentacle on this device: the browser, or the Tentacle
app on your home screen.

- **To save something:** in Tentacle's Library open a movie or a show and
  tap **Save offline** (a show saves every episode; the ⤓ next to a season
  saves that season, next to an episode that episode); or in Jellyfin in a
  browser, **More → Save offline** on a movie, an episode, a show or a
  season. Jellyfin's top bar has an **Offline** button next to Favorites
  that opens the page.
- **Everything plays:** a title the device can't play as it is (an MKV
  file, surround sound a computer's browser doesn't play, an old video
  format) is turned into an MP4 by Tentacle while it sends it: the picture
  is copied as it is whenever the device plays it, and only what must be is
  converted (the sound to AAC stereo; an old video format to H.264, on the
  server's graphics chip when it has one). The server needs room for the
  title while it does this (set `OFFLINE_READY_DIR` to a folder on a big
  disk, see [Docker Compose](../getting-started/docker-compose.md#gpu-for-multi-view));
  it keeps the copy until your device has it.
- **The server does the slow part first.** When you tap Save offline,
  Tentacle gets the title from your provider and prepares it on its own;
  you can close the page (and lock the phone). When it's ready you get a
  message: "*Madden (2026) is ready to save*", on the phone or computer you
  saved from (if you allowed Tentacle's notifications there; tap **Tell me
  when it's ready** on the Offline page) and in Jellyfin. Your device then
  only copies the finished file from your server, at full speed.
- **Chrome (Android, computers)** carries on copying with the page closed,
  with its own progress notification. On an iPhone the copy runs while the
  Offline page is open, and carries on from where it stopped each time you
  open it; the page says how far it got.
- **Phones and tablets get a smaller copy**, by themselves: at most 1080p,
  made on the server's graphics chip (HEVC with HDR kept on an iPhone,
  H.264 elsewhere), with stereo sound. A 4K film is a few GB instead of
  15+. Computers get the full picture.
- **The page** shows what is saved here, what is still saving (with how far
  it got), and ✕ to remove a title from this device. Tap a title to play it.
- **With no internet** the Tentacle app opens straight to Offline.
- **Where you stopped** goes back to Jellyfin when the device is online
  again, so the TV carries on from there.

### iPhone

- Save right where you are: in Safari, in Chrome or in the Tentacle app on
  your home screen. Each keeps its own saved titles (something saved in
  Safari isn't in the home-screen app).
- iOS may clear what a browser saved if you don't open Tentacle there for
  about a week; the home-screen app keeps it.
- The server prepares the title with the page closed, but the iPhone's own
  copy runs only while the page is open with the screen on. Tentacle keeps
  the screen awake meanwhile, and when you open it again it carries on from
  where it stopped. Notifications work in the home-screen app (iOS 16.4+),
  not in a Safari or Chrome tab.

## Who can save offline

**Server → People → Can save offline** turns it on or off for each person
(it is Jellyfin's "Allow media downloading", so it is also Download in
Jellyfin's apps). A person only ever saves what they can watch: their
libraries and their age limit apply. A recording that is still recording
can't be saved until it has finished.

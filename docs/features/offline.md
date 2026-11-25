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

- **To save something:** in Tentacle's Library open a movie and tap
  **Download** (for a show, the ⤓ next to each episode); or in Jellyfin in a
  browser, **More → Save for offline**. Jellyfin's top bar has an
  **Offline** button next to Favorites that opens the page.
- **The page** shows what is saved here, what is still downloading (with
  how far it got), and ✕ to remove a title from this device. Tap a title to
  play it.
- **With no internet** the Tentacle app opens straight to Offline.
- **Where you stopped** goes back to Jellyfin when the device is online
  again, so the TV carries on from there.

### iPhone

- Save titles from the **Tentacle app on your home screen** (in Safari tap
  Share → Add to Home Screen, then open Tentacle from there). Safari and the
  home-screen app keep separate storage, so something saved in Safari would
  not be in the app.
- An iPhone downloads only while the app is open with the screen on.
  Tentacle keeps the screen awake while a download runs, and when you open
  the app again it carries on from where it stopped.
- An iPhone can't play MKV files on this page: for those, use Download in
  the Jellyfin app.

## Who can download

**Server → People → Can download** turns it on or off for each person
(it is Jellyfin's "Allow media downloading"). A person only ever downloads
what they can watch: their libraries and their age limit apply. A recording
that is still recording can't be downloaded until it has finished.

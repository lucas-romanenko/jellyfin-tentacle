"""Recording rules (Live TV): record a team's games or anything a guide
listing names, as ordinary Jellyfin one-off recordings that Tentacle books
and cancels itself. Jellyfin stays the recorder.

  leagues   the built-in list of leagues and teams (leagues.json; no league
            schedules are fetched: games are found in the guide)
  match     does a listing name a team or a rule's words
  guide     the listings to look at: the guide Tentacle serves Jellyfin
            (regular channels, and event channels' slots)
  plan      what each rule would record and why (pure: no writes)
  book      books, re-finds and cancels Jellyfin timers for rules that
            record; keeps recording_bookings up to date
"""

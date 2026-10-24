"""Pictures for Live TV recordings (#815): every recording gets one 16:9
picture, set as both Primary and Thumb in Jellyfin, so every client shows
recordings in one shape.

It only adds pictures. It never renames, moves or rewrites a recording, its
.nfo or the guide, so item ids and everyone's watched state stay as they
are. It never replaces a picture it didn't set, and never sets one again
that a person changed or removed (the recording_artwork table keeps the
image tags it set).

- what.py: what a recording is (a game of which teams, or a show or film),
  from Tentacle's own booking or the title and description.
- cards.py: the generated pictures (a matchup card, a league card, a title
  card) and making any picture 16:9.
- frames.py: the best of a few frames from the finished recording.
- job.py: the pass on the scheduler, the sources in order, the uploads, the
  setting.
"""

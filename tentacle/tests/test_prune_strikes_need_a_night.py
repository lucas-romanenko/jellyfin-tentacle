"""The VOD prune's "two strikes" must be two nights, not two syncs.

_prune_removed_content marks a title the provider didn't list
(provider_missing_since) and deletes it -- .strm/.nfo files included -- on the
next sync that still doesn't list it. Nothing looks at how long ago the mark
was made, so the nightly sync and a "Sync now" a few minutes later are the two
strikes. A provider that answers one category short for an hour (a partial
answer, not an empty one, so the empty-category guard doesn't see it) loses up
to max(PRUNE_MIN_ALLOWANCE, 5 %) titles per provider and type -- 1,000 films
on a 20,000-film library. Each delete is a new Jellyfin item if the title comes
back: everyone's watched state, resume points and favourites go with it.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
from datetime import datetime, timedelta

from models.database import Movie
from nightly_harness import FakeTMDB, NightlyHarness


class PruneStrikesNeedANight(NightlyHarness):
    def _short_by_40(self):
        self.add_category("1")
        self.catalogue_movies("1", [f"Movie {i}" for i in range(400)])
        self.night()
        full = list(self.client.movies["1"])
        self.client.movies["1"] = full[:360]          # one category answers 40 titles short (10 %)
        return [FakeTMDB.ids[t] for t, _ in full[360:]]

    def test_the_nightly_and_a_sync_now_minutes_later_delete_nothing(self):
        missing = self._short_by_40()
        self.night()           # 03:00 nightly: strike one
        self.sync_only()       # "Sync now" a few minutes later
        gone = [i for i in missing if self.movie(i) is None]
        self.assertEqual([], gone, f"{len(gone)} of 40 films deleted by two syncs minutes apart")

    def test_still_missing_the_next_night_is_removed(self):
        missing = self._short_by_40()
        self.night()
        # A day on (the nightly may start a little earlier than the night before)
        self.db.query(Movie).filter(Movie.provider_missing_since.isnot(None)).update(
            {"provider_missing_since": datetime.utcnow() - timedelta(hours=23)})
        self.db.commit()
        self.night()
        self.assertEqual(missing, [i for i in missing if self.movie(i) is None])

"""Manage Episodes must not unmonitor a show's Specials (season 0) that no
client ever showed.

None of the three clients lists season 0 in the episode picker:
tentacle-discover.js (`if (ep.seasonNumber === 0) return;`), the dashboard
(pages.js keeps `ep.seasonNumber > 0`) and the TV app (EpisodePickerDialog
filters `seasonNumber > 0`). So a selection never contains a special, and
routers/discover.py manage_episodes computes

    to_unmonitor = [ep["id"] for ep in episodes if ep["id"] not in selected_set]

over EVERY episode Sonarr has, season 0 included: each save silently
unmonitors every monitored special of the show (and "untick all" does too).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from types import SimpleNamespace

import test_manage_episodes as me   # its web stubs + fake Sonarr
from test_manage_episodes import _resp, _sonarr

discover = me.discover

SPECIAL_ON, SPECIAL_OFF = 900, 901
EPISODES = [
    {"id": SPECIAL_ON, "seasonNumber": 0, "episodeNumber": 1, "monitored": True, "hasFile": True},
    {"id": SPECIAL_OFF, "seasonNumber": 0, "episodeNumber": 2, "monitored": False, "hasFile": False},
] + [{"id": 100 + n, "seasonNumber": 1, "episodeNumber": n, "monitored": True, "hasFile": True}
     for n in range(1, 4)]


class SonarrWithSpecials(me.FakeSonarr):
    def get(self, url, params=None, **kw):
        if url.endswith("/api/v3/episode"):
            self.calls.append(("GET", url, params, None))
            return _resp(200, EPISODES)
        return super().get(url, params=params, **kw)


class ManageEpisodesKeepsSpecials(unittest.TestCase):
    def _call(self, fake, selected, **extra):
        body = discover.ManageEpisodesBody(tmdb_id=5555, selected_episodes=selected, **extra)
        with _sonarr(fake):
            return discover.manage_episodes(body, db=None, user=SimpleNamespace(id=1))

    @staticmethod
    def _unmonitored(fake):
        return {i for c in fake.calls if c[0] == "PUT" and c[3].get("monitored") is False
                for i in c[3]["episodeIds"]}

    def test_saving_a_season_pick_leaves_a_monitored_special_monitored(self):
        fake = SonarrWithSpecials()
        # What every client sends: the regular episodes the person kept ticked.
        self._call(fake, [{"season": 1, "episode": 1}, {"season": 1, "episode": 2}])
        off = self._unmonitored(fake)
        self.assertIn(103, off)                       # the unticked regular episode: as asked
        self.assertNotIn(SPECIAL_ON, off, "a monitored special was unmonitored although no client shows it")

    def test_unticking_every_shown_episode_leaves_specials_alone(self):
        fake = SonarrWithSpecials()
        self._call(fake, [], unmonitor_all=True)
        off = self._unmonitored(fake)
        self.assertEqual({101, 102, 103} - off, set())
        self.assertNotIn(SPECIAL_ON, off)

    def test_a_special_that_is_sent_is_still_monitored(self):
        # Control: a client that does list specials can still pick one.
        fake = SonarrWithSpecials()
        self._call(fake, [{"season": 0, "episode": 2}, {"season": 1, "episode": 1}])
        on = {i for c in fake.calls if c[0] == "PUT" and c[3].get("monitored") is True
              for i in c[3]["episodeIds"]}
        self.assertIn(SPECIAL_OFF, on)


if __name__ == "__main__":
    unittest.main()

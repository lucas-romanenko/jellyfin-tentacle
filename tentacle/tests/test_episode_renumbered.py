"""The provider corrects an episode's number (inserts a missing
S01E01 and shifts the rest: id 3201 E1->E2, 3202 E2->E3).

Each SxxEyy file plays the episode the provider lists at that number now
(the old id is listed at ANOTHER number, so this is not the "two listed ids,
don't flip" case), and the inserted episode appears. Before #376, E01 kept
playing 3201 (now E02), E02 kept 3202 (now E03), E03 was a second copy of
3202, and the inserted E01 (3200) was never written. No network.
"""
import unittest
from pathlib import Path as _RealPath

import services.sync as sync
from models.database import Provider
from tmp_dirs import temp_dir


class EpisodeRenumbered(unittest.TestCase):
    def setUp(self):
        self.show = _RealPath(temp_dir(self)) / "Pokemon (1997)"
        self.client = sync.XtreamClient(Provider(id=1, name="P", server_url="http://provider",
                                                 username="u", password="p"))

    def write(self, *eps):
        return sync._write_episode_strms(
            self.client, {"1": [{"id": i, "episode_num": n, "container_extension": "mp4"} for i, n in eps]},
            self.show, self.show.name)

    def plays(self, n):
        return int((self.show / "Season 01" / f"Pokemon (1997) S01E{n:02d}.strm").read_text().rsplit("/", 1)[1].split(".")[0])

    def test_renumbered_episodes_follow_their_number(self):
        self.write((3201, 1), (3202, 2))
        self.write((3200, 1), (3201, 2), (3202, 3))
        got = {n: self.plays(n) for n in (1, 2, 3)}
        self.assertEqual({1: 3200, 2: 3201, 3: 3202}, got)


if __name__ == "__main__":
    unittest.main()

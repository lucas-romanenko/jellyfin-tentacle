"""An M3U name with a comma is kept whole (#525).

Run from the tentacle/ directory:  python -m unittest discover -s tests

An #EXTINF line is `#EXTINF:<duration> <attributes>,<name>`. The parser took
the name from after the LAST comma, so "PPV 1: UFC 300, Pereira vs Hill" was
stored as "Pereira vs Hill": two event channels "..., English" both read
"English" in the guide, and the M3U VOD sync looked "Crazy, Stupid, Love."
up on TMDB as "Love.". The name is everything after the first comma outside
the quoted attributes. Live TV rows an older build stored under the cut name
keep their guide number (stream_id) when the name becomes whole.
"""
import os
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import LiveChannel, LiveChannelGroup, Provider
import routers.livetv as livetv
from services.m3u_parser import parse_m3u
from services.sync import M3UClient
from tmp_dirs import temp_dir


def _name(line):
    return parse_m3u(f"#EXTM3U\n{line}\nhttp://p.example/live/1.ts\n")[0]["name"]


class NameWithACommaIsKeptWhole(unittest.TestCase):
    def test_no_tvg_name(self):
        self.assertEqual("PPV 1: UFC 300, Pereira vs Hill",
                         _name('#EXTINF:-1 tvg-id="ppv1.us" group-title="PPV",PPV 1: UFC 300, Pereira vs Hill'))

    def test_empty_tvg_name(self):
        self.assertEqual("NBA 01: Lakers vs Celtics, English",
                         _name('#EXTINF:-1 tvg-name="" group-title="NBA",NBA 01: Lakers vs Celtics, English'))

    def test_comma_in_group_title_and_name(self):
        self.assertEqual("Crazy, Stupid, Love. (2011)",
                         _name('#EXTINF:-1 group-title="Movies, 2011",Crazy, Stupid, Love. (2011)'))

    def test_no_attributes(self):
        self.assertEqual("Love, Death & Robots S01E01", _name("#EXTINF:-1,Love, Death & Robots S01E01"))

    def test_comma_only_in_an_attribute(self):
        self.assertEqual("TSN 1", _name('#EXTINF:-1 group-title="Sports, Canada",TSN 1'))

    def test_name_without_a_comma_reads_as_before(self):
        self.assertEqual("BBC One", _name('#EXTINF:-1 tvg-id="bbc1.uk" group-title="UK",BBC One'))

    def test_tvg_name_still_wins(self):
        self.assertEqual("Sky Sports", _name('#EXTINF:-1 tvg-name="Sky Sports",Sky, Sports'))


def _session():
    tmp = temp_dir()
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


NBA = ["NBA 01: Lakers vs Celtics, English", "NBA 02: Knicks vs Nets, English"]


def _playlist(names):
    return "#EXTM3U\n" + "".join(
        f'#EXTINF:-1 group-title="Sports",{name}\nhttp://panel/live/u/p/{i}.ts\n'
        for i, name in enumerate(names))


class LiveTvRowsStoredUnderTheCutName(unittest.TestCase):
    def setUp(self):
        self.db = _session()
        p = Provider(name="P", server_url="http://panel", username="u", password="p",
                     provider_type="m3u_url", live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="Sports", enabled=True))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _rows(self):
        self.db.expire_all()
        return self.db.query(LiveChannel).filter_by(provider_id=self.pid).order_by(LiveChannel.stream_url).all()

    def test_full_names_and_the_same_guide_numbers(self):
        names = NBA + [f"Channel {i}" for i in range(28)]
        # What an older build stored: each name cut to the text after its last comma.
        old = [{"name": n.rsplit(",", 1)[-1].strip(), "stream_url": f"http://panel/live/u/p/{i}.ts",
                "group_title": "Sports"} for i, n in enumerate(names)]
        livetv._upsert_channels_from_m3u(self.pid, old, self.db)
        self.db.commit()
        before = self._rows()
        self.assertEqual(["English", "English"], [r.name for r in before[:2]])
        before[0].enabled = False
        before[1].channel_number = 702
        self.db.commit()
        ids = {r.stream_url: (r.id, r.stream_id) for r in before}

        livetv._upsert_channels_from_m3u(self.pid, parse_m3u(_playlist(names)), self.db)
        self.db.commit()
        after = self._rows()
        self.assertEqual(NBA, [r.name for r in after[:2]])
        self.assertEqual(ids, {r.stream_url: (r.id, r.stream_id) for r in after})
        self.assertFalse(after[0].enabled)
        self.assertEqual(702, after[1].channel_number)


class M3uVodTitleWithAComma(unittest.TestCase):
    def test_movie_and_show_names_are_whole(self):
        path = os.path.join(temp_dir(self), "vod.m3u")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n"
                    '#EXTINF:-1 group-title="Movies",Crazy, Stupid, Love. (2011)\n'
                    "http://panel/movie/u/p/101.mkv\n"
                    '#EXTINF:-1 group-title="Shows",Love, Death & Robots S01E01\n'
                    "http://panel/series/u/p/201.mkv\n")
        client = M3UClient(Provider(name="P", provider_type="m3u_file", m3u_url=path))
        self.assertEqual(["Crazy, Stupid, Love. (2011)"], [s["name"] for s in client.get_vod_streams("Movies")])
        self.assertEqual(["Love, Death & Robots"], [s["name"] for s in client.get_series_list("Shows")])


if __name__ == "__main__":
    unittest.main()

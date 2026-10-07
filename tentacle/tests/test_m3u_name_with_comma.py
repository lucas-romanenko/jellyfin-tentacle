"""M3U: a channel or title whose name contains a comma keeps its whole name.

Run from the tentacle/ directory:  python tests/hermetic.py discover -s tests

An #EXTINF line is "#EXTINF:<duration> <key="value" ...>,<name>": the FIRST
comma outside the quoted attribute values ends the attributes, and everything
after it is the name, commas included (RFC 8216 4.3.2.1 defines the same
"#EXTINF:<duration>,[<title>]" line). services/m3u_parser.parse_m3u took the
name after the LAST comma instead, so on a line without tvg-name (or with an
empty one) "PPV 1: UFC 300, Pereira vs Hill" became "Pereira vs Hill".

The name is what an M3U provider's Live TV channel is called in the lineup,
and what the M3U VOD sync (services/sync.M3UClient) files a movie or show
under, so "Crazy, Stupid, Love. (2011)" was synced as "Love. (2011)" and
"Love, Death & Robots S01E01" as the show "Death & Robots".

A comma inside a quoted attribute (group-title="Sports, PPV") is not the
separator; the guards below keep that working.
"""
import logging
import os
import random
import shutil
import types
import unittest
from pathlib import Path as _RealPath
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv
import services.m3u_parser as m3u_parser
import services.sync as sync
from models.database import LiveChannel, LiveChannelGroup, Movie
from services.m3u_parser import parse_m3u
from test_vod_namesakes import Base as VodBase, TMDB, _meta as _vod_meta
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _one(line, url="http://p.example/live/1.ts"):
    channels = parse_m3u(f"#EXTM3U\n{line}\n{url}\n")
    assert len(channels) == 1, channels
    return channels[0]


class NameWithACommaIsKeptWhole(unittest.TestCase):
    def test_no_tvg_name(self):
        ch = _one('#EXTINF:-1 tvg-id="ppv1.us" group-title="PPV",PPV 1: UFC 300, Pereira vs Hill')
        self.assertEqual(ch["name"], "PPV 1: UFC 300, Pereira vs Hill")

    def test_no_attributes_at_all(self):
        ch = _one("#EXTINF:-1,UFC 300, Pereira vs Hill")
        self.assertEqual(ch["name"], "UFC 300, Pereira vs Hill")

    def test_empty_tvg_name_falls_back_to_the_whole_name(self):
        ch = _one('#EXTINF:-1 tvg-id="ppv1.us" tvg-name="" group-title="PPV",PPV 1: UFC 300, Pereira vs Hill')
        self.assertEqual(ch["name"], "PPV 1: UFC 300, Pereira vs Hill")

    def test_comma_in_an_attribute_and_in_the_name(self):
        ch = _one('#EXTINF:-1 tvg-id="ppv1.us" group-title="Sports, PPV",PPV 1: UFC 300, Pereira vs Hill')
        self.assertEqual(ch["name"], "PPV 1: UFC 300, Pereira vs Hill")
        self.assertEqual(ch["group_title"], "Sports, PPV")


class GuardsThatAlreadyHold(unittest.TestCase):
    """These pass on main and must keep passing with a fix."""

    def test_comma_only_inside_an_attribute_is_not_the_separator(self):
        ch = _one('#EXTINF:-1 tvg-id="tsn1.ca" group-title="Sports, Canada",TSN 1')
        self.assertEqual(ch["name"], "TSN 1")
        self.assertEqual(ch["group_title"], "Sports, Canada")

    def test_tvg_name_still_wins(self):
        ch = _one('#EXTINF:-1 tvg-name="UFC 300, Pereira vs Hill" group-title="PPV",PPV 1')
        self.assertEqual(ch["name"], "UFC 300, Pereira vs Hill")
        self.assertEqual(ch["tvg_name"], "UFC 300, Pereira vs Hill")

    def test_plain_name(self):
        self.assertEqual(_one('#EXTINF:-1 tvg-id="a",Channel A')["name"], "Channel A")
        self.assertEqual(_one("#EXTINF:-1,Channel A")["name"], "Channel A")


def _old_parse(text):
    """What the parser returned before: the name after the LAST comma when
    tvg-name is missing or empty (to build rows as an older build stored them)."""
    out = []
    for ch in parse_m3u(text):
        if not ch["tvg_name"]:
            ch = {**ch, "name": ch["name"].rsplit(",", 1)[-1].strip()}
        out.append(ch)
    return out


class RandomLines(unittest.TestCase):
    def test_random_attributes_and_names(self):
        """Whatever the quoted attributes hold (commas included), the name is
        everything after the attributes; a name without a comma reads as before."""
        seeds = int(os.environ.get("M3U_NAME_SEEDS", "1000"))
        pieces = ["A", "b", "1", " ", ",", ":", "-", "|", "'", "&", "(", ")", ".", "é", "日"]
        for seed in range(seeds):
            rnd = random.Random(seed)
            word = lambda: "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 12)))
            attrs = {k: word() for k in rnd.sample(["tvg-id", "tvg-logo", "group-title", "tvg-chno"], rnd.randint(0, 4))}
            name = word().strip() or "X"
            line = "#EXTINF:-1" + "".join(f' {k}="{v}"' for k, v in attrs.items()) + "," + name
            ch = _one(line)
            self.assertEqual(name.strip(), ch["name"], f"seed {seed}: {line!r}")
            self.assertEqual(attrs.get("group-title") or None, ch["group_title"], f"seed {seed}")
            if "," not in name:
                self.assertEqual(_old_parse(f"#EXTM3U\n{line}\nhttp://p.example/1.ts\n")[0]["name"], ch["name"],
                                 f"seed {seed}: a name without a comma must read as before")


class LiveTvLineupKeepsTheNames(unittest.TestCase):
    """Two event channels whose names end the same must not both become that ending."""

    def setUp(self):
        tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         provider_type="m3u_url", live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id

    def test_channel_rows_carry_the_whole_names(self):
        text = (
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="NBA",NBA 01: Lakers vs Celtics, English\n'
            "http://192.0.2.10/live/1.ts\n"
            '#EXTINF:-1 group-title="NBA",NBA 02: Knicks vs Nets, English\n'
            "http://192.0.2.10/live/2.ts\n"
        )
        livetv._upsert_channels_from_m3u(self.pid, parse_m3u(text), self.db)
        self.db.commit()
        self.db.expire_all()
        names = sorted(c.name for c in self.db.query(LiveChannel).filter_by(provider_id=self.pid))
        self.assertEqual(names, ["NBA 01: Lakers vs Celtics, English", "NBA 02: Knicks vs Nets, English"])


class RowsStoredUnderTheCutNameKeepTheirNumber(unittest.TestCase):
    """Upgrade: rows an older build stored under the cut name are renamed in
    place. Jellyfin keys a channel's timers and favourites on its GuideNumber
    (stream_id), so a new row would lose them (#259)."""

    TEXT = (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="NBA",NBA 01: Lakers vs Celtics, English\n'
        "http://192.0.2.10/live/1.ts\n"
        '#EXTINF:-1 group-title="NBA",NBA 02: Knicks vs Nets, English\n'
        "http://192.0.2.10/live/2.ts\n"
        '#EXTINF:-1 group-title="NBA",NBA TV\n'
        "http://192.0.2.10/live/3.ts\n"
    )

    def setUp(self):
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         provider_type="m3u_url", live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="NBA", enabled=True))
        self.db.commit()

    def _sync(self, channels):
        livetv._upsert_channels_from_m3u(self.pid, channels, self.db)
        self.db.commit()
        self.db.expire_all()
        return {c.stream_url: c for c in self.db.query(LiveChannel).filter_by(provider_id=self.pid)}

    def test_the_rows_are_renamed_and_keep_number_and_settings(self):
        before = self._sync(_old_parse(self.TEXT))
        self.assertEqual(["English", "English", "NBA TV"], sorted(c.name for c in before.values()))
        numbers = {url: c.stream_id for url, c in before.items()}
        ids = {url: c.id for url, c in before.items()}
        before["http://192.0.2.10/live/2.ts"].enabled = False      # the user's own "off"
        before["http://192.0.2.10/live/1.ts"].channel_number = 501
        self.db.commit()
        after = self._sync(parse_m3u(self.TEXT))
        self.assertEqual({"http://192.0.2.10/live/1.ts": "NBA 01: Lakers vs Celtics, English",
                          "http://192.0.2.10/live/2.ts": "NBA 02: Knicks vs Nets, English",
                          "http://192.0.2.10/live/3.ts": "NBA TV"},
                         {url: c.name for url, c in after.items()})
        self.assertEqual(numbers, {url: c.stream_id for url, c in after.items()})
        self.assertEqual(ids, {url: c.id for url, c in after.items()})
        self.assertFalse(after["http://192.0.2.10/live/2.ts"].enabled)
        self.assertEqual(501, after["http://192.0.2.10/live/1.ts"].channel_number)
        # And the next sync changes nothing.
        again = self._sync(parse_m3u(self.TEXT))
        self.assertEqual(numbers, {url: c.stream_id for url, c in again.items()})

    def test_a_cut_name_row_with_another_url_is_not_taken(self):
        """Only the row with the same URL is the same channel."""
        self._sync(_old_parse('#EXTM3U\n#EXTINF:-1 group-title="NBA",English\nhttp://192.0.2.10/live/9.ts\n'))
        after = self._sync(parse_m3u('#EXTM3U\n#EXTINF:-1 group-title="NBA",NBA 01: Lakers vs Celtics, English\n'
                                     'http://192.0.2.10/live/1.ts\n'))
        self.assertEqual(["http://192.0.2.10/live/1.ts"], list(after))
        self.assertEqual(livetv._m3u_stable_id("NBA 01: Lakers vs Celtics, English", "http://192.0.2.10/live/1.ts"),
                         after["http://192.0.2.10/live/1.ts"].stream_id)

    def test_one_url_listed_twice_is_not_guessed(self):
        """Two entries with one URL (two names): which one the old row was is unknown."""
        text = ('#EXTM3U\n#EXTINF:-1 group-title="NBA",Game A, English\nhttp://192.0.2.10/live/1.ts\n'
                '#EXTINF:-1 group-title="NBA",Game B, English\nhttp://192.0.2.10/live/1.ts\n')
        before = self._sync(_old_parse(text))
        old_number = before["http://192.0.2.10/live/1.ts"].stream_id
        self._sync(parse_m3u(text))
        rows = self.db.query(LiveChannel).filter_by(provider_id=self.pid).all()
        self.assertEqual(["Game A, English", "Game B, English"], sorted(r.name for r in rows))
        self.assertNotIn(old_number, {r.stream_id for r in rows})


class M3uVodTitlesKeepTheirCommas(unittest.TestCase):
    """The M3U VOD sync (M3UClient) files titles under the parsed name."""

    def _client(self, body):
        from services.sync import M3UClient
        tmp = temp_dir(self)
        path = os.path.join(tmp, "vod.m3u")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return M3UClient(types.SimpleNamespace(
            name="P", provider_type="m3u_file", m3u_url=path, user_agent=None))

    def test_movie_title(self):
        c = self._client(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="" group-title="Movies",Crazy, Stupid, Love. (2011)\n'
            "http://192.0.2.10/movie/u/p/101.mkv\n"
        )
        names = [m["name"] for m in c.get_vod_streams("Movies")]
        self.assertEqual(names, ["Crazy, Stupid, Love. (2011)"])

    def test_series_show_name(self):
        c = self._client(
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="Series",Love, Death & Robots S01E01\n'
            "http://192.0.2.10/series/u/p/201.mkv\n"
        )
        shows = [s["name"] for s in c.get_series_list("Series")]
        self.assertEqual(shows, ["Love, Death & Robots"])


def _vod_search(_self, name, year=None, **k):
    """TMDB name search with the year, as the sync asks it."""
    TMDB.calls.append(("search", name, year))
    tid = {("Love", "2011"): 777, ("Love", "2015"): 333, ("Crazy, Stupid, Love", "2011"): 50646,
           ("Up", "2009"): 14160}.get((name, str(year))) or TMDB.search.get(name)
    return _vod_meta(tid, *TMDB.films[tid]) if tid else None


class M3uVodUpgrade(VodBase):
    """The real M3U VOD sync, before and after: a film an older build matched
    under its cut name ("Love. (2011)" -> a film called "Love", 2011) is
    imported under its real title on the next sync, and the wrong film goes
    by the usual two-sync prune. Other films are untouched."""

    FILLER = range(9000, 9040)      # so the prune's cap allows one removal

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(TMDB, "search_movie", _vod_search)
        patch.start()
        self.addCleanup(patch.stop)
        playlist = _RealPath(self.vod).parent / "vod.m3u"
        names = ["Crazy, Stupid, Love. (2011)", "Love (2015)", "Up (2009)"] + [f"Filler {k} (2010)" for k in range(40)]
        playlist.write_text("#EXTM3U\n" + "".join(
            f'#EXTINF:-1 group-title="a",{n}\nhttp://provider/movie/u/p/{i}.mp4\n' for i, n in enumerate(names)),
            encoding="utf-8")
        self.p.provider_type, self.p.m3u_url = "m3u_file", str(playlist)
        self.db.commit()
        sync.make_provider_client = lambda p: sync.M3UClient(p)
        TMDB.films = {50646: ("Crazy, Stupid, Love.", "2011"), 777: ("Love", "2011"), 333: ("Love", "2015"),
                      14160: ("Up", "2009")}
        for k, tid in enumerate(self.FILLER):
            TMDB.films[tid] = (f"Filler {k}", "2010")
            TMDB.search[f"Filler {k}"] = tid

    def films(self):
        """tmdb id -> the stream its .strm plays"""
        return {m.tmdb_id: _RealPath(m.strm_path).read_text().rsplit("/", 1)[-1]
                for m in self.db.query(Movie) if m.tmdb_id not in self.FILLER}

    def test_the_real_title_replaces_the_wrong_match(self):
        def old_from_file(path):
            with open(path, encoding="utf-8") as f:
                return _old_parse(f.read())

        with mock.patch.object(m3u_parser, "parse_m3u_from_file", old_from_file):
            self.night()
        self.assertEqual({777: "0.mp4", 333: "1.mp4", 14160: "2.mp4"}, self.films())   # as an older build left it
        unchanged = {tid: self.row(tid).strm_path for tid in (333, 14160)}
        self.night()
        self.assertEqual({777: "0.mp4", 333: "1.mp4", 14160: "2.mp4", 50646: "0.mp4"}, self.films())
        self.night()
        self.assertEqual({333: "1.mp4", 14160: "2.mp4", 50646: "0.mp4"}, self.films())
        self.night()
        self.assertEqual({333: "1.mp4", 14160: "2.mp4", 50646: "0.mp4"}, self.films())
        self.assertEqual(unchanged, {tid: self.row(tid).strm_path for tid in (333, 14160)})

if __name__ == "__main__":
    unittest.main()

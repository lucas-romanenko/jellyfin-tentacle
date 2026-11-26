"""Subtitles got ready ahead of time, per person (Nov 2026, Lucas: "Sometimes
works sometimes doesnt … I clicked english and still says fetching").

Run from the tentacle/ directory:  python -m unittest discover -s tests

- Each person's languages, when subtitles show, and the skip choices, with
  the household's starting point (grown-ups and kids) filling in what they
  never set; written into Jellyfin's own user configuration.
- Only the household's languages are read out of a file (not all 48
  tracks), one entry per language and kind, never one already beside it.
- Files beside a title are named the way Jellyfin and Bazarr read them.
- A fetched subtitle is lined up with the speech; a right one is left alone.
- Tentacle's own reads take the lowest provider slot and are not listed as
  something playing (a deploy may cut them; they start again).
- The queue: a play puts the title first and its season's next episodes
  after it; a title done for the household's languages isn't read again.
"""
import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir


def _db(test):
    import models.database as mdb
    engine = create_engine(f"sqlite:///{temp_dir(test)}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db, name, kids=None, **prefs):
    from models.database import TentacleUser
    u = TentacleUser(jellyfin_user_id=name.lower() * 4, display_name=name, kids_level=kids, **prefs)
    db.add(u)
    db.commit()
    return u


class Choices(unittest.TestCase):
    def setUp(self):
        from services import watch_prefs
        self.W = watch_prefs
        self.db = _db(self)

    def test_codes_fold_to_one(self):
        n = self.W.normalize
        self.assertEqual([n("en"), n("eng"), n("ENG"), n("fra"), n("fre"), n("fr"), n("pt-BR"), n("eus")],
                         ["eng", "eng", "eng", "fre", "fre", "fre", "por", "baq"])
        self.assertEqual([n("und"), n(""), n(None), n("english")], ["", "", "", ""])

    def test_nobody_chose_anything_english_off_buttons(self):
        me = _user(self.db, "Roman")
        p = self.W.for_person(self.db, me)
        self.assertEqual((p["subtitle_languages"], p["subtitle_mode"], p["skip_intro"], p["skip_credits"]),
                         (["eng"], "off", "button", "button"))
        self.assertFalse(any(p["own"].values()))

    def test_kids_follow_the_kids_starting_point(self):
        kid = _user(self.db, "Mia", kids="kids")
        self.W.save_defaults(self.db, {"skip_intro": "off"}, kids=True)
        self.assertEqual(self.W.for_person(self.db, kid)["skip_intro"], "off")
        self.W.save_defaults(self.db, {"skip_intro": "auto", "subtitle_languages": ["fr"]})
        self.assertEqual(self.W.for_person(self.db, kid)["skip_intro"], "off")       # grown-ups' isn't theirs
        self.assertEqual(self.W.for_person(self.db, kid)["subtitle_languages"], ["eng"])

    def test_own_choices_win_and_reset(self):
        me = _user(self.db, "Roman")
        self.W.save_defaults(self.db, {"subtitle_mode": "always"})
        p = self.W.save_person(self.db, me, {"subtitle_languages": ["eng", "fra", "eng"], "skip_credits": "auto"})
        self.assertEqual(p["subtitle_languages"], ["eng", "fre"])
        self.assertEqual((p["subtitle_mode"], p["skip_credits"]), ("always", "auto"))
        self.assertEqual(me.subtitle_languages, "eng,fre")
        p = self.W.reset_person(self.db, me)
        self.assertEqual((p["subtitle_languages"], p["skip_credits"]), (["eng"], "button"))

    def test_wrong_choices_are_refused(self):
        me = _user(self.db, "Roman")
        for bad in ({"subtitle_languages": []}, {"subtitle_languages": ["klingon"]}, {"subtitle_mode": "sometimes"},
                    {"skip_intro": "yes"}):
            with self.assertRaises(ValueError):
                self.W.save_person(self.db, me, bad)

    def test_household_languages_are_everyones(self):
        _user(self.db, "Roman", subtitle_languages="eng,fre")
        _user(self.db, "Ana", subtitle_languages="spa")
        _user(self.db, "Mia", kids="kids")
        self.assertEqual(self.W.household_languages(self.db), ["eng", "fre", "spa"])

    def test_jellyfin_gets_the_first_language_and_the_mode(self):
        prefs = {"subtitle_languages": ["fre", "eng"], "subtitle_mode": "foreign"}
        conf = self.W.jellyfin_configuration({"AudioLanguagePreference": "", "PlayDefaultAudioTrack": True}, prefs)
        self.assertEqual((conf["SubtitleLanguagePreference"], conf["SubtitleMode"], conf["AudioLanguagePreference"]),
                         ("fre", "Smart", "fre"))
        self.assertTrue(conf["PlayDefaultAudioTrack"])        # the rest is kept
        conf = self.W.jellyfin_configuration({"AudioLanguagePreference": "jpn"}, {**prefs, "subtitle_mode": "off"})
        self.assertEqual((conf["SubtitleMode"], conf["AudioLanguagePreference"]), ("None", "jpn"))
        self.assertEqual(self.W.jellyfin_configuration({}, {**prefs, "subtitle_mode": "always"})["SubtitleMode"], "Always")


def _sub(index, lang, codec="subrip", title=None, hi=0, forced=0):
    tags = {"language": lang}
    if title:
        tags["title"] = title
    return {"index": index, "codec_type": "subtitle", "codec_name": codec, "tags": tags,
            "disposition": {"hearing_impaired": hi, "forced": forced}}


class WhatIsRead(unittest.TestCase):
    def setUp(self):
        from services import subtitles
        self.S = subtitles

    def test_only_the_households_languages_once_each_kind(self):
        streams = [{"index": 0, "codec_type": "video"}, _sub(2, "eng"), _sub(3, "eng", title="English SDH", hi=1),
                   _sub(4, "fra"), _sub(5, "spa"), _sub(6, "eng"), _sub(7, "cat"), _sub(8, "eng", forced=1),
                   _sub(9, "fre", codec="hdmv_pgs_subtitle"), _sub(10, "eng", title="English (CC)")]
        picks = self.S.choose(streams, ["eng", "fre"], {})
        self.assertEqual(picks, [(2, ("eng", False, False)), (3, ("eng", True, False)), (4, ("fre", False, False)),
                                 (8, ("eng", False, True))])

    def test_nothing_already_beside_the_title(self):
        picks = self.S.choose([_sub(2, "eng"), _sub(4, "fre")], ["eng", "fre"],
                              {("eng", False, False): Path("x.en.srt")})
        self.assertEqual(picks, [(4, ("fre", False, False))])

    def test_names_beside_a_title(self):
        media = Path("/m/Show (2021)/Season 01/Show S01E02.strm")
        self.assertEqual(self.S.sidecar_path(media, "eng").name, "Show S01E02.en.srt")
        self.assertEqual(self.S.sidecar_path(media, "fre", hi=True).name, "Show S01E02.fr.sdh.srt")
        self.assertEqual(self.S.sidecar_path(media, "baq", forced=True).name, "Show S01E02.eu.forced.srt")
        k = self.S.sidecar_key
        self.assertEqual(k(Path("Show S01E02.en.srt"), "Show S01E02"), ("eng", False, False))
        self.assertEqual(k(Path("Show S01E02.eng.hi.srt"), "Show S01E02"), ("eng", True, False))
        self.assertEqual(k(Path("Show S01E02.fr.forced.ass"), "Show S01E02"), ("fre", False, True))
        self.assertIsNone(k(Path("Show S01E02.nfo"), "Show S01E02"))
        self.assertIsNone(k(Path("Show S01E03.en.srt"), "Show S01E02"))

    def test_jellyfins_path_found_under_tentacles_roots(self):
        root = Path(temp_dir(self))
        f = root / "vod" / "shows" / "Show (2021)" / "Season 01" / "Show S01E02.strm"
        f.parent.mkdir(parents=True)
        f.write_text("x")
        self.assertEqual(self.S.local_path("/data/media/vod/shows/Show (2021)/Season 01/Show S01E02.strm",
                                           [str(root / "vod" / "shows")]), f)
        self.assertIsNone(self.S.local_path("/data/media/vod/shows/Other/Season 01/X.strm", [str(root)]))

    def test_a_relay_title_from_the_strm_address(self):
        self.assertEqual(self.S.relay_title("http://10.0.0.2:8888/api/vod/series/1.2235787.94d8501cf2921ffbb978.mkv"),
                         "series/1.2235787.94d8501cf2921ffbb978.mkv")

    def test_background_reads_say_so_to_the_relay(self):
        args = self.S._input_args("http://127.0.0.1:8888/api/vod/movie/1.2.ab.mkv", True)
        self.assertIn("x-tentacle-background: subtitles\r\n", args)
        self.assertEqual(self.S._input_args("/media/movies/a.mkv", False), ["-i", "/media/movies/a.mkv"])


class LiningUp(unittest.TestCase):
    def setUp(self):
        from services import subtitle_sync
        self.Y = subtitle_sync
        import random
        r = random.Random(7)
        self.cues, t = [], 2.0
        while t < 600:
            d = r.uniform(1.0, 3.0)
            self.cues.append((t, t + d, "line"))
            t += d + r.uniform(0.5, 2.5)
        speech = bytearray(int(610 * self.Y.RATE))
        for a, b, _ in self.cues:
            speech[int(a * self.Y.RATE):int(b * self.Y.RATE)] = b"\x01" * (int(b * self.Y.RATE) - int(a * self.Y.RATE))
        self.speech = bytes(speech)

    def test_a_late_subtitle_is_moved(self):
        late = self.Y.write_srt([(a + 2.4, b + 2.4, t) for a, b, t in self.cues])
        out, ms = self.Y.line_up(late, self.speech)
        self.assertAlmostEqual(ms / 1000, -2.4, delta=0.06)
        self.assertAlmostEqual(self.Y.parse_srt(out)[0][0], self.cues[0][0], delta=0.06)

    def test_a_subtitle_for_another_frame_rate_is_stretched(self):
        k = 23.976 / 25
        other = self.Y.write_srt([(a * k, b * k, t) for a, b, t in self.cues])
        speed, shift, _ = self.Y.fit(self.speech, self.Y.parse_srt(other))
        self.assertAlmostEqual(speed, 25 / 23.976, places=4)
        self.assertAlmostEqual(shift, 0, delta=0.06)

    def test_a_right_one_is_left_alone(self):
        right = self.Y.write_srt(self.cues)
        self.assertEqual(self.Y.line_up(right, self.speech), (right, None))
        self.assertEqual(self.Y.line_up(right, b"\x00" * 1000), (right, None))

    def test_a_subtitle_for_something_else_is_not_moved(self):
        import random
        r = random.Random(3)
        other, t = [], 1.0
        while t < 600:
            d = r.uniform(1.0, 3.0)
            other.append((t, t + d, "x"))
            t += d + r.uniform(0.5, 2.5)
        self.assertIsNone(self.Y.fit(self.speech, other))

    def test_a_subtitle_longer_than_the_film_does_not_belong(self):
        self.assertTrue(self.Y.belongs(self.cues, self.speech))
        self.assertFalse(self.Y.belongs([(5000.0, 5003.0, "x")], self.speech))

    def test_speech_is_the_loud_part(self):
        quiet, loud = [1e-6] * 100, [1e-2] * 50
        self.assertEqual(self.Y.speech_from_power(quiet + loud + quiet), bytes(100) + b"\x01" * 50 + bytes(100))
        self.assertEqual(self.Y.speech_from_power([1e-3] * 200), bytes(200))        # no contrast, no guess

    def test_srt_round_trip(self):
        text = "1\r\n00:00:01,000 --> 00:00:02,500\r\nHello\r\nthere\r\n\r\n2\n00:01:00.25 --> 00:01:01,000\nBye\n"
        cues = self.Y.parse_srt(text)
        self.assertEqual(cues, [(1.0, 2.5, "Hello\nthere"), (60.25, 61.0, "Bye")])
        self.assertEqual(self.Y.parse_srt(self.Y.write_srt(cues)), cues)


class SiteChoice(unittest.TestCase):
    def test_best_first_never_one_tried(self):
        from services.subtitle_fetch import rank
        results = [{"Id": "a", "Format": "srt", "DownloadCount": 10}, {"Id": "b", "Format": "srt", "DownloadCount": 900},
                   {"Id": "c", "Format": "ass", "DownloadCount": 5000}, {"Id": "d", "Format": "srt", "IsForced": True},
                   {"Id": "e", "Format": "srt", "DownloadCount": 50, "HearingImpaired": True},
                   {"Id": "f", "Format": "srt", "DownloadCount": 1, "IsHashMatch": True}]
        self.assertEqual([r["Id"] for r in rank(results, set())], ["f", "b", "a", "e"])
        self.assertEqual([r["Id"] for r in rank(results, {"f", "b"})], ["a", "e"])

    def test_text_in_any_encoding(self):
        from services.subtitle_fetch import decode
        self.assertEqual(decode("Ça va\n".encode("cp1252")), "Ça va\n")
        self.assertEqual(decode("﻿Ça va".encode("utf-8")), "Ça va")


class Queue(unittest.TestCase):
    def setUp(self):
        from services import subtitles
        self.S = subtitles
        self.db = _db(self)
        _user(self.db, "Roman", subtitle_languages="eng,fre")

    def test_first_in_line_and_not_read_twice(self):
        from models.database import SubtitleJob
        S = self.S
        S.queue(self.db, "aa", S.SEASON)
        S.queue(self.db, "bb", S.WATCHING)
        S.queue(self.db, "aa", S.ASKED)
        self.assertEqual(S._next_job(self.db).jellyfin_item_id, "aa")
        job = self.db.query(SubtitleJob).filter_by(jellyfin_item_id="aa").one()
        job.state, job.languages = "done", "eng,fre"
        self.db.commit()
        self.assertEqual(S.queue(self.db, "aa", S.ASKED).state, "done")
        self.assertEqual(S._next_job(self.db).jellyfin_item_id, "bb")
        # Someone in the house now reads Spanish: read again
        _user(self.db, "Ana", subtitle_languages="spa")
        self.assertEqual(S.queue(self.db, "aa", S.SEASON).state, "queued")

    def test_a_play_queues_the_title_then_the_rest_of_its_season(self):
        S = self.S
        eps = [{"Id": f"e{i}", "Type": "Episode", "SeriesName": "Show", "ParentIndexNumber": 1, "IndexNumber": i}
               for i in range(1, 11)]
        item = dict(eps[2], SeasonId="s1", SeriesId="show")

        class R:
            def raise_for_status(self): pass
            def json(self): return {"Items": eps}
        with mock.patch.object(S, "jellyfin_items", return_value=[item]), \
                mock.patch.object(S, "_jellyfin", return_value=("http://jf", "k")), \
                mock.patch("requests.get", return_value=R()):
            n = S.on_play(self.db, "e3")
        from models.database import SubtitleJob
        jobs = {j.jellyfin_item_id: j for j in self.db.query(SubtitleJob).all()}
        self.assertEqual(n, 1 + S.SEASON_AHEAD)
        self.assertEqual(jobs["e3"].priority, S.WATCHING)
        self.assertEqual(sorted(k for k, j in jobs.items() if j.priority == S.SEASON), [f"e{i}" for i in range(4, 10)])
        self.assertNotIn("e1", jobs)
        self.assertEqual(jobs["e3"].name, "Show S01E03")


class TheLowestSlot(unittest.TestCase):
    def test_background_ranks_below_downloads_and_is_never_a_recording(self):
        from routers import livetv
        p = livetv._LEASE_PRIORITY
        self.assertGreater(p["background"], p["download"])
        self.assertGreater(p["download"], p["vod"])
        self.assertIn("background", livetv._FILM_KINDS)

    def test_a_background_read_is_not_listed_as_playing(self):
        """The deploy guard counts what /api/live/streams lists: a download
        someone waits for is there, Tentacle's own read is not."""
        from routers import livetv, vod
        db = _db(self)
        loop = asyncio.new_event_loop()
        try:
            async def go():
                mine = vod._Download("vod:movie:1:2", "127.0.0.1", "background")
                theirs = vod._Download("vod:movie:1:3", "10.0.0.5", "download")
                vod._downloads.extend([mine, theirs])
                try:
                    return livetv._stream_snapshot(db)
                finally:
                    vod._downloads.remove(mine)
                    vod._downloads.remove(theirs)
            rows = loop.run_until_complete(go())
        finally:
            loop.close()
        names = [r.get("channel") for r in rows]
        self.assertIn("vod:movie:1:3", names)
        self.assertNotIn("vod:movie:1:2", names)

if __name__ == "__main__":
    unittest.main()

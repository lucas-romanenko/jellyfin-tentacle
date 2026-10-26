"""Team rules record only games really played then, never reruns (seen 26
Oct: "Canadiens at Blackhawks" on an event channel was set to record that
night; the game was played on the 23rd and the Canadiens played the Blues
on the 27th). The team's schedule comes from ESPN's public team schedule;
the guide's <previously-shown> is a second check. ESPN unreachable: the
rule plans as before.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import os
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from tmp_dirs import temp_dir
from services.recording_rules import guide as G
from services.recording_rules import plan as P
from services.recording_rules import schedule as S

NOW = datetime(2026, 10, 26, 20, 0)


def airing(key, channel, start, minutes, title, description="", event=False, previously_shown=None):
    return G.Airing(pool_key=key, channel=channel, channel_raw=channel, number=int(key[1:]),
                    start=start, stop=start + timedelta(minutes=minutes), title=title, description=description,
                    from_event=event, previously_shown=previously_shown, channel_id=int(key[1:]), provider_id=1)


def habs(**kw):
    return P.Rule(id=3, name="Montreal Canadiens", kind="team", league="nhl", team="mtl", **kw)


def habs_schedule(complete=True):
    # ESPN's, 26 Oct 2026 (UTC)
    return S.Schedule("nhl", "mtl", [
        S.Game(datetime(2026, 10, 21, 23, 0), "sjs", "Sharks", True),
        S.Game(datetime(2026, 10, 24, 0, 0), "chi", "Blackhawks", False),
        S.Game(datetime(2026, 10, 25, 23, 0), "wpg", "Jets", False),
        S.Game(datetime(2026, 10, 28, 0, 0), "stl", "Blues", False),
        S.Game(datetime(2027, 4, 10, 23, 0), "ott", "Senators", False),
    ], complete)


def tonight_and_tomorrow():
    items = [
        # the event channel's slot, still named after the 23 Oct game
        airing("c552", "Canadiens at Blackhawks", datetime(2026, 10, 27, 0, 0), 180, "Canadiens at Blackhawks",
               "From the channel name", event=True),
        airing("c5", "Sportsnet East", datetime(2026, 10, 28, 0, 0), 150, "Live: NHL Hockey",
               "Montreal Canadiens at St. Louis Blues. From Enterprise Center."),
    ]
    G.link_next(items)
    return items


class Verdict(unittest.TestCase):
    def test_an_old_game_is_a_rerun_with_its_date(self):
        why = S.verdict(habs_schedule(), datetime(2026, 10, 27, 0, 0), ["chi"])
        self.assertIsNotNone(why)
        self.assertTrue(why.startswith("rerun: the Canadiens played the Blackhawks on "), why)
        # the evening of the 23rd where the server is (America/Toronto here), the 24th in UTC
        with mock.patch.dict(os.environ, {"TZ": "America/Toronto"}):
            time.tzset()
            try:
                self.assertIn("Fri 23 Oct", S.verdict(habs_schedule(), datetime(2026, 10, 27, 0, 0), ["chi"]))
            finally:
                os.environ.pop("TZ", None)
                time.tzset()

    def test_the_real_game_is_a_game(self):
        self.assertIsNone(S.verdict(habs_schedule(), datetime(2026, 10, 28, 0, 0), ["stl"]))
        self.assertIsNone(S.verdict(habs_schedule(), datetime(2026, 10, 27, 23, 30), []), "the pregame's game")

    def test_another_opponent_than_the_one_played_then(self):
        why = S.verdict(habs_schedule(), datetime(2026, 10, 28, 0, 0), ["bos"])
        self.assertIn("play the Blues then, not the Bruins", why)

    def test_no_game_of_theirs_then(self):
        why = S.verdict(habs_schedule(), datetime(2026, 10, 30, 0, 0), [])
        self.assertTrue(why.startswith("rerun: no Canadiens game then"), why)

    def test_outside_the_schedule_or_unknown_is_left_alone(self):
        self.assertIsNone(S.verdict(None, datetime(2026, 10, 27, 0, 0), ["chi"]), "ESPN unreachable: as before")
        self.assertIsNone(S.verdict(habs_schedule(), datetime(2027, 4, 20, 23, 0), []), "playoffs not set yet")
        self.assertIsNone(S.verdict(habs_schedule(complete=False), datetime(2026, 10, 30, 0, 0), []),
                          "one competition's schedule: a cup tie isn't in it")
        self.assertIsNone(S.verdict(habs_schedule(complete=False), datetime(2026, 12, 1, 0, 0), ["chi"]),
                          "one competition's schedule: a game long ago isn't a reason")


class Planning(unittest.TestCase):
    def test_tonights_old_game_is_a_rerun_and_tomorrows_records(self):
        p = P.plan_rule(habs(), tonight_and_tomorrow(), NOW, habs_schedule())
        self.assertEqual([("record", "c5")], [(e.state, e.best.airing.pool_key) for e in p.events])
        self.assertEqual("c552", p.near[0].airing.pool_key)
        self.assertIn("played the Blackhawks on", p.near[0].reason)
        self.assertEqual(["c552"], [n.airing.pool_key for n in p.reruns])

    def test_without_a_schedule_it_plans_as_before(self):
        p = P.plan_rule(habs(), tonight_and_tomorrow(), NOW, None)
        self.assertEqual(["c552", "c5"], [e.best.airing.pool_key for e in p.events])
        self.assertEqual([], p.reruns)

    def test_record_replays_too_records_it(self):
        p = P.plan_rule(habs(skip_replays=False), tonight_and_tomorrow(), NOW, habs_schedule())
        self.assertEqual(["c552", "c5"], [e.best.airing.pool_key for e in p.events])

    def test_the_guide_says_shown_before(self):
        items = [airing("c5", "Sportsnet East", datetime(2026, 10, 28, 0, 0), 150, "NHL Hockey",
                        "Montreal Canadiens at St. Louis Blues.", previously_shown=True),
                 airing("c6", "Sportsnet One", datetime(2026, 10, 28, 0, 0), 150, "Live: NHL Hockey",
                        "Montreal Canadiens at St. Louis Blues.", previously_shown=True)]
        G.link_next(items)
        p = P.plan_rule(habs(), items, NOW, None)
        self.assertEqual(["c6"], [e.best.airing.pool_key for e in p.events], "a Live: listing is live")
        self.assertEqual("rerun: the guide marks it as shown before", p.near[0].reason)


class Fetching(unittest.TestCase):
    TEAMS = {"sports": [{"leagues": [{"teams": [
        {"team": {"id": "10", "abbreviation": "MTL", "displayName": "Montreal Canadiens", "name": "Canadiens"}},
        {"team": {"id": "4", "abbreviation": "CHI", "displayName": "Chicago Blackhawks", "name": "Blackhawks"}},
        {"team": {"id": "19", "abbreviation": "STL", "displayName": "St. Louis Blues", "name": "Blues"}},
    ]}]}]}

    def event(self, date, home_id, away_id):
        side = lambda i, h: {"homeAway": h, "team": {"id": i, "shortDisplayName": {"10": "Canadiens", "4": "Blackhawks",
                                                                                  "19": "Blues"}[i]}}
        return {"date": date, "competitions": [{"competitors": [side(home_id, "home"), side(away_id, "away")]}]}

    def setUp(self):
        d = temp_dir(self)
        pt = mock.patch.dict(os.environ, {"DATA_DIR": d})
        pt.start()
        self.addCleanup(pt.stop)
        S._mem.clear()
        self.addCleanup(S._mem.clear)
        self.calls = []
        self.down = False

    def fake_get(self, url, session=None):
        self.calls.append(url)
        if self.down:
            import requests
            raise requests.ConnectionError("no route")
        r = mock.Mock()
        if url.endswith("/teams?limit=500"):
            r.json.return_value = self.TEAMS
        elif url.endswith("/teams/10/schedule"):
            r.json.return_value = {"events": [self.event("2026-10-24T00:00Z", "4", "10"),
                                              self.event("2026-10-28T00:00Z", "19", "10")]}
        else:
            r.json.return_value = {"events": []}
        return r

    def test_espns_schedule_becomes_the_teams_games(self):
        with mock.patch("services.recording_art.logos._get", self.fake_get):
            s = S.schedule("nhl", "mtl")
        self.assertEqual([(datetime(2026, 10, 24), "chi", False), (datetime(2026, 10, 28), "stl", False)],
                         [(g.start, g.opponent, g.home) for g in s.games])
        self.assertTrue(s.complete)
        self.assertTrue(any(u.endswith("?seasontype=3") for u in self.calls), "playoffs too")

    def test_espn_down_uses_the_last_copy_then_nothing(self):
        with mock.patch("services.recording_art.logos._get", self.fake_get):
            S.schedule("nhl", "mtl")
            S._mem.clear()
            self.down = True
            with mock.patch.object(S, "SCHEDULE_MAX_AGE", 0):
                s = S.schedule("nhl", "mtl")
            self.assertEqual(2, len(s.games), "the copy kept on disk")
        S._mem.clear()
        with mock.patch.dict(os.environ, {"DATA_DIR": temp_dir(self)}), \
                mock.patch("services.recording_art.logos._get", self.fake_get):
            self.assertIsNone(S.schedule("nhl", "mtl"))
            n = len(self.calls)
            self.assertIsNone(S.schedule("nhl", "mtl"))
            self.assertEqual(n, len(self.calls), "not asked again on every scan")

    def test_leagues_without_a_source(self):
        with mock.patch("services.recording_art.logos._get", self.fake_get):
            self.assertIsNone(S.schedule("cfl", "tor"))
            self.assertIsNone(S.schedule("iihf", "can"))
        self.assertEqual([], self.calls)


class Guide(unittest.TestCase):
    def test_previously_shown_and_new_are_read(self):
        from services.xmltv import parse_xmltv
        xml = ('<tv><programme start="20261027000000 +0000" stop="20261027030000 +0000" channel="a">'
               '<title>NHL Hockey</title><previously-shown start="20261024000000" /></programme>'
               '<programme start="20261028000000 +0000" stop="20261028030000 +0000" channel="a">'
               '<title>NHL Hockey</title><new /></programme>'
               '<programme start="20261028030000 +0000" stop="20261028040000 +0000" channel="a">'
               '<title>Post-game</title></programme></tv>')
        _, progs = parse_xmltv(xml)
        self.assertEqual([True, False, None], [p["previously_shown"] for p in progs])


class Booked(unittest.TestCase):
    """A rerun booked before its team's schedule was known is cancelled."""

    def test_a_booked_rerun_is_cancelled(self):
        import test_recording_rules as T
        from models.database import RecordingBooking, RecordingRule
        from services.recording_rules import book as B
        db = T._db()
        self.addCleanup(db.close)
        rule = RecordingRule(name="Montreal Canadiens", kind="team", league="nhl", team="mtl",
                             pre_padding_minutes=5, post_padding_minutes=60, playoff_extra_minutes=0,
                             mode="record", active=True, pregame_next=True, skip_replays=True)
        db.add(rule)
        db.commit()
        slot = datetime(2026, 10, 27, 0, 0)
        jf = T.FakeJellyfin([])
        jf.timers.append({"Id": "t9", "ProgramId": "p9", "ChannelId": "jf552", "Status": "New",
                          "StartDate": "2026-10-27T00:00:00.0000000Z", "EndDate": "2026-10-27T03:00:00.0000000Z"})
        db.add(RecordingBooking(rule_id=rule.id, pool_key="c552", start=slot, stop=slot + timedelta(hours=3),
                                state="booked", jellyfin_timer_id="t9", title="Canadiens at Blackhawks",
                                pre_padding_seconds=300, post_padding_seconds=3600, created_at=NOW, attempts=0))
        db.commit()
        items = tonight_and_tomorrow()[:1]
        for name, value in (("channel_ids", lambda db, entries=None: ({"c552": "jf552"}, {"jf552": "c552"})),
                            ("notify", lambda: None)):
            pt = mock.patch.object(B, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        with mock.patch.object(S, "schedule", lambda league, team, session=None: habs_schedule()), \
                mock.patch("routers.livetv.provider_stream_limit", lambda db, p: 2):
            summary = B.scan(db, jf=jf, now=NOW, airings=items)
        self.assertEqual(1, summary["cancelled"])
        self.assertEqual([("DELETE", "/LiveTv/Timers/t9")], jf.writes)
        self.assertEqual([], db.query(RecordingBooking).all())


if __name__ == "__main__":
    unittest.main()

"""The same game is never recorded twice, in every league (26 Oct: only 11
of the 16 team leagues have a real schedule; "I don't want multiple same
games recorded"). Once a team rule has recorded or booked the team against
an opponent, the same two teams within four days is a rerun, unless it is
the playoffs, a "Live" listing or a game the team's schedule has then. The
CFL's home-and-home a week apart still records; MLB's series are left to
its schedule.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from services.recording_rules import guide as G
from services.recording_rules import plan as P
from services.recording_rules import schedule as S

NOW = datetime(2026, 10, 26, 20, 0)


def airing(key, channel, start, minutes, title, description="", event=False):
    return G.Airing(pool_key=key, channel=channel, channel_raw=channel, number=int(key[1:]),
                    start=start, stop=start + timedelta(minutes=minutes), title=title, description=description,
                    from_event=event, channel_id=int(key[1:]), provider_id=1)


def guide(*items):
    items = list(items)
    G.link_next(items)
    return items


def argos(**kw):
    return P.Rule(id=7, name="Argonauts", kind="team", league="cfl", team="tor", **kw)


def canada(**kw):
    return P.Rule(id=8, name="Canada", kind="team", league="fifa", team="can", **kw)


FRIDAY = datetime(2026, 10, 23, 23, 0)
CFL_GAME = "CFL Football: Toronto Argonauts at Hamilton Tiger-Cats"


def played_friday(**kw):
    return P.Recorded(FRIDAY, CFL_GAME, description="From Tim Hortons Field.", channel_raw="TSN 1", **kw)


class RecordedMatchup(unittest.TestCase):
    def test_the_same_game_again_is_a_rerun(self):
        items = guide(airing("c552", "Argonauts at Tiger-Cats", datetime(2026, 10, 26, 23, 0), 180,
                             "Argonauts at Tiger-Cats", event=True))
        p = P.plan_rule(argos(), items, NOW, None, [played_friday()])
        self.assertEqual([], p.events)
        self.assertEqual(["c552"], [n.airing.pool_key for n in p.reruns])
        self.assertTrue(p.near[0].reason.startswith("rerun: the Argonauts vs the Tiger-Cats was recorded on "),
                        p.near[0].reason)
        self.assertIn(f"{FRIDAY:%b}", p.near[0].reason)

    def test_without_it_recorded_it_records(self):
        items = guide(airing("c552", "Argonauts at Tiger-Cats", datetime(2026, 10, 26, 23, 0), 180,
                             "Argonauts at Tiger-Cats", event=True))
        self.assertEqual(["c552"], [e.best.airing.pool_key for e in P.plan_rule(argos(), items, NOW).events])

    def test_the_home_and_home_a_week_later_records(self):
        items = guide(airing("c1", "TSN 1", FRIDAY + timedelta(days=7), 180,
                             "CFL Football: Hamilton Tiger-Cats at Toronto Argonauts", "From BMO Field."))
        p = P.plan_rule(argos(), items, NOW, None, [played_friday()])
        self.assertEqual(["c1"], [e.best.airing.pool_key for e in p.events])

    def test_another_opponent_records(self):
        items = guide(airing("c1", "TSN 1", datetime(2026, 10, 26, 23, 0), 180,
                             "CFL Football: Toronto Argonauts at Montreal Alouettes"))
        p = P.plan_rule(argos(), items, NOW, None, [played_friday()])
        self.assertEqual(["c1"], [e.best.airing.pool_key for e in p.events])

    def test_playoffs_and_live_listings_still_record(self):
        sunday = datetime(2026, 10, 26, 23, 0)
        playoff = guide(airing("c1", "TSN 1", sunday, 180, "CFL Football: Eastern Final",
                               "Toronto Argonauts at Hamilton Tiger-Cats. Playoffs."))
        self.assertEqual(1, len(P.plan_rule(argos(), playoff, NOW, None, [played_friday()]).events))
        live = guide(airing("c1", "TSN 1", sunday, 180, "Live: " + CFL_GAME))
        self.assertEqual(1, len(P.plan_rule(argos(), live, NOW, None, [played_friday()]).events))

    def test_record_replays_too_turns_it_off(self):
        items = guide(airing("c552", "Argonauts at Tiger-Cats", datetime(2026, 10, 26, 23, 0), 180,
                             "Argonauts at Tiger-Cats", event=True))
        p = P.plan_rule(argos(skip_replays=False), items, NOW, None, [played_friday()])
        self.assertEqual(1, len(p.events))

    def test_two_showings_in_this_guide(self):
        # tonight's game, and the same matchup two days later on an event channel
        items = guide(airing("c1", "TSN 1", datetime(2026, 10, 26, 23, 0), 180, CFL_GAME),
                      airing("c552", "Argonauts at Tiger-Cats", datetime(2026, 10, 28, 23, 0), 180,
                             "Argonauts at Tiger-Cats", event=True))
        p = P.plan_rule(argos(), items, NOW)
        self.assertEqual(["c1"], [e.best.airing.pool_key for e in p.events])
        self.assertIn("vs the Tiger-Cats is set to record on", p.reruns[0].reason)

    def test_an_event_slot_never_makes_the_real_broadcast_a_rerun(self):
        # Saturday's game named on Friday lands on Friday on the event
        # channel; TSN has it on Saturday
        items = guide(airing("c552", "Argonauts at Tiger-Cats", datetime(2026, 10, 30, 23, 0), 180,
                             "Argonauts at Tiger-Cats", event=True),
                      airing("c1", "TSN 1", datetime(2026, 10, 31, 23, 0), 180, CFL_GAME))
        p = P.plan_rule(argos(), items, NOW)
        self.assertIn("c1", [e.best.airing.pool_key for e in p.events])
        self.assertEqual([], p.reruns)

    def test_a_rerun_never_counts_as_the_game(self):
        # Fri the game, Mon a rerun of it (booked before), Wed the real
        # rematch: five days after the game, so it records
        rerun = P.Recorded(datetime(2026, 10, 26, 23, 0), "Argonauts at Tiger-Cats", channel_raw="Argonauts at Tiger-Cats",
                           from_event=True)
        items = guide(airing("c1", "TSN 1", FRIDAY + timedelta(days=5), 180, CFL_GAME))
        p = P.plan_rule(argos(), items, NOW, None, [played_friday(), rerun])
        self.assertEqual(["c1"], [e.best.airing.pool_key for e in p.events])

    def test_the_schedule_has_the_game_then(self):
        # Concacaf Nations League: Canada and Guatemala twice in three days
        first = P.Recorded(datetime(2026, 11, 14, 2, 0), "Concacaf Nations League: Guatemala vs. Canada")
        items = guide(airing("c1", "TSN 1", datetime(2026, 11, 17, 1, 0), 150,
                             "Concacaf Nations League: Canada vs. Guatemala"))
        now = datetime(2026, 11, 16, 12, 0)
        p = P.plan_rule(canada(), items, now, None, [first])
        self.assertEqual([], p.events, "no schedule: the same two teams again is a rerun")
        self.assertTrue(p.reruns[0].reason.startswith("rerun: Canada vs Guatemala was recorded on "), p.reruns[0].reason)
        s = S.Schedule("fifa", "can", [S.Game(datetime(2026, 11, 14, 2, 0), "gua", "Guatemala", False),
                                       S.Game(datetime(2026, 11, 17, 1, 0), "gua", "Guatemala", True)], False)
        p = P.plan_rule(canada(), items, now, s, [first])
        self.assertEqual(["c1"], [e.best.airing.pool_key for e in p.events])

    def test_mlb_series_are_left_to_the_schedule(self):
        jays = P.Rule(id=9, name="Blue Jays", kind="team", league="mlb", team="tor")
        first = P.Recorded(datetime(2026, 7, 10, 23, 0), "MLB Baseball: New York Yankees at Toronto Blue Jays")
        items = guide(airing("c1", "Sportsnet", datetime(2026, 7, 11, 23, 0), 180,
                             "MLB Baseball: New York Yankees at Toronto Blue Jays"))
        p = P.plan_rule(jays, items, datetime(2026, 7, 11, 12, 0), None, [first])
        self.assertEqual(1, len(p.events))


class History(unittest.TestCase):
    """What counts as recorded comes from recording_bookings."""

    def setUp(self):
        import test_recording_rules as T
        from models.database import RecordingRule
        self.T = T
        self.db = T._db()
        self.addCleanup(self.db.close)
        self.rule = RecordingRule(name="Argonauts", kind="team", league="cfl", team="tor", pre_padding_minutes=5,
                                  post_padding_minutes=60, playoff_extra_minutes=0, mode="record", active=True,
                                  pregame_next=True, skip_replays=True)
        self.db.add(self.rule)
        self.db.commit()

    def booking(self, state, start=FRIDAY, title=CFL_GAME, key="c1", **kw):
        from models.database import RecordingBooking
        b = RecordingBooking(rule_id=self.rule.id, pool_key=key, start=start, stop=start + timedelta(hours=3),
                             state=state, title=title, channel_name="TSN 1", created_at=NOW, attempts=0,
                             pre_padding_seconds=300, post_padding_seconds=3600, **kw)
        self.db.add(b)
        self.db.commit()
        return b

    def test_which_bookings_count(self):
        from services.recording_rules import book as B
        for state, hour in (("ended", 0), ("covered", 4), ("skipped", 8), ("failed", 12), ("preview", 16)):
            self.booking(state, datetime(2026, 10, 24, hour, 0))
        self.booking("ended", datetime(2026, 10, 20, 23, 0))   # longer ago than four days
        rule = B.load_rules(self.db)[0]
        got = [r.start.hour for r in B.recorded(self.db, [rule], NOW)[rule.id]]
        # not skipped (someone said no), failed, a record rule's old previews, or long ago
        self.assertEqual([0, 4], got)
        rule.mode = "preview"
        self.assertEqual([0, 4, 16], [r.start.hour for r in B.recorded(self.db, [rule], NOW)[rule.id]])

    def test_a_booked_rerun_is_cancelled(self):
        from models.database import RecordingBooking
        from services.recording_rules import book as B
        self.booking("ended")
        slot = datetime(2026, 10, 26, 23, 0)
        self.booking("booked", slot, "Argonauts at Tiger-Cats", key="c552", jellyfin_timer_id="t9", from_event=True)
        jf = self.T.FakeJellyfin([])
        jf.timers.append({"Id": "t9", "ProgramId": "p9", "ChannelId": "jf552", "Status": "New",
                          "StartDate": "2026-10-26T23:00:00.0000000Z", "EndDate": "2026-10-27T02:00:00.0000000Z"})
        items = guide(airing("c552", "Argonauts at Tiger-Cats", slot, 180, "Argonauts at Tiger-Cats", event=True))
        for name, value in (("channel_ids", lambda db, entries=None: ({"c552": "jf552"}, {"jf552": "c552"})),
                            ("notify", lambda: None)):
            pt = mock.patch.object(B, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        with mock.patch("routers.livetv.provider_stream_limit", lambda db, p: 2):
            summary = B.scan(self.db, jf=jf, now=NOW, airings=items)
        self.assertEqual(1, summary["cancelled"])
        self.assertEqual([("DELETE", "/LiveTv/Timers/t9")], jf.writes)
        self.assertEqual(["ended"], [b.state for b in self.db.query(RecordingBooking).all()])


if __name__ == "__main__":
    unittest.main()

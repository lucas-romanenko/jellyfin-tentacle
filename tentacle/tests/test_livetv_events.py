"""Event / PPV channels: the event read from the channel's name.

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_events

Names are real ones from a provider's live list (2026-10-21), plus the
examples the request gave. The clock is fixed: Wednesday 2026-10-21 10:30 in
Toronto (14:30 UTC).
"""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from services import livetv_events as E

TOR = ZoneInfo("America/Toronto")
NOW = datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)


def parse(name, group=None):
    return E.parse(name, group, now=NOW, server_tz=TOR)


def local(dt):
    return dt.astimezone(TOR).strftime("%Y-%m-%d %H:%M")


class Events(unittest.TestCase):
    def assertEvent(self, name, title, start, group=None, hours=3):
        ev = parse(name, group)
        self.assertIsInstance(ev, dict, name)
        self.assertEqual(title, ev["title"])
        self.assertEqual(start, local(ev["start"]))
        self.assertTrue(ev["timed"])
        self.assertEqual(hours * 3600, (ev["stop"] - ev["start"]).total_seconds())

    def test_the_requests_examples(self):
        self.assertEvent("NHL: Toronto Maple Leafs @ 7:00 PM", "NHL: Toronto Maple Leafs", "2026-10-21 19:00")
        self.assertEvent("PPV 3: UFC 300 - 10/22 8PM ET", "UFC 300", "2026-10-22 20:00")
        self.assertEvent("Leafs vs Bruins 19:00", "Leafs vs Bruins", "2026-10-21 19:00")

    def test_time_only_is_today_or_the_next_occurrence(self):
        self.assertEvent("NHL | 02 - 7:30pm Panthers at Senators", "Panthers at Senators", "2026-10-21 19:30",
                         group="US| NHL PPV")
        # 6 am is over (more than an event ago): tomorrow's.
        self.assertEvent("Boxing 6: Fury v Makhmudov 06:00", "Fury v Makhmudov", "2026-10-22 06:00",
                         group="US| PPV EVENT")
        # 9 am started an hour and a half ago: still today's.
        self.assertEvent("Boxing 6: Fury v Makhmudov 09:00", "Fury v Makhmudov", "2026-10-21 09:00",
                         group="US| PPV EVENT")

    def test_date_time_and_zone(self):
        self.assertEvent("NEXT | ORL - MAGIC VS ATL - HAWKS | Wed 21 Oct 18:00 EDT (US) | 8K EXCLUSIVE | "
                         "US: NBA PASS PPV 1", "ORL - Magic vs ATL - Hawks", "2026-10-21 18:00")
        self.assertEvent("NEXT | CHICAGO @ TORONTO | Wed 21 Oct 20:30 NDT (CA) | 8K EXCLUSIVE | CA: SPORTSNET PPV 9",
                         "Chicago @ Toronto", "2026-10-21 19:00")
        self.assertEvent("NBA: Chicago vs Toronto @ Oct 21 7:00 PM :Sportsnet+  08", "NBA: Chicago vs Toronto",
                         "2026-10-21 19:00")
        self.assertEvent("Next | Bayern Munich vs. Arsenal | UEFA Champions League | 2026-10-21 | 19:00 (GMT) | "
                         "8K EXCLUSIVE | CA: DAZN PPV 12", "Bayern Munich vs. Arsenal", "2026-10-21 15:00")
        self.assertEvent("PPV EVENT 01: No Mercy 17 (10.21 4:45 PM ET)", "No Mercy 17", "2026-10-21 16:45")
        self.assertEvent("US (ESPN+ 011) | American University vs. Army Oct 21 1:00PM ET (2026-10-21 13:00:00)",
                         "American University vs. Army", "2026-10-21 13:00", group="US| ESPN+ PPV")
        self.assertEvent("CBC 9: sports|Canada West Womens Volleyball: Alberta vs UFV - 21/10 16:00",
                         "Canada West Womens Volleyball: Alberta vs UFV", "2026-10-21 16:00", group="CA| CBC PPV")

    def test_a_given_end(self):
        ev = parse("MLB 01 : Rays x Guardians start:2026-10-21 22:08:00 stop:2026-10-22 01:30:00", "US| MLB PPV")
        self.assertEqual("Rays x Guardians", ev["title"])
        self.assertEqual("2026-10-21 18:08", local(ev["start"]))
        self.assertEqual("2026-10-21 21:30", local(ev["stop"]))

    def test_no_time_is_a_block_from_now(self):
        ev = parse("LIVE EVENT 26 - MAIN STAGE Live From Coachella 2026", "US| PPV EVENT")
        self.assertEqual("MAIN STAGE Live From Coachella 2026", ev["title"])
        self.assertFalse(ev["timed"])
        self.assertEqual("2026-10-21 10:00", local(ev["start"]))

    def test_no_event(self):
        for name, group in [
            ("- NO EVENT STREAMING - | 8K EXCLUSIVE | US: NBA PASS PPV 12", "US| NBA PASS PPV"),
            ("LIVE EVENT 02 - NO EVENT", "US| PPV EVENT"),
            ("PPV EVENT 02", "US| PPV EVENT ⁽ᴮᴷ⁾"),
            (":Milb  02", "US| MILB PPV"),
            ("NHL | 08 -", "US| NHL PPV"),
            ("MLB 03 :", "US| MLB PPV"),
            ("US (ESPN+ 501) |  (2098-12-31 08:00:09)", "US| ESPN+ PPV"),
            ("CA | CFL 02ⓧ: No Scheduled Event", "CA| CFL PPV"),
            ("NBA: REPLAY 4 HD", "US| NBA TEAM PPV"),
            ("CA EN: ROGERS SUPER SPORTS PACK 8 ᴴᴰ", "CA| ROGERS SUPER SPORTS PACK EN"),
        ]:
            with self.subTest(name=name):
                self.assertEqual("no_event", parse(name, group))

    def test_long_gone_events_are_no_event(self):
        # Last April's playoffs, still in the name.
        self.assertEqual("no_event", parse("WHL TV 07: Round 2 - Game 3: SPO @ VIC | GAME REPLAYS | Wed Apr 16th 10:05PM",
                                           "CA| WHL PPV"))
        self.assertEqual("no_event", parse("NBA 02: Knicks (NYK) x Timberwolves (MIN) start:2025-01-18 00:20:00 "
                                           "stop:2025-01-18 04:20:00", "US| NBA PPV"))
        # A weekday that fits last year's date: last year's.
        self.assertEqual("no_event", parse("CA | CFL 00ⓧ: Montreal Alouettes vs Hamilton Tiger-Cats | Sat 8th Nov "
                                           "3:00 PM ET", "CA| CFL PPV"))

    def test_regular_channels_are_no_event_channels(self):
        for name, group in [
            ("US: NHL NETWORK", "US| NHL PPV"),
            ("US: BALLY SPORTS SOUTH (NORTH CAROLINA 2) HD", "US| BALLY SPORTS PPV"),
            ("NBA: BOSTON CELTICS ᴴᴰ", "US| NBA TEAM PPV"),
            ("TV: ESPN2 ᴿᴬᵂ", "US| DIREC TV ᴿᴬᵂ ⁶⁰ᶠᵖˢ"),
            ("CH: BLUE SPORT 1 ᴿᴬᵂ", "CH| BLUE SPORT ᴿᴬᵂ"),
            ("CA: TSN 2 ᴿᴬᵂ", "CA| TSN SPORT ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ"),
            ("US: BOX OFFICE HD", "US| PPV EVENT"),
            ("CA EN: CTV NEWS CHANNEL", "CA| NEWS EN"),
        ]:
            with self.subTest(name=name):
                self.assertIsNone(parse(name, group))


if __name__ == "__main__":
    unittest.main()

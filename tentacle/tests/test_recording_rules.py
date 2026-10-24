"""Recording rules: follow a team (league -> team, from the built-in list) or
typed words, recorded as ordinary Jellyfin one-off timers (#770).

The listings below are the shapes real guides use (titles and descriptions
as an IPTV provider's guide and event channels write them). The fake
Jellyfin does what 10.11.8 does with timers: Defaults for a programme,
POST without a body, GET /LiveTv/Timers hiding Completed, 400 for a second
timer on one programme.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

import requests

from tmp_dirs import temp_dir
from services.recording_rules import book as B
from services.recording_rules import guide as G
from services.recording_rules import leagues as LG
from services.recording_rules import match as M
from services.recording_rules import plan as P

NOW = datetime(2026, 10, 24, 20, 0)


def at(h, m=0, day=24):
    return datetime(2026, 10, day, h, m)


def airing(key, channel, start, minutes, title, description="", event=False, provider=1, number=None,
           raw="", category=""):
    return G.Airing(pool_key=key, channel=channel, channel_raw=raw or channel, number=number or int(key[1:]),
                    start=start, stop=start + timedelta(minutes=minutes), title=title, description=description,
                    category=category, from_event=event, channel_id=int(key[1:]), provider_id=provider)


def guide(*items):
    items = list(items)
    G.link_next(items)
    return items


def leafs(**kw):
    return P.Rule(id=1, name="Toronto Maple Leafs", kind="team", league="nhl", team="tor",
                  pre_padding_minutes=5, post_padding_minutes=60, **kw)


def chosen(plan):
    return [(e.state, e.options[e.chosen].airing if e.chosen is not None else None) for e in plan.events]


def recorded(plan):
    return [e.options[e.chosen].airing for e in plan.events if e.state == "record" and e.chosen is not None]


class Folding(unittest.TestCase):
    def test_accents_case_and_ampersand(self):
        self.assertTrue(M.has(M.fold("Canadiens de Montréal"), "canadiens de montreal"))
        self.assertTrue(M.has(M.fold("Kyper & Bourne"), "Kyper and Bourne"))

    def test_whole_words_only(self):
        self.assertFalse(M.has(M.fold("Maple Leaf Pro Wrestling"), "Maple Leafs"))
        self.assertFalse(M.has(M.fold("Kingston Frontenacs"), "Kings"))

    def test_live_and_new_are_not_part_of_the_title(self):
        self.assertEqual("NHL Hockey", M.strip_prefix("Live: NHL Hockey"))
        self.assertEqual(M.identity("Live: NHL Hockey", "From Ball Arena"), M.identity("NHL Hockey", "From Ball Arena"))


class Teams(unittest.TestCase):
    def lg(self):
        return LG.league("nhl")

    def hit(self, title, description="", channel="", team="tor", league="nhl"):
        return M.match_team(M.Listing(title, "", description, "", channel), LG.league(league),
                            LG.team(league, team), LG.foreign_names(league, team))

    def test_the_built_in_list_has_the_major_leagues(self):
        ids = {lg["id"] for lg in LG.leagues()}
        self.assertTrue({"nhl", "nba", "nfl", "mlb", "mls", "cfl", "epl", "f1"} <= ids)
        self.assertEqual(32, len(LG.league("nhl")["teams"]))
        t = LG.team("nhl", "tor")
        self.assertIn("Leafs", t["nicknames"])
        self.assertIn("Scotiabank Arena", t["venue"])

    def test_full_name_is_enough(self):
        self.assertTrue(self.hit("Toronto Maple Leafs Pregame").matched)

    def test_a_nickname_needs_the_league(self):
        self.assertTrue(self.hit("Live: NHL Hockey", "Leafs host the Bruins").matched)
        self.assertFalse(self.hit("Leafs of Autumn", "A walk in the park").matched)

    def test_maple_leaf_pro_wrestling_is_not_the_leafs(self):
        self.assertFalse(self.hit("Maple Leaf Pro Wrestling Mayhem",
                                  "Professional wrestling showcasing top Canadian talent").matched)

    def test_toronto_in_another_teams_name_is_not_the_leafs(self):
        h = self.hit("Live: NBA Basketball", "The Wizards host the Toronto Raptors at Capital One Arena.")
        self.assertFalse(h.matched)
        self.assertFalse(self.hit("Live: CFL Football", "Toronto Argonauts at Montreal Alouettes").matched)

    def test_a_city_in_another_hockey_league_is_not_the_leafs(self):
        self.assertFalse(self.hit("AHL Hockey", "Toronto Marlies at Belleville Senators").matched)

    def test_home_arena_means_a_home_game(self):
        h = self.hit("Live: NHL Hockey", "From Scotiabank Arena in Toronto.")
        self.assertEqual((True, True), (h.matched, h.home))

    def test_at_says_who_is_home(self):
        self.assertEqual(True, self.hit("Penguins@Maple Leafs @7").home)
        self.assertEqual(False, self.hit("Maple Leafs at Penguins").home)
        self.assertIsNone(self.hit("NHL: Toronto vs Pittsburgh").home)

    def test_two_cities_of_one_league_make_a_game(self):
        self.assertTrue(self.hit("Pittsburgh@Toronto").matched)

    def test_french_listing(self):
        h = self.hit("Live: Hockey LNH", "En provenance du Centre Bell à Montréal.", team="mtl")
        self.assertEqual((True, True), (h.matched, h.home))

    def test_codes_only_in_capitals(self):
        self.assertTrue(self.hit("NHL: PIT @ TOR").matched)
        self.assertFalse(self.hit("Ancient tor of Dartmoor").matched)


class Planning(unittest.TestCase):
    def test_pregame_alone_isnt_the_game_but_whats_after_it_is(self):
        g = guide(
            airing("c5", "Sportsnet Ontario", at(23), 30, "Live: Toronto Maple Leafs Pregame",
                   "The Leafs get set to take on the Penguins."),
            airing("c5", "Sportsnet Ontario", at(23, 30), 150, "Live: NHL Hockey",
                   "From PPG Paints Arena in Pittsburgh."),
            airing("c6", "Sportsnet 360", at(23, 30), 150, "Live: NHL Hockey", "From Ball Arena in Denver."))
        p = P.plan_rule(leafs(), g, NOW)
        got = recorded(p)
        self.assertEqual([("c5", at(23, 30))], [(a.pool_key, a.start) for a in got])
        self.assertIn("Pregame", p.events[0].best.why)
        self.assertEqual(False, p.events[0].best.home, "another team's arena: an away game")

    def test_a_short_show_naming_the_team_is_a_near_miss(self):
        g = guide(airing("c5", "Sportsnet", at(21), 60, "Live: Real Kyper & Bourne",
                         "Talking Maple Leafs and the NHL."))
        p = P.plan_rule(leafs(), g, NOW)
        self.assertEqual([], p.events)
        self.assertIn("too short", p.near[0].reason)

    def test_one_game_one_recording_regular_channel_first(self):
        g = guide(
            airing("c7", "Penguins@Maple Leafs @7", at(23), 180, "Penguins@Maple Leafs @7", event=True,
                   raw="NHL 03: Penguins@Maple Leafs @7"),
            airing("c5", "Sportsnet Ontario", at(23), 150, "Live: NHL Hockey", "From Scotiabank Arena in Toronto."),
            airing("c8", "TVA Sports", at(23), 150, "Live: Hockey LNH", "En provenance du Scotiabank Arena."))
        p = P.plan_rule(leafs(), g, NOW)
        self.assertEqual(1, len(p.events))
        self.assertEqual("c5", p.events[0].best.airing.pool_key)
        self.assertEqual(3, len(p.events[0].options))

    def test_the_event_slot_links_the_regular_channel_that_doesnt_name_the_team(self):
        # an event channel names the game; Sportsnet only says where it's from
        g = guide(
            airing("c7", "Canadiens at Blackhawks", at(0, 0, 25), 180, "Canadiens at Blackhawks", event=True,
                   raw="NHL | 01 - 8pm Canadiens at Blackhawks"),
            airing("c5", "TSN 2", at(0, 0, 25), 150, "Live: NHL Hockey", "From the United Center in Chicago."),
            airing("c6", "Sportsnet 360", at(1, 0, 25), 150, "Live: NHL Hockey", "From Ball Arena in Denver."))
        p = P.plan_rule(P.Rule(id=2, name="Habs", kind="team", league="nhl", team="mtl"), g, NOW)
        self.assertEqual(["c5"], [a.pool_key for a in recorded(p)])
        self.assertEqual(False, p.events[0].best.home)

    def test_a_replay_is_skipped(self):
        g = guide(
            airing("c5", "Sportsnet", at(23), 150, "Live: NHL Hockey", "From Scotiabank Arena in Toronto."),
            airing("c5", "Sportsnet", at(5, 0, 25), 150, "NHL Hockey", "From Scotiabank Arena in Toronto."))
        p = P.plan_rule(leafs(), g, NOW)
        self.assertEqual([("record", at(23)), ("replay", at(5, 0, 25))],
                         [(e.state, e.best.airing.start) for e in p.events])
        p = P.plan_rule(leafs(skip_replays=False), g, NOW)
        self.assertEqual(["record", "record"], [e.state for e in p.events])

    def test_an_event_slot_about_the_team_that_isnt_a_game(self):
        g = guide(airing("c7", "The Rebuild: Inside the Montreal Canadiens", at(23), 180,
                         "The Rebuild: Inside the Montreal Canadiens", event=True))
        p = P.plan_rule(P.Rule(id=2, name="Habs", kind="team", league="nhl", team="mtl"), g, NOW)
        self.assertEqual([], p.events)

    def test_home_games_only(self):
        g = guide(airing("c5", "Sportsnet", at(23), 150, "Live: NHL Hockey", "From PPG Paints Arena",
                         raw="NHL"),
                  airing("c6", "TSN", at(23), 150, "Live: NHL Hockey: Maple Leafs at Penguins"))
        p = P.plan_rule(leafs(home_away="home"), g, NOW)
        self.assertEqual([], recorded(p))
        self.assertTrue(any("away" in n.reason for n in p.near))

    def test_finished_listings_are_not_looked_at(self):
        g = guide(airing("c5", "Sportsnet", at(15), 150, "Live: NHL Hockey", "From Scotiabank Arena in Toronto."))
        self.assertEqual([], P.plan_rule(leafs(), g, NOW).events)

    def test_a_game_already_on_can_still_be_recorded(self):
        g = guide(airing("c5", "Sportsnet", at(19), 150, "Live: NHL Hockey", "From Scotiabank Arena in Toronto."))
        self.assertEqual(1, len(recorded(P.plan_rule(leafs(), g, NOW))))

    def test_playoff_games_get_extra_time(self):
        a = airing("c5", "Sportsnet", at(23), 150, "Live: NHL Hockey", "Stanley Cup Playoffs, Game 7")
        r = leafs(playoff_extra_minutes=60)
        self.assertEqual((300, 7200), P.padding(r, a))

    def test_other_leagues_and_series(self):
        g = guide(
            airing("c1", "TSN 5", at(23), 180, "Live: CFL Football", "From BMO Field in Toronto."),
            airing("c2", "Sportsnet One", at(23), 150, "Live: NBA Basketball", "The Wizards host the Raptors."),
            airing("c3", "TSN 2", at(18), 150, "Live: Formula 1 Racing", "United States Grand Prix"),
            airing("c4", "MotoGP", at(19), 180, "MotoGP: Tissot Sprint Grand Prix of Australia", event=True))
        argos = P.plan_rule(P.Rule(id=3, name="Argos", kind="team", league="cfl", team="tor"), g, NOW)
        self.assertEqual(["c1"], [a.pool_key for a in recorded(argos)])
        raptors = P.plan_rule(P.Rule(id=4, name="Raptors", kind="team", league="nba", team="tor"), g, NOW)
        self.assertEqual(["c2"], [a.pool_key for a in recorded(raptors)])
        f1 = P.plan_rule(P.Rule(id=5, name="F1", kind="series", league="f1"), g, NOW)
        self.assertEqual(["c3"], [a.pool_key for a in recorded(f1)], "MotoGP's Grand Prix isn't F1")
        words = P.plan_rule(P.Rule(id=6, name="Grand Prix", kind="keywords", terms=["grand prix"]), g, NOW)
        self.assertEqual({"c4"}, {a.pool_key for a in recorded(words)}, "titles only, unless asked")
        words = P.plan_rule(P.Rule(id=6, name="Grand Prix", kind="keywords", terms=["grand prix"],
                                   search_description=True), g, NOW)
        self.assertEqual({"c3", "c4"}, {a.pool_key for a in recorded(words)})

    def test_keywords_any_all_and_excludes(self):
        g = guide(airing("c1", "CBC", at(23), 60, "The Tonight Show Starring Jimmy Fallon"),
                  airing("c2", "CTV", at(23, 30), 60, "Tonight in Toronto"))
        r = P.Rule(id=7, name="x", kind="keywords", terms=["Tonight Show"], min_minutes=0)
        self.assertEqual(["c1"], [a.pool_key for a in recorded(P.plan_rule(r, g, NOW))])
        r = P.Rule(id=7, name="x", kind="keywords", terms=["tonight"], excludes=["toronto"], min_minutes=0)
        self.assertEqual(["c1"], [a.pool_key for a in recorded(P.plan_rule(r, g, NOW))])
        r = P.Rule(id=7, name="x", kind="keywords", terms=["tonight", "fallon"], match_all=True, min_minutes=0)
        self.assertEqual(["c1"], [a.pool_key for a in recorded(P.plan_rule(r, g, NOW))])

    def test_a_channel_limit(self):
        g = guide(airing("c1", "A", at(23), 150, "Live: NHL Hockey", "From Scotiabank Arena"),
                  airing("c2", "B", at(23), 150, "Live: NHL Hockey", "From Scotiabank Arena"))
        p = P.plan_rule(leafs(channels={"c2"}), g, NOW)
        self.assertEqual(["c2"], [a.pool_key for a in recorded(p)])


class Connections(unittest.TestCase):
    def test_one_connection_the_second_game_waits_then_takes_its_replay(self):
        g = guide(
            airing("c1", "Sportsnet", at(23), 150, "Live: NHL Hockey", "From Scotiabank Arena in Toronto."),
            airing("c2", "TSN", at(23, 30), 150, "Live: NBA Basketball", "The Raptors visit the Celtics at TD Garden."),
            airing("c2", "TSN", at(4, 0, 25), 150, "NBA Basketball", "The Raptors visit the Celtics at TD Garden."))
        plans = [P.plan_rule(leafs(priority=1), g, NOW),
                 P.plan_rule(P.Rule(id=4, name="Raptors", kind="team", league="nba", team="tor"), g, NOW)]
        P.fit(plans, [], lambda pid: 1)
        self.assertEqual("record", plans[0].events[0].state)
        first, replay = plans[1].events
        self.assertEqual("waiting", first.state)
        self.assertIn("in use", first.reason)
        self.assertEqual(("record", at(4, 0, 25)), (replay.state, replay.best.airing.start))

    def test_a_recording_set_in_jellyfin_takes_a_connection_too(self):
        g = guide(airing("c1", "Sportsnet", at(23), 150, "Live: NHL Hockey", "From Scotiabank Arena in Toronto."))
        p = P.plan_rule(leafs(), g, NOW)
        P.fit([p], [P.Window(1, "c9", at(22), at(23, 59), "Movie")], lambda pid: 1)
        self.assertEqual("waiting", p.events[0].state)
        p = P.plan_rule(leafs(), g, NOW)
        P.fit([p], [P.Window(1, "c1", at(22), at(23, 59), "Same channel")], lambda pid: 1)
        self.assertEqual("record", p.events[0].state, "two recordings of one channel share its stream")


# ── Booking, with a database and a fake Jellyfin ───────────────────────────

class FakeJellyfin:
    """Jellyfin 10.11.8's timers, as far as rules use them."""

    def __init__(self, programmes):
        self.programmes = programmes     # [{Id, ChannelId, StartDate, EndDate, Name}]
        self.timers = []
        self.writes = []
        self.n = 0

    def get(self, path, **p):
        if path == "/LiveTv/Programs":
            lo, hi = B._parse(p["minStartDate"]), B._parse(p["maxStartDate"])
            return {"Items": [x for x in self.programmes if x["ChannelId"] == p["channelIds"]
                              and lo <= B._parse(x["StartDate"]) <= hi]}
        if path == "/LiveTv/Timers/Defaults":
            prog = next(x for x in self.programmes if x["Id"] == p["programId"])
            return {"ProgramId": prog["Id"], "ChannelId": prog["ChannelId"], "Name": prog["Name"],
                    "StartDate": prog["StartDate"], "EndDate": prog["EndDate"], "PrePaddingSeconds": 0,
                    "PostPaddingSeconds": 0}
        if path == "/LiveTv/Timers":
            return {"Items": [dict(t) for t in self.timers if t["Status"] != "Completed"]}
        raise AssertionError(path)

    def post(self, path, body=None, **p):
        assert path == "/LiveTv/Timers", path
        self.writes.append(("POST", body))
        if any(t["ProgramId"] == body["ProgramId"] and t["Status"] in ("New", "InProgress") for t in self.timers):
            r = requests.Response()
            r.status_code = 400
            raise requests.HTTPError("400", response=r)
        self.n += 1
        # Jellyfin echoes dates at 7 digits
        t = dict(body, Id=f"t{self.n}", Status="New",
                 StartDate=body["StartDate"][:19] + ".0000000Z", EndDate=body["EndDate"][:19] + ".0000000Z")
        self.timers.append(t)

    def delete(self, path, **p):
        self.writes.append(("DELETE", path))
        tid = path.rsplit("/", 1)[1]
        self.timers = [t for t in self.timers if t["Id"] != tid]


def _db():
    import models.database as mdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{temp_dir()}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class Booking(unittest.TestCase):
    def setUp(self):
        from models.database import RecordingRule
        self.db = _db()
        self.addCleanup(self.db.close)
        self.rule = RecordingRule(name="Toronto Maple Leafs", kind="team", league="nhl", team="tor",
                                  pre_padding_minutes=5, post_padding_minutes=60, playoff_extra_minutes=0,
                                  mode="record", active=True, pregame_next=True, skip_replays=True)
        self.db.add(self.rule)
        self.db.commit()
        self.game = airing("c5", "Sportsnet Ontario", at(23), 150, "Live: NHL Hockey",
                           "From Scotiabank Arena in Toronto.")
        self.jf = FakeJellyfin([{"Id": "p1", "ChannelId": "jf5", "Name": "NHL Hockey",
                                 "StartDate": "2026-10-24T23:00:00.0000000Z", "EndDate": "2026-10-25T01:30:00.0000000Z"}])
        pt = mock.patch.object(B, "channel_ids", lambda db, entries=None: ({"c5": "jf5"}, {"jf5": "c5"}))
        pt.start()
        self.addCleanup(pt.stop)
        pt = mock.patch.object(B, "notify", lambda: None)
        pt.start()
        self.addCleanup(pt.stop)
        pt = mock.patch("routers.livetv.provider_stream_limit", lambda db, p: 2)
        pt.start()
        self.addCleanup(pt.stop)

    def scan(self, now=NOW, items=None):
        return B.scan(self.db, jf=self.jf, now=now, airings=guide(*(items or [self.game])))

    def rows(self):
        from models.database import RecordingBooking
        return self.db.query(RecordingBooking).order_by(RecordingBooking.id).all()

    def test_preview_writes_nothing_to_jellyfin(self):
        self.rule.mode = "preview"
        self.db.commit()
        self.scan()
        self.assertEqual([], self.jf.writes)
        self.assertEqual(["preview"], [b.state for b in self.rows()])

    def test_books_like_the_record_button_with_the_rules_padding(self):
        self.scan()
        (kind, body), = self.jf.writes
        self.assertEqual(("POST", "p1", 300, 3600, True), (kind, body["ProgramId"], body["PrePaddingSeconds"],
                                                            body["PostPaddingSeconds"], body["IsPostPaddingRequired"]))
        b, = self.rows()
        self.assertEqual(("booked", "t1", "jf5"), (b.state, b.jellyfin_timer_id, b.jellyfin_channel_id))

    def test_a_second_scan_changes_nothing(self):
        self.scan()
        self.scan(now=NOW + timedelta(minutes=30))
        self.assertEqual(1, len(self.jf.writes), "a re-scan with the same guide must make no Jellyfin writes")

    def test_a_recording_set_in_jellyfin_is_covered_and_never_touched(self):
        self.jf.timers.append({"Id": "mine", "ProgramId": "p1", "ChannelId": "jf5", "Status": "New",
                               "StartDate": "2026-10-24T23:00:00.0000000Z", "EndDate": "2026-10-25T01:30:00.0000000Z"})
        self.scan()
        self.assertEqual([], self.jf.writes)
        self.assertEqual(["covered"], [b.state for b in self.rows()])
        # the rule goes away: the person's own timer stays
        self.rule.active = False
        self.db.commit()
        self.scan()
        self.assertEqual(["mine"], [t["Id"] for t in self.jf.timers])

    def test_a_timer_gone_before_its_start_is_booked_again(self):
        self.scan()
        self.jf.timers.clear()                    # a guide refresh dropped it
        self.scan(now=NOW + timedelta(minutes=30))
        self.assertEqual(["POST", "POST"], [w[0] for w in self.jf.writes])
        self.assertEqual("booked", self.rows()[0].state)

    def test_a_timer_moved_to_a_new_id_is_followed_not_doubled(self):
        self.scan()
        self.jf.timers[0]["Id"] = "moved"          # a lineup change re-made it
        self.scan(now=NOW + timedelta(minutes=30))
        self.assertEqual(1, len(self.jf.writes))
        self.assertEqual("moved", self.rows()[0].jellyfin_timer_id)

    def test_recording_then_ended(self):
        self.scan()
        self.jf.timers[0]["Status"] = "InProgress"
        self.scan(now=at(23, 10))
        self.assertEqual("recording", self.rows()[0].state)
        self.jf.timers[0]["Status"] = "Completed"
        self.scan(now=at(2, 40, 25))
        self.assertEqual("ended", self.rows()[0].state)

    def test_putting_the_rule_back_in_preview_cancels_its_own_upcoming_timer(self):
        self.scan()
        self.rule.mode = "preview"
        self.db.commit()
        self.scan(now=NOW + timedelta(minutes=5))
        self.assertEqual(("DELETE", "/LiveTv/Timers/t1"), self.jf.writes[-1])
        self.assertEqual([], self.jf.timers)
        self.assertEqual(["preview"], [b.state for b in self.rows()])

    def test_a_running_recording_is_never_cancelled(self):
        self.scan()
        self.jf.timers[0]["Status"] = "InProgress"
        self.scan(now=at(23, 5))
        self.rule.mode = "preview"
        self.db.commit()
        self.scan(now=at(23, 10))
        self.assertEqual(1, len(self.jf.timers))
        self.assertNotIn("DELETE", [w[0] for w in self.jf.writes])

    def test_skip_this_showing_is_never_booked_again(self):
        self.scan()
        b = self.rows()[0]
        self.assertTrue(B.cancel(self.db, self.jf, b, "person", "Skipped", NOW, state="skipped"))
        self.db.commit()
        self.scan(now=NOW + timedelta(minutes=30))
        self.assertEqual(["POST", "DELETE"], [w[0] for w in self.jf.writes])
        self.assertEqual(["skipped"], [r.state for r in self.rows()])

    def test_not_in_jellyfins_guide_yet_waits(self):
        self.jf.programmes = []
        self.scan()
        b, = self.rows()
        self.assertEqual("waiting", b.state)
        self.assertIn("guide", b.reason)

    def test_every_write_has_a_journal_row_first(self):
        from models.database import RecordingBookingEvent
        self.scan()
        actions = [e.action for e in self.db.query(RecordingBookingEvent).order_by(RecordingBookingEvent.id)]
        self.assertEqual(["book", "booked"], actions)

    def test_upcoming_follows_a_recording_starting_and_ending(self):
        self.scan()
        # booked 23:00-01:30 with 5 min before, 60 after: the next change is 22:55, then 02:30
        self.assertEqual(at(22, 55), B.next_change(self.db, NOW))
        self.assertEqual(at(2, 30, 25), B.next_change(self.db, at(23, 0)))
        asked = []
        with mock.patch.object(B, "request_scan", lambda delay=3.0: asked.append(delay)):
            B._look_again_at(at(22, 55), at(22, 50))
            B._look_again_at(at(23, 55), at(22, 50))      # past the next regular scan: no
            B._look_again_at(None, at(22, 50))
        self.assertEqual([360.0], asked)

    def test_once_the_booked_listing_ends_the_games_other_listing_isnt_booked_too(self):
        # Seen on production (23 Oct): TSN 2's listing ended at 02:30 while its
        # recording ran on in the padding; the next scan booked the game's
        # event-channel slot (to 03:00) as well, a second recording of one game.
        slot = airing("c7", "Penguins at Maple Leafs", at(23), 180, "Penguins at Maple Leafs", event=True,
                      raw="NHL | 01 - 7pm Penguins at Maple Leafs")
        self.game = airing("c5", "Sportsnet Ontario", at(23), 150, "Live: NHL Hockey", "From Scotiabank Arena in Toronto.")
        self.scan(items=[self.game, slot])
        self.assertEqual(["c5"], [b.pool_key for b in self.rows()])
        self.jf.timers[0]["Status"] = "InProgress"
        self.scan(now=at(1, 40, 25), items=[slot])      # Sportsnet's listing is over; the slot runs to 02:00
        self.assertEqual(1, len([w for w in self.jf.writes if w[0] == "POST"]))
        self.assertEqual(["c5"], [b.pool_key for b in self.rows()])

    def test_date_precision_doesnt_matter(self):
        self.assertTrue(B._same_start(B._parse("2026-10-24T23:00:00.0000000Z"), B._parse("2026-10-24T23:00:00.000Z")))
        self.assertTrue(B._same_start(B._parse("2026-10-24T23:00:00+00:00"), at(23)))


class Api(unittest.TestCase):
    def setUp(self):
        from models.database import TentacleUser
        from routers import recordings as R
        self.R = R
        self.db = _db()
        self.addCleanup(self.db.close)
        self.admin = TentacleUser(jellyfin_user_id="a" * 32, display_name="Admin", is_admin=True)
        self.ann = TentacleUser(jellyfin_user_id="b" * 32, display_name="Ann", is_admin=False)
        self.kid = TentacleUser(jellyfin_user_id="c" * 32, display_name="Kid", is_admin=False, kids_level="kids")
        self.db.add_all([self.admin, self.ann, self.kid])
        self.db.commit()
        for name, value in (("_changed", lambda db: None),):
            pt = mock.patch.object(R, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        R._record_permission.clear()

    def as_user(self, user, may_record=False):
        pt = mock.patch.object(self.R, "_caller", lambda request, db: user)
        pt.start()
        self.addCleanup(pt.stop)
        R = self.R

        class Jf:
            def get(self, path, **p):
                return {"Policy": {"EnableLiveTvManagement": may_record}}
        pt = mock.patch("services.livetv_tuners.jellyfin_for", lambda db: Jf())
        pt.start()
        self.addCleanup(pt.stop)
        R._record_permission.clear()

    def test_someone_who_may_record_follows_their_own_team(self):
        self.as_user(self.ann, may_record=True)
        r = self.R.create_rule(self.R.RuleBody(kind="team", league="nhl", team="tor"), None, self.db)
        self.assertEqual(("Toronto Maple Leafs", "preview", True, 60), (r["name"], r["mode"], r["mine"],
                                                                       r["post_padding_minutes"]))

    def test_without_jellyfins_recording_permission_no(self):
        self.as_user(self.ann, may_record=False)
        with self.assertRaises(self.R.HTTPException) as e:
            self.R.create_rule(self.R.RuleBody(kind="team", league="nhl", team="tor"), None, self.db)
        self.assertEqual(403, e.exception.status_code)

    def test_kids_never(self):
        self.as_user(self.kid, may_record=True)
        self.assertFalse(self.R.can_record(self.db, self.kid))

    def test_only_the_owner_or_an_admin_changes_a_rule(self):
        self.as_user(self.admin)
        r = self.R.create_rule(self.R.RuleBody(kind="keywords", terms=["Grand Prix"]), None, self.db)
        self.as_user(self.ann, may_record=True)
        with self.assertRaises(self.R.HTTPException) as e:
            self.R.update_rule(r["id"], self.R.RulePatch(mode="record"), None, self.db)
        self.assertEqual(403, e.exception.status_code)

    def test_a_keyword_rule_needs_a_word(self):
        self.as_user(self.admin)
        with self.assertRaises(self.R.HTTPException):
            self.R.create_rule(self.R.RuleBody(kind="keywords", terms=["  "]), None, self.db)

    def test_a_cancel_in_jellyfin_skips_that_showing(self):
        from models.database import RecordingBooking
        b = RecordingBooking(rule_id=1, pool_key="c5", start=at(23), stop=at(1, 30, 25), state="booked",
                             jellyfin_timer_id="abc")
        self.db.add(b)
        self.db.commit()
        with mock.patch.object(B, "notify", lambda: None), \
                mock.patch.object(self.R, "_cancel_confirmed", lambda db, t: t == "abc"), \
                mock.patch("routers.auth._has_internal_secret", lambda r, db: False), \
                mock.patch.object(self.R, "_caller", lambda r, db: self.ann):
            with self.assertRaises(self.R.HTTPException):
                self.R.timer_cancelled(self.R.CancelledBody(timer_id="abcd0000"), None, self.db)
            self.db.query(RecordingBooking).filter_by(id=b.id).update({"jellyfin_timer_id": "abcd1234"})
            self.db.commit()
            with mock.patch.object(self.R, "_cancel_confirmed", lambda db, t: t == "abcd1234"):
                out = self.R.timer_cancelled(self.R.CancelledBody(timer_id="abcd1234"), None, self.db)
        self.assertEqual(1, out["skipped"])
        self.assertEqual("skipped", self.db.get(RecordingBooking, b.id).state)


if __name__ == "__main__":
    unittest.main()

"""My teams: EuroLeague clubs and FIFA / IIHF national teams (#770).

Their names are everyday words: "Canada" is in the news, travel shows and a
rugby game; "Real Madrid" and "Bayern Munich" are football clubs too. So in
these leagues no name is enough alone: a listing needs the league's own
words (EuroLeague, FIFA, World Cup, IIHF, World Juniors), or its sport plus a
second team of it ("Soccer: Canada vs. Mexico"); and a word of another sport
("rugby", "basketball", "field hockey") rules it out. The football leagues
learn the same about basketball, so a Real Madrid football rule doesn't
record the basketball club.

The titles are the ones production guides use (beIN, Fox Soccer+, RDS, the
Greek Novasports guide, a Cypriot guide, event channels).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from datetime import datetime, timedelta

from services.recording_rules import guide as G
from services.recording_rules import leagues as LG
from services.recording_rules import match as M
from services.recording_rules import plan as P

NOW = datetime(2026, 10, 24, 10, 0)


def hit(league, team, title, description="", category="", channel=""):
    return M.match_team(M.Listing(title, "", description, category, channel), LG.league(league),
                        LG.team(league, team), LG.foreign_names(league, team))


def airing(key, start, minutes, title, description="", category="Sports", event=False):
    return G.Airing(pool_key=key, channel=key.upper(), channel_raw=key.upper(), number=int(key[1:]),
                    start=start, stop=start + timedelta(minutes=minutes), title=title, description=description,
                    category=category, from_event=event, channel_id=int(key[1:]), provider_id=1)


def recorded(rule, *items):
    items = list(items)
    G.link_next(items)
    plan = P.plan_rule(rule, items, NOW)
    return [e.options[e.chosen].airing.pool_key for e in plan.events if e.state == "record" and e.chosen is not None]


class TheList(unittest.TestCase):
    def test_the_three_leagues_are_there(self):
        self.assertEqual(20, len(LG.league("euroleague")["teams"]))
        self.assertEqual(211, len(LG.league("fifa")["teams"]))
        self.assertGreaterEqual(len(LG.league("iihf")["teams"]), 25)
        self.assertIn("Ρεάλ Μαδρίτης", LG.team("euroleague", "rma")["nicknames"])
        self.assertEqual("Suède", LG.team("iihf", "swe")["fr"])

    def test_every_team_has_an_id_a_name_and_a_code_once(self):
        for lg in LG.leagues():
            ids = [t["id"] for t in lg.get("teams") or []]
            self.assertEqual(len(ids), len(set(ids)), lg["id"])
            names = [M.fold(t["name"]) for t in lg.get("teams") or []]
            self.assertEqual(len(names), len(set(names)), lg["id"])
            for t in lg.get("teams") or []:
                self.assertTrue(t["name"] and t.get("abbr"), (lg["id"], t["id"]))

    def test_the_picker_lists_them(self):
        names = {lg["id"]: lg for lg in LG.summary()}
        self.assertEqual("FIFA (national teams)", names["fifa"]["name"])
        self.assertIn("Canada", [t["name"] for t in names["iihf"]["teams"]])


class EuroLeague(unittest.TestCase):
    def test_english_listing(self):
        h = hit("euroleague", "rma", "EuroLeague Basketball: Real Madrid vs. FC Barcelona", "From Movistar Arena in Madrid.")
        self.assertTrue(h.matched)
        self.assertTrue(h.home)
        self.assertFalse(hit("euroleague", "bar", "EuroLeague Basketball: Real Madrid vs. FC Barcelona",
                             "From Movistar Arena in Madrid.").home)

    def test_greek_guide(self):
        # Novasports: "Basketball: Maccabi Tel Aviv - Real Madrid", "EuroLeague 2026/27." below
        t = "Live: Μπάσκετ: Μακάμπι Τελ Αβίβ - Ρεάλ Μαδρίτης"
        self.assertTrue(hit("euroleague", "rma", t, "EuroLeague 2026/27.").matched)
        self.assertTrue(hit("euroleague", "tel", t, "EuroLeague 2026/27.").matched)
        self.assertFalse(hit("euroleague", "hta", t, "EuroLeague 2026/27.").matched, "the other Tel Aviv club")
        self.assertTrue(hit("euroleague", "oly", "Ολυμπιακός - Φενέρμπαχτσε (Ζ) : EuroLeague 2026/27").matched)
        self.assertTrue(hit("euroleague", "ulk", "Ολυμπιακός - Φενέρμπαχτσε (Ζ) : EuroLeague 2026/27").matched)

    def test_the_football_club_is_not_the_basketball_club(self):
        for t, d in (("LaLiga Soccer: Real Madrid vs. FC Barcelona", "From Santiago Bernabéu in Madrid."),
                     ("Live: Fútbol LaLiga: Barcelona vs. Real Madrid", "El Clásico."),
                     ("Real Madrid vs. FC Barcelona", "")):
            self.assertFalse(hit("euroleague", "rma", t, d, "Soccer").matched, t)
        self.assertFalse(hit("euroleague", "oly", "OLYMPIACOS - SPARTA PRAGUE (Z)", "UEFA EUROPA LEAGUE 2026-27.").matched)
        self.assertFalse(hit("euroleague", "bay", "Bundesliga: Bayern Munich vs. Dortmund").matched)

    def test_and_the_football_rules_leave_basketball_alone(self):
        t = "EuroLeague Basketball: Real Madrid vs. FC Barcelona"
        self.assertFalse(hit("laliga", "rma", t).matched)
        self.assertFalse(hit("laliga", "bar", t).matched)
        self.assertFalse(hit("laliga", "rma", "Basketball: Real Madrid vs. Baskonia", "Liga Endesa.").matched)
        self.assertFalse(hit("bundesliga", "fcb", "EuroLeague: Bayern Munich vs. Olympiacos").matched)
        self.assertTrue(hit("laliga", "rma", "LaLiga Soccer: Real Madrid vs. FC Barcelona").matched)
        self.assertTrue(hit("bundesliga", "fcb", "Bundesliga: Bayern Munich vs. Dortmund").matched)

    def test_the_clubs_other_basketball_games_count(self):
        # its league at home is basketball too: a follower wants it
        self.assertTrue(hit("euroleague", "rma", "Basketball: Real Madrid vs. Baskonia", "Liga Endesa.").matched)

    def test_not_the_nba_or_the_womens_euroleague(self):
        self.assertFalse(hit("euroleague", "prs", "NBA Paris Game: Spurs vs. Pacers", "From Accor Arena in Paris.",
                             "Basketball").matched)
        self.assertFalse(hit("euroleague", "pan", "Μπάσκετ: Παναθηναϊκός - Αθηναϊκός", "Ευρωλίγκα Γυναικών 2026/27.").matched)

    def test_a_name_alone_or_with_basketball_alone_is_not_enough(self):
        self.assertFalse(hit("euroleague", "rma", "Real Madrid: The Documentary").matched)
        self.assertFalse(hit("euroleague", "ulk", "Basketball: Fenerbahçe Beko", "Turkish league highlights.").matched)


class Fifa(unittest.TestCase):
    def test_games_as_guides_write_them(self):
        self.assertTrue(hit("fifa", "esp", "Spain vs Australia - FIFA U-17 Women's World Cup 2026").matched)
        self.assertTrue(hit("fifa", "chn", "China PR vs Samoa - FIFA U-17 Women's World Cup 2026").matched)
        self.assertTrue(hit("fifa", "arg", "2026 FIFA U-17 Women's World Cup: Argentina vs. Morocco", "Group C.").matched)
        self.assertTrue(hit("fifa", "can", "International Friendly: Canada vs. Wales", "From BMO Field in Toronto.").matched)
        self.assertTrue(hit("fifa", "can", "Concacaf Gold Cup: Mexico vs. Canada").matched)
        self.assertTrue(hit("fifa", "can", "Soccer: Canada vs. Mexico").matched, "the sport and two countries")
        self.assertTrue(hit("fifa", "usa", "Live: Fútbol: Estados Unidos vs. México", "Eliminatorias.").matched)
        self.assertTrue(hit("fifa", "can", "Coupe du monde de la FIFA : Canada c. Qatar").matched)

    def test_greek_guide(self):
        t = "Ποδόσφαιρο: Κροατία - Αγγλία"
        self.assertTrue(hit("fifa", "eng", t, "UEFA Nations League 2026-27.").matched)
        self.assertTrue(hit("fifa", "cro", t, "UEFA Nations League 2026-27.").matched)

    def test_a_country_alone_is_not_a_game(self):
        self.assertFalse(hit("fifa", "can", "Travel Thru History", "Vancouver, British Columbia, Canada.").matched)
        self.assertFalse(hit("fifa", "can", "Canada's Got Talent").matched)
        self.assertFalse(hit("fifa", "geo", "Soccer: Georgia vs. #11 South Carolina").matched, "college soccer")
        self.assertFalse(hit("fifa", "isl", "ESPN+: Rhode Island vs. Davidson", "Soccer.").matched)
        self.assertFalse(hit("fifa", "aus", "Live: AFL Women's Premiership Football", "Australia.").matched)
        self.assertFalse(hit("fifa", "can", "Soccer: Canada", "A friendly look at the season.").matched)

    def test_other_sports_with_country_names(self):
        self.assertFalse(hit("fifa", "can", "Canada vs. England", "Women's Rugby World Cup final.", "Rugby").matched)
        self.assertFalse(hit("fifa", "eng", "Rugby: Canada vs. England", "Women's Rugby World Cup final.").matched)
        self.assertFalse(hit("fifa", "ind", "Cricket World Cup: India vs. Australia").matched)
        self.assertFalse(hit("fifa", "can", "IIHF World Junior Championship: Canada vs. Finland").matched)
        self.assertFalse(hit("fifa", "can", "Field Hockey: Canada vs. Argentina").matched)
        self.assertFalse(hit("fifa", "usa", "FIBA World Cup: United States vs. Serbia", "Basketball.").matched)

    def test_club_football_is_not_the_national_team(self):
        self.assertFalse(hit("fifa", "egy", "FIFA Club World Cup: Al Ahly (Egypt) vs. Inter Miami (USA)").matched)
        self.assertFalse(hit("fifa", "can", "Canadian Premier League Soccer", "Action from Canada's top tier.").matched)
        self.assertFalse(hit("fifa", "esp", "Soccer: Cordoba vs. Eibar (Cordoba vs. Eibar (ESP))").matched)

    def test_ireland_is_not_northern_ireland(self):
        t = "UEFA Nations League: Northern Ireland vs. Hungary"
        self.assertFalse(hit("fifa", "irl", t).matched)
        self.assertTrue(hit("fifa", "nir", t).matched)
        self.assertFalse(hit("fifa", "irl", "Ποδόσφαιρο: Βόρεια Ιρλανδία - Ουγγαρία", "UEFA Nations League.").matched)
        self.assertTrue(hit("fifa", "nir", "Ποδόσφαιρο: Βόρεια Ιρλανδία - Ουγγαρία", "UEFA Nations League.").matched)
        self.assertFalse(hit("fifa", "gui", "World Cup qualifier: Equatorial Guinea vs. Ghana").matched)

    def test_codes_are_not_names(self):
        # "AND" (Andorra) and "PER" (Peru) are words in capital-letter guides
        self.assertFalse(hit("fifa", "and", "FIFA WORLD CUP QUALIFIER: TRINIDAD AND TOBAGO VS JAMAICA").matched)
        self.assertTrue(hit("fifa", "tri", "FIFA WORLD CUP QUALIFIER: TRINIDAD AND TOBAGO VS JAMAICA").matched)

    def test_men_women_and_youth_all_count_leave_out_words_narrow_it(self):
        g = [airing("c1", datetime(2026, 10, 24, 14, 0), 135, "Spain vs Australia - FIFA U-17 Women's World Cup 2026"),
             airing("c2", datetime(2026, 10, 25, 19, 0), 120, "FIFA World Cup Qualifier: Spain vs. Türkiye")]
        r = P.Rule(id=1, name="Spain", kind="team", league="fifa", team="esp")
        self.assertEqual(["c1", "c2"], recorded(r, *g))
        r = P.Rule(id=1, name="Spain", kind="team", league="fifa", team="esp", excludes=["Women's", "U-17"])
        self.assertEqual(["c2"], recorded(r, *g))


class Iihf(unittest.TestCase):
    def test_games_as_guides_write_them(self):
        self.assertTrue(hit("iihf", "can", "IIHF World Junior Championship: Canada vs. Finland").matched)
        self.assertTrue(hit("iihf", "swe", "Hockey: Suède vs. Canada", "Championnat mondial junior de l'IIHF.").matched)
        self.assertTrue(hit("iihf", "can", "Olympic Men's Hockey: Canada vs. United States", "Gold medal game.").matched)
        self.assertTrue(hit("iihf", "usa", "Olympic Men's Hockey: Canada vs. United States").matched)
        self.assertTrue(hit("iihf", "can", "Spengler Cup: Team Canada vs. HC Davos").matched)
        self.assertTrue(hit("iihf", "can", "Hockey: Canada vs. Sweden", "Women's World Championship.").matched)
        self.assertTrue(hit("iihf", "cze", "Hockey sur glace : Tchéquie c. Slovaquie").matched)

    def test_not_the_nhl_and_not_the_other_hockeys(self):
        self.assertFalse(hit("iihf", "can", "Hockey Night in Canada", "Maple Leafs at Canadiens.").matched)
        self.assertFalse(hit("iihf", "can", "Field Hockey: Canada vs. Argentina").matched)
        self.assertFalse(hit("iihf", "can", "Para Ice Hockey: Canada vs. United States", "Paralympic Games.").matched)
        self.assertFalse(hit("iihf", "can", "Soccer: Canada vs. Mexico").matched)
        self.assertFalse(hit("iihf", "can", "Travel Thru History", "The Olympic city of Vancouver, Canada.").matched)

    def test_and_the_nhl_rules_leave_national_teams_alone(self):
        for t in ("IIHF World Junior Championship: Canada vs. Finland", "Olympic Men's Hockey: Canada vs. United States",
                  "IIHF World Championship: Sweden vs. Switzerland"):
            for team in ("tor", "mtl", "dal", "min", "lak"):
                self.assertFalse(hit("nhl", team, t).matched, (team, t))
        self.assertTrue(hit("nhl", "tor", "Live: NHL Hockey: Maple Leafs at Canadiens").matched)

    def test_the_same_country_in_football_and_hockey(self):
        # one name, two leagues: each keeps its own sport
        self.assertEqual([], [n for n in LG.foreign_names("fifa", "can") if M.fold(n) == M.fold("Canada")])
        self.assertTrue(hit("fifa", "can", "Soccer: Canada vs. Mexico").matched)
        self.assertTrue(hit("iihf", "can", "IIHF World Championship: Canada vs. Latvia").matched)

    def test_a_planned_game(self):
        g = [airing("c1", datetime(2026, 12, 26, 18, 0), 180, "Live: IIHF World Junior Championship",
                    "Canada vs. Czechia. From the Scotiabank Arena."),
             airing("c2", datetime(2026, 12, 26, 18, 0), 30, "Canada Tonight", category="News")]
        self.assertEqual(["c1"], recorded(P.Rule(id=1, name="Canada", kind="team", league="iihf", team="can"), *g))
        self.assertEqual([], recorded(P.Rule(id=2, name="Canada", kind="team", league="fifa", team="can"), *g))


class LeaveOutWordsOnTeams(unittest.TestCase):
    def test_a_team_rules_leave_out_words_count(self):
        g = [airing("c1", datetime(2026, 10, 24, 23, 0), 180, "Live: NHL Hockey: Maple Leafs at Canadiens"),
             airing("c2", datetime(2026, 10, 25, 23, 0), 180, "NHL Hockey: Maple Leafs at Bruins (Alumni Game)")]
        r = P.Rule(id=1, name="Leafs", kind="team", league="nhl", team="tor", excludes=["Alumni"])
        self.assertEqual(["c1"], recorded(r, *g))


if __name__ == "__main__":
    unittest.main()

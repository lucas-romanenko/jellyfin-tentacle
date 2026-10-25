"""Team logos on the recordings' matchup cards (services/recording_art/logos.py):
the sources in order (ESPN, EuroLeague, TheSportsDB, logo.dev only with
its key), the cache (no fetch while drawing, misses asked again a day
later, "too many requests" stops the pass), the cards drawn with them,
and the pass drawing Tentacle's older matchup cards again with logos, never
a picture a person changed.

Run from the tentacle/ directory:  python tests/hermetic.py test_recording_logos
"""
import io
import json
import os
import time
import unittest
from datetime import timedelta
from unittest import mock

from tmp_dirs import temp_dir
from services.recording_art import cards as C
from services.recording_art import job as J
from services.recording_art import logos as L
from services.recording_art import what as W
from services.recording_rules import leagues as LG
from test_recording_art import NOW, Base, _size

GAME = "Canadiens at Blackhawks"


def _png(color=(200, 16, 46), size=(500, 500), alpha=True) -> bytes:
    """A logo: a coloured disc on transparency."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", size, (0, 0, 0, 0) if alpha else (255, 255, 255, 255))
    ImageDraw.Draw(img).ellipse([size[0] // 8, size[1] // 8, size[0] * 7 // 8, size[1] * 7 // 8], fill=color + (255,))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _espn(*teams) -> dict:
    return {"sports": [{"leagues": [{"teams": [{"team": {
        "abbreviation": abbr, "displayName": name, "shortDisplayName": name.split()[-1], "name": name.split()[-1],
        "location": " ".join(name.split()[:-1]),
        "logos": [{"href": f"https://espn.test/500/{abbr}.png", "rel": ["full", "default"]},
                  {"href": f"https://espn.test/500-dark/{abbr}.png", "rel": ["full", "dark"]},
                  {"href": f"https://espn.test/500-dark/scoreboard/{abbr}.png", "rel": ["full", "scoreboard", "dark"]}]}}
        for abbr, name in teams]}]}]}


class FakeWeb:
    """The logo sources: a dict of address prefix → (status, body)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        for prefix, (status, body) in self.routes.items():
            if url.startswith(prefix):
                r = mock.Mock(status_code=status)
                r.content = body if isinstance(body, bytes) else json.dumps(body).encode()
                r.json = lambda body=body: body
                r.raise_for_status = (lambda: None) if status < 400 else mock.Mock(side_effect=Exception(str(status)))
                return r
        r = mock.Mock(status_code=404, content=b"", json=lambda: {})
        r.raise_for_status = mock.Mock(side_effect=Exception("404"))
        return r


NHL_WEB = {
    "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams": (200, _espn(
        ("MTL", "Montreal Canadiens"), ("CHI", "Chicago Blackhawks"), ("NYR", "New York Rangers"),
        ("LA", "Los Angeles Kings"))),
    "https://espn.test/500-dark/MTL.png": (200, _png((175, 30, 45))),
    "https://espn.test/500-dark/CHI.png": (200, _png((207, 10, 44))),
    "https://espn.test/500-dark/LA.png": (200, _png((200, 200, 200))),
}


class Cached(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"DATA_DIR": temp_dir(self)})
        env.start()
        self.addCleanup(env.stop)
        L._mem.clear()
        self.game = W.identify(GAME)
        self.nhl = self.game.league


class Sources(Cached):
    def test_espn_by_full_name_the_dark_logo(self):
        web = FakeWeb(NHL_WEB)
        mtl = self.game.teams[0]
        self.assertEqual("espn", L.fetch_one(self.nhl, mtl, session=web))
        self.assertIn("https://espn.test/500-dark/MTL.png", web.calls)
        self.assertNotIn("https://espn.test/500/MTL.png", web.calls)
        img = L.logo(self.nhl, mtl)
        self.assertEqual("RGBA", img.mode)
        self.assertLessEqual(max(img.size), L.KEEP_SIDE)
        # trimmed to the logo: the transparent margin is gone
        self.assertEqual((0, 0) + img.size, img.getchannel("A").getbbox())

    def test_code_differs_from_espns(self):
        # leagues.json says LAK, ESPN LA: the name finds it
        kings = next(t for t in self.nhl["teams"] if t["name"] == "Los Angeles Kings")
        self.assertEqual("LAK", kings["abbr"])
        self.assertEqual("espn", L.fetch_one(self.nhl, kings, session=FakeWeb(NHL_WEB)))

    def test_the_team_list_is_fetched_once(self):
        web = FakeWeb(NHL_WEB)
        for t in self.game.teams:
            L.fetch_one(self.nhl, t, session=web)
        self.assertEqual(1, sum("site.api.espn.com" in u for u in web.calls))

    def test_match_never_takes_a_shared_short_name_first(self):
        listed = [{"abbr": "NYR", "names": ["New York Rangers", "Rangers"], "logo": "nyr"},
                  {"abbr": "TEX", "names": ["Texas Rangers", "Rangers"], "logo": "tex"}]
        self.assertEqual("tex", L.match({"name": "Texas Rangers", "short": "Rangers", "abbr": "XXX"}, listed))
        self.assertIsNone(L.match({"name": "Glasgow Rangers", "abbr": "RFC"}, listed))
        # a longer name with the year: one starts with the other
        listed = [{"abbr": "HDH", "names": ["1. FC Heidenheim 1846"], "logo": "hdh"}]
        self.assertEqual("hdh", L.match({"name": "1. FC Heidenheim", "abbr": "FCH"}, listed))

    def test_national_teams_get_their_flag(self):
        fifa = LG.league("fifa")
        can = next(t for t in fifa["teams"] if t["abbr"] == "CAN")
        web = FakeWeb({"https://a.espncdn.com/i/teamlogos/countries/500/can.png": (200, _png((255, 0, 0)))})
        self.assertEqual("espn", L.fetch_one(fifa, can, session=web))
        self.assertEqual(["https://a.espncdn.com/i/teamlogos/countries/500/can.png"], web.calls)

    def test_euroleague_crests_get_a_plate(self):
        el = LG.league("euroleague")
        rma = next(t for t in el["teams"] if t["name"] == "Real Madrid")
        web = FakeWeb({
            "https://api-live.euroleague.net/": (200, {"data": [
                {"tvCode": "RMB", "name": "Real Madrid", "abbreviatedName": "Real Madrid",
                 "images": {"crest": "https://crest.test/rma.png"}}]}),
            "https://crest.test/rma.png": (200, _png((250, 250, 250)))})
        self.assertEqual("euroleague", L.fetch_one(el, rma, session=web))
        self.assertTrue(L.logo(el, rma).info["plate"])

    def test_a_dark_logo_gets_a_plate_a_bright_one_none(self):
        from PIL import Image
        dark = Image.open(io.BytesIO(_png((25, 25, 25)))).convert("RGBA")
        bright = Image.open(io.BytesIO(_png((200, 16, 46)))).convert("RGBA")
        self.assertTrue(L.needs_plate(dark, "espn"))
        self.assertFalse(L.needs_plate(bright, "espn"))

    def test_sportsdb_when_espn_doesnt_list_it(self):
        cfl = LG.league("cfl")
        ham = next(t for t in cfl["teams"] if t["abbr"] == "HAM")
        web = FakeWeb({
            # "Hamilton Tiger-Cats" finds nothing there; "Hamilton Tiger Cats" does
            "https://www.thesportsdb.com/api/v1/json/123/searchteams.php?t=Hamilton_Tiger_Cats": (200, {"teams": [
                {"strTeam": "Hamilton Tiger-Cats", "strSport": "Soccer", "strLeague": "x",
                 "strBadge": "https://badge.test/wrong.png"},
                {"strTeam": "Hamilton Tiger-Cats", "strSport": "American Football", "strLeague": "CFL",
                 "strBadge": "https://badge.test/ham.png"}]}),
            "https://www.thesportsdb.com/": (200, {"teams": None}),
            "https://badge.test/ham.png": (200, _png((255, 184, 28)))})
        self.assertEqual("sportsdb", L.fetch_one(cfl, ham, session=web))
        self.assertNotIn("https://badge.test/wrong.png", web.calls)

    def test_logo_dev_only_with_its_key_and_the_key_is_never_kept(self):
        mtl = self.game.teams[0]
        web = FakeWeb({"https://img.logo.dev/name/": (200, _png())})
        self.assertIsNone(L.fetch_one(self.nhl, mtl, session=web))
        self.assertFalse(any("logo.dev" in u for u in web.calls))
        L.MISS_RETRY, old = 0, L.MISS_RETRY
        self.addCleanup(setattr, L, "MISS_RETRY", old)
        self.assertEqual("logo.dev", L.fetch_one(self.nhl, mtl, logodev_token="pk_secret", session=web))
        with open(L._index_path()) as f:
            self.assertNotIn("pk_secret", f.read())
        # a failing logo.dev says so without its address (the key is in it)
        web = FakeWeb({})
        L.fetch_one(self.nhl, mtl, logodev_token="pk_secret", session=web)
        self.assertNotIn("pk_secret", json.dumps(L.index()))

    def test_not_a_logo(self):
        self.assertIsNone(L._clean(b"<html>not found</html>"))
        self.assertIsNone(L._clean(_png(size=(12, 12))))
        from PIL import Image
        flat = io.BytesIO()
        Image.new("RGB", (300, 300), (240, 240, 240)).save(flat, "PNG")
        self.assertIsNone(L._clean(flat.getvalue()))           # a placeholder square
        self.assertIsNone(L._clean(b"x" * (L.MAX_BYTES + 1)))


class Budget(Cached):
    def test_a_miss_is_asked_again_a_day_later(self):
        mtl = self.game.teams[0]
        web = FakeWeb({})
        self.assertEqual(0, L.fetch_missing(self.nhl, [mtl], [8], session=web))
        self.assertEqual([], L.missing(self.nhl, [mtl]))
        idx = L.index()
        idx[f"nhl-{mtl['id']}"]["at"] = time.time() - L.MISS_RETRY - 1
        L._write_json(L._index_path(), idx)
        self.assertEqual([mtl], L.missing(self.nhl, [mtl]))

    def test_too_many_requests_stops_the_pass_without_a_miss(self):
        cfl = LG.league("cfl")
        web = FakeWeb({"https://www.thesportsdb.com/": (429, {})})
        budget = [8]
        self.assertEqual(0, L.fetch_missing(cfl, cfl["teams"][:3], budget, session=web))
        self.assertEqual(0, budget[0])
        self.assertEqual(1, len(web.calls))
        self.assertEqual(3, len(L.missing(cfl, cfl["teams"][:3])))    # asked again next pass

    def test_drawing_never_fetches(self):
        with mock.patch("requests.get", side_effect=AssertionError("fetched")):
            self.assertIsNone(L.logo(self.nhl, self.game.teams[0]))
            C.matchup_card(self.nhl, self.game.teams, True, NOW, "TSN 2")


class Cards(Cached):
    def logos(self):
        web = FakeWeb(NHL_WEB)
        for t in self.game.teams:
            L.fetch_one(self.nhl, t, session=web)
        return [L.logo(self.nhl, t) for t in self.game.teams]

    def test_both_logos_on_both_shapes(self):
        from PIL import Image
        logos = self.logos()
        for draw, size, spots in ((C.matchup_card, (1280, 720), [(330, 290), (950, 290)]),
                                  (C.matchup_poster, (800, 1200), [(400, 340), (400, 780)])):
            data = draw(self.nhl, self.game.teams, True, NOW, "TSN 2", logos=logos)
            img = Image.open(io.BytesIO(data)).convert("RGB")
            self.assertEqual(size, img.size)
            for spot in spots:          # the logos' red where the badges were blue-grey
                r, g, b = img.getpixel(spot)
                self.assertGreater(r, 140, (draw.__name__, spot))
                self.assertLess(g, 80, (draw.__name__, spot))

    def test_one_logo_missing_draws_neither(self):
        logos = self.logos()
        plain = C.matchup_card(self.nhl, self.game.teams, True, NOW, "TSN 2")
        self.assertEqual(plain, C.matchup_card(self.nhl, self.game.teams, True, NOW, "TSN 2", logos=[logos[0], None]))

    def test_logos_keep_out_of_the_safe_area_and_the_footer(self):
        # long two-line names, a plate, both shapes: nothing below the footer's line but the footer
        from PIL import Image
        logos = self.logos()
        for p in logos:
            p.info["plate"] = True
        teams = [dict(t, name=t["name"] + " Hockey Club") for t in self.game.teams]
        wide_rule = C.footer_box()[0]
        for data, rule, safe in ((C.matchup_card(self.nhl, teams, True, NOW, "TSN 2", logos=logos), wide_rule,
                                  C.WIDE_SAFE_Y),
                                 (C.matchup_poster(self.nhl, teams, True, NOW, "TSN 2", logos=logos),
                                  C.poster_footer_box()["rule"], C.SAFE_Y)):
            img = Image.open(io.BytesIO(data)).convert("L")
            w, h = img.size
            px = img.load()
            for y in list(range(rule - 8, rule - 1)) + list(range(safe, h)):
                row = [px[x, y] for x in range(0, w, 4)]
                self.assertLess(max(row) - min(row), 24, f"something drawn at y={y} of {w}x{h}")

    def test_a_long_league_name_on_the_poster(self):
        fifa = LG.league("fifa")
        logos = self.logos()
        teams = fifa["teams"][:2]
        self.assertEqual((800, 1200), _size(C.matchup_poster(fifa, teams, False, NOW, "TSN 1", logos=logos)))


class Pass(Base):
    def setUp(self):
        super().setUp()
        L._mem.clear()
        self.game = W.identify(GAME)
        self.nhl = self.game.league

    def fetch(self):
        web = FakeWeb(NHL_WEB)
        for t in self.game.teams:
            L.fetch_one(self.nhl, t, session=web)

    def test_a_new_game_gets_its_logos(self):
        self.fetch()
        self.jf.add("r1", GAME, NOW - timedelta(hours=4), minutes=180)
        self.run_pass()
        row = self.row("r1")
        self.assertEqual(("set", "matchup", 1), (row.state, row.source, row.team_logos))
        self.assertEqual(1, J.status(self.db)["with_logos"])

    def test_the_pass_fetches_only_when_asked(self):
        self.jf.add("r1", GAME, NOW - timedelta(hours=4), minutes=180)
        with mock.patch.object(L, "fetch_missing") as fetch:
            self.run_pass()
        fetch.assert_not_called()
        self.assertIsNone(self.row("r1").team_logos)

    def test_the_pass_fetches_within_its_budget(self):
        self.jf.add("r1", GAME, NOW - timedelta(hours=4), minutes=180)
        web = FakeWeb(NHL_WEB)
        with mock.patch.object(L, "requests", web):
            self.run_pass(fetch_team_logos=True)
        self.assertEqual(1, self.row("r1").team_logos)

    def test_an_older_card_is_drawn_again_with_logos_once(self):
        self.jf.add("r1", GAME, NOW - timedelta(hours=4), minutes=180)
        self.run_pass()
        row = self.row("r1")
        self.assertIsNone(row.team_logos)
        before = (self.jf.image("r1", "Primary"), self.jf.image("r1", "Thumb"))
        self.jf.writes.clear()
        self.assertEqual(0, self.run_pass()["logos"])        # no logos yet: nothing
        self.assertEqual([], self.jf.writes)
        self.fetch()
        self.assertEqual(1, self.run_pass()["logos"])
        self.assertEqual({("upload", "r1", "Primary"), ("upload", "r1", "Thumb")}, set(self.jf.writes))
        row = self.row("r1")
        self.assertEqual(("set", 1, J.POSTER), (row.state, row.team_logos, row.primary_shape))
        self.assertEqual((self.jf.tags("r1")["Primary"], self.jf.tags("r1")["Thumb"]),
                         (row.primary_tag, row.thumb_tag))
        self.assertNotEqual(before, (self.jf.image("r1", "Primary"), self.jf.image("r1", "Thumb")))
        self.assertEqual((800, 1200), _size(self.jf.image("r1", "Primary")))
        self.assertEqual((1280, 720), _size(self.jf.image("r1", "Thumb")))
        self.jf.writes.clear()
        self.assertEqual(0, self.run_pass()["logos"])        # once
        self.assertEqual([], self.jf.writes)

    def test_a_card_a_person_changed_is_never_drawn_again(self):
        self.jf.add("r1", GAME, NOW - timedelta(hours=4), minutes=180)
        self.run_pass()
        self.jf.put("r1", "Primary", "person")
        self.fetch()
        self.jf.writes.clear()
        self.assertEqual(0, self.run_pass()["logos"])
        self.assertEqual([], self.jf.writes)
        self.assertEqual("left", self.row("r1").state)

    def test_only_matchup_cards(self):
        self.jf.add("r1", "Jeopardy!", NOW - timedelta(hours=4), minutes=30)
        self.run_pass()
        self.fetch()
        self.jf.writes.clear()
        self.assertEqual(0, self.run_pass()["logos"])
        self.assertEqual([], self.jf.writes)


if __name__ == "__main__":
    unittest.main()

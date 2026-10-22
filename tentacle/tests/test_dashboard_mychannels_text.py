"""My Channels and Live TV → Channels / People: channel and person names show
exactly as they came (provider names are text, never markup), and the page's
rules: everyone has the same channels and stars favourites (quick picks star
or unstar a type's short list), event slots with nothing on hidden, channels
that don't work at the bottom.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import re
import unittest

from dashboard_js import HAVE_NODE, JS, functions, missing, missing_text, node, render

VALUES = ["O'Brien & \"Sons\" <3", "Tom &#39; Jerry &amp; co", "Ocean’s “Eleven”",
          "Šėšta `tick` ${x} </div>", "a\\\\b\\"]
ESC = functions("app.js", ["escHtml"])

ROW_FNS = ["_mychByKey", "_mychIsAdmin", "_mychChoosable", "_mychFavs", "_mychLogo", "_mychStar",
           "_mychMatches", "_mychAgo", "_mychWhenEvent", "_mychVia", "_mychWorks", "_mychGuide", "_mychMeta",
           "_mychRow", "_mychPicks", "mychRenderPicks", "mychRenderFavs", "mychRenderAll"]


def _top(names):
    """Top-level `const NAME = ...;` statements of mychannels.js."""
    src = (JS / "mychannels.js").read_text(encoding="utf-8")
    out = []
    for name in names:
        m = re.search(r"^const %s = [\s\S]*?;\n(?=\S|\n)" % re.escape(name), src, re.M)
        if not m:
            raise AssertionError(f"const {name} not found in mychannels.js")
        out.append(m.group(0))
    return "".join(out)


STATE = _top(["mych", "MYCH_PAGE", "MYCH_TYPE_ORDER", "MYCH_TYPE_LABEL"])


def _chan(i, name, **kw):
    c = {"key": f"c{i}", "name": name, "logo_url": None, "number": i + 1, "favourite": i % 2 == 0, "types": ["Entertainment"],
         "has_guide": True, "event": None, "variants": 1, "variant_name": name,
         "check": {"state": "works", "at": "2026-10-21T10:00:00Z"}}
    c.update(kw)
    return c


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class MyChannelsNames(unittest.TestCase):
    def test_channel_names_in_both_lists(self):
        src = ESC + STATE + functions("mychannels.js", ROW_FNS)
        chans = [_chan(i, v) for i, v in enumerate(VALUES)]
        call = ("mych.data = %s; mychRenderFavs(); mychRenderAll(); mychRenderPicks()"
                % json.dumps({"channels": chans, "total": len(chans)}))
        html = render(src, "var state = {currentUser: {is_admin: false}};", call)
        favs = [v for i, v in enumerate(VALUES) if i % 2 == 0]
        self.assertEqual([], missing_text(favs, html["mych-fav-list"]))
        self.assertEqual([VALUES[1], VALUES[3]], missing_text(VALUES, html["mych-fav-list"]))
        self.assertEqual([], missing_text(VALUES, html["mych-all-list"]))
        # Every row has a star (on for favourites), and the lineup's number;
        # nothing adds or removes channels.
        allh = html["mych-all-list"][-1]
        self.assertEqual(len(VALUES), allh.count('data-act="star"'))
        self.assertEqual(len(favs), allh.count('aria-pressed="true"'))
        self.assertEqual(len(favs), html["mych-fav-list"][-1].count('aria-pressed="true"'))
        self.assertNotIn('data-act="add"', allh)
        self.assertNotIn('data-act="remove"', allh)
        self.assertIn('<span class="mych-num">5</span>', allh)

    def test_event_titles_and_variant_names(self):
        src = ESC + STATE + functions("mychannels.js", ROW_FNS)
        chans = [_chan(1, "NBC", variants=16, variant_name=VALUES[0]),
                 _chan(2, "PPV 1", types=["Sports", "Event/PPV"], has_guide=False,
                       event={"title": VALUES[1], "start": "2026-10-21T23:00:00Z", "stop": None})]
        data = json.dumps({"channels": chans, "total": 2})
        people = render(src, "var state = {currentUser: {is_admin: false}};",
                        "mych.data = %s; mychRenderAll()" % data)
        admins = render(src, "var state = {currentUser: {is_admin: true}};",
                        "mych.data = %s; mychRenderAll()" % data)
        self.assertEqual([], missing_text([VALUES[1]], people["mych-all-list"]))
        # Where a network streams from is the admin's business: people see "NBC".
        self.assertEqual([VALUES[0]], missing_text([VALUES[0]], people["mych-all-list"]))
        self.assertEqual([], missing_text([VALUES[0]], admins["mych-all-list"]))

    def test_people(self):
        src = ESC + functions("mychannels.js", ["loadLivePeople"])
        people = {"total": 30, "kids_channels": 4, "setup": {},
                  "people": [{"name": v, "kids": i == 1, "channels": 4 if i == 1 else 30,
                              "favourites": None if i == 2 else i} for i, v in enumerate(VALUES)]}
        html = render(src, "API['/api/live/people'] = %s;" % json.dumps(people), "await loadLivePeople()")
        self.assertEqual([], missing_text(VALUES, html["live-people"]))
        out = html["live-people"][-1]
        self.assertIn("0 favourites", out)
        self.assertIn("1 favourite<", out)
        self.assertIn("Kids channels only (4)", out)
        self.assertIn("Not known yet", out)
        self.assertNotIn("tuner", out.lower())


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class MyChannelsRules(unittest.TestCase):
    def _run(self, chans, call):
        src = (STATE + functions("mychannels.js", ["_mychByKey", "_mychChoosable", "_mychFavs", "_mychPicks",
                                                   "_mychPickChange"]))
        return node(src + "mych.data = %s; %s" % (json.dumps({"channels": chans}), call))

    def test_choosable_hides_empty_event_slots_and_sinks_broken(self):
        chans = [_chan(1, "A", check={"state": "broken", "at": None}), _chan(2, "B", event="no_event"),
                 _chan(3, "C"), _chan(4, "D", check={"state": "unchecked", "at": None})]
        out = self._run(chans, "console.log(JSON.stringify(_mychChoosable().map(c => c.key)))")
        self.assertEqual(["c3", "c4", "c1"], out)

    def test_favourites_in_lineup_order(self):
        chans = [_chan(i, f"C{i}", favourite=i in (5, 2, 7)) for i in range(1, 9)]
        out = self._run(chans, "console.log(JSON.stringify(_mychFavs().map(c => c.key)))")
        self.assertEqual(["c2", "c5", "c7"], out)

    def test_quick_pick_stars_the_short_list_then_unstars_it(self):
        chans = [_chan(1, "TSN", types=["Sports"], main_types=["Sports"], favourite=False),
                 _chan(2, "CNN", types=["News"], main_types=["News"], favourite=True),
                 _chan(3, "Sportsnet", types=["Sports"], main_types=["Sports"], favourite=True),
                 _chan(4, "Obscure Sports 7", types=["Sports"], main_types=[], favourite=False),
                 _chan(5, "Fuse", types=["Movies", "Music"], main_types=["Movies", "Music"], favourite=False),
                 _chan(6, "DAZN", types=["Sports"], main_types=["Sports"], favourite=False)]
        star = "const star = c => { for (const k of c.keys) _mychByKey()[k].favourite = c.favourite; return c; };"
        out = self._run(chans, star + "const a = star(_mychPickChange('Sports')); const b = star(_mychPickChange('Sports'));"
                        "star(_mychPickChange('Movies')); const c = _mychPickChange('Music');"
                        "console.log(JSON.stringify([a, b, c]))")
        # Off: stars only the short list's unstarred channels, never the rest of the type.
        self.assertEqual({"favourite": True, "keys": ["c1", "c6"]}, out[0])
        # On: tapping again unstars the pick's channels.
        self.assertEqual({"favourite": False, "keys": ["c1", "c3", "c6"]}, out[1])
        # Music is on through Movies: unstarring Music keeps Fuse, which Movies (on) has too.
        self.assertEqual({"favourite": False, "keys": []}, out[2])

    def test_pick_counts_and_state(self):
        chans = [_chan(i, f"S{i}", types=["Sports"], main_types=["Sports"], favourite=i == 1) for i in range(1, 5)]
        out = self._run(chans, "const a = _mychPicks()[0]; _mychByKey().c2.favourite = true;"
                               "const b = _mychPicks()[0]; console.log(JSON.stringify([[a.on, a.starred, a.channels.length], [b.on, b.starred]]))")
        self.assertEqual([[False, 1, 4], [True, 2]], out)

    def test_types_without_a_short_list_have_no_pick(self):
        chans = [_chan(1, "Sky News", types=["News", "International"], main_types=[]),
                 _chan(2, "CNN", types=["News"], main_types=["News"])]
        out = self._run(chans, "console.log(JSON.stringify(_mychPicks().map(p => [p.type, p.channels.length])))")
        self.assertEqual([["News", 1]], out)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class PoolNames(unittest.TestCase):
    def test_pool_rows_and_variants(self):
        src = ESC + STATE + _top(["pool"]) + functions("mychannels.js", [
            "_mychLogo", "_mychAgo", "_mychWorks", "_mychGuide", "_mychMeta", "_poolNet", "_poolFiltered",
            "_poolVariantLabel", "_poolVariantSelect", "_poolTypes", "_poolRow", "_poolNameLogo", "_poolLoadLogos", "_poolDetail",
            "_poolMergeBtn",
            "poolRender"])
        variants = [{"channel_id": 10 + i, "name": v, "provider_name": v, "enabled": True, "chosen": i == 0,
                     "has_guide": True, "check": {"state": "works", "at": None}} for i, v in enumerate(VALUES)]
        entries = [{"key": "n1", "name": VALUES[0], "logo_url": None, "types": ["Local"], "has_guide": True,
                    "event": None, "variants": 5, "variant_name": VALUES[1], "check": {"state": "works", "at": None}}] + [
            {"key": f"c{i}", "name": v, "logo_url": None, "types": ["News"], "has_guide": False, "event": None,
             "variants": 1, "variant_name": v} for i, v in enumerate(VALUES[1:], 2)]
        setup = ("pool.entries = %s; pool.nets = {1: {id: 1, key: 'n1', name: %s, count: 5, variants: %s}};"
                 "pool.types = ['News', 'Local']; pool.open = 'n1';"
                 % (json.dumps(entries), json.dumps(VALUES[0]), json.dumps(variants)))
        html = render(src, setup, "poolRender()")
        self.assertEqual([], missing_text(VALUES, html["pool-list"]))
        self.assertEqual([], missing([f"{v} · Guide ✓ · ✓ Works" for v in VALUES], html["pool-list"]))


if __name__ == "__main__":
    unittest.main()

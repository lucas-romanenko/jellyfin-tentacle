"""My Channels and Live TV → People: channel and person names show exactly
as they came (provider names are text, never markup).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import re
import unittest

from dashboard_js import HAVE_NODE, JS, functions, missing_text, render

VALUES = ["O'Brien & \"Sons\" <3", "Tom &#39; Jerry &amp; co", "Ocean’s “Eleven”",
          "Šėšta `tick` ${x} </div>", "a\\\\b\\"]
ESC = functions("app.js", ["escHtml"])


def _state():
    src = (JS / "mychannels.js").read_text(encoding="utf-8")
    return re.search(r"^const mych = [\s\S]*?\n};\n", src, re.M).group(0)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class MyChannelsNames(unittest.TestCase):
    def test_channel_names_in_every_list(self):
        src = ESC + _state() + functions("mychannels.js", [
            "_mychByKey", "_mychLogo", "_mychStar", "_mychMatches", "mychRenderAll", "mychRenderOwn", "mychRenderAdd"])
        chans = [{"key": f"c{i}", "name": v, "logo_url": None, "favourite": i % 2 == 0} for i, v in enumerate(VALUES)]
        call = ("mych.data = %s; mych.list = %s; mychRenderAll(); mychRenderOwn(); mychRenderAdd()"
                % (json.dumps({"channels": chans, "total": len(chans)}), json.dumps(["c0", "c1", "c2"])))
        html = render(src, "", call)
        self.assertEqual([], missing_text(VALUES, html["mych-all-list"]))
        self.assertEqual([], missing_text(VALUES[:3], html["mych-own-list"]))
        self.assertEqual([], missing_text(VALUES, html["mych-add-list"]))

    def test_people(self):
        src = ESC + functions("mychannels.js", ["_mychWhen", "loadLivePeople"])
        people = {"total": 3, "people": [{"name": v, "own_list": i % 2 == 1, "count": 2, "changed": None}
                                         for i, v in enumerate(VALUES)], "setup": {}}
        html = render(src, "API['/api/live/people'] = %s;" % json.dumps(people), "await loadLivePeople()")
        self.assertEqual([], missing_text(VALUES, html["live-people"]))


if __name__ == "__main__":
    unittest.main()

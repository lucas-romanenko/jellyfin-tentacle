"""#521: every <category> of a programme reaches Jellyfin.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Jellyfin keeps every category as a genre and sets IsSports/IsNews/IsKids/
IsMovie when ANY of them is in its lists. Only the first one was stored and
served, so "Hockey" + "Sports" was never flagged as sports. Every non-empty
category is kept in feed order (an exact repeat once), one <category> each.
"""
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime

from services import xmltv


def _feed(*categories):
    cats = "".join(f'<category lang="en">{c}</category>' for c in categories)
    return ('<tv><programme start="20261101230000 +0000" stop="20261102020000 +0000" channel="a">'
            f'<title>Maple Leafs at Canadiens</title>{cats}</programme></tv>')


def _served(programs):
    guide = ET.fromstring(xmltv.generate_xmltv([{"id": "a", "name": "A"}], programs))
    return [c.text for c in guide.find("programme").findall("category")]


def _round_trip(*categories):
    _channels, programs = xmltv.parse_xmltv(_feed(*categories))
    return _served(programs)


class EveryCategoryIsServed(unittest.TestCase):
    def test_hockey_and_sports_both_reach_jellyfin(self):
        self.assertEqual(["Hockey", "Sports"], _round_trip("Hockey", "Sports"))

    def test_feed_order_is_kept(self):
        self.assertEqual(["Talk", "News", "Politics"], _round_trip("Talk", "News", "Politics"))

    def test_an_exact_repeat_is_served_once(self):
        self.assertEqual(["Sports", "Hockey"], _round_trip("Sports", "Hockey", "Sports"))

    def test_an_empty_category_is_skipped(self):
        self.assertEqual(["Sports"], _round_trip("", "Sports"))

    def test_one_category_is_stored_and_served_as_before(self):
        _channels, programs = xmltv.parse_xmltv(_feed("Sports"))
        self.assertEqual("Sports", programs[0]["category"])
        self.assertEqual(["Sports"], _served(programs))

    def test_no_category_is_none_so_the_inference_still_runs(self):
        _channels, programs = xmltv.parse_xmltv(_feed())
        self.assertIsNone(programs[0]["category"])
        self.assertEqual([], _served(programs))

    def test_a_category_with_a_comma_or_newline_stays_one(self):
        self.assertEqual(["Arts, Culture", "Drama\nSeries"], _round_trip("Arts, Culture", "Drama\nSeries"))

    def test_a_row_stored_by_an_older_build_serves_as_one(self):
        prog = {"channel_id": "a", "title": "T", "start": datetime(2026, 11, 1, 23),
                "stop": datetime(2026, 11, 2, 2), "category": "Sports"}
        self.assertEqual(["Sports"], _served([prog]))


if __name__ == "__main__":
    unittest.main()

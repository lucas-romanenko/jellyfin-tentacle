"""#141: a feed id whose display names are two different channels is never
taken by name; one channel's name variants are still one channel.

Run from the tentacle/ directory:  python -m unittest discover -s tests

From a real feed: LRTPlus.lt lists both "LV: LRT PLUS" and "LV: LRT", so
"LT: LRT HD" took LRT Plus's guide. Timeshifts ("+1"), "(BK)", spacing, a
brand prefix ("DISCOVERY HISTORY" / "HISTORY") and "Channel" do not make an
id two channels; "GOLD +1" next to "GOLD" leaves the plain id as the channel.
"""
import unittest

from services.epg_match import resolve_guide_ids

FEED = [
    {"id": "LRTPlus.lt", "names": ["LV: LRT PLUS", "LV: LRT"]},
    {"id": "ITV1.uk", "names": ["ITV 1", "ITV 1 +1", "ITV1 (BK)"]},
    {"id": "Gold.uk", "names": ["GOLD"]},
    {"id": "GoldPlus1.uk", "names": ["GOLD +1", "GOLD"]},
    {"id": "SportsnetWest.ca", "names": ["SPORTSN ET WEST", "Sportsnet West"]},
    {"id": "History4K.uk", "names": ["DISCOVERY HISTORY", "HISTORY"]},
    {"id": "Disney.ca", "names": ["Disney Channel", "Disney"]},
    {"id": "ITV4.uk", "names": ["ITV 4", "ITV4 +1", "4"]},
]


def _resolve(name):
    return resolve_guide_ids([{"id": 1, "name": name}], FEED)[1]


class MixedIds(unittest.TestCase):
    def test_an_id_with_two_channels_names_is_not_taken(self):
        r = _resolve("LT: LRT HD")
        self.assertIsNone(r["guide_id"])
        self.assertEqual("ambiguous", r["reason"])

    def test_one_channels_variants_are_one_channel(self):
        for name, gid in (("UK: ITV 1", "ITV1.uk"), ("CA: Sportsnet West", "SportsnetWest.ca"),
                          ("UK: HISTORY 4K", "History4K.uk"), ("CA: Disney Channel", "Disney.ca"),
                          ("UK: ITV 4", "ITV4.uk")):
            self.assertEqual(gid, _resolve(name)["guide_id"], name)

    def test_the_plain_id_beats_the_one_with_a_timeshift(self):
        self.assertEqual("Gold.uk", _resolve("UK: GOLD")["guide_id"])


if __name__ == "__main__":
    unittest.main()


class EmptyFeedIds(unittest.TestCase):
    """A real feed has <channel id=""> elements. The empty id must never be a
    candidate: "UK: ITV 1" was matched to "" because that element named only
    "ITV 1" and the real id also lists "ITV 1 +1" (the plain-vs-timeshift
    preference then picked the empty one)."""

    FEED = [
        {"id": "", "names": ["ITV 1"]},
        {"id": "ITV1.uk", "names": ["ITV 1", "ITV 1 +1"]},
        {"id": "", "names": ["GOLD"]},
    ]

    def test_an_empty_feed_id_is_never_matched(self):
        r = resolve_guide_ids([{"id": 1, "name": "UK: ITV 1"}], self.FEED)[1]
        self.assertEqual("ITV1.uk", r["guide_id"])
        self.assertEqual("name", r["method"])

    def test_a_name_only_an_empty_id_carries_has_no_guide(self):
        r = resolve_guide_ids([{"id": 1, "name": "UK: GOLD"}], self.FEED)[1]
        self.assertIsNone(r["guide_id"])

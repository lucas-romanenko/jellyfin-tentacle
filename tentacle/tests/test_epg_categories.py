"""Tests for XMLTV category inference.

Xtream providers commonly send no <category>, so Jellyfin never set
IsSports/IsNews/IsKids/IsMovie: no sports badge in the guide, empty genre
filters, and sports DVR padding defaults that never applied.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from services.epg_categories import infer_category


class TestInferCategory(unittest.TestCase):
    def test_league_and_sport_names_in_the_title(self):
        for title in ("NHL: Maple Leafs vs Canadiens", "MLB Tonight", "UFC 300",
                      "Premier League: Arsenal v Spurs", "Cricket World Cup Final",
                      "SportsCentre", "PGA Championship Round 2"):
            self.assertEqual(infer_category(title), "Sports", title)

    def test_live_prefix_is_treated_as_sport(self):
        self.assertEqual(infer_category("Live: Raptors at Celtics"), "Sports")
        self.assertEqual(infer_category("Live - Grand Prix Qualifying"), "Sports")

    def test_channel_group_used_only_when_the_title_says_nothing(self):
        self.assertEqual(infer_category("Breaking News at 6", "CA - NEWS"), "News")
        self.assertEqual(infer_category("Paw Patrol", "EN - KIDS"), "Kids")
        self.assertEqual(infer_category("Some Film", "EN - CINEMA"), "Movie")
        # An informative title wins over the group
        self.assertEqual(infer_category("NHL Tonight", "EN - ENTERTAINMENT"), "Sports")

    def test_nothing_confident_returns_none(self):
        # A wrong guess mislabels the guide, so silence beats a bad answer.
        self.assertIsNone(infer_category("The Tonight Show"))
        self.assertIsNone(infer_category("Talk Show", "EN - ENTERTAINMENT"))
        self.assertIsNone(infer_category(None, None))
        self.assertIsNone(infer_category("", ""))

    def test_matches_are_word_anchored(self):
        # "Golf" inside another word must not trigger
        self.assertIsNone(infer_category("Golfstream Documentary Hour"))

    def test_case_insensitive(self):
        self.assertEqual(infer_category("nhl tonight"), "Sports")
        self.assertEqual(infer_category("Show", "en - sports"), "Sports")


if __name__ == "__main__":
    unittest.main()

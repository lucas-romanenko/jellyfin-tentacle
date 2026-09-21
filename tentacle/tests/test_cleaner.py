"""Tests for services.cleaner.clean_title (VOD title cleaning).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from services.cleaner import clean_title


class TestCleanTitle(unittest.TestCase):
    def test_known_prefix_stripped(self):
        self.assertEqual(clean_title("NF - Breaking Bad (2008)"), ("Breaking Bad", "2008"))
        self.assertEqual(clean_title("AMZ - The Boys (2019)"), ("The Boys", "2019"))
        self.assertEqual(clean_title("D+ - Loki"), ("Loki", None))

    def test_bracketed_prefix_stripped(self):
        self.assertEqual(clean_title("[HBO] Game of Thrones (2011)"), ("Game of Thrones", "2011"))

    def test_scene_dot_notation(self):
        self.assertEqual(clean_title("The.Matrix.1999.1080p.WEB-DL"), ("The Matrix", "1999"))

    def test_quality_tags_stripped(self):
        self.assertEqual(clean_title("Dune Part Two 2024 2160p UHD BluRay"), ("Dune Part Two", "2024"))

    def test_numbered_ranking_stripped(self):
        self.assertEqual(clean_title("250. The Shawshank Redemption (1994)"),
                         ("The Shawshank Redemption", "1994"))

    def test_plain_year_extraction(self):
        self.assertEqual(clean_title("Inception (2010)"), ("Inception", "2010"))

    def test_no_prefix_preserved(self):
        self.assertEqual(clean_title("Some Movie"), ("Some Movie", None))

    def test_unknown_acronym_not_overstripped(self):
        # Guards against over-aggressive prefix stripping: "CSI" is a real show
        # name, not a streaming-service prefix, so it must NOT be stripped.
        self.assertEqual(clean_title("CSI - Miami"), ("CSI - Miami", None))

    def test_empty_input(self):
        self.assertEqual(clean_title(""), (None, None))



class TestTrailingSceneTagsAfterYear(unittest.TestCase):
    def test_release_tags_after_the_year_are_dropped(self):
        for raw, title, year in (("Show (2023) HBO", "Show", "2023"),
                                 ("Movie (2020) PROPER", "Movie", "2020"),
                                 ("Movie (2020) NF REPACK", "Movie", "2020")):
            with self.subTest(raw=raw):
                self.assertEqual(clean_title(raw), (title, year))

    def test_a_title_word_that_looks_like_a_tag_stays(self):
        # Only tags that trail the year are release tags.
        self.assertEqual(clean_title("Real Steel (2011)"), ("Real Steel", "2011"))
        self.assertEqual(clean_title("Max (2015)"), ("Max", "2015"))


if __name__ == "__main__":
    unittest.main()

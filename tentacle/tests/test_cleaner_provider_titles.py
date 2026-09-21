"""Tests for services.cleaner.clean_title on real catalogue titles.

Provider names that carry no "EN - " style prefix must survive cleaning: the
strip list holds words that are also the first word of real titles ("Top Gun",
"New Girl", "Max Steel"), whole titles ("Max", "Cam"), and scene tags that are
ordinary English ("Dan in Real Life", "The Real Housewives of ...").

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

from services.cleaner import clean_title


class TestLeadingWordNotDropped(unittest.TestCase):
    """A prefix word is only a prefix when a separator follows it."""

    def test_first_word_kept_when_no_separator(self):
        self.assertEqual(clean_title("Top Gun"), ("Top Gun", None))
        self.assertEqual(clean_title("Top Gun: Maverick (2022)"), ("Top Gun: Maverick", "2022"))
        self.assertEqual(clean_title("New Girl (2011)"), ("New Girl", "2011"))
        self.assertEqual(clean_title("Max Steel (2016)"), ("Max Steel", "2016"))
        self.assertEqual(clean_title("New Year's Eve (2011)"), ("New Year's Eve", "2011"))
        self.assertEqual(clean_title("Top Boy (2019)"), ("Top Boy", "2019"))

    def test_title_that_is_only_a_prefix_word_survives(self):
        # "Max", "Cam" and "Stan" are in STRIP_PREFIXES; they are also films.
        self.assertEqual(clean_title("Max (2015)"), ("Max", "2015"))
        self.assertEqual(clean_title("Cam (2018)"), ("Cam", "2018"))
        self.assertEqual(clean_title("Stan the Man (2025)"), ("Stan the Man", "2025"))
        self.assertEqual(clean_title("Top of the Lake (2013)"), ("Top of the Lake", "2013"))


class TestPrefixStrippingStillWorks(unittest.TestCase):
    """Must-not-change: the documented prefix formats keep working."""

    def test_separator_prefixes(self):
        self.assertEqual(clean_title("EN - Top Gun (1986)"), ("Top Gun", "1986"))
        self.assertEqual(clean_title("NF - Breaking Bad (2008)"), ("Breaking Bad", "2008"))
        self.assertEqual(clean_title("AMZ - The Boys (2019)"), ("The Boys", "2019"))
        self.assertEqual(clean_title("D+ - Loki"), ("Loki", None))
        self.assertEqual(clean_title("NF-DO - Some Show (2021)"), ("Some Show", "2021"))
        self.assertEqual(clean_title("EN-TOP - 250. Movie Name (2019)"), ("Movie Name", "2019"))
        self.assertEqual(clean_title("[HBO] Game of Thrones (2011)"), ("Game of Thrones", "2011"))
        self.assertEqual(clean_title("4K - Avatar (2009)"), ("Avatar", "2009"))

    def test_scene_dot_notation(self):
        self.assertEqual(clean_title("The.Matrix.1999.1080p.WEB-DL"), ("The Matrix", "1999"))
        self.assertEqual(clean_title("NF.The.Matrix.1999.1080p.WEB-DL"), ("The Matrix", "1999"))
        # A title whose own first word is in the strip list, scene-style.
        self.assertEqual(clean_title("Top.Gun.1986.1080p.BluRay"), ("Top Gun", "1986"))


class TestSceneTagsThatAreEnglishWords(unittest.TestCase):
    """REAL/PROPER/NF & co. only come off scene-style names."""

    def test_real_words_kept_in_catalogue_titles(self):
        self.assertEqual(clean_title("Dan in Real Life (2007)"), ("Dan in Real Life", "2007"))
        self.assertEqual(clean_title("The Real Housewives of Beverly Hills (2010)"),
                         ("The Real Housewives of Beverly Hills", "2010"))
        self.assertEqual(clean_title("Love with the Proper Stranger (1963)"),
                         ("Love with the Proper Stranger", "1963"))
        self.assertEqual(clean_title("Daniel Isn't Real (2019)"), ("Daniel Isn't Real", "2019"))
        self.assertEqual(clean_title("Real Madrid: Until the End (2023)"),
                         ("Real Madrid: Until the End", "2023"))

    def test_scene_tags_still_stripped_from_release_names(self):
        self.assertEqual(clean_title("Movie.Name.2020.PROPER.1080p.WEB-DL"), ("Movie Name", "2020"))
        self.assertEqual(clean_title("Some Film 2019 REAL PROPER 1080p BluRay"),
                         ("Some Film", "2019"))
        self.assertEqual(clean_title("Another Film 2021 AMZN WEB-DL x264"),
                         ("Another Film", "2021"))


if __name__ == "__main__":
    unittest.main()

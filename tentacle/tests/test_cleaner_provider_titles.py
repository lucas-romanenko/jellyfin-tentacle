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


class TestRealTitlesTheValidatorRejected(unittest.TestCase):
    """Issue #452: real titles were dropped or cut down to the wrong words."""

    def test_y_counts_as_a_vowel(self):
        self.assertEqual(clean_title("Psych (2006)"), ("Psych", "2006"))
        self.assertEqual(clean_title("Flynn (2012)"), ("Flynn", "2012"))
        self.assertEqual(clean_title("Rhythm + Flow (2019)"), ("Rhythm + Flow", "2019"))
        self.assertEqual(clean_title("Psych: The Movie (2017)"), ("Psych: The Movie", "2017"))

    def test_provider_title_that_starts_lower_case(self):
        self.assertEqual(clean_title("iCarly (2007)"), ("iCarly", "2007"))
        self.assertEqual(clean_title("eXistenZ (1999)"), ("eXistenZ", "1999"))
        self.assertEqual(clean_title("mother! (2017)"), ("mother!", "2017"))
        self.assertEqual(clean_title("mid90s (2018)"), ("mid90s", "2018"))
        self.assertEqual(clean_title("EN - iCarly (2007)"), ("iCarly", "2007"))
        self.assertEqual(clean_title("[NF] mother! (2017)"), ("mother!", "2017"))

    def test_title_the_cleaner_cut_into_is_still_rejected(self):
        self.assertEqual(clean_title("12.to.Midnight.2024"), (None, None))
        self.assertEqual(clean_title("4K-iCarly (2007)"), (None, None))

    def test_bracketed_title_word_is_kept(self):
        self.assertEqual(clean_title("[REC] (2007)"), ("[REC]", "2007"))
        self.assertEqual(clean_title("[REC] 2 (2009)"), ("[REC] 2", "2009"))
        self.assertEqual(clean_title("[REC]³ Genesis"), ("[REC]³ Genesis", None))

    def test_bracketed_tags_still_stripped(self):
        self.assertEqual(clean_title("[NF] (2020)"), (None, None))
        self.assertEqual(clean_title("[HEVC] (2020)"), (None, None))
        self.assertEqual(clean_title("[HEVC] Movie Name (2020)"), ("Movie Name", "2020"))
        self.assertEqual(clean_title("(500) Days of Summer (2009)"), ("Days of Summer", "2009"))

    def test_number_after_a_colon_is_not_the_year(self):
        self.assertEqual(clean_title("Space: 1999"), ("Space: 1999", None))
        self.assertEqual(clean_title("Fear Street: 1978"), ("Fear Street: 1978", None))
        self.assertEqual(clean_title("Space: 1999 (1975)"), ("Space: 1999", "1975"))
        self.assertEqual(clean_title("Some Film 2019"), ("Some Film", "2019"))

    def test_labels_without_a_title_still_rejected(self):
        self.assertEqual(clean_title("(2011)"), (None, None))
        self.assertEqual(clean_title("EN - X"), (None, None))


if __name__ == "__main__":
    unittest.main()

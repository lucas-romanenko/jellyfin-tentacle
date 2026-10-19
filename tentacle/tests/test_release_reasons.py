"""Release-check rejections get the right label (#142).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Radarr's size rejections ("10.8 GB is larger than maximum allowed 8.8 GB (for
X)") contain none of "size / too large / too small", so they showed as "other
reasons"; and the bare "age" matched the title Radarr appends ("(for
Rampage)", "(for The Age of Adaline)"), so for those titles they showed as
"too old or too new". Four more were found against Radarr's and Sonarr v4's
source. The strings below are their real wording.
"""
import unittest

from services.arr_insight import reason_of

CASES = [
    # Size (Radarr AcceptableSizeSpecification / MaximumSizeSpecification, Sonarr alike)
    ("10.8 GB is larger than maximum allowed 8.8 GB (for The Assassination Bureau)", "size"),
    ("1,009.3 MB is smaller than minimum allowed 1.4 GB (for The Assassination Bureau)", "size"),
    ("1.8 GB is smaller than minimum allowed 5.6 GB (for Rampage)", "size"),
    ("10.8 GB is larger than maximum allowed 8.8 GB (for The Age of Adaline)", "size"),
    ("2.0 GB is smaller than minimum allowed 5.6 GB (for Mirage (2018))", "size"),
    ("30.1 GB is too big, maximum size is 20.0 GB (Settings->Indexers->Maximum Size)", "size"),
    # Delay profile, not a quality problem
    ("Waiting for better quality release", "delay"),
    ("Delay is 120 minutes", "delay"),
    # A release already on its way / already there
    ("Release in queue meets quality cutoff", "existing"),
    ("Release in queue already meets cutoff: Bluray-1080p", "existing"),
    ("Existing file on disk is of equal or higher preference: Bluray-1080p", "existing"),
    # Custom format score, even when a format is named after a language
    ("Custom Formats LQ have score -10000 below Movie's profile minimum 0", "format"),
    ("Custom Formats Language: Not English have score -10000 below Movie's profile minimum 0", "format"),
    ("Custom Formats x265 (HD) have score -10000 below Series profile minimum 0", "format"),
    # Language
    ("English is wanted, but found French", "language"),
    ("Language French is not wanted in profile", "language"),
    # Quality
    ("DVD is not wanted in profile", "quality"),
    ("Unknown is not wanted in profile", "quality"),
    # The rest
    ("Release is blocklisted", "blocklist"),
    ("Not enough seeders: 0. Minimum seeders: 1", "seeders"),
    ("Older than configured retention", "age"),
    ("Unable to parse release", "match"),
    ("Wrong movie", "match"),
]


class ReleaseReasons(unittest.TestCase):
    def test_each_rejection_gets_its_label(self):
        wrong = [(text, reason_of(text)[0], want) for text, want in CASES if reason_of(text)[0] != want]
        self.assertEqual([], wrong)

    def test_a_future_rewording_is_other_never_a_wrong_label(self):
        self.assertEqual(("other", "other reasons"), reason_of("Something Radarr says one day"))

    def test_labels_are_unchanged_for_the_apps(self):
        self.assertEqual("wrong size", reason_of(CASES[0][0])[1])
        self.assertEqual("custom format score too low", reason_of(CASES[12][0])[1])


if __name__ == "__main__":
    unittest.main()

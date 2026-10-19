"""Release-check reasons against Radarr's and Sonarr's actual rejection wording (#142).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The messages are the ones their DecisionEngine specifications produce (Radarr
develop, Sonarr v4 main), with a title, a custom format name or a term filled
in where they take one.
"""
import unittest

from services import arr_insight

TRAP_TITLES = ["Rampage", "The Age of Adaline", "Mirage", "Savage", "Super Size Me", "The Language of Love",
               "Wrong Turn", "Quality Street", "Delay of Game", "Unknown", "Existing Files", "Blocklisted",
               "Star Wars: Episode IV", "Movie (2019)"]


def key(text):
    return arr_insight.reason_of(text)[0]


class TestRealWording(unittest.TestCase):
    def test_size_rejections_are_wrong_size_whatever_the_title(self):
        for t in TRAP_TITLES:
            for msg in (f"10.8 GB is larger than maximum allowed 8.8 GB (for {t})",
                        f"1,009.3 MB is smaller than minimum allowed 1.4 GB (for {t})",
                        f"1.8 GB is smaller than minimum allowed 5.6 GB (for {t})"):
                self.assertEqual("size", key(msg), msg)

    def test_sonarr_size_rejections(self):
        for msg in ("2.1 GB is larger than maximum allowed 1.5 GB (for 45 minutes)",
                    "150.0 MB is smaller than minimum allowed 250.0 MB (for S01E01)",
                    "Runtime of all episodes is 0, unable to validate size until it is available",
                    "Movie runtime is 0, unable to validate size until it is available"):
            self.assertEqual("size", key(msg), msg)

    def test_the_issue_examples(self):
        # #142: these read "other reasons" / "too old or too new"
        self.assertEqual("wrong size", arr_insight.reason_of(
            "10.8 GB is larger than maximum allowed 8.8 GB (for The Assassination Bureau)")[1])
        self.assertEqual("wrong size", arr_insight.reason_of(
            "1.8 GB is smaller than minimum allowed 5.6 GB (for Rampage)")[1])
        self.assertEqual("wrong size", arr_insight.reason_of(
            "10.8 GB is larger than maximum allowed 8.8 GB (for The Age of Adaline)")[1])

    def test_age(self):
        for msg in ("Older than configured retention", "Only 5 minutes old, minimum age is 30 minutes"):
            self.assertEqual("age", key(msg), msg)

    def test_a_title_never_decides_the_reason(self):
        # Radarr names the movie in its availability message; not an age/size/language reason.
        for t in TRAP_TITLES:
            msg = f"Movie {t} will only be considered available 0 days after Released"
            self.assertEqual("other", key(msg), msg)

    def test_custom_format_names_dont_decide_the_reason(self):
        for names in ("LQ", "Language: Not English", "Bad Size", "x265 (HD)", "Wrong Group, Unknown Quality"):
            for app in ("Movie's", "Series"):
                msg = f"Custom Formats {names} have score -10000 below {app} profile minimum 0"
                self.assertEqual("format", key(msg), msg)

    def test_every_fixed_message(self):
        cases = {
            "delay": ["Waiting for better quality release",
                      "Release containing at least one matching movie is already pending, delaying pushed release"],
            "blocklist": ["Release is blocklisted"],
            "existing": ["Existing file on disk meets Custom Format cutoff: 100",
                         "Existing file on disk has a equal or higher Custom Format score: 50",
                         "Existing file meets cutoff: Bluray-1080p [LQ]",
                         "Existing file on disk meets quality cutoff: Bluray-1080p",
                         "Existing file on disk is of equal or higher preference: WEBDL-1080p v1",
                         "Existing file on disk and Quality Profile 'Language Size' does not allow upgrades",
                         "Existing file and the Quality profile does not allow upgrades",
                         "Release in queue meets quality cutoff: Bluray-1080p",
                         "Release in queue is of equal or higher preference: WEBDL-1080p v1",
                         "Quality for release in queue already meets cutoff: WEBDL-1080p v1",
                         "Release in queue already meets cutoff: WEBDL-1080p v1",
                         "Recent grab event in history already meets cutoff: WEBDL-1080p v1",
                         "CDH is disabled and grab event in history already meets cutoff: WEBDL-1080p v1",
                         "Movie grab event in history meets quality cutoff: Bluray-1080p",
                         "Has same torrent hash as a grabbed and imported release",
                         "Has same release name as a grabbed and imported release"],
            "language": ["English is wanted, but found French",
                         "Original Language (English) is wanted, but found German",
                         "Language German is not wanted in profile"],
            "quality": ["DVD is not wanted in profile", "Unknown is not wanted in profile",
                        "HDTV-720p is not wanted in profile"],
            "seeders": ["Not enough seeders: 0. Minimum seeders: 1"],
            "match": ["Wrong movie", "Wrong episode", "Wrong season", "Wrong series",
                      "Episode wasn't requested: 1x2", "Episode wasn't requested",
                      "Unable to determine release group for this release"],
        }
        for want, msgs in cases.items():
            for msg in msgs:
                self.assertEqual(want, key(msg), msg)

    def test_terms_and_names_after_a_colon_dont_decide(self):
        for msg in ("Does not contain one of the required terms: size, age, language",
                    "Contains these ignored terms: delay, wrong",
                    "Hardcode subs found: language"):
            self.assertEqual("other", key(msg), msg)
        self.assertEqual("other", key("Indexer Size Age Wrong is blocked till 2026-01-01 due to failures, cannot grab release."))

    def test_summary_counts_size_as_wrong_size(self):
        # The Assassination Bureau on the TV app: "9 other reasons" were these nine.
        def rel(i, rejections):
            return {"title": f"r{i}", "quality": {"quality": {"name": "WEBDL-1080p", "resolution": 1080}},
                    "size": 1, "rejected": True, "rejections": rejections, "guid": f"g{i}"}
        t = "The Assassination Bureau"
        size = ([f"{x} GB is larger than maximum allowed 8.8 GB (for {t})" for x in ("10.8", "11.2", "11.5")]
                + [f"1,009.3 MB is smaller than minimum allowed 1.4 GB (for {t})"] * 2
                + [f"2.0 GB is smaller than minimum allowed 5.6 GB (for {t})"] * 4)
        releases = [rel(i, [m]) for i, m in enumerate(size)]
        releases += [rel(100 + i, ["Custom Formats LQ have score -10000 below Movie's profile minimum 0"]) for i in range(9)]
        s = arr_insight.summarize(releases, "Radarr")
        self.assertIn("9 wrong size", s["summary"])
        self.assertNotIn("other reasons", s["summary"])


if __name__ == "__main__":
    unittest.main()

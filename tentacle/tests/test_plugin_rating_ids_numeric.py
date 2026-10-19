"""The plugin's rating proxies must accept only a numeric tmdbId.

/Tentacle/Tmdb/EpisodeRating and /SeasonRatings put tmdbId into the TMDB
request path as given, and every distinct value became a cache entry;
MDBList spent one call on its key per new string and kept it for 7 days.
TMDB ids are numbers (at most 10 digits); anything else is now 400 before
any request goes out.

There is no C# test host here, so this reads the controller source.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

API = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api"


def _body(src, name):
    start = src.index(f"public async Task<ActionResult> {name}(")
    nxt = src.find("[HttpGet(", start)
    return src[start:nxt if nxt > 0 else len(src)]


class TestRatingIdsNumeric(unittest.TestCase):
    def test_tmdb_actions_reject_non_digits_before_building_the_url(self):
        src = (API / "TmdbRatingsController.cs").read_text()
        for name in ("GetEpisodeRating", "GetSeasonRatings"):
            with self.subTest(action=name):
                body = _body(src, name)
                check = body.find("All(char.IsAsciiDigit)")
                self.assertGreater(check, 0)
                self.assertIn("Length > 10", body)
                self.assertLess(check, body.find("https://api.themoviedb.org/3/tv/"))

    def test_mdblist_rejects_non_digits_before_spending_the_key(self):
        body = _body((API / "MdbListController.cs").read_text(), "GetRatings")
        check = body.find("All(char.IsAsciiDigit)")
        self.assertGreater(check, 0)
        self.assertIn("> 10", body)
        self.assertLess(check, body.find("GetMdbListApiKey()"))


if __name__ == "__main__":
    unittest.main()

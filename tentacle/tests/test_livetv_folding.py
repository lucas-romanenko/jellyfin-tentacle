"""Folding a provider's regional / quality duplicates into one network.

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_folding

The names are real ones from a provider's live list (2026-10): the same
network per city, call sign, quality and time zone feed must reduce to one
key; different networks that share a word ("NBC Sports", "NBC News", "TSN 1"
vs "TSN 2") must not.
"""
import unittest

from services import livetv_names as N


def key(name):
    return N.fold_key(name)


class SameNetwork(unittest.TestCase):
    def assertFold(self, names, expected):
        for n in names:
            with self.subTest(name=n):
                self.assertEqual(expected, key(n))

    def test_local_stations_fold_into_their_network(self):
        self.assertFold([
            "US: NBC BOSTON",
            "NBC WNBC NEW YORK",
            "NBC (KNBC) LOS ANGELES",
            "TV: A3 NEW YORK 04 WNBC NBC NEW YORK ᶜᶦᵗʸ ᴿᴬᵂ",
            "TV: ATLANTA, GA WXIA NBC 11 A3 HD ATLANTA ᶜᶦᵗʸ ᴿᴬᵂ",
            "US: MONTGOMERY, AL WSFA NBC 12 (A) ᴿᴬᵂ",
            "US: NBC 23 (KNDO) YAKIMA (H) ᴿᴬᵂ",
            "US: NBC 3 (WSAZ) HUNTINGTON (D) ᴿᴬᵂ",      # a town the place list lacks: the call sign says it
            "CA: NBC EAST",
        ], "nbc")

    def test_other_networks_stations(self):
        self.assertFold(["US: CBS (WBOC) SALISBURY", "US: CBS 2 WCBS (NEW YORK) HD",
                         "US: CBS 23 (WIFR-LD) ROCKFORD HD"], "cbs")
        self.assertFold(["US: ABC 13 (KTRK) HOUSTON HD", "US: ABC HD [NEW YORK]", "US: ABC 6 HD [AUGUSTA]",
                         "TV: ABC7 BAY AREA SAN FRANCISCO ᶜᶦᵗʸ ᴿᴬᵂ"], "abc")
        self.assertFold(["US: FOX 6 (WITI) MILWAUKEE HD", "US: FOX (WBFF) BALTIMORE HD", "US: FOX 10 HD [MOBILE]"], "fox")
        self.assertFold(["CA EN: CTV TORONTO HD (CFTO)", "CA EN: CTV Sudbury HD", "CA EN: CTV OTTAWA HD"], "ctv")
        self.assertFold(["CA EN: CITYTV TORONTO HD", "CA: CITY TV CALGARY HD", "CA EN: CITY CALGARY"], "city")
        self.assertFold(["CA EN: GLOBAL NEWS CALGARY HD", "CA EN: GLOBAL NEWS HALIFAX HD"], "globalnewslocal")

    def test_quality_and_copies_fold(self):
        self.assertFold(["CA EN: AMC 4K", "TV: AMC ᴿᴬᵂ", "US: AMC HD", "CA: AMC", "PRIME: AMC CANADA ᴿᴬᵂ"], "amc")
        self.assertFold(["US: BOX OFFICE HD", "US: BOX OFFICE SD", "US: BOX OFFICE HEVC", "US: BOX OFFICE 4K"],
                        "boxoffice")
        self.assertFold(["CA: TSN 1 ᴿᴬᵂ", "CA: TSN 1 ⁽ᴮᴷ⁾ ᴿᴬᵂ", "CA EN: TSN 1"], "tsn1")
        self.assertFold(["US: FOX NEWS HD", "US: FOX NEWS. UHD", "TV: FOX NEWS CHANNEL ᴿᴬᵂ"], "foxnews")
        self.assertFold(["|US| TLC", "US| TLC HD", "CA: TLC CANADA", "US: TLC WEST"], "tlc")

    def test_time_zone_feeds_fold(self):
        self.assertFold(["US: CARTOON NETWORK EAST HD", "US: CARTOON NETWORK WEST HD", "CA: CARTOON NETWORK"],
                        "cartoonnetwork")
        self.assertFold(["US: DISNEY JR HD", "US: DISNEY JR WEST HD", "CA: DISNEY JUNIOR"], "disneyjunior")

    def test_a_providers_typo_joins(self):
        self.assertEqual(key("CA EN: SPORTSNET ONE 4K"), key("CA: SPORTSN ET ONE ⁽ᴮᴷ⁾ ᴿᴬᵂ"))


class DifferentNetworks(unittest.TestCase):
    def test_nbc_sports_news_golf_stay_apart(self):
        keys = {key("US: NBC BOSTON"), key("US: NBC SPORTS BOSTON (A) ᴿᴬᵂ"), key("TV: NBC NEWS NOW ᴿᴬᵂ"),
                key("US: NBC GOLF")}
        self.assertEqual(4, len(keys))
        self.assertNotEqual(key("US: NBC SPORTS BAY AREA"), key("US: NBC SPORTS CALIFORNIA"))

    def test_numbered_channels_stay_apart(self):
        self.assertNotEqual(key("CA: TSN 1 ᴿᴬᵂ"), key("CA: TSN 2 ᴿᴬᵂ"))
        self.assertNotEqual(key("US: FOX SPORTS 1 HD"), key("US: FOX SPORTS 2 HD"))
        self.assertNotEqual(key("CA: OMNI 1"), key("CA: OMNI 2"))
        self.assertNotEqual(key("CA EN: CTV2 BARRIE HD"), key("CA EN: CTV BARRIE HD"))

    def test_national_news_never_folds_with_city_newscasts(self):
        for national, local in [("CA EN: CTV NEWS CHANNEL HD", "CA: CTV NEWS MONTREAL"),
                                ("US: ABC NEWS HD", "US: ABC TAMPA NEWS (WFTS)"),
                                ("CA: CBC NEWS", "CA EN: CBC WINDSOR NEWS HD"),
                                ("US: NBC NEWS", "US: NBC BOSTON NEWS (PC) ᴿᴬᵂ")]:
            with self.subTest(national=national):
                self.assertNotEqual(key(national), key(local))
        # City newscasts of one network fold among themselves.
        self.assertEqual(key("CA: CTV NEWS MONTREAL"), key("CA: CTV News Regina HD"))
        self.assertEqual(key("CA EN: GLOBAL NEWS CALGARY HD"), key("CA EN: GLOBAL NEWS TORONTO HD"))
        self.assertEqual("CTV News (local)", N.display_name(N.analyse("CA: CTV NEWS MONTREAL")["display"]))
        self.assertEqual(key("CA EN: CTV NEWS CHANNEL"), key("CA EN: CTV NEWS CHANNEL HD"))

    def test_regional_sports_networks_keep_their_place(self):
        self.assertNotEqual(key("CA EN: SPORTSNET EAST"), key("CA EN: SPORTSNET PACIFIC HD"))
        self.assertNotEqual(key("US: BALLY SPORTS OHIO HD"), key("US: BALLY SPORTS DETROIT HD"))
        self.assertNotEqual(key("US: BALLY SPORTS SOUTH (TENNESSEE) HD"), key("US: BALLY SPORTS SOUTH (NORTH CAROLINA) HD"))

    def test_brand_words_that_look_like_places_or_tags(self):
        self.assertEqual("usanetwork", key("US: USA NETWORK HD"))
        self.assertEqual("nationalgeographic", key("CA EN: NATIONAL GEOGRAPHIC HD"))
        self.assertNotEqual(key("TV: BBC AMERICA ᴿᴬᵂ"), key("CA: BBC CANADA"))
        self.assertEqual("wwe", key("US: WWE HD"))          # not a call sign
        self.assertNotEqual("", key("US: KIDS"))

    def test_independent_stations_never_fold(self):
        self.assertEqual("", key("TV: ATLANTA, GA WANF IND 46 A3 HD ATLANTA ᶜᶦᵗʸ ᴿᴬᵂ"))

    def test_teams_stay_apart(self):
        self.assertNotEqual(key("NBA: BOSTON CELTICS ᴴᴰ"), key("NBA: BROOKLYN NETS ᴴᴰ"))
        self.assertEqual(key("NBA: BOSTON CELTICS ᴴᴰ"), key("NBA: BOSTON CELTICS SD"))


class Names(unittest.TestCase):
    def test_display_names(self):
        self.assertEqual("NBC", N.display_name(N.analyse("US: NBC (KNBC) LOS ANGELES")["display"]))
        self.assertEqual("Cartoon Network", N.display_name(N.analyse("US: CARTOON NETWORK EAST HD")["display"]))
        self.assertEqual("FOX News Channel", N.display_name(N.analyse("TV: FOX NEWS CHANNEL ᴿᴬᵂ")["display"]))
        self.assertEqual("HGTV", N.display_name(N.analyse("US: HGTV WEST HD")["display"]))

    def test_clean_names_people_see(self):
        for raw, clean in [
            ("CA EN: SPORTSNET ONTARIO HD", "Sportsnet Ontario"),
            ("US: NBC (KNBC) LOS ANGELES", "NBC Los Angeles"),
            ("TV: AMC ᴿᴬᵂ", "AMC"),
            ("CA EN: CTV TORONTO HD (CFTO)", "CTV Toronto"),
            ("US: ABC 7 HD [BUFFALO]", "ABC 7 Buffalo"),
            ("CA EN: TELETOON AT NIGHT", "Teletoon at Night"),
            ("CA: SPORTSN ET ONE ⁽ᴮᴷ⁾ ᴿᴬᵂ", "Sportsnet One"),
            ("CA: TSN 1 ⁽ᴮᴷ⁾ ᴿᴬᵂ", "TSN 1"),
            ("CA EN: CBC ST JOHN'S (HD)", "CBC St John's"),
            ("PRIME: AMC+ CANADA ᴿᴬᵂ", "AMC+ Canada"),
            ("CA EN: CTV Sudbury HD", "CTV Sudbury"),     # mixed case is left as written
            ("US: HGTV WEST HD", "HGTV West"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(clean, N.clean_name(raw))

    def test_region_words(self):
        self.assertIn("TORONTO", N.region_words("CA EN: CTV TORONTO HD (CFTO)"))
        self.assertIn("CA", N.region_words("CA: NBC EAST"))
        self.assertIn("EAST", N.region_words("CA: NBC EAST"))


if __name__ == "__main__":
    unittest.main()

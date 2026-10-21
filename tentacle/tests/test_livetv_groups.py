"""Reading a provider's group names (services/livetv_groups.py): the
country or language, a clean title and the tags around it.

Run from the tentacle/ directory:  python -m unittest tests.test_livetv_groups
"""
import unittest

from services.livetv_groups import CodeBook, describe


class Describe(unittest.TestCase):
    def check(self, name, title, tags=(), codes=None, event=False, language=None):
        d = describe(name)
        self.assertEqual(title, d["title"], name)
        self.assertEqual(list(tags), d["tags"], name)
        self.assertEqual(event, d["event"], name)
        if codes is not None:
            self.assertEqual(codes, d["codes"], name)
        if language is not None:
            self.assertEqual(language, d["language"], name)

    def test_superscript_tags_become_labels(self):
        self.check("ES| DAZN BALONCESTO ᴿᴬᵂ", "DAZN Baloncesto", ["Raw"], ["ES"])
        self.check("NL| TELE2 VERMAAK ᴿᴬᵂ ᴳᴼᴸᴰ", "Tele2 Vermaak", ["Raw", "Gold"], ["NL"])
        self.check("US| ENTERTAINMENT ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ", "Entertainment", ["HD", "Raw", "60 fps"])
        self.check("US| TV ᶜᶦᵗʸ ᴿᴬᵂ ⁶⁰ᶠᵖˢ", "TV", ["Local", "Raw", "60 fps"])
        self.check("UK| NOW TV SPORT ᵁᴴᴰ ³⁸⁴⁰ᴾ", "Now TV Sport", ["4K"])

    def test_plain_tags_and_backup(self):
        self.check("ES| MAX PPV ⁽ᴮᴷ⁾", "Max", ["Backup"], event=True)
        self.check("DE| SKY MOVIES HD/4K", "Sky Movies", ["HD", "4K"])
        self.check("FR| FRANCE HEVC VIP", "France", ["HEVC", "VIP"])

    def test_event_groups_lose_the_word_unless_nothing_else_is_left(self):
        self.check("ES| F1 PPV", "F1", event=True)
        self.check("US| NBA PASS PPV", "NBA Pass", event=True)
        self.check("US| PPV EVENT", "PPV Event", event=True)
        self.check("US| NFL REPLAY", "NFL Replay")

    def test_other_scripts_symbols_and_languages(self):
        self.check("GR| ΓΕΝΙΚΟΣ/GENERAL ⱽᴵᴾ", "General", ["VIP"], ["GR"])
        self.check("GR| NOVA/SPORTS  ⱽᴵᴾ", "Nova Sports", ["VIP"])
        self.check("AR| MOROCCO ᴿᴬᵂ ▶ المغرب", "Morocco", ["Raw"], ["AR"])
        self.check("AR| BEIN SPORTS ⁸ᴷ & AFC ⚽", "beIN Sports & AFC", ["8K"])
        self.check("CA| CINEMA FR", "Cinema", language="fr")
        self.check("CA EN: SPORTS", "Sports", codes=["CA"], language="en")
        self.check("ES| TELEFÓNICA ᴿᴬᵂ", "Telefónica", ["Raw"])
        self.check("US| AT&T ᴿᴬᵂ ⁶⁰ᶠᵖˢ", "AT&T", ["Raw", "60 fps"])
        self.check("US| 24/7 COMEDY ᴿᴬᵂ ⁶⁰ᶠᵖˢ", "24/7 Comedy", ["Raw", "60 fps"])
        self.check("ES| MOTOGP PPV", "MotoGP", event=True)

    def test_prefix_forms(self):
        self.assertEqual(["US"], describe("US: NEWS")["codes"])
        self.assertEqual(["UK"], describe("[UK] SPORT")["codes"])
        self.assertEqual(["US"], describe("|US| ENTERTAINMENT")["codes"])
        self.assertEqual("PRIME", describe("PRIME: MOVIES")["source"])
        self.assertEqual([], describe("SPORTS")["codes"])


class ChannelNames(unittest.TestCase):
    def test_an_att_prefix_folds_into_the_network(self):
        from services.livetv_names import clean_name, fold_key
        self.assertEqual("AMC", clean_name("AT&T: AMC ᴿᴬᵂ"))
        self.assertEqual(fold_key("US: AMC"), fold_key("AT&T: AMC ᴿᴬᵂ"))


class Places(unittest.TestCase):
    NAMES = ["US| NEWS", "UK| SPORT", "UK| UKRAINE HD/4K", "ES| F1 PPV", "SR| SERBIA ᴴᴰ", "SR| SERBIA ⱽᴵᴾ",
             "LA| ARGENTINA", "LA| CHILE", "AR| MOROCCO ▶ المغرب", "AR| BEIN SPORTS", "CH| SWITZERLAND",
             "CH| BLUE SPORT", "CH| CHINA", "4K| RELAX", "TS| TENNIS LIVE", "CR| CARIBBEAN HUB",
             "CR| CARIBBEAN AMP", "CR| ESPN PLAY PPV", "US| TV ᴿᴬᵂ"]

    def setUp(self):
        self.book = CodeBook(self.NAMES)

    def place(self, name):
        p = self.book.read(name)["place"]
        return p and (p["code"], p["iso"], p["name"], p["kind"])

    def test_iso_codes_and_the_uk(self):
        self.assertEqual(("US", "US", "United States", "country"), self.place("US| NEWS"))
        self.assertEqual(("UK", "GB", "United Kingdom", "country"), self.place("UK| SPORT"))
        self.assertEqual(("ES", "ES", "Spain", "country"), self.place("ES| F1 PPV"))

    def test_a_code_its_groups_name_is_that_country(self):
        self.assertEqual("Serbia", self.place("SR| SERBIA ᴴᴰ")[2])   # ISO SR is Suriname
        self.assertEqual("Caribbean", self.place("CR| ESPN PLAY PPV")[2])

    def test_languages_and_regions(self):
        self.assertEqual(("AR", None, "Arabic", "region"), self.place("AR| BEIN SPORTS"))
        self.assertEqual(("LA", None, "Latin America", "region"), self.place("LA| CHILE"))
        self.assertEqual("Arabic", self.place("AR| MOROCCO ▶ المغرب")[2])

    def test_a_title_naming_another_country_is_that_country(self):
        self.assertEqual("Switzerland", self.place("CH| BLUE SPORT")[2])
        self.assertEqual("China", self.place("CH| CHINA")[2])
        self.assertEqual("Ukraine", self.place("UK| UKRAINE HD/4K")[2])

    def test_topics_are_no_place(self):
        self.assertIsNone(self.place("4K| RELAX"))
        self.assertIsNone(self.place("TS| TENNIS LIVE"))

    def test_language_of_the_place_and_a_bare_tv_title(self):
        d = self.book.read("US| TV ᴿᴬᵂ")
        self.assertEqual(("US TV", "en"), (d["title"], d["lang"]))
        self.assertEqual("es", self.book.read("ES| F1 PPV")["lang"])


if __name__ == "__main__":
    unittest.main()

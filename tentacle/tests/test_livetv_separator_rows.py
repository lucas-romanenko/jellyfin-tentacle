"""#158: which new rows are a provider's separators (never start enabled).

Run from the tentacle/ directory:  python -m unittest discover -s tests

Cases from real panels: two-symbol runs, a country tag in front, non-ASCII
decorations. Real channel names -- including production names that end in a
single "◉" -- still follow their group.
"""
import unittest

from routers.livetv import _is_separator


class Separators(unittest.TestCase):
    def test_separator_rows(self):
        for n in ("##### SPORTS #####", "####################", "===== NHL =====",
                  "--- EVENTS ---", "  ####  ", "*** LIVE EVENTS ***", "~~~~ PPV ~~~~",
                  "## NOW TV SPORT ᵁᴴᴰ ##", "## UK SPORTS ##", "UK: ##### SPORTS #####",
                  "UK| ## EVENTS ##", "US:==== NFL ====", "★★ PPV EVENTS ★★", "★★★★★★★★",
                  "▬▬▬▬ NEWS ▬▬▬▬", "◉◉ MOVIES ◉◉", "●● KIDS ●●", "•• MUSIC ••",
                  "CA| ▬▬▬▬▬▬▬▬", "== 24/7 ==", "___ NEWS ___", "________"):
            self.assertTrue(_is_separator(n), n)

    def test_channel_names(self):
        for n in ("TSN 1", "CA: TSN 5 ᴿᴬᵂ", "#1 Hits", "NHL 01: MTL @ TOR", "C-SPAN",
                  "Sky Sports - PL", "***Premium*** Movies", "A-Z Kids", "Sky Sports ---",
                  "UK: Sky Sports ---", "#1 Hits ##", "## 1 Hits", "★ Star Movies", "• News 24",
                  "UK: BBC One", "UK| ITV 1", "|| Live", "Eurosport 1 ★★",
                  "__NAME__", "__TSN 1__",
                  "UK: TNT SPORTS 1 ᴴᴰ ◉", "UK: BEIN SP⚽RTS 1 ENGLISH ᴴᴰ ◉"):
            self.assertFalse(_is_separator(n), n)


if __name__ == "__main__":
    unittest.main()

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
                  "CA| ▬▬▬▬▬▬▬▬", "== 24/7 ==", "___ NEWS ___", "________",
                  # Lucas's provider, 22 Oct 2026 (the four that were switched on first)
                  "####### SPORTSNET PPV #######", "#### SPORTSNET+ PPV ####", "##### BLUE SPORT ᴿᴬᵂ #####",
                  "######## SPORTS FR ########", "####### MiLB PPV ⁽ᴮᴷ⁾ #######", "##### CY| CYTAVISION VIP #####",
                  "##### FANSEAT & ESS PPV #####", "###### MLB -1 PPV ######", "## COSMOTE FILM 24/7 ⱽᴵᴾ ##"):
            self.assertTrue(_is_separator(n), n)

    def test_channel_names(self):
        for n in ("TSN 1", "CA: TSN 5 ᴿᴬᵂ", "#1 Hits", "NHL 01: MTL @ TOR", "C-SPAN",
                  "Sky Sports - PL", "***Premium*** Movies", "A-Z Kids", "Sky Sports ---",
                  "UK: Sky Sports ---", "#1 Hits ##", "## 1 Hits", "★ Star Movies", "• News 24",
                  "UK: BBC One", "UK| ITV 1", "|| Live", "Eurosport 1 ★★",
                  "__NAME__", "__TSN 1__",
                  "UK: TNT SPORTS 1 ᴴᴰ ◉", "UK: BEIN SP⚽RTS 1 ENGLISH ᴴᴰ ◉",
                  # Lucas's provider: real channels with decoration at one end only
                  "- NO EVENT STREAMING - | 8K EXCLUSIVE | US: ESPN+ PPV 26", "NFL   | 4k -", "LIVE EVENT 33 -",
                  ":Sportsnet+  12", "CBC 22:", "TSN+ 04 :", "AR: ARRYADIA TNT ᴿᴬᵂ *LOCAL*",
                  "GR: ANT1+ SPORTS 1 ᴿᴬᵂ ◉", "CA EN: CTV CALARY NEWS"):
            self.assertFalse(_is_separator(n), n)


if __name__ == "__main__":
    unittest.main()

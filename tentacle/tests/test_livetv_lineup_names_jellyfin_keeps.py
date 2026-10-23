"""Lineup channel names must reach Jellyfin as Tentacle wrote them.

livetv_lineups.m3u() writes each name after the comma of the #EXTINF line.
Jellyfin 10.11.8's M3U parser (M3uParser.GetChannelName) reads the text after
the LAST comma, and when its first space-separated word parses as a number
(the SAT>IP "84. VOX" form) drops that word, even when tvg-chno gives the
number. So "3 Sport" shows as "Sport", "5 USA" as "USA", "1 2 3 Kids" as
"2 3 Kids", and "Leafs, Live" as "Live". tvg-name is used only when the comma
part is empty.

`jellyfin_channel_name` below is a line-by-line port of GetChannelName and
ParseExtInf (src/Jellyfin.LiveTv/TunerHosts/M3uParser.cs, v10.11.8).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest

from services import livetv_lineups as L

BASE = "http://tentacle.lan:8888"
# double.TryParse(span, CultureInfo.InvariantCulture): NumberStyles.Float | AllowThousands
_DOTNET_DOUBLE = re.compile(r"\s*[+-]?(\d[\d,]*\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$|\s*[+-]?(Infinity|NaN|∞)\s*$", re.I)


def jellyfin_channel_name(extinf_line: str) -> str:
    ext = extinf_line[len("#EXTINF:"):].strip()
    attrs = {}
    for m in re.finditer(r'([a-z0-9\-_]+)="([^"]+)"', ext, re.I):   # KeyValueRegex
        attrs[m.group(1).lower()] = m.group(2)
        ext = re.sub(re.escape(f'{m.group(1)}="{m.group(2)}"'), "", ext, flags=re.I)
    parts = [p for p in ext.split(",") if p]                         # RemoveEmptyEntries
    name = parts[-1].strip() if len(parts) > 1 else None
    if name:
        i = name.find(" ")
        if i > 0 and _DOTNET_DOUBLE.match(name[:i].strip(" .")):
            name = name[i + 1:].strip(" -")
    if not (name or "").strip():
        name = attrs.get("tvg-name")
    if not (name or "").strip():
        name = attrs.get("tvg-id")
    return name


def _line(name, number=12):
    entry = {"key": "c1", "name": name, "xmltv_id": "277122", "number": number,
             "stream_path": "/api/live/stream/1", "logo_url": "http://img/1.png"}
    return L.m3u([entry], BASE).splitlines()[1]


class JellyfinKeepsTheName(unittest.TestCase):
    def test_the_port_reads_lines_as_jellyfin_does(self):
        self.assertEqual("Sport", jellyfin_channel_name('#EXTINF:-1 tvg-chno="5",3 Sport'))
        self.assertEqual("VOX Schweiz", jellyfin_channel_name("#EXTINF:0,84. VOX Schweiz"))
        self.assertEqual("3 Sport", jellyfin_channel_name('#EXTINF:-1 tvg-name="3 Sport",'))
        self.assertEqual("News 24", jellyfin_channel_name("#EXTINF:-1,News 24"))

    def test_names_starting_with_a_number_or_holding_a_comma_are_kept(self):
        for name in ("3 Sport", "5 USA", "1 2 3 Kids", "24 Kitchen", "Leafs, Live"):
            with self.subTest(name=name):
                self.assertEqual(name, jellyfin_channel_name(_line(name)))

    def test_the_guide_id_number_and_logo_stay(self):
        line = _line("3 Sport", number=7)
        self.assertIn('tvg-id="277122"', line)
        self.assertIn('tvg-chno="7"', line)
        self.assertIn('tvg-logo="http://img/1.png"', line)

    def test_other_names_are_written_as_before(self):
        # No churn for every other channel: the same line as today.
        self.assertEqual('#EXTINF:-1 tvg-id="277122" tvg-chno="12" tvg-logo="http://img/1.png",Sportsnet One',
                         _line("Sportsnet One"))
        self.assertEqual("24/7 Comedy", jellyfin_channel_name(_line("24/7 Comedy")))


if __name__ == "__main__":
    unittest.main()

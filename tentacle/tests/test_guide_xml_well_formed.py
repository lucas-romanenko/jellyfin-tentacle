"""The served guide and M3U survive control characters in names (#260).

Run from the tentacle/ directory:  python -m unittest discover -s tests

ElementTree escapes & < > but writes XML-1.0-illegal control characters
through unchanged. One in any channel name (Xtream, M3U, an admin's custom
name, a YouTube title) or programme text made /api/live/xmltv.xml not
well-formed, and Jellyfin dropped the guide of every channel. The same CR/LF
in a name split an #EXTINF line of /api/live/playlist.m3u in two.
"""
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from unittest.mock import MagicMock, patch

from services.xmltv import generate_xmltv


class GuideIsWellFormed(unittest.TestCase):
    def _gen(self, name="Ch", title="T", desc=None, sub=None, cat=None):
        return generate_xmltv([{"id": "1001", "name": name, "logo_url": None}],
                              [{"channel_id": "1001", "title": title, "description": desc,
                                "sub_title": sub, "category": cat,
                                "start": datetime(2026, 11, 1, 5), "stop": datetime(2026, 11, 1, 6)}])

    def test_control_character_in_a_channel_name(self):
        root = ET.fromstring(self._gen(name="Sports\x0b HD"))
        self.assertEqual("Sports HD", root.find("channel/display-name").text)

    def test_control_character_in_a_programme_title(self):
        ET.fromstring(self._gen(title="Final\x1b"))

    def test_nul_in_a_description_sub_title_and_category(self):
        root = ET.fromstring(self._gen(desc="a\x00b", sub="s\x01", cat="c\x1f"))
        self.assertEqual("ab", root.find("programme/desc").text)

    def test_ordinary_unicode_and_markup_still_round_trip(self):
        root = ET.fromstring(self._gen(name="Zürich Ōsaka – «Liga» & <x>", title='日本語 "q"\ttab\nline'))
        self.assertEqual("Zürich Ōsaka – «Liga» & <x>", root.find("channel/display-name").text)
        self.assertEqual('日本語 "q"\ttab\nline', root.find("programme/title").text)


class M3uKeepsOneLinePerChannel(unittest.TestCase):
    def test_a_cr_lf_in_a_name_does_not_inject_a_channel(self):
        import routers.livetv as livetv
        ch = MagicMock(stream_id="24", id=24, guide_epg_id="e1", logo_url=None,
                       group_title="News\r\nX", guide_name='Bad\x0bName "q" \n#EXTINF:-1,injected')
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [ch]
        request = MagicMock(base_url="http://tentacle.test:8888/")
        with patch.object(livetv.youtube_livetv, "live_channels", return_value=[]):
            resp = livetv.live_playlist_m3u(request, db)
        lines = resp.body.decode().split("\n")
        self.assertEqual(1, sum(1 for line in lines if line.startswith("#EXTINF")), lines)
        self.assertEqual("http://tentacle.test:8888/api/live/stream/24", lines[2])


if __name__ == "__main__":
    unittest.main()

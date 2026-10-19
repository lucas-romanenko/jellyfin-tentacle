"""#147/#148: which programme icons reach Jellyfin.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The stored icon is the first http(s) one (a feed can list a file: or data:
source first), and the guide never serves an icon hosted on the provider's
server, its channels' stream hosts or its guide (EPG) URL host -- Jellyfin
fetches it as a recording starts, one more connection to the panel.
"""
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest import mock

from routers import livetv
from services import xmltv


class IconSources(unittest.TestCase):
    def test_the_first_web_icon_is_kept(self):
        el = ET.fromstring('<programme start="20260925180000 +0000" stop="20260925190000 +0000" channel="a">'
                           '<title>T</title><icon src="file:///etc/passwd"/>'
                           '<icon src="https://img.example/g.jpg"/></programme>')
        self.assertEqual("https://img.example/g.jpg", xmltv._programme_dict(el, "a")["icon_url"])

    def test_no_web_icon_is_none(self):
        el = ET.fromstring('<programme start="20260925180000 +0000" stop="20260925190000 +0000" channel="a">'
                           '<title>T</title><icon src="data:image/png;base64,AA"/></programme>')
        self.assertIsNone(xmltv._programme_dict(el, "a")["icon_url"])

    def test_provider_hosted_icons_are_not_served(self):
        p = SimpleNamespace(server_url="http://panel.test:8080", epg_url="http://guide.test/xmltv.php")
        ch = SimpleNamespace(stream_url="http://edge.test/live/1.ts")
        with mock.patch.object(livetv, "live_tv_providers", lambda db: [p]):
            hosts = livetv._provider_hosts(None, [ch])
        self.assertEqual({"panel.test", "guide.test", "edge.test"}, hosts)
        for url in ("http://panel.test:8080/i.png", "http://guide.test/logo.png", "http://edge.test/x.jpg",
                    "file:///etc/passwd"):
            self.assertIsNone(livetv._third_party_icon(url, hosts), url)
        self.assertEqual("https://image.tmdb.org/t/p/w300/a.jpg",
                         livetv._third_party_icon("https://image.tmdb.org/t/p/w300/a.jpg", hosts))


if __name__ == "__main__":
    unittest.main()

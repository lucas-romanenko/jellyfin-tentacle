"""The URL a channel is added from (abe72f4).

indexer.parse_input_url() canonicalises what the user pasted to a youtube.com
URL for every form but one: the legacy /c/<name> and /user/<name> forms return
the pasted URL unchanged, host and all. routers/youtube.py then hands that URL
straight to yt-dlp, whose generic extractor will fetch any host — including the
private addresses services/ssrf.py exists to keep this server away from.

Needs sqlalchemy only (as CI has).
Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest
from urllib.parse import urlparse

from services.youtube.indexer import parse_input_url

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be"}


class TestCanonicalUrlStaysOnYouTube(unittest.TestCase):
    def test_every_accepted_form_canonicalises_to_youtube(self):
        for pasted in (
            "https://www.youtube.com/@BBCNews",
            "@Bluey",
            "https://www.youtube.com/channel/UC16niRr50-MSBwiO3YDb3RA",
            "https://www.youtube.com/playlist?list=PLabc",
            "https://www.youtube.com/c/LinusTechTips",
            "https://www.youtube.com/user/BBC",
        ):
            with self.subTest(pasted=pasted):
                host = (urlparse(parse_input_url(pasted)["canonical"]).hostname or "").lower()
                self.assertIn(host, YOUTUBE_HOSTS)

    def test_a_private_address_is_not_accepted_as_a_channel(self):
        """The /c/ and /user/ forms must not carry an arbitrary host through."""
        for pasted in (
            "http://169.254.169.254/latest/meta-data/c/x",
            "http://10.0.0.5:8096/user/admin",
            "http://localhost:8888/c/anything",
        ):
            with self.subTest(pasted=pasted):
                try:
                    canonical = parse_input_url(pasted)["canonical"]
                except ValueError:
                    continue  # refused outright — also fine
                host = (urlparse(canonical).hostname or "").lower()
                self.assertIn(
                    host, YOUTUBE_HOSTS,
                    f"{pasted!r} is passed to yt-dlp as {canonical!r}, so the "
                    "server fetches that host",
                )


if __name__ == "__main__":
    unittest.main()

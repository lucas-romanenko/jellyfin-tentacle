"""On Now (Lucas, 2026-10-21): Tentacle's own Live TV home row, what's on
right now on the person's favourite channels, replacing Jellyfin's Live TV
row (the first 12 channels by number, logos stretched into posters).

There is no JS test host here, so this reads the plugin's home script.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject"


def _function(src: str, name: str) -> str:
    start = src.index("function " + name + "(")
    nxt = re.search(r"\n  function ", src[start + 10:])
    return src[start:start + 10 + nxt.start()] if nxt else src[start:]


class OnNowRow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (PLUGIN / "tentacle-home.js").read_text(encoding="utf-8")
        cls.css = (PLUGIN / "tentacle-home.css").read_text(encoding="utf-8")

    def test_the_builtin_section_draws_on_now(self):
        body = _function(self.js, "loadBuiltinSection")
        self.assertIn("sectionId === 'onnow'", body)
        self.assertIn("loadOnNow(row)", body)

    def test_it_asks_jellyfin_for_this_persons_favourite_channels_with_whats_on(self):
        """Jellyfin's own favourites (Plan B) as that person, so kids' channel
        limits (blocked tags) apply without Tentacle doing anything."""
        body = _function(self.js, "loadOnNow")
        self.assertIn("LiveTv/Channels?UserId=' + MH.userId", body)
        self.assertIn("IsFavorite=true", body)
        self.assertIn("AddCurrentProgram=true", body)

    def test_only_channels_with_something_on_now_and_hidden_when_none(self):
        body = _function(self.js, "loadOnNow")
        self.assertIn("p.EndDate", body)
        self.assertIn("mh-row-hidden", body)
        self.assertIn(".catch(", body, "no Live TV access or no guide: the row hides")

    def test_it_refills_when_a_programme_ends(self):
        body = _function(self.js, "loadOnNow")
        self.assertIn("setTimeout", body)
        self.assertIn("MH.generation", body, "a timer from an older render never draws")

    def test_cards_show_the_programme_channel_and_time_left_and_tune_in(self):
        body = _function(self.js, "createOnNowCard")
        self.assertIn("p.Name", body)
        self.assertIn("channel.Name", body)
        self.assertIn("m left", body)
        self.assertIn("playLive(channel.Id)", body)
        self.assertNotIn("api_key", body, "no session token in an <img src>")
        self.assertIn("TentacleLiveTV.playChannel(channelId)", _function(self.js, "playLive"))

    def test_logos_are_contained_not_stretched(self):
        self.assertRegex(self.css, r"\.mh-onnow-logo img \{[^}]*object-fit: contain")


if __name__ == "__main__":
    unittest.main()

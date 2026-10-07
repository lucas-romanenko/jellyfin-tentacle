"""The plugin's user-facing endpoints answer failures with fixed text.

Run from the tentacle/ directory:  python -m unittest discover -s tests

TentacleHome (Playlists, Hero, Reorder), TentacleDiscover (Follow,
DeleteLibraryItem, AddToRadarr, AddToSonarr, ManageEpisodes) and the MDBList
ratings proxy put ex.Message into the reply. For a connection failure .NET's
message names the Tentacle server's address and port ("Connection refused
(host:port)"), and the reply reaches every signed-in user's browser. They now answer
with DiscoverController.DescribeFailure()'s fixed texts (MDBList with its
own), and the exception stays in the plugin's log.

There is no C# test host here, so this reads the controller sources.
"""
import re
import unittest
from pathlib import Path

API = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api"


class PluginErrorText(unittest.TestCase):
    def test_no_exception_message_in_a_reply(self):
        bad = []
        for name in ("HomeScreenController.cs", "DiscoverController.cs", "MdbListController.cs"):
            for n, line in enumerate((API / name).read_text(encoding="utf-8").splitlines(), 1):
                if "ex.Message" in line and "_logger." not in line:
                    bad.append(f"{name}:{n}: {line.strip()}")
        self.assertEqual([], bad)

    def test_the_failures_use_the_fixed_texts(self):
        home = (API / "HomeScreenController.cs").read_text(encoding="utf-8")
        disc = (API / "DiscoverController.cs").read_text(encoding="utf-8")
        self.assertEqual(3, home.count("message = TentacleDiscoverController.DescribeFailure(ex).Message"))
        self.assertEqual(5, disc.count("detail = DescribeFailure(ex).Message"))


if __name__ == "__main__":
    unittest.main()

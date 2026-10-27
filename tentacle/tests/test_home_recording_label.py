"""A Live TV recording's card on Jellyfin Web's Tentacle home says
"Recording · <day>", not just the year (Lucas, 2026-10-24: under "Live NHL
Hockey" the TV app said "Movie 2026": "Maybe recording or something like
that to differentiate it"). Jellyfin files recordings as Movies; the card
knows one by Jellyfin's recordings folder or its file name.

Runs the real recordingMeta under node (skipped without node).

Run from the tentacle/ directory:  python -m unittest tests.test_home_recording_label
"""
import json
import re
import unittest

from inject_js import HAVE_NODE, INJECT, node

SRC = (INJECT / "tentacle-home.js").read_text(encoding="utf-8")


def _function(name):
    start = SRC.index("  function " + name + "(")
    return SRC[start:SRC.index("\n  }\n", start) + 4]


class AsksForThePath(unittest.TestCase):
    def test_the_rows_ask_for_path_and_date_created(self):
        """Continue Watching and Recordings come from the plugin (RowDtoOptions)."""
        cs = (INJECT.parent / "Api" / "HomeScreenController.cs").read_text(encoding="utf-8")
        opts = cs[cs.index("private static DtoOptions RowDtoOptions()"):]
        opts = opts[:opts.index("};")]
        self.assertIn("ItemFields.Path", opts)
        self.assertIn("ItemFields.DateCreated", opts)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class Label(unittest.TestCase):
    def meta(self, item):
        return node("process.env.TZ = 'America/Toronto';" + _function("recordingMeta")
                    + "console.log(JSON.stringify(recordingMeta(%s)));" % json.dumps(item))

    def test_a_recording_says_recording_and_the_air_day(self):
        m = self.meta({"Type": "Movie", "ProductionYear": 2026, "DateCreated": "2026-10-23T23:55:00.0000000Z",
                       "Path": "/config/data/livetv/recordings/Live  NHL Hockey/Live  NHL Hockey 2026_10_23_20_00_00.ts"})
        self.assertTrue(m.startswith("Recording · "), m)
        self.assertIn("23", m)
        self.assertNotIn("2026", m)

    def test_a_recorded_film_without_the_stamp(self):
        m = self.meta({"Type": "Movie", "DateCreated": "2026-10-20T01:00:00Z",
                       "Path": "/config/data/livetv/recordings/Heat (1995)/Heat (1995).ts"})
        self.assertTrue(m.startswith("Recording · "), m)

    def test_a_custom_recordings_folder_by_the_file_name(self):
        m = self.meta({"Path": "D:\\TV\\Jeopardy!\\Jeopardy! 2026_10_21_19_30_00.ts"})
        self.assertTrue(m.startswith("Recording · "), m)

    def test_films_and_episodes_are_not_recordings(self):
        for path in ("/data/media/movies/Heat (1995)/Heat (1995).mkv",
                     "/data/media/vod/Movies/One Last Shot (2026)/One Last Shot (2026).strm", None):
            self.assertIsNone(self.meta({"Type": "Movie", "Path": path, "ProductionYear": 2026}))

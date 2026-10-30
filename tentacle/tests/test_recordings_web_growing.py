"""Jellyfin Web: a recording still recording plays from its start (TV #239 on the web).

Lucas, 2026-10-30: "im testing 239, dont see the jump to live button. testing on web".
A click on a "Recording now" card asks Watch live / Resume from … / Play from start
(tentacle-recordings.js), and Play from start plays the growing file in Jellyfin's
player: seeks stay inside what's written, a resume waits for the server's remux, and
the playback reports leave the position out (Jellyfin marks an item without a runtime
watched at 0:00 on any position) and stop as failed, with the position saved on its
own. Runs the real functions under node (skipped without node).

Run from the tentacle/ directory:  python -m unittest tests.test_recordings_web_growing
"""
import json
import unittest

from inject_js import HAVE_NODE, INJECT, node

SRC = (INJECT / "tentacle-recordings.js").read_text(encoding="utf-8")


def _function(name):
    start = SRC.index("  function " + name + "(")
    return SRC[start:SRC.index("\n  }\n", start) + 4]


def _line(prefix):
    start = SRC.index(prefix)
    return SRC[start:SRC.index("\n", start) + 1]


FUNCS = "".join(_function(f) for f in ("position", "clock", "recordingChoices", "normId", "growingSeekTarget",
                                       "growingResumeReady", "growingStopTicks", "growingReport"))
PRELUDE = _line("  var EDGE_MS =") + _line("  var GR =")


def run(body):
    return node(PRELUDE + FUNCS + "console.log(JSON.stringify((function () {" + body + "})()));")


class Wiring(unittest.TestCase):
    def test_a_click_on_a_recording_now_asks_first(self):
        self.assertIn("if (recording(item)) recordingMenu(item, { el: a, short: true });", SRC)

    def test_play_from_start_never_names_a_start_of_0(self):
        # hls.js takes a start of 0 for "the live edge": 1 ms
        self.assertIn("START_TICKS = 10000", SRC)
        self.assertIn("pm.play({ ids: [item.Id], startPositionTicks: START_TICKS });", SRC)

    def test_a_recording_that_finished_at_its_end_is_stopped_as_watched(self):
        body = _function("checkFinished")
        self.assertIn("GR.ended = true;", body)
        self.assertIn("Command: 'Stop'", body)

    def test_the_three_reports_are_wrapped(self):
        self.assertIn("['reportPlaybackStart', 'reportPlaybackProgress', 'reportPlaybackStopped']", SRC)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class Choices(unittest.TestCase):
    def labels(self, user_data):
        return run("return recordingChoices({ UserData: %s }).map(function (c) { return c.label; });" % json.dumps(user_data))

    def test_no_position(self):
        self.assertEqual(["Watch live", "Play from start"], self.labels({"PlaybackPositionTicks": 0}))

    def test_a_saved_position_offers_resume_from_it(self):
        self.assertEqual(["Watch live", "Resume from 10:36", "Play from start"], self.labels({"PlaybackPositionTicks": 636 * 10**7}))
        self.assertEqual("Resume from 1:02:05", self.labels({"PlaybackPositionTicks": 3725 * 10**7})[1])


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class Seeking(unittest.TestCase):
    def test_seeks(self):
        out = run("""return [growingSeekTarget(-5, 600000), growingSeekTarget(30000, 600000), growingSeekTarget(999999, 600000),
          growingSeekTarget(5000, 4000), growingSeekTarget(70000, 0)];""")
        # inside: as asked; past the end: 10 s short of what's written; nothing written yet: as asked
        self.assertEqual([0, 30000, 590000, 0, 70000], out)

    def test_resume_waits_for_the_remux_then_gives_up_waiting(self):
        out = run("return [growingResumeReady(600000, 300000, 1000), growingResumeReady(600000, 610000, 1000), growingResumeReady(600000, 300000, 45000)];")
        self.assertEqual([False, True, True], out)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class Reports(unittest.TestCase):
    def report(self, method, info, gr="{ id: 'abc123', ended: false }"):
        return run("Object.assign(GR, %s); return growingReport(%s, %s);" % (gr, json.dumps(method), json.dumps(info)))

    def test_progress_leaves_the_position_out(self):
        out = self.report("reportPlaybackProgress", {"ItemId": "ABC123", "PositionTicks": 900000000, "IsPaused": False})
        self.assertEqual({"ItemId": "ABC123", "IsPaused": False}, out)

    def test_start_leaves_the_position_out(self):
        self.assertNotIn("PositionTicks", self.report("reportPlaybackStart", {"ItemId": "abc-123", "PositionTicks": 1020455999}))

    def test_stop_is_failed_and_saves_the_position_on_its_own(self):
        out = self.report("reportPlaybackStopped", {"ItemId": "abc123", "PositionTicks": 900000000})
        self.assertEqual({"ItemId": "abc123", "Failed": True, "__save": 900000000}, out)

    def test_under_a_minute_saves_no_position(self):
        self.assertEqual(0, self.report("reportPlaybackStopped", {"ItemId": "abc123", "PositionTicks": 590000000})["__save"])

    def test_a_recording_played_to_its_end_stops_as_any_other(self):
        info = {"ItemId": "abc123", "PositionTicks": 72000000000}
        self.assertEqual(info, self.report("reportPlaybackStopped", info, "{ id: 'abc123', ended: true }"))

    def test_other_items_are_untouched(self):
        info = {"ItemId": "other", "PositionTicks": 900000000}
        self.assertEqual(info, self.report("reportPlaybackProgress", info))
        self.assertEqual(info, self.report("reportPlaybackStopped", info, "{ id: null, ended: false }"))


if __name__ == "__main__":
    unittest.main()
